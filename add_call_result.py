"""
Push a terminal call outcome to Tamweely's AddCallResult API, keyed by TrackingId.

Why this exists
---------------
Tamweely's AddCallResult push is normally driven by the *ElevenLabs post-call
webhook*: a conversation finishes, ElevenLabs notifies the .NET side, and it
POSTs the result. A call that is never answered -- busy, no answer, ring
timeout, hangup mid-ring -- produces no ElevenLabs conversation at all, so that
webhook never fires and **no result ever reaches Tamweely**. This middleware is
the only component that knows those outcomes.

`POST /v1/data/add-call-result/by-tracking-id/{trackingId}?status=` closes that
gap. Only the no-answer family is pushed from here (Cancelled / Timeout);
answered, transferred and transfer-failed calls are left entirely to the
existing ElevenLabs path. See ADD_CALL_RESULT.md.

There is no enable/disable flag. Setting ADD_CALL_RESULT_BASE_URL and
ADD_CALL_RESULT_API_KEY is itself the decision to push, and every unanswered
call is then reported. Blanking either one and restarting is the off-switch.

Why `?status=` is always passed
-------------------------------
When FinalOutcome is still null the Tamweely payload falls back to the
AgentType default -- 206 for Contact, 202 for Collection. A no-answer call on a
Contact agent would therefore push 206. Only `?status=Cancelled` /
`?status=Timeout` force 202 ("العميل عدم رد"), so the status is always sent
explicitly. The enum binds by name, and those names match db.py's STATUS_*
constants character for character.

Threading
---------
Modelled on webhook_client.WebhookSender, which already solved this problem
(F-18): no thread in the pool ever sleeps, retries are rescheduled on a daemon
threading.Timer, and a request that runs out of attempts is appended to a
dead-letter file instead of vanishing.

push_async() is called from the SIP thread at end of call, so it does no
network I/O: the HTTP request always happens on the pool. It is not
unconditionally free, though, and the docstring there says exactly where it
is not -- CallManager builds the sender eagerly at startup to keep the one
expensive case off the SIP thread entirely.
"""
from __future__ import annotations

import json
import itertools
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import quote, urlparse

import requests

from config import Settings
from config import settings as _module_settings
from logging_config import get_logger

logger = get_logger("add_call_result")

# Documented in AddCallResult_API_Guide §2. The configured base URL is an
# origin only (e.g. https://host); this path is appended here so the route
# lives with the code that knows the contract, not in an operator's .env.
_PATH_TEMPLATE = "/v1/data/add-call-result/by-tracking-id/{tracking_id}"

# Non-5xx codes that still describe a transient condition. 429 and 408 are the
# realistic ones behind a load balancer; the rest are cheap to include.
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429})

# SIP final responses that mean *the customer* did not take the call, and so
# may honestly be reported to Tamweely as 202 "العميل عدم رد".
#
# Deliberately NARROWER than db._CANCELLED_SIP_CODES, which is {486, 503}.
# 503 Service Unavailable is the PBX or an upstream trunk failing -- the
# customer's phone was never reached, let alone unanswered. Recording it
# locally as Cancelled is fine (that column is ours), but pushing it as 202
# would tell Tamweely something false and permanently overwrite FinalOutcome
# and SummaryArabic on their side. Same reasoning that keeps connect_timeout
# and connect_failed out of the push entirely.
#
# 480/408/600/603/604 are NOT here: whether those count as customer no-answer
# is a Tamweely business question, still open in CALL_STATUS_TRACKING.md
# ("SIP rejection codes other than 486/503"). Nothing is guessed.
CUSTOMER_NO_ANSWER_SIP_CODES = frozenset({486})

# The endpoint's own response is the only thing that says whether a 200 was
# actually accepted, so it is logged verbatim -- but bounded. A proxy error
# page or an HTML login redirect can be tens of kilobytes, and service.log is
# not the place for it.
_MAX_LOGGED_BODY_CHARS = 1000

# §4 defines trackingId as a GUID. main.py only checks the value is non-empty,
# so this is advisory: a mismatch is logged and still sent, because the local
# rule is a guess about their route binding and theirs is the one that counts.
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _response_fields(body: object) -> dict:
    """A case-insensitive view of the response object.

    AddCallResult_API_Guide §7 documents camelCase (`isSuccess`), but the
    deployed endpoint answers in PascalCase:

        {"IsSuccess":true,"Data":true,"ErrorMessage":null,
         "ValidationErrors":[],"Message":null}

    ASP.NET Core emits either, depending on whether JsonSerializerOptions
    .PropertyNamingPolicy is left at its camelCase default or set to null, so
    which one arrives is a deployment detail of theirs that can change under
    us without notice. Reading only the documented spelling made five
    DELIVERED pushes read as "not accepted", retry four extra times and then
    dead-letter as permanently failed (observed 2026-08-23). Match on the
    field name and ignore its casing.

    A non-object body ("true", a list, a string) yields {} -- it carries no
    fields, and the caller treats a missing isSuccess as not-a-success.
    """
    if not isinstance(body, dict):
        return {}
    return {str(k).lower(): v for k, v in body.items()}


def is_customer_no_answer(sip_code: Optional[int]) -> bool:
    """True if this SIP final response may be pushed to Tamweely as 202."""
    return sip_code in CUSTOMER_NO_ANSWER_SIP_CODES


class AddCallResultSender:
    """Fire-and-forget delivery of one call outcome, with retries.

    Constructed with a Settings so tests can drive it directly; production
    code goes through the module-level push_async()/shutdown() below.
    """

    def __init__(self, settings: Settings):
        self._base_url = settings.add_call_result_base_url.rstrip("/")
        self._api_key = settings.add_call_result_api_key
        # Configuring both of these IS the decision to push -- there is no
        # separate on/off flag. Blanking either one is the off-switch.
        self._enabled = bool(self._base_url and self._api_key)
        self._timeout = settings.add_call_result_timeout_seconds
        # TOTAL attempts, not retries-after-the-first: delivery gives up when
        # `attempt >= _max_retries`, so 5 means one initial try plus four
        # retries. Same counting as WebhookSender.
        self._max_retries = max(1, settings.add_call_result_max_retries)
        self._max_retry_age = settings.add_call_result_max_retry_age_seconds
        self._dead_letter_path = settings.add_call_result_dead_letter_path

        # Deliberately separate from the call-worker executor and from
        # WebhookSender's -- pushing a result must never compete for a thread
        # with call handling or with webhook delivery.
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="add-call-result"
        )
        # entry_id -> (timer, record, attempt). A dict, not a list, because
        # each pending retry has to be CLAIMED by exactly one of two racing
        # parties -- the timer that fires it, or shutdown() that cancels it.
        # See _submit_retry for why is_alive() cannot decide that.
        self._timers: dict[int, tuple[threading.Timer, dict, int]] = {}
        self._entry_ids = itertools.count()
        self._timers_lock = threading.Lock()
        self._dead_letter_lock = threading.Lock()
        self._shutting_down = False

        if self._dead_letter_path:
            os.makedirs(os.path.dirname(self._dead_letter_path) or ".", exist_ok=True)

        # Say plainly, once, at startup which mode this process is in. With no
        # flag to read back, this log line is how an operator confirms whether
        # unanswered calls are reaching Tamweely -- and which host they reach.
        # The hostname only: never the full URL, never the key.
        if self._enabled:
            logger.info(
                f"AddCallResult push ACTIVE -> {urlparse(self._base_url).hostname} "
                f"-- unanswered calls WILL be reported to Tamweely"
            )
        else:
            logger.warning(
                "AddCallResult push INACTIVE: ADD_CALL_RESULT_BASE_URL and/or "
                "ADD_CALL_RESULT_API_KEY is not set -- unanswered calls will "
                "NOT be reported to Tamweely"
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _safe(self, text: object) -> str:
        """Scrub the API key out of anything server-controlled before logging.

        errorMessage, validationErrors and a RequestException's str() are all
        written by the far end. A proxy that echoes the request headers back
        in an error body would otherwise put a live credential in service.log.
        Belt and braces -- the key should never appear there at all."""
        out = str(text)
        if self._api_key and self._api_key in out:
            out = out.replace(self._api_key, "<redacted>")
        return out

    def _body_for_log(self, resp) -> str:
        """The raw response body, redacted and truncated, for the log line.

        Reading .text can itself raise (a decoding failure on a malformed
        charset), and a log call must never be the thing that fails a push."""
        try:
            text = resp.text or ""
        except Exception as e:  # pragma: no cover - defensive
            return f"<unreadable body: {type(e).__name__}: {self._safe(e)}>"
        text = " ".join(text.split())  # collapse newlines: one log line, one record
        if len(text) > _MAX_LOGGED_BODY_CHARS:
            text = text[:_MAX_LOGGED_BODY_CHARS] + f"...<truncated, {len(text)} chars>"
        return self._safe(text) if text else "<empty body>"

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def push_async(self, tracking_id: str, status: str) -> None:
        """Schedule the push and return. Never raises.

        No network I/O happens here -- the request runs on the pool, and this
        measured 0.7 ms on the SIP thread. Two paths do touch the disk, both
        deliberately:

        * Constructing the sender runs os.makedirs() on the dead-letter
          directory. CallManager calls get_sender() at startup precisely so
          this never lands on a SIP thread; it only would if something
          reached push_async() before the manager existed.
        * After shutdown, the record is dead-lettered inline -- one small
          append, and the alternative is losing the outcome.

        Neither is a network round trip, which is the thing that must never
        sit in front of a live call.
        """
        if not self._enabled or not tracking_id:
            return

        if not _GUID_RE.match(tracking_id):
            logger.warning(
                f"tracking_id {tracking_id!r} is not a GUID; the endpoint defines it "
                "as one and will most likely reject this push at route binding"
            )

        record = {
            "tracking_id": tracking_id,
            "status": status,
            "_first_attempt_at": time.time(),
        }

        # A call hung up by the shutdown drain still reaches this point. The
        # executor is already down by then, so record the outcome for manual
        # re-push instead of losing it.
        if self._shutting_down:
            self._dead_letter(record, reason="shutting_down")
            return

        try:
            self._executor.submit(self._attempt, record, 1)
        except Exception as e:
            logger.error(
                f"failed to schedule AddCallResult push for TrackingId {tracking_id}: {e}"
            )
            self._dead_letter(record, reason="schedule_failed")

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------
    def _attempt(self, record: dict, attempt: int) -> None:
        tracking_id = record["tracking_id"]
        status = record["status"]
        # tracking_id is untrusted: main.py only checks it is non-empty, never
        # that it is a GUID, and it lands in a URL path here (cf. F-23).
        url = f"{self._base_url}{_PATH_TEMPLATE.format(tracking_id=quote(tracking_id, safe=''))}"

        try:
            resp = requests.post(
                url,
                params={"status": status},
                # Content-Type is what the endpoint documents (§2) even though
                # this request carries no body and the §9 curl omits it.
                headers={"X-Api-Key": self._api_key, "Content-Type": "application/json"},
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            logger.warning(
                f"AddCallResult push failed for TrackingId {tracking_id} "
                f"(attempt {attempt}/{self._max_retries}): {self._safe(e)}"
            )
            self._schedule_retry(record, attempt)
            return
        except Exception:
            logger.exception(
                f"unexpected error pushing AddCallResult for TrackingId {tracking_id}"
            )
            self._dead_letter(record, reason="unexpected_error")
            return

        code = resp.status_code

        # Log what came back on EVERY attempt, before any branching, so the
        # 401 / 5xx / non-JSON / isSuccess:false paths are all covered by one
        # line. Without it the only visible evidence of an isSuccess:false was
        # its errorMessage, which is not enough to tell "no row yet" from a
        # response shape the parsing below does not recognise.
        logger.info(
            f"AddCallResult response for TrackingId {tracking_id} "
            f"(attempt {attempt}/{self._max_retries}): HTTP {code} "
            f"body={self._body_for_log(resp)}"
        )

        # 401 is not worth retrying: the key is wrong for every attempt, and
        # hammering an auth endpoint just fills the log with the same line.
        if code == 401:
            logger.error(
                f"AddCallResult push rejected 401 Unauthorized for TrackingId "
                f"{tracking_id} -- check ADD_CALL_RESULT_API_KEY. Not retrying."
            )
            self._dead_letter(record, reason="unauthorized")
            return

        # §8: 500 is explicitly documented as safe to retry. 408/409/425/429
        # are transient too even though the guide does not enumerate them.
        if code >= 500 or code in _RETRYABLE_STATUS_CODES:
            logger.warning(
                f"AddCallResult push got HTTP {code} for TrackingId {tracking_id} "
                f"(attempt {attempt}/{self._max_retries})"
            )
            self._schedule_retry(record, attempt)
            return

        if not 200 <= code < 300:
            logger.error(
                f"AddCallResult push got HTTP {code} for TrackingId {tracking_id}; "
                "not retrying"
            )
            self._dead_letter(record, reason=f"http_{code}")
            return

        # §8: a 200 means the request was *processed*, not that it succeeded --
        # both "tracking id not found" and a Tamweely-side failure come back
        # 200. The body is the only thing that says which.
        try:
            body = resp.json()
        except ValueError:
            logger.warning(
                f"AddCallResult push returned HTTP {code} with a non-JSON body for "
                f"TrackingId {tracking_id} (attempt {attempt}/{self._max_retries})"
            )
            self._schedule_retry(record, attempt)
            return

        fields = _response_fields(body)

        # `is True` and not truthiness: a body carrying the string "false", or
        # 1, or a stray HTML page parsed as something odd, must not read as
        # success.
        if fields.get("issuccess") is True:
            logger.info(
                f"AddCallResult pushed for TrackingId {tracking_id} "
                f"status={status} (attempt {attempt})"
            )
            return

        # `Message` is the live shape's second text field; fall back to it so
        # a failure that only populates that one is not logged as silent.
        error_message = self._safe(
            fields.get("errormessage")
            or fields.get("message")
            or "<no errorMessage in response>"
        )

        # Field-level validation errors are the one isSuccess:false variety
        # that is definitively permanent -- the request itself is malformed,
        # so every retry rebuilds the identical bad request while re-forcing
        # FinalOutcome on their side. Dead-letter it immediately instead.
        validation_errors = fields.get("validationerrors")
        if validation_errors:
            logger.error(
                f"AddCallResult push rejected as invalid for TrackingId "
                f"{tracking_id}: {error_message} {self._safe(validation_errors)}; "
                "not retrying"
            )
            self._dead_letter(record, reason="validation_error")
            return

        # Everything else is retried on purpose. The two documented
        # isSuccess:false cases are both transient:
        #   * "No BatchCallDetail found for TrackingId ..." -- the
        #     row-arrives-late race in CALL_STATUS_TRACKING.md ("The RingAt
        #     race"): the .NET client can create the row up to ~3.4 s after
        #     POST /calls is accepted, and a 486 Busy can resolve a call in
        #     ~1 s, so early attempts legitimately find nothing.
        #   * a Tamweely-side failure -- re-sending after exactly that is the
        #     endpoint's own stated purpose (§1).
        #
        # This is at-least-once delivery: a lost response means a duplicate
        # push, and the guide does not promise the receiving API is
        # idempotent. Bounded by _max_retries; see ADD_CALL_RESULT.md.
        logger.warning(
            f"AddCallResult push not accepted for TrackingId {tracking_id} "
            f"(attempt {attempt}/{self._max_retries}): {error_message}"
        )
        self._schedule_retry(record, attempt)

    def _schedule_retry(self, record: dict, attempt: int) -> None:
        tracking_id = record["tracking_id"]
        age = time.time() - record.get("_first_attempt_at", time.time())
        if attempt >= self._max_retries or age >= self._max_retry_age:
            logger.error(
                f"AddCallResult push permanently failed for TrackingId "
                f"{tracking_id} after {attempt} attempt(s)"
            )
            self._dead_letter(record, reason="retries_exhausted")
            return

        base_backoff = min(2 ** (attempt - 1), 30)
        backoff = base_backoff + random.uniform(0, base_backoff * 0.25)  # jitter

        # Give up now if the wait itself would carry the record past its
        # budget, rather than sleeping through the deadline and only noticing
        # on the far side. With the 30 s cap plus jitter that overshoot
        # reached ~37 s.
        if age + backoff >= self._max_retry_age:
            logger.error(
                f"AddCallResult push permanently failed for TrackingId "
                f"{tracking_id}: the next retry would fall outside "
                f"ADD_CALL_RESULT_MAX_RETRY_AGE_SECONDS"
            )
            self._dead_letter(record, reason="retries_exhausted")
            return

        entry_id = next(self._entry_ids)
        timer = threading.Timer(
            backoff, lambda: self._submit_retry(entry_id, record, attempt + 1)
        )
        timer.daemon = True
        with self._timers_lock:
            # The shutdown check, the registration and the start all happen
            # under the one lock shutdown() also takes. WebhookSender checks
            # the flag OUTSIDE its lock, so a shutdown landing between the
            # check and the append leaves a timer that escapes the cancel
            # sweep and later submits to a closed executor. This cannot.
            stopped = self._shutting_down
            if not stopped:
                self._timers[entry_id] = (timer, record, attempt + 1)
                timer.start()

        # Outside the lock: _dead_letter writes to disk, and shutdown() waits
        # on this same lock.
        if stopped:
            self._dead_letter(record, reason="shutting_down")

    def _submit_retry(self, entry_id: int, record: dict, attempt: int) -> None:
        """Runs on the Timer thread -- hand straight back to the pool.

        Claims the entry by popping it. Exactly one of this method and
        shutdown() can win that pop, and that is what makes each pending
        record handled once and only once.

        The obvious-looking alternative -- letting shutdown() tell a fired
        timer from a pending one via Timer.is_alive() -- does NOT work. A
        Timer is a Thread, and it stays alive for as long as its callback
        runs, i.e. throughout this very method. Shutdown would read it as
        still pending, dead-letter it, and the attempt submitted a
        microsecond later could then succeed -- putting a DELIVERED result
        into the manual re-push worklist and causing exactly the duplicate
        push this module exists to avoid.
        """
        with self._timers_lock:
            claimed = self._timers.pop(entry_id, None) is not None
            stopped = self._shutting_down

        if not claimed:
            # shutdown() got here first and has already dead-lettered this
            # record. Writing it again would double the worklist entry.
            return
        if stopped:
            self._dead_letter(record, reason="shutting_down")
            return
        try:
            self._executor.submit(self._attempt, record, attempt)
        except Exception:
            self._dead_letter(record, reason="shutting_down")

    def _dead_letter(self, record: dict, reason: str = "") -> None:
        """Append an undelivered result so it can be re-pushed by hand.

        The written record carries only tracking_id and status -- the API key
        is never part of a record, so it cannot leak here. Underscore-prefixed
        bookkeeping keys are stripped.
        """
        if not self._dead_letter_path:
            return
        try:
            out = {k: v for k, v in record.items() if not k.startswith("_")}
            out["_dead_lettered_at"] = time.time()
            if reason:
                out["_reason"] = reason
            line = json.dumps(out, default=str) + "\n"
            # Four workers plus Timer threads can reach this concurrently.
            with self._dead_letter_lock:
                with open(self._dead_letter_path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            logger.exception("failed to write AddCallResult dead-letter record")

    def shutdown(self) -> None:
        """Stop scheduling new work and cancel pending retries.

        In-flight HTTP requests are NOT abandoned: ThreadPoolExecutor worker
        threads are non-daemonic, so the interpreter joins them on the way
        out and a request already on the wire runs to its timeout. What that
        cannot tell us is whether a request interrupted by process exit was
        received -- the dead-letter file plus a manual re-push is the answer
        to that, not a longer wait here.
        """
        with self._timers_lock:
            # Set INSIDE the lock so it is impossible for _schedule_retry to
            # register a timer after this sweep has run.
            self._shutting_down = True
            # Whatever is still in the dict has NOT been claimed by its timer,
            # so it is genuinely un-run: claim it here by clearing the dict.
            # A timer that already fired removed its own entry in
            # _submit_retry and is therefore absent -- which is the whole
            # reason entries are keyed by entry_id rather than filtered with
            # Timer.is_alive(), a question that answers "still pending" while
            # the callback is in fact already running.
            pending = list(self._timers.values())
            self._timers.clear()
            for timer, _record, _attempt in pending:
                timer.cancel()
        self._executor.shutdown(wait=False)

        # Outside the lock (file I/O). Cancelling a Timer throws away the
        # retry it was holding, so without this the outcome would be neither
        # delivered nor recoverable -- exactly what the dead-letter file
        # exists to prevent. Every record here was claimed above, so its
        # timer's _submit_retry finds nothing to pop and returns without
        # writing a second line.
        for _timer, record, _attempt in pending:
            self._dead_letter(record, reason="shutdown_cancelled_retry")


# --------------------------------------------------------------------------
# Module-level surface.
#
# call_session.py consumes this the same way it consumes db.py -- plain module
# functions, no injected object -- because _record_call_ended() already calls
# db.mark_ended() that way and the two writes belong side by side.
# --------------------------------------------------------------------------
_sender: Optional[AddCallResultSender] = None
_sender_lock = threading.Lock()
_shutdown_called = False


def get_sender() -> AddCallResultSender:
    """The process-wide sender.

    CallManager builds it eagerly at startup so no SIP thread ever pays for
    the constructor (ThreadPoolExecutor + os.makedirs). This stays correct if
    something reaches it first.

    The whole check runs under the lock -- no double-checked fast path. The
    unlocked pre-check raced shutdown(): reader sees None, shutdown() takes
    the lock and also sees None so has nothing to stop, reader then builds a
    LIVE sender that nothing will ever shut down. A push is once per
    unanswered call, so an uncontended lock costs nothing worth optimising.
    """
    global _sender
    with _sender_lock:
        if _sender is None:
            _sender = AddCallResultSender(_module_settings)
            if _shutdown_called:
                # Built after the service already stopped: born shut down, so
                # anything handed to it is dead-lettered rather than sent.
                _sender.shutdown()
        return _sender


def push_async(tracking_id: str, status: str) -> None:
    get_sender().push_async(tracking_id, status)


def shutdown() -> None:
    """Stop delivery. The instance is deliberately kept rather than cleared:
    a push arriving after this point must be dead-lettered by the shut-down
    sender, not silently handled by a freshly built live one."""
    global _shutdown_called
    with _sender_lock:
        _shutdown_called = True
        if _sender is not None:
            _sender.shutdown()
