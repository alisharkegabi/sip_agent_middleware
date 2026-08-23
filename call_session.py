"""
CallSession: everything about ONE call, isolated from every other call.

Refactor of the original main() control flow, now hardened per
PRODUCTION_HARDENING_WORK_ORDER.md. Every step — TCP connect, INVITE, 401
challenge, authenticated re-INVITE, waiting for 200 OK / rejection, ACK, RTP
bridge bring-up, ElevenLabs conversation start, in-dialog request handling,
BYE/CANCEL, and cleanup — happens on its own thread with its own socket, RTP
port, SIP dialog identifiers, and ElevenLabs session, same as before.

What's new here relative to the original refactor:
  - F-02: every wait loop has a monotonic deadline, not just a hangup flag.
  - F-04: uses SipStream (a real framer) instead of the old recv_full().
  - F-05: full SIP response-class handling + qop digest auth, capped retries.
  - F-06: heavy references are dropped at the end of cleanup.
  - F-09: RTP silence starts flowing immediately after ACK.
  - F-12: RTP is targeted at the SDP answer's media address, with latching.
  - F-14/F-15: fresh branch per transaction; CANCEL for unconfirmed dialogs;
    in-dialog OPTIONS/INFO/NOTIFY/UPDATE/re-INVITE are answered instead of
    silently ignored.
  - F-17: the real end-of-call reason is recorded, not a blanket COMPLETED.
  - F-24: ended_at/conversation_id are written under _status_lock.

No two CallSessions ever touch each other's state. The only shared object
they touch is the PortAllocator (acquire/release), which is internally
locked, and log files, which are per-call.
"""
from __future__ import annotations

import queue
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import ClientTools, Conversation, ConversationInitiationData

import add_call_result
import db
import sip_protocol as sip
from audio_bridge import RtpAudioInterface
from config import Settings
from transfer_targets import TransferTargets
from logging_config import get_call_logger
from models import CallStatus
from port_allocator import PortAllocator
from transfer_trigger import matches_transfer_phrase


def _read_conversation_id(conversation) -> Optional[str]:
    """Best-effort read of the id populated early by the ElevenLabs SDK."""
    try:
        conversation_id = getattr(conversation, "_conversation_id", None)
        return str(conversation_id) if conversation_id else None
    except Exception:
        # This is a private SDK seam. A renamed property or unusual SDK value
        # must degrade to a missing id, never interrupt the live SIP call.
        return None


@dataclass
class _TransferRequest:
    """Handoff between whichever thread requested a transfer (the
    ElevenLabs websocket thread for an agent-spoken phrase, ClientTools'
    executor thread for the legacy client tool, or FastAPI's threadpool for
    the HTTP fallback) and the SIP thread running _bridge()'s loop -- the
    only thread allowed to touch self._sock / self._stream. The requesting
    thread blocks on result_event; the bridge loop picks the request off
    the queue, chooses the target itself (extension starts as None for
    every entry point now -- see CallSession._request_transfer /
    _maybe_trigger_transfer), does the REFER, and sets the result."""
    extension: Optional[str] = None   # None => the SIP thread chooses it
    source: str = "agent_phrase"      # "agent_phrase" | "client_tool" | "http"
    result_event: threading.Event = field(default_factory=threading.Event)
    result: Optional[dict] = None


class CallSession:
    def __init__(
        self,
        *,
        phone_number: str,
        dynamic_variables: dict,
        settings: Settings,
        port_allocator: PortAllocator,
        transfer_targets: Optional[TransferTargets] = None,
        tracking_id: Optional[str] = None,
        speech_dynamic_variables: Optional[dict] = None,
        busy_frames: Optional[list] = None,
    ):
        self.call_id = uuid.uuid4().hex  # our internal id, exposed via the API
        self.phone_number = phone_number
        self.dynamic_variables = dynamic_variables
        # Arabic name spellings corrected for TTS (see name_normalizer.py),
        # built by the API layer BEFORE dialling. Only _bridge() reads it, so
        # to_webhook_payload() keeps echoing the client's raw payload. Falls
        # back to the raw dict, which is what every existing caller and test
        # gets by not passing it.
        self.speech_dynamic_variables = (
            speech_dynamic_variables if speech_dynamic_variables is not None else dynamic_variables
        )
        self.tracking_id = tracking_id or dynamic_variables.get("tracking_id")
        self.settings = settings
        self._port_allocator = port_allocator
        self._transfer_targets = transfer_targets
        self.logger = get_call_logger(self.call_id)
        self.conversation_id = None
        # Filled in after the call by AnalysisFetcher (evaluation criteria +
        # data collection results). None until ElevenLabs finishes analysing.
        self.analysis: Optional[dict] = None
        self.transferred_to: Optional[str] = None
        self._transfer_requests: "queue.Queue[_TransferRequest]" = queue.Queue()

        # Per-call transfer state. Everything here lives on self -- the only
        # shared object across calls is TransferTargets, which is internally
        # locked -- which is what structurally prevents one call's trigger
        # phrase (or its transfer outcome) from ever touching another call.
        self._transfer_guard = threading.Lock()
        self._transfer_started = False   # one-shot: at most ONE transfer attempt per call
        self._transfer_failed = False    # D6: an announced transfer that never completed
        self._el_session_closed = False  # makes _close_el_session() idempotent
        self._conversation_id_missing_warned = False
        self._busy_frames = busy_frames  # pre-built mu-law frames, or None if unavailable

        # Stable for the whole dialog (RFC 3261): Call-ID and From tag.
        self._sip_call_id = f"{uuid.uuid4()}@{settings.local_ip}"
        self._from_tag = uuid.uuid4().hex[:8]
        # F-14: branch is now per-TRANSACTION, generated fresh each time via
        # sip.new_branch() -- no single fixed value for the whole dialog.

        self.status: CallStatus = CallStatus.PENDING
        self.error: Optional[str] = None
        self.exit_reason: Optional[str] = None  # F-17
        self.answered: bool = False
        self.created_at = time.time()
        self.queued_at = time.time()  # F-19
        self.dialed_at: Optional[float] = None
        self.connected_at: Optional[float] = None
        self.ended_at: Optional[float] = None
        self.local_rtp_port: Optional[int] = None
        self.remote_rtp_port: Optional[int] = None
        self.last_turn_latency: Optional[dict] = None

        self._status_lock = threading.Lock()
        self._hangup_requested = threading.Event()  # per-call, replaces the global shutdown_requested
        self._hangup_reason = "api"  # set by request_hangup(); see _record_call_ended

        self._sock: Optional[socket.socket] = None
        self._stream: Optional[sip.SipStream] = None
        self._rtp_interface: Optional[RtpAudioInterface] = None
        self._conversation: Optional[Conversation] = None
        self._last_reject_code: Optional[int] = None

    def _db_call(self, func, *args, **kwargs) -> None:
        """Best-effort write to BatchCallDetails -- never let a DB hiccup
        affect the SIP call flow."""
        if not self.tracking_id:
            return
        try:
            func(self.tracking_id, *args, **kwargs)
        except Exception:
            self.logger.exception(f"failed to record call status in BatchCallDetails ({func.__name__})")

    def _push_call_result(self, status: str) -> None:
        """Best-effort push of a terminal outcome to Tamweely's AddCallResult
        API -- same contract as _db_call: no-op without a tracking_id, and a
        Tamweely outage can never affect the SIP call flow. The push itself is
        asynchronous (add_call_result.push_async returns immediately), so this
        does not block the SIP thread it is called from."""
        if not self.tracking_id:
            return
        try:
            add_call_result.push_async(self.tracking_id, status)
        except Exception:
            self.logger.exception(
                f"failed to push call result to Tamweely (status={status})"
            )

    # ------------------------------------------------------------------
    # Public control surface (called by CallManager / API layer)
    # ------------------------------------------------------------------
    def request_hangup(self, reason: str = "api") -> None:
        """reason distinguishes who asked. "api" is the .NET client calling
        POST /calls/{id}/hangup -- a real cancellation of that call.
        "shutdown" is CallManager draining every live session so the service
        can stop, which is not a statement about this call at all and must
        never be reported to Tamweely as "customer no answer".

        FIRST writer wins, under the lock. Both can fire on one call: a
        shutdown drain hangs up every live session, and an API hangup for the
        same call can land in the same instant. Last-writer-wins let a
        default-"api" call arriving after the drain flip the reason back and
        push 202 for a deploy. Whoever asked first is the one that actually
        ended the call, and that holds in both orderings."""
        with self._status_lock:
            if not self._hangup_requested.is_set():
                self._hangup_reason = reason
            self._hangup_requested.set()

    def request_transfer(self) -> dict:
        """HTTP fallback (POST /calls/{call_id}/transfer or
        /calls/by-tracking-id/{tracking_id}/transfer) -- kept for backward
        compatibility (D7). The primary transfer trigger is now the agent's
        own transcript phrase; see _maybe_trigger_transfer. Blocks the
        calling thread until the transfer resolves or times out, so callers
        (main.py) must run this off the event loop."""
        return self._request_transfer(source="http")

    def set_analysis(self, analysis: dict) -> None:
        """Called from an analysis-fetch thread once ElevenLabs' post-call
        record is available; guarded like every other cross-thread write to
        session state (F-24)."""
        with self._status_lock:
            self.analysis = analysis

    def _capture_conversation_id(self) -> None:
        """Capture the SDK's early conversation id without risking call flow."""
        if self.conversation_id:
            return

        conversation = self._conversation
        if conversation is None:
            return

        try:
            attribute_missing = not hasattr(conversation, "_conversation_id")
            conversation_id = _read_conversation_id(conversation)
        except Exception:
            # This runs on every bridge tick against a private SDK seam. A
            # descriptor that raises must not escape into the SIP thread and
            # terminate an otherwise healthy call.
            return

        if attribute_missing:
            if not self._conversation_id_missing_warned:
                self._conversation_id_missing_warned = True
                self.logger.warning(
                    "ElevenLabs Conversation has no _conversation_id attribute; "
                    "the SDK renamed it and post-call analysis will be lost"
                )
            return
        if not conversation_id:
            return

        captured = False
        # F-24: to_dict()/to_webhook_payload() read under _status_lock, so
        # every writer -- including this SIP-thread early capture -- must use it.
        with self._status_lock:
            if not self.conversation_id:
                self.conversation_id = conversation_id
                captured = True
        if captured:
            self.logger.info(f"captured ElevenLabs conversation_id={conversation_id}")

    def _set_status(self, status: CallStatus, error: Optional[str] = None) -> None:
        with self._status_lock:
            self.status = status
            if error:
                self.error = error

    def to_dict(self) -> dict:
        with self._status_lock:
            talk_seconds = None
            if self.connected_at is not None and self.ended_at is not None:
                talk_seconds = round(self.ended_at - self.connected_at, 3)
            return {
                "call_id": self.call_id,
                "conversation_id": self.conversation_id,
                "phone_number": self.phone_number,
                "tracking_id": self.tracking_id,
                "status": self.status,
                "error": self.error,
                "created_at": self.created_at,
                "dialed_at": self.dialed_at,
                "connected_at": self.connected_at,
                "ended_at": self.ended_at,
                "remote_rtp_port": self.remote_rtp_port,
                "local_rtp_port": self.local_rtp_port,
                "last_turn_latency": self.last_turn_latency,
                "exit_reason": self.exit_reason,
                "answered": self.answered,
                "talk_seconds": talk_seconds,
                "transferred_to": self.transferred_to,
                "analysis": self.analysis,
            }

    def to_webhook_payload(self, event: str = "call_status") -> dict:
        """Terminal-state notification payload sent to the configured webhook.

        Reuses the same fields as to_dict()/CallDetail -- no new state is
        introduced, this is just a different shape for external consumers.

        Sent twice per call when post-call analysis is enabled:
          - event="call_status"   immediately at terminal status, analysis=null
          - event="call_analysis" seconds later, once ElevenLabs has produced
                                  the evaluation/data-collection results
        Everything except `event` and `analysis` is identical between the
        two, so a consumer that only cares about completion can keep
        processing the first one exactly as before and ignore the second.
        """
        with self._status_lock:
            started_at = self.dialed_at if self.dialed_at is not None else self.created_at
            duration_seconds = None
            if self.ended_at is not None and started_at is not None:
                duration_seconds = round(self.ended_at - started_at, 3)
            return {
                "event": event,
                "call_id": self.call_id,
                "conversation_id": self.conversation_id,
                "status": self.status.value,
                "started_at": started_at,
                "ended_at": self.ended_at,
                "duration_seconds": duration_seconds,
                "reason": self.exit_reason or self.error or self.status.value,
                "analysis": self.analysis,
                "metadata": {
                    "phone_number": self.phone_number,
                    "tracking_id": self.tracking_id,
                    "remote_rtp_port": self.remote_rtp_port,
                    "local_rtp_port": self.local_rtp_port,
                    "last_turn_latency": self.last_turn_latency,
                    "answered": self.answered,
                    "dynamic_variables": self.dynamic_variables,
                    "transferred_to": self.transferred_to,
                },
            }

    # ------------------------------------------------------------------
    # Main entry point — runs entirely on a CallManager worker thread.
    # ------------------------------------------------------------------
    def run(self) -> None:
        # F-19: a call that sat in the executor queue too long (e.g. a
        # morning batch of hundreds of calls) shouldn't dial hours after it
        # was relevant.
        queue_wait = time.time() - self.queued_at
        if queue_wait > self.settings.max_queue_wait_seconds:
            self._set_status(CallStatus.CANCELLED, "queue_timeout")
            self._finish("queue_timeout")
            return

        try:
            self.local_rtp_port = self._port_allocator.acquire()
        except Exception as e:
            self._set_status(CallStatus.FAILED, str(e))
            self.logger.error(f"could not allocate RTP port: {e}")
            self._finish("port_exhausted")
            return

        exit_reason = "unknown"
        try:
            exit_reason = self._dial()
        except _CallAborted as e:
            self._set_status(CallStatus.CANCELLED if e.cancelled else CallStatus.FAILED, str(e) or None)
            exit_reason = e.reason
        except _CallRejected as e:
            self._set_status(CallStatus.REJECTED, str(e))
            exit_reason = e.reason
        except Exception as e:
            self.logger.exception("unhandled error during call")
            self._set_status(CallStatus.FAILED, str(e))
            exit_reason = "internal_error"
        finally:
            self._cleanup()
            self._port_allocator.release(self.local_rtp_port)
            self._finish(exit_reason)

    def _finish(self, exit_reason: str) -> None:
        with self._status_lock:  # F-24: write under the same lock to_dict() reads under
            self.exit_reason = exit_reason
            self.ended_at = time.time()
        self._record_call_ended(exit_reason)

    def _record_call_ended(self, exit_reason: str) -> None:
        """BatchCallDetails.EndedAt is set for every terminal reason. Status
        is only overwritten for terminal outcomes the PBX call-status scheme
        distinguishes in their own right: Cancelled (486/503, or a local
        hangup before the callee answered), Timeout (CANCEL sent after
        max_ring_seconds with no answer), Transfer (an internal transfer
        completed), and TranFail (a transfer was announced -- the agent
        said the trigger phrase -- but never completed, whether because no
        extension was free or because the PBX rejected/timed out the
        REFER). Every other terminal reason (BYE either side, max
        duration, RTP inactivity, or a failure outside this scheme) just
        stamps EndedAt and leaves Status as whatever mark_ringing/
        mark_answered already set.

        Order matters, in this exact sequence:
          1. "transferred" first -- a call that failed one transfer attempt
             and then succeeded on a retry must record Transfer, not
             TranFail. The sticky self._transfer_failed flag must never
             beat an actual success.
          2. self._transfer_failed second, ahead of ring_timeout /
             local_hangup / reject_status. Those three are all pre-answer
             states where no transfer could have been announced, so there
             is no real conflict -- but this position also means a failed
             transfer followed by e.g. sip_disconnect or internal_error
             (reasons nothing else here writes a status for) still records
             TranFail. That's intended: the transfer failure is a known
             fact, not a guess, and the callback obligation holds
             regardless of how the call finally ended.

        A SUBSET of those statuses is additionally pushed to Tamweely's
        AddCallResult API (see ADD_CALL_RESULT.md) -- the calls that never
        produced an ElevenLabs conversation, and so would otherwise reach
        Tamweely with no result at all. The push set is narrower than the DB
        set on both edges:

          * 503 writes Cancelled locally but is NOT pushed -- that is the PBX
            failing, not the customer declining.
          * a shutdown-initiated hangup writes Cancelled locally but is NOT
            pushed -- that is our deploy, not the customer's behaviour.

        Both exclusions exist for the same reason the failure reasons below
        write nothing at all: a status we would have to guess at is worse
        than no status, because a pushed Cancelled/Timeout permanently
        overwrites FinalOutcome and SummaryArabic on Tamweely's side.
        """
        reject_status = db.sip_response_to_status(self._last_reject_code) if self._last_reject_code else None
        # Set only for the no-answer family -- the calls that produce no
        # ElevenLabs conversation, and therefore no post-call webhook, and
        # therefore no result at Tamweely unless we push one. See
        # ADD_CALL_RESULT.md.
        push_status: Optional[str] = None
        if exit_reason == "transferred":
            self._db_call(db.mark_ended, status=db.STATUS_TRANSFERRED)
        elif exit_reason == "transfer_unavailable" or self._transfer_failed:
            self._db_call(db.mark_ended, status=db.STATUS_TRANSFER_FAILED)
        elif exit_reason == "ring_timeout":
            self._db_call(db.mark_ended, status=db.STATUS_TIMEOUT)
            push_status = db.STATUS_TIMEOUT
        elif exit_reason == "local_hangup" and not self.answered:
            self._db_call(db.mark_ended, status=db.STATUS_CANCELLED)
            # Only when a client actually cancelled this call. CallManager's
            # shutdown drain hangs up every ringing session so the service can
            # stop; reporting a deploy or a service restart to Tamweely as
            # "customer no answer" would state something false about a
            # customer who was still being rung. Status stays Cancelled
            # locally either way -- that column is ours.
            if self._hangup_reason != "shutdown":
                push_status = db.STATUS_CANCELLED
        elif reject_status is not None:
            self._db_call(db.mark_ended, status=reject_status)
            # Narrower than the DB mapping on purpose: db treats 486 and 503
            # alike, but 503 is the PBX or a trunk failing, not the customer
            # declining. See CUSTOMER_NO_ANSWER_SIP_CODES.
            if add_call_result.is_customer_no_answer(self._last_reject_code):
                push_status = reject_status
        elif exit_reason in (
            "remote_bye", "agent_ended", "local_hangup", "max_duration", "rtp_timeout",
        ):
            self._db_call(db.mark_ended)

        # After the DB write, never before: mark_ended() owns EndedAt and is
        # the record we keep even if Tamweely is unreachable. The push adds
        # what only Tamweely's side can do -- FinalOutcome 202 and
        # SummaryArabic -- and is asynchronous, so this returns at once.
        if push_status is not None:
            self._push_call_result(push_status)

    # ------------------------------------------------------------------
    # SIP handshake + bridge (equivalent to the reference script's main())
    # ------------------------------------------------------------------
    def _dial(self) -> str:
        """Returns the exit_reason string for a call that never reached the
        bridge (rejected/failed/cancelled during handshake), or delegates to
        _bridge() once connected."""
        cfg = self.settings
        self._set_status(CallStatus.DIALING)
        self.dialed_at = time.time()
        deadline_connect = time.monotonic() + cfg.sip_connect_timeout

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # F-04: SO_LINGER(1,0) forces an RST on close, which can leave the
        # dialog dangling on the PBX side. Keep it available only as a
        # last-resort abort path (see _abort_close()); normal close is a
        # graceful FIN after BYE/CANCEL.
        self._sock.bind((cfg.local_ip, 0))
        local_port = self._sock.getsockname()[1]

        # F-02: connect() previously had NO timeout at all; on Windows an
        # unreachable/overloaded PBX blocked this for ~21s per attempt.
        self._sock.settimeout(cfg.sip_connect_timeout)
        self.logger.info(f"connecting to SIP PBX at {cfg.pbx_ip}:{cfg.pbx_port} (local port {local_port})")
        try:
            self._sock.connect((cfg.pbx_ip, cfg.pbx_port))
        except socket.timeout:
            raise _CallAborted("connect timed out", reason="connect_timeout")
        except OSError as e:
            raise _CallAborted(f"connect failed: {e}", reason="connect_failed")

        # F-04: Nagle can hold small SIP messages (ACK/BYE) for ~200ms and
        # makes coalescing of adjacent messages more likely.
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._stream = sip.SipStream(self._sock)

        sdp = sip.build_sdp(cfg.local_ip, self.local_rtp_port)
        dialog_kwargs = dict(
            local_ip=cfg.local_ip,
            local_port=local_port,
            pbx_ip=cfg.pbx_ip,
            target_number=self.phone_number,
            ext_user=cfg.ext_user,
            call_id=self._sip_call_id,
            from_tag=self._from_tag,
        )

        invite_branch = sip.new_branch()
        self._sock.sendall(
            sip.build_invite(**dialog_kwargs, branch=invite_branch, sdp=sdp, cseq=1).encode()
        )
        frame = self._stream.read_message(cfg.sip_recv_timeout)
        if frame.kind == sip.FrameKind.CLOSED:
            raise _CallAborted("PBX dropped socket during initial INVITE", reason="sip_disconnect")
        text = frame.text or ""
        if "401 Unauthorized" not in text and "407 Proxy Authentication" not in text:
            raise _CallAborted("initialization failed: no auth challenge received", reason="no_auth_challenge")

        auth_attempts = 0
        remote_tag = ""
        answer_sdp = ""
        cseq = 1

        while auth_attempts < cfg.max_auth_attempts:
            auth_attempts += 1
            realm, nonce, qop, opaque = sip.parse_www_auth(text)
            uri = f"sip:{self.phone_number}@{cfg.pbx_ip}"
            resp, cnonce = sip.digest_response(
                cfg.ext_user, cfg.ext_pass, realm, nonce, "INVITE", uri, qop=qop
            )
            auth_header = (
                f'Authorization: Digest username="{cfg.ext_user}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", response="{resp}", algorithm=MD5'
            )
            if qop:
                auth_header += f', qop={qop}, nc=00000001, cnonce="{cnonce}"'
            if opaque:
                auth_header += f', opaque="{opaque}"'
            auth_header += "\r\n"

            cseq += 1
            invite_branch = sip.new_branch()  # F-14: fresh branch per transaction
            self._sock.sendall(
                sip.build_invite(
                    **dialog_kwargs, branch=invite_branch, sdp=sdp, cseq=cseq, extra_headers=auth_header
                ).encode()
            )
            self._set_status(CallStatus.RINGING)
            self._db_call(db.mark_ringing)
            self.logger.info(f"sent authenticated INVITE (attempt {auth_attempts}), waiting for target to answer")

            outcome, remote_tag, answer_sdp = self._wait_for_answer(invite_branch, cseq)
            if outcome == "reauth":
                # Server issued another 401/407 even after credentials --
                # try once more (bounded by max_auth_attempts) rather than
                # looping forever.
                frame = self._last_challenge_frame
                text = frame.text or ""
                continue
            break
        else:
            raise _CallRejected("authentication rejected after max attempts", reason="auth_rejected")

        if outcome == "cancelled":
            self._send_cancel(invite_branch, cseq, dialog_kwargs)
            raise _CallAborted("cancelled during handshake", cancelled=True, reason="local_hangup")
        if outcome == "rejected":
            raise _CallRejected(self._last_reject_reason, reason=self._last_reject_reason)
        if outcome == "no_answer":
            self._send_cancel(invite_branch, cseq, dialog_kwargs)
            raise _CallAborted("ring timeout, no answer", reason="ring_timeout")
        if outcome != "answered":
            raise _CallAborted(f"unexpected handshake outcome: {outcome}", reason="handshake_error")

        parsed_media_ip = self._parse_sdp_media_address(answer_sdp)
        if parsed_media_ip and parsed_media_ip != cfg.pbx_ip:
            self.logger.warning(
                f"SDP answer c= line ({parsed_media_ip}) differs from PBX_IP "
                f"({cfg.pbx_ip}); sending RTP to PBX_IP to match known-good behavior"
            )
        # Known-good behavior (matches the version that transmits audio
        # successfully): always target the PBX's signaling IP for RTP,
        # rather than trusting the SDP answer's c= line. Some PBX/SBC
        # deployments advertise an internal media address there that isn't
        # actually reachable the same way, which breaks the RTP path.
        media_ip = cfg.pbx_ip
        self.answered = True
        self._db_call(db.mark_answered)

        ack_branch = sip.new_branch()  # F-14: ACK to a 2xx is its own transaction
        self._sock.sendall(
            sip.build_ack(**dialog_kwargs, branch=ack_branch, cseq=cseq, remote_tag=remote_tag).encode()
        )
        self.logger.info("handshake established, starting ElevenLabs session")

        return self._bridge(
            local_port=local_port,
            dialog_kwargs=dialog_kwargs,
            remote_tag=remote_tag,
            media_ip=media_ip,
            next_cseq=cseq + 1,
        )

    # ------------------------------------------------------------------
    def _wait_for_answer(self, invite_branch: str, invite_cseq: int) -> tuple:
        """F-02/F-05: bounded wait for a final response to the INVITE.
        Returns (outcome, remote_tag, answer_sdp) where outcome is one of
        'answered' | 'rejected' | 'cancelled' | 'no_answer' | 'reauth'."""
        cfg = self.settings
        deadline = time.monotonic() + cfg.max_ring_seconds
        remote_tag = ""
        self._last_reject_reason = "rejected"
        self._last_reject_code = None
        self._last_challenge_frame = None

        while not self._hangup_requested.is_set():
            if time.monotonic() >= deadline:
                return "no_answer", "", ""

            remaining = deadline - time.monotonic()
            frame = self._stream.read_message(min(cfg.sip_recv_timeout, max(remaining, 0.1)))
            if frame.kind == sip.FrameKind.CLOSED:
                raise _CallAborted("PBX closed connection during handshake", reason="sip_disconnect")
            if frame.kind == sip.FrameKind.TIMEOUT:
                continue

            response = frame.text or ""
            first_line = response.splitlines()[0] if response else ""
            self.logger.info(f"[SIP Status] {first_line}")

            parsed = sip.parse_status_line(response)
            if parsed is None:
                continue  # not a status line (shouldn't happen mid-handshake); keep waiting
            code, reason_phrase = parsed

            if 100 <= code < 200:
                continue  # informational (100 Trying, 180 Ringing, 183...) — keep waiting

            if code in (401, 407):
                self._last_challenge_frame = frame
                return "reauth", "", ""

            if 200 <= code < 300:
                cseq_match = re.search(r"CSeq:\s*(\d+)\s+INVITE", response, re.IGNORECASE)
                if cseq_match and int(cseq_match.group(1)) != invite_cseq:
                    continue  # response to a stale/earlier transaction; keep waiting
                tag_match = re.search(r"To: .*;tag=(.*)", response, re.IGNORECASE)
                if tag_match:
                    remote_tag = f";tag={tag_match.group(1).strip()}"
                media_match = re.search(r"m=audio (\d+)", response)
                if not media_match:
                    self._last_reject_reason = "no_sdp_answer"
                    return "rejected", "", ""
                self.remote_rtp_port = int(media_match.group(1))
                self.logger.info(f"media connected, remote RTP port {self.remote_rtp_port}")
                return "answered", remote_tag, response

            if 300 <= code < 400:
                self._last_reject_reason = "redirect_unsupported"
                self._last_reject_code = code
                return "rejected", "", ""

            if code in (486, 603, 600, 604):
                self._last_reject_reason = f"rejected_{code}"
                self._last_reject_code = code
                return "rejected", "", ""

            # Any other 4xx/5xx/6xx (e.g. 503)
            self._last_reject_reason = f"failed_{code}_{reason_phrase}".strip()
            self._last_reject_code = code
            return "rejected", "", ""

        return "cancelled", "", ""

    def _send_cancel(self, invite_branch: str, invite_cseq: int, dialog_kwargs: dict) -> None:
        """F-15: CANCEL an unconfirmed dialog instead of abandoning it (which
        leaves the callee's leg ringing/orphaned on the PBX)."""
        cfg = self.settings
        try:
            self._sock.sendall(
                sip.build_cancel(
                    **{k: v for k, v in dialog_kwargs.items() if k != "branch"},
                    branch=invite_branch,  # CANCEL MUST reuse the INVITE's branch
                    cseq=invite_cseq,
                ).encode()
            )
            self.logger.info("sent CANCEL for unconfirmed dialog")
            deadline = time.monotonic() + cfg.cancel_wait_seconds
            while time.monotonic() < deadline:
                frame = self._stream.read_message(max(deadline - time.monotonic(), 0.1))
                if frame.kind == sip.FrameKind.MESSAGE and frame.text and "487" in frame.text.splitlines()[0]:
                    # ACK the 487 to fully close the CANCEL transaction.
                    ack_branch = sip.new_branch()
                    self._sock.sendall(
                        sip.build_ack(
                            **{k: v for k, v in dialog_kwargs.items() if k != "branch"},
                            branch=ack_branch,
                            cseq=invite_cseq,
                        ).encode()
                    )
                    break
                if frame.kind == sip.FrameKind.CLOSED:
                    break
        except Exception:
            self.logger.exception("error sending CANCEL")

    # ------------------------------------------------------------------
    # Call transfer (SIP REFER to an internal extension)
    # ------------------------------------------------------------------
    def _claim_transfer(self) -> bool:
        """Compare-and-set. Returns True exactly once per CallSession, no
        matter which entry point (agent phrase / client tool / HTTP) calls
        it first or how many times it's called after. This, plus the fact
        that on_agent_response is a closure bound to one CallSession
        instance created fresh per call in _bridge(), is what structurally
        prevents one call's trigger from ever moving another call's leg or
        writing another call's tracking_id."""
        with self._transfer_guard:
            if self._transfer_started:
                return False
            self._transfer_started = True
            return True

    def _mark_transfer_failed(self) -> None:
        """The agent told the caller they were being transferred and it did
        not happen -- no free line, or the PBX rejected/timed out the REFER
        (D6). Sticky for the rest of the call: a later successful transfer
        still wins over this, because _record_call_ended() checks
        exit_reason == "transferred" before it checks this flag."""
        with self._status_lock:
            self._transfer_failed = True

    def _maybe_trigger_transfer(self, text: str) -> None:
        """Called from on_agent_response for every agent utterance on every
        live call -- runs on the ElevenLabs SDK's websocket receive thread,
        NOT the SIP thread, so this must return in microseconds: no I/O, no
        extension acquisition, no socket work, no DB work. It only matches
        the transcript against the configured trigger phrase(s) and, if
        matched, hands off to the SIP thread via self._transfer_requests --
        the actual REFER happens in _bridge()'s loop. Wrapped in try/except
        because an exception raised inside an SDK callback can kill the
        receive thread and take the whole conversation down with it."""
        try:
            if not matches_transfer_phrase(text, self.settings.transfer_trigger_phrases_normalized):
                return
            if not self._claim_transfer():
                self.logger.info("transfer phrase seen again, already in progress; ignoring")
                return
            self.logger.info(
                f"transfer phrase detected; queueing internal transfer "
                f"(call_id={self.call_id} tracking_id={self.tracking_id})"
            )
            self._transfer_requests.put(_TransferRequest(extension=None, source="agent_phrase"))
        except Exception:
            self.logger.exception("transfer trigger check failed")

    def _handle_transfer_tool_call(self, parameters: dict) -> dict:
        """ElevenLabs client tool "transfer_call" -- legacy entry point,
        kept as a fallback (D7): the primary trigger is now the agent's own
        transcript phrase (see _maybe_trigger_transfer). Runs on
        ClientTools' own executor thread, not the SIP thread."""
        return self._request_transfer(source="client_tool")

    def _request_transfer(self, *, source: str) -> dict:
        """Shared body for every transfer entry point (agent phrase, client
        tool, HTTP fallback). Claims the one-shot guard, enqueues a request
        for the SIP thread -- which alone may choose the target and send
        SIP, so a transfer behaves identically no matter which entry point
        triggered it -- and blocks for the outcome. The dict returned
        here becomes the client_tool_result the agent sees (for the
        "agent_phrase" and "client_tool" sources) or the HTTP response body
        (for "http")."""
        if not self._claim_transfer():
            return {
                "success": False,
                "status": "already_requested",
                "message": "A transfer has already been requested for this call.",
            }

        request = _TransferRequest(extension=None, source=source)
        self._transfer_requests.put(request)

        # A little slack over transfer_wait_seconds so a normal timeout
        # inside the bridge loop's handling always wins the race and
        # produces a proper result dict instead of this fallback firing
        # first.
        if not request.result_event.wait(timeout=self.settings.transfer_wait_seconds + 5.0):
            self.logger.warning(f"transfer ({source}) never picked up by the bridge loop")
            with self._transfer_guard:
                self._transfer_started = False
            return {
                "success": False,
                "status": "error",
                "message": "Transfer could not be started; the call may already be ending.",
            }

        return request.result

    def _close_el_session(self) -> None:
        """Close the ElevenLabs websocket. Idempotent (also called from
        _cleanup()) and bounded -- end_session() joins the SDK's own
        threads and must never be allowed to stall the SIP thread mid-
        transfer (D3: only called once a transfer is confirmed successful,
        or right before the busy prompt plays)."""
        with self._status_lock:
            if self._el_session_closed:
                return
            self._el_session_closed = True

        conversation = self._conversation
        if conversation is None:
            return

        done = threading.Event()

        def _end():
            try:
                conversation.end_session()
            except Exception:
                pass
            finally:
                done.set()

        threading.Thread(target=_end, daemon=True, name=f"el-end-{self.call_id}").start()
        if not done.wait(timeout=self.settings.el_end_session_timeout_seconds):
            self.logger.warning("ElevenLabs end_session did not return within timeout, abandoning wait")

    def _wait_for_playout(
        self, *, quiet_seconds: float, timeout: float, wait_for_start: float = 0.0
    ) -> None:
        """Block until the RTP play queue has been empty, with no new TTS
        chunk having arrived, for `quiet_seconds`, or until `timeout`
        elapses. Runs on the SIP thread; the RTP send/recv loops are
        separate threads and keep flowing throughout -- this only waits, it
        does not pace anything itself. Used so a just-announced sentence
        (the transfer trigger phrase, or the busy prompt itself) actually
        finishes reaching the caller before the next thing happens. Bounded
        by `timeout` so a stuck TTS stream can never wedge the SIP thread.

        `wait_for_start` exists because of the ordering that motivates this
        whole method: on_agent_response fires when the LLM's TEXT arrives,
        which is BEFORE any audio for that sentence reaches output(). At
        that instant the previous turn's audio has long since drained, so
        `pending == 0` and `last_output_monotonic` is already seconds old --
        both exit conditions are satisfied and the wait returned
        immediately, which defeated its entire purpose and let the REFER go
        out before the caller heard a word of the announcement. When
        `wait_for_start` > 0 we first spend up to that long waiting for the
        sentence's audio to actually START (queue becomes non-empty, or a
        new chunk arrives), and only then begin measuring drain/quiet. If
        the audio never starts -- the agent produced text but no speech, or
        the session died -- we give up after `wait_for_start` and carry on
        rather than burning the whole `timeout`."""
        if self._rtp_interface is None:
            return
        poll_interval = 0.05
        deadline = time.monotonic() + timeout

        if wait_for_start > 0:
            baseline = self._rtp_interface.last_output_monotonic
            start_deadline = min(time.monotonic() + wait_for_start, deadline)
            started = False
            while time.monotonic() < start_deadline:
                if (
                    self._rtp_interface.playout_pending() > 0
                    or self._rtp_interface.last_output_monotonic != baseline
                ):
                    started = True
                    break
                time.sleep(poll_interval)
            if not started:
                self.logger.warning(
                    f"no TTS audio started within {wait_for_start:.2f}s of the agent's text; "
                    "not waiting for playout"
                )
                return

        while time.monotonic() < deadline:
            pending = self._rtp_interface.playout_pending()
            last_output = self._rtp_interface.last_output_monotonic
            quiet_for = (time.monotonic() - last_output) if last_output is not None else quiet_seconds
            if pending == 0 and quiet_for >= quiet_seconds:
                return
            time.sleep(poll_interval)

    def _play_busy_prompt_and_close(self) -> None:
        """Runs on the SIP thread, from _bridge()'s loop. Plays the "all
        lines are busy" static prompt to completion, then closes the
        ElevenLabs session. Returns either way -- the caller sets
        exit_reason = "transfer_unavailable" and ends the call with a BYE
        (D5), so the prompt is always fully transmitted before the BYE.

        ORDER IS LOAD-BEARING: the ElevenLabs session must be closed AFTER
        the prompt has played, not before. Conversation.end_session() calls
        stop() on our audio interface, which sets is_running=False and
        closes the RTP socket -- closing the session first left
        play_static_frames() with nothing to transmit and the caller heard
        silence. The agent can't talk over the prompt in the meantime
        because play_static_frames() latches _static_playback, which makes
        RtpAudioInterface.output() drop any TTS still arriving."""
        cfg = self.settings
        if not cfg.busy_prompt_enabled or self._busy_frames is None or self._rtp_interface is None:
            self.logger.warning(
                f"busy prompt not played: enabled={cfg.busy_prompt_enabled} "
                f"frames_loaded={self._busy_frames is not None} "
                f"rtp_interface={self._rtp_interface is not None} "
                f"(path={cfg.busy_prompt_audio_path!r}); hanging up without it"
            )
            self._close_el_session()
            return

        approx_prompt_seconds = len(self._busy_frames) * cfg.frame_ms / 1000
        self.logger.info(
            f"playing 'all lines busy' prompt to caller "
            f"({len(self._busy_frames)} frames, ~{approx_prompt_seconds:.2f}s)"
        )
        if not self._rtp_interface.play_static_frames(self._busy_frames):
            # play_static_frames has already logged the specific reason.
            self.logger.warning("busy prompt could not be queued; hanging up without playing it")
            self._close_el_session()
            return

        # Wait for the play queue to drain (quiet_seconds=0: we only care
        # that every frame has been handed to the sender), then hold for
        # the tail so those last frames are actually on the wire before the
        # BYE goes out.
        self._wait_for_playout(quiet_seconds=0.0, timeout=approx_prompt_seconds + 5.0)
        time.sleep(cfg.busy_prompt_tail_seconds)
        self.logger.info("'all lines busy' prompt finished playing, ending call")

        self._close_el_session()

    def _send_bye(self, *, dialog_kwargs: dict, remote_tag: str, cseq: int) -> int:
        """Send an in-dialog BYE as its own transaction; return the CSeq it
        used so the caller can keep counting from there.

        The increment is the whole point of this helper existing. RFC 3261
        §12.2.1.1: each new request within a dialog MUST carry a CSeq
        strictly greater than the previous one. The bridge loop's hangup
        path used to build its BYE with whatever `cseq` it happened to be
        holding -- the INVITE's on the normal path, and after a failed
        REFER, the REFER's own. Same number, different method: a strict peer
        answers 500 and the PBX-side dialog outlives our cleanup. Rare
        before, because a failed transfer was rare; this branch made an
        announced-but-failed transfer an ordinary occurrence, so it stopped
        being theoretical. Both BYE sites now go through here."""
        cseq += 1
        try:
            self._sock.sendall(
                sip.build_bye(
                    **dialog_kwargs,
                    branch=sip.new_branch(),  # F-14: BYE is its own transaction
                    cseq=cseq,
                    remote_tag=remote_tag,
                ).encode()
            )
        except Exception:
            self.logger.exception("failed to send BYE")
            return cseq
        self._await_bye_response(cseq)
        return cseq

    def _await_bye_response(self, bye_cseq: int) -> None:
        """Read the final response to the BYE we just sent, and say plainly
        in the log whether the PBX accepted it.

        Until this existed the BYE was fire-and-forget: sendall(), return,
        and _cleanup() closed the TCP socket a moment later. Nothing ever
        looked at the answer, so a REJECTED BYE -- 481 Call/Transaction Does
        Not Exist, 500, anything -- was indistinguishable from a clean
        hangup, and our leg could stay up on the PBX with no trace of why.
        That is the leading suspect for "the transferred call ended and the
        customer was left on a silent line": a PBX that held our leg for the
        transfer will retrieve the caller back onto it when the extension
        hangs up, and by then we have torn down RTP and gone.

        Waiting is also just correct UAC behaviour (RFC 3261 §15.1.1: the
        UAC considers the session ended on the final response, not on
        send). Bounded by BYE_RESPONSE_TIMEOUT_SECONDS so a silent PBX can
        never hold the call thread.
        """
        if self._stream is None:
            return
        deadline = time.monotonic() + self.settings.bye_response_timeout_seconds
        while time.monotonic() < deadline:
            frame = self._stream.read_message(max(deadline - time.monotonic(), 0.1))
            if frame.kind == sip.FrameKind.CLOSED:
                self.logger.warning("SIP connection closed before the BYE was answered")
                return
            if frame.kind == sip.FrameKind.TIMEOUT:
                continue

            msg = frame.text or ""
            parsed = sip.parse_status_line(msg)
            if parsed is None:
                # A request crossing our BYE on the wire. Method only -- the
                # request line carries the callee's number.
                self.logger.info(f"in-dialog {sip.parse_method(msg)} arrived while awaiting the BYE response")
                continue

            code, reason = parsed
            cseq_match = re.search(r"CSeq:\s*(\d+)\s+BYE", msg, re.IGNORECASE)
            if not (cseq_match and int(cseq_match.group(1)) == bye_cseq):
                continue  # a response to some earlier transaction
            if 200 <= code < 300:
                self.logger.info(f"BYE accepted ({code}); the PBX has released our leg")
            else:
                self.logger.warning(
                    f"BYE REJECTED ({code} {reason}) -- our leg may still be up on the PBX. "
                    "This is what leaves a caller on a silent line after a transferred call ends."
                )
            return

        self.logger.warning(
            f"no final response to our BYE within {self.settings.bye_response_timeout_seconds:.1f}s -- "
            "cannot confirm the PBX released our leg"
        )

    def _perform_transfer(
        self, request: "_TransferRequest", *, dialog_kwargs: dict, remote_tag: str, cseq: int
    ) -> tuple[int, str]:
        """Runs on the SIP thread, inside _bridge()'s loop. Sends the
        in-dialog REFER, then handles subsequent SIP traffic itself
        (matching the REFER's own CSeq, answering the transfer-progress
        NOTIFY(s)) until a final outcome or timeout, and reports it back to
        the waiting requester thread.

        Returns (next_cseq, outcome) where outcome is one of:
          "transferred" -- the caller is on the extension; our leg is done.
          "remote_bye"  -- the caller hung up mid-transfer. The dialog is
                           already torn down and 200 OK'd, so the bridge
                           loop must NOT send a BYE of its own.
          "failed"      -- the REFER was rejected or timed out. The call is
                           still up and the conversation continues."""
        cfg = self.settings
        cseq += 1
        refer_branch = sip.new_branch()
        self.logger.info(f"transferring call to extension {request.extension} via SIP REFER")

        try:
            self._sock.sendall(
                sip.build_refer(
                    **dialog_kwargs,
                    branch=refer_branch,
                    cseq=cseq,
                    refer_to_extension=request.extension,
                    remote_tag=remote_tag,
                ).encode()
            )
        except Exception as e:
            self._mark_transfer_failed()
            request.result = {
                "success": False,
                "status": "error",
                "message": f"Failed to send transfer request: {e}",
            }
            request.result_event.set()
            return cseq, "failed"

        deadline = time.monotonic() + cfg.transfer_wait_seconds
        refer_accepted = False
        # True once a NOTIFY reports the referred-to leg alerting (>= 180).
        # Distinguishes "the PBX BYEd our leg because the transfer is
        # completing" from "the caller hung up while waiting" -- see the
        # BYE arm below.
        refer_progressed = False
        outcome: Optional[str] = None  # "success" | "failed" | None (== timeout)

        while time.monotonic() < deadline:
            frame = self._stream.read_message(max(deadline - time.monotonic(), 0.1))
            if frame.kind == sip.FrameKind.CLOSED:
                outcome = "failed"
                break
            if frame.kind == sip.FrameKind.TIMEOUT:
                continue

            msg = frame.text or ""
            first_line = msg.splitlines()[0] if msg else ""

            if not refer_accepted:
                parsed = sip.parse_status_line(msg)
                if parsed is not None:
                    code, _ = parsed
                    cseq_match = re.search(r"CSeq:\s*(\d+)\s+REFER", msg, re.IGNORECASE)
                    if cseq_match and int(cseq_match.group(1)) == cseq:
                        if code < 200:
                            # Provisional. A PBX may well send "100 Trying"
                            # for an in-dialog REFER before the 202
                            # Accepted; that means the transaction is alive,
                            # NOT that it resolved. Treating any non-2xx as
                            # a rejection here aborted the transfer on the
                            # very first message such a PBX sent.
                            self.logger.info(f"REFER provisional response ({code}), still waiting")
                        elif 200 <= code < 300:
                            refer_accepted = True
                            self.logger.info(f"REFER accepted ({code}), waiting for transfer outcome")
                        else:
                            outcome = "failed"
                            self.logger.info(f"REFER rejected: {first_line}")
                            break
                    continue

            method = sip.parse_method(msg)
            if method == "NOTIFY" and "refer" in msg.lower():
                try:
                    self._sock.sendall(sip.build_ok_response(msg).encode())
                except Exception:
                    pass
                body = msg.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in msg else ""
                frag = sip.parse_status_line(body.strip())
                if frag is None:
                    continue
                frag_code, _ = frag
                if frag_code < 200:
                    # RFC 3515: the notifier relays the referred-to leg's
                    # provisional responses -- "100 Trying" first, then
                    # "180 Ringing" / "183 Session Progress" for as long as
                    # the extension is alerting. Only a final (>= 200)
                    # fragment resolves the transfer. Treating everything
                    # except exactly 100 as final meant every transfer to
                    # an extension that rings before a human picks up --
                    # i.e. the normal case -- was recorded as a failure.
                    if frag_code >= 180:
                        # The referred-to leg is genuinely alerting. This
                        # is the evidence the BYE arm below needs to tell a
                        # completing transfer from a caller hangup.
                        refer_progressed = True
                    self.logger.info(f"transfer progress NOTIFY ({frag_code}), still waiting")
                    continue
                outcome = "success" if 200 <= frag_code < 300 else "failed"
                break
            elif method == "BYE":
                try:
                    self._sock.sendall(sip.build_ok_response(msg).encode())
                except Exception:
                    pass
                if refer_progressed:
                    # The referred-to leg was alerting or better, so this
                    # BYE is the PBX tearing our leg down as it completes
                    # the transfer itself -- no BYE of our own is needed.
                    self.transferred_to = request.extension
                    self._close_el_session()  # D3: only now, transfer is confirmed
                    request.result = {
                        "success": True,
                        "status": "transferred",
                        "message": f"Call transferred to extension {request.extension}.",
                    }
                    request.result_event.set()
                    return cseq, "transferred"
                # Nothing ever indicated the referred-to leg progressed, so
                # the far likelier reading is that the CALLER hung up while
                # the transfer was pending -- they had just been told to
                # hold. Recording that as a success wrote Status=Transfer
                # for a call no human ever took.
                self.logger.info("caller hung up while the transfer was still pending")
                self._mark_transfer_failed()
                request.result = {
                    "success": False,
                    "status": "failed",
                    "message": "The caller hung up before the transfer completed.",
                }
                request.result_event.set()
                return cseq, "remote_bye"
            elif method in ("OPTIONS", "INFO", "UPDATE"):
                try:
                    self._sock.sendall(sip.build_ok_response(msg).encode())
                except Exception:
                    pass
                continue
            else:
                # Anything else in-dialog is still ignored, but no longer
                # SILENTLY. This window is where a PBX puts our leg on hold
                # for the transfer, and it does that with a re-INVITE --
                # which this loop does not answer, unlike the main bridge
                # loop, whose F-14B comment records that unanswered
                # re-INVITEs make a PBX tear calls down. Whether that is
                # happening here has never been observable; now it is.
                # Method/status only: the request line carries the callee's
                # number.
                status = sip.parse_status_line(msg)
                if status is not None:
                    self.logger.info(f"transfer wait: unhandled response {status[0]}")
                else:
                    self.logger.info(f"transfer wait: unhandled in-dialog request {method}")

        if outcome is None:
            outcome = "failed"
            self.logger.info(f"transfer to extension {request.extension} timed out")

        if outcome == "success":
            self.logger.info(f"call transferred to extension {request.extension}, ending our leg")
            self.transferred_to = request.extension
            self._close_el_session()  # D3: only now, transfer is confirmed
            cseq = self._send_bye(dialog_kwargs=dialog_kwargs, remote_tag=remote_tag, cseq=cseq)
            request.result = {
                "success": True,
                "status": "transferred",
                "message": f"Call transferred to extension {request.extension}.",
            }
            request.result_event.set()
            return cseq, "transferred"

        self._mark_transfer_failed()
        request.result = {
            "success": False,
            "status": "failed",
            "message": f"Transfer to extension {request.extension} failed.",
        }
        request.result_event.set()
        return cseq, "failed"

    @staticmethod
    def _parse_sdp_media_address(sdp_text: str) -> Optional[str]:
        """F-12: prefer a media-level c= line (appears after m=audio) over
        the session-level one -- the last c= line in the body is the best
        approximation of "most specific" without a full SDP parser."""
        matches = re.findall(r"^c=IN IP4 (\S+)", sdp_text, re.MULTILINE)
        return matches[-1] if matches else None

    def _bridge(
        self,
        *,
        local_port: int,
        dialog_kwargs: dict,
        remote_tag: str,
        media_ip: str,
        next_cseq: int,
    ) -> str:
        cfg = self.settings

        def _on_turn_latency(payload: dict) -> None:
            self.last_turn_latency = payload

        self._rtp_interface = RtpAudioInterface(
            self.local_rtp_port,
            media_ip,  # F-12: SDP answer's media address, not the raw PBX IP
            self.remote_rtp_port,
            call_id=self.call_id,
            frame_ms=cfg.frame_ms,
            frame_bytes=cfg.frame_bytes,
            log_dir=cfg.log_dir,
            on_turn_latency=_on_turn_latency,
            antialias=cfg.audio_antialias,
            antialias_cutoff_hz=cfg.audio_antialias_cutoff_hz,
            antiimage=cfg.audio_antiimage,
            antiimage_cutoff_hz=cfg.audio_antiimage_cutoff_hz,
            logger=self.logger,
        )

        # F-09: start RTP transmission (silence frames) immediately, before
        # the ElevenLabs SDK exists at all -- otherwise there's dead air
        # between ACK and start_session() returning, and many SBCs tear
        # down the call on a media timeout during exactly that gap.
        media_start = time.monotonic()
        self._rtp_interface.start(lambda _pcm: None)

        client = ElevenLabs(api_key=cfg.elevenlabs_api_key)
        # The ONE consumer of the corrected names -- everything else (webhook,
        # DB, API responses) uses self.dynamic_variables as the client sent it.
        config = ConversationInitiationData(dynamic_variables=self.speech_dynamic_variables)

        # Registers "transfer_call" as an ElevenLabs client tool -- kept as
        # a fallback entry point (D7). The primary transfer trigger is now
        # the agent's own transcript phrase, detected below in
        # on_agent_response -> _maybe_trigger_transfer; this tool no longer
        # needs to be configured in the ElevenLabs dashboard for transfers
        # to work, though it's harmless to leave registered there. Runs on
        # ClientTools' own executor thread, so the actual SIP REFER is
        # handed off to the _bridge() loop below via
        # self._transfer_requests -- see _handle_transfer_tool_call /
        # _perform_transfer.
        client_tools = ClientTools()
        client_tools.register("transfer_call", self._handle_transfer_tool_call)

        def on_agent_response(t):
            self._rtp_interface.record_llm_first_text()
            if cfg.log_transcripts:
                # Deliberately .info(), not .debug(): visibility is
                # controlled solely by the LOG_TRANSCRIPTS flag so it
                # doesn't require cranking the whole app to DEBUG (which
                # would also surface every other noisy debug line).
                self.logger.info(f"AI: {t}")
            # Primary transfer trigger: the agent's own transcript, not an
            # ElevenLabs tool call or webhook POST. This closure is bound to
            # `self` (one CallSession per call), which is what keeps this
            # per-call -- see _maybe_trigger_transfer's docstring.
            self._maybe_trigger_transfer(t)

        def on_user_transcript(t):
            self._rtp_interface.record_stt_complete()
            if cfg.log_transcripts:
                self.logger.info(f"Caller: {t}")

        self._conversation = Conversation(
            client=client,
            agent_id=cfg.agent_id,
            requires_auth=bool(cfg.elevenlabs_api_key),
            audio_interface=self._rtp_interface,
            config=config,
            client_tools=client_tools,
            callback_agent_response=on_agent_response,
            callback_user_transcript=on_user_transcript,
        )

        # F-02: watchdog around start_session() -- under network lag to
        # ElevenLabs (the reported trigger) this call could hang forever.
        start_error: list[Exception] = []
        started = threading.Event()

        def _start():
            try:
                self._conversation.start_session()
            except Exception as e:
                start_error.append(e)
            finally:
                started.set()

        threading.Thread(target=_start, daemon=True, name=f"el-start-{self.call_id}").start()
        if not started.wait(timeout=cfg.el_start_timeout_seconds):
            raise _CallAborted("ElevenLabs start_session timed out", reason="el_start_timeout")
        if start_error:
            raise _CallAborted(f"ElevenLabs start_session failed: {start_error[0]}", reason="el_start_failed")

        media_start_gap_ms = round((time.monotonic() - media_start) * 1000)
        self.logger.info(f"media_start_gap_ms={media_start_gap_ms}")

        self._set_status(CallStatus.CONNECTED)
        self.connected_at = time.time()
        self.logger.info("bridge running, audio actively routed to PBX")

        session_ended = threading.Event()

        def wait_for_end():
            while True:
                try:
                    conversation_id = self._conversation.wait_for_session_end()
                    # F-24: this remains a fallback writer for agent-ended
                    # calls, but must not overwrite an id captured in-loop.
                    with self._status_lock:
                        if not self.conversation_id and conversation_id:
                            self.conversation_id = str(conversation_id)
                    break
                except Exception:
                    self.logger.exception("el-wait thread error, retrying")
                    time.sleep(1.0)
                    if self._hangup_requested.is_set() or session_ended.is_set():
                        break
            session_ended.set()

        threading.Thread(
            target=wait_for_end, daemon=True, name=f"el-wait-{self.call_id}"
        ).start()

        call_deadline = time.monotonic() + cfg.max_call_seconds
        exit_reason = "unknown"
        cseq = next_cseq

        while not session_ended.is_set() and not self._hangup_requested.is_set():
            self._capture_conversation_id()

            if time.monotonic() >= call_deadline:
                exit_reason = "max_duration"
                break

            last_rx = self._rtp_interface.last_rx_monotonic
            if last_rx is not None and (time.monotonic() - last_rx) > cfg.rtp_inactivity_seconds:
                exit_reason = "rtp_timeout"
                break

            try:
                transfer_request = self._transfer_requests.get_nowait()
            except queue.Empty:
                transfer_request = None

            if transfer_request is not None:
                if transfer_request.extension is None:
                    # Every entry point (agent phrase, client tool, HTTP)
                    # arrives here with no target yet -- the choice happens
                    # on the SIP thread, in one place, so a transfer
                    # behaves identically no matter what triggered it.
                    # First let whatever was just said (the trigger phrase
                    # itself) finish reaching the caller.
                    self._wait_for_playout(
                        quiet_seconds=cfg.transfer_playout_quiet_seconds,
                        timeout=cfg.transfer_playout_timeout_seconds,
                        wait_for_start=cfg.transfer_playout_start_timeout_seconds,
                    )
                    # The target is a PBX queue: it holds callers, it does
                    # not fill up, and this service has no visibility into
                    # its real state anyway. So the REFER always goes out
                    # and the PBX's own response decides the outcome -- see
                    # transfer_targets.py for the model this replaced and
                    # the failure it caused.
                    target = (
                        self._transfer_targets.next_target()
                        if self._transfer_targets is not None
                        else None
                    )
                    if target is None:
                        # Nothing configured at all. Not a busy queue -- a
                        # misconfigured service -- but the caller has just
                        # been told they are being transferred, so they get
                        # the prompt rather than silence and a BYE.
                        self.logger.error(
                            "transfer requested but no transfer targets are configured "
                            "(TRANSFER_EXTENSIONS is empty)"
                        )
                        transfer_request.result = {
                            "success": False,
                            "status": "busy",
                            "message": "No transfer target is configured.",
                        }
                        transfer_request.result_event.set()
                        self._mark_transfer_failed()
                        self._play_busy_prompt_and_close()
                        exit_reason = "transfer_unavailable"
                        break
                    transfer_request.extension = target

                cseq, transfer_outcome = self._perform_transfer(
                    transfer_request, dialog_kwargs=dialog_kwargs, remote_tag=remote_tag, cseq=cseq
                )
                if transfer_outcome == "transferred":
                    exit_reason = "transferred"
                    break
                if transfer_outcome == "remote_bye":
                    # The caller hung up mid-transfer. _perform_transfer
                    # already 200 OK'd their BYE, so fall out of the loop
                    # WITHOUT sending one of our own -- "remote_bye" is
                    # deliberately absent from the send-BYE set below. Not
                    # breaking here would spin the loop until max_duration
                    # on a dialog that no longer exists.
                    exit_reason = "remote_bye"
                    break
                # Failed (REFER rejected/timed out) -- the call continues,
                # so allow a legitimate retry (e.g. the agent says the
                # trigger phrase again).
                with self._transfer_guard:
                    self._transfer_started = False
                continue

            frame = self._stream.read_message(cfg.sip_bridge_poll_timeout)
            if frame.kind == sip.FrameKind.CLOSED:
                self.logger.info("SIP pipe disconnected abruptly")
                exit_reason = "sip_disconnect"
                break
            if frame.kind == sip.FrameKind.TIMEOUT:
                continue

            sip_msg = frame.text or ""
            method = sip.parse_method(sip_msg)
            if method == "BYE":
                self.logger.info("remote party closed the line (BYE received)")
                try:
                    self._sock.sendall(sip.build_ok_response(sip_msg).encode())
                except Exception:
                    pass
                exit_reason = "remote_bye"
                break
            elif method in ("OPTIONS", "INFO", "NOTIFY", "UPDATE"):
                # F-14B: these were previously silently ignored. Unanswered
                # OPTIONS/keepalives and session-timer refreshes
                # (NOTIFY/UPDATE) can cause the PBX to tear the call down,
                # which looked like "calls randomly drop after N minutes."
                self.logger.info(f"answering in-dialog {method}")
                try:
                    self._sock.sendall(sip.build_ok_response(sip_msg).encode())
                except Exception:
                    pass
                continue
            elif method == "INVITE":
                # Re-INVITE (commonly a session-timer refresh). Answer with
                # the current SDP so the session doesn't expire.
                self.logger.info("answering re-INVITE with current SDP")
                refresh_sdp = sip.build_sdp(cfg.local_ip, self.local_rtp_port)
                try:
                    self._sock.sendall(
                        sip.build_ok_response(sip_msg, sdp=refresh_sdp, to_tag=self._from_tag).encode()
                    )
                except Exception:
                    pass
                continue
            # Unrecognized in-dialog traffic: log and keep going rather than
            # silently dropping it forever (old behavior) or crashing.
            self.logger.info(f"ignoring unrecognized in-dialog message: {sip_msg.splitlines()[0] if sip_msg else ''}")

        if exit_reason == "unknown" and session_ended.is_set():
            exit_reason = "agent_ended"

        if self._hangup_requested.is_set() and exit_reason == "unknown":
            exit_reason = "local_hangup"

        # Reasons whose dialog is ALREADY torn down. Checked first and
        # unconditionally, because the `_hangup_requested` disjunct below
        # otherwise overrides the exclusion: an API hangup landing between
        # the loop exiting and this line made the condition true regardless
        # of exit_reason, and out went a stray BYE on a dead dialog.
        #   "transferred" -- _perform_transfer sent our BYE, or the PBX did.
        #   "remote_bye"  -- the far end sent the BYE; we answered 200 OK.
        dialog_already_closed = exit_reason in ("transferred", "remote_bye")

        if not dialog_already_closed and (
            self._hangup_requested.is_set()
            or exit_reason in (
                "max_duration", "rtp_timeout", "local_hangup", "agent_ended", "transfer_unavailable",
            )
        ):
            self.logger.info(f"ending call (reason={exit_reason}), sending BYE")
            # Return value intentionally dropped: nothing reads cseq after this.
            self._send_bye(dialog_kwargs=dialog_kwargs, remote_tag=remote_tag, cseq=cseq)

        status = (
            CallStatus.COMPLETED
            if exit_reason in (
                "remote_bye", "agent_ended", "local_hangup", "transferred", "transfer_unavailable",
            )
            else CallStatus.FAILED
        )
        self._set_status(status)
        return exit_reason

    # ------------------------------------------------------------------
    def _cleanup(self) -> None:
        # Last-chance capture before closing and dropping the live SDK object:
        # metadata may have arrived during the bridge loop's final tick.
        self._capture_conversation_id()
        self.logger.info("cleaning up call resources")
        try:
            # Idempotent + bounded (see _close_el_session): a transfer may
            # already have closed this; if not, this is the normal-path
            # close for every other exit reason.
            self._close_el_session()
        except Exception:
            pass
        try:
            if self._rtp_interface is not None:
                self._rtp_interface.stop()
        except Exception:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass

        # F-06: this call's record can sit in CallManager's registry for up
        # to CALL_RETENTION_SECONDS after ending. Without this, it keeps
        # strong references to the ElevenLabs SDK object (with its own
        # threads/buffers), the RTP interface (including any still-queued
        # TTS frames), and the raw socket -- all needed state for the API
        # (last_turn_latency, conversation_id) has already been captured
        # into plain fields on `self`, so it's safe to drop the rest.
        self._conversation = None
        self._rtp_interface = None
        self._sock = None
        self._stream = None


class _CallAborted(Exception):
    def __init__(self, message: str = "", cancelled: bool = False, reason: str = "failed"):
        super().__init__(message)
        self.cancelled = cancelled
        self.reason = reason


class _CallRejected(Exception):
    def __init__(self, message: str = "", reason: str = "rejected"):
        super().__init__(message)
        self.reason = reason
