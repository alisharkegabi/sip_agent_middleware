"""
Unit tests for add_call_result.AddCallResultSender -- delivery of a terminal
call outcome to Tamweely's AddCallResultByTrackingId endpoint.

Everything here runs against a fake `requests.post`; no socket is opened and
no real endpoint is contacted. Retries are driven by calling _attempt directly
rather than by waiting on the real threading.Timer, so the suite stays fast and
deterministic -- the scheduling itself is asserted separately by capturing the
timers instead of starting them.

The response contract under test comes from AddCallResult_API_Guide §7-§8: the
endpoint returns HTTP 200 for BOTH success and failure, so the body is what
decides.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import add_call_result  # noqa: E402
from add_call_result import AddCallResultSender  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures / doubles
# --------------------------------------------------------------------------
class _FakeSettings:
    """Only the fields AddCallResultSender reads."""

    def __init__(self, **overrides):
        self.add_call_result_base_url = "https://tamweely.example"
        self.add_call_result_api_key = "test-key-do-not-log"
        self.add_call_result_timeout_seconds = 10.0
        self.add_call_result_max_retries = 5
        self.add_call_result_max_retry_age_seconds = 3600.0
        self.add_call_result_dead_letter_path = ""
        for k, v in overrides.items():
            setattr(self, f"add_call_result_{k}", v)


class _FakeResponse:
    def __init__(self, status_code=200, body=None, raw=None):
        self.status_code = status_code
        self._body = body
        self._raw = raw

    @property
    def text(self):
        """requests.Response.text always exists -- the sender logs it on
        every attempt, so a fake without it would exercise nothing but the
        defensive except in _body_for_log()."""
        if self._raw is not None:
            return self._raw
        if self._body is None:
            return ""
        return json.dumps(self._body)

    def json(self):
        if self._raw is not None:
            raise ValueError("not JSON")
        return self._body


GUID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


@pytest.fixture
def sender(tmp_path):
    s = AddCallResultSender(
        _FakeSettings(dead_letter_path=str(tmp_path / "dead.jsonl"))
    )
    yield s
    s.shutdown()


def _record(tracking_id=GUID, status="Timeout", first_attempt_at=None):
    return {
        "tracking_id": tracking_id,
        "status": status,
        "_first_attempt_at": first_attempt_at if first_attempt_at is not None else time.time(),
    }


def _dead_letters(sender):
    path = sender._dead_letter_path
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _patch_post(monkeypatch, response=None, exc=None):
    """Replace requests.post; return the list that captures its calls."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(add_call_result.requests, "post", fake_post)
    return calls


def _capture_retries(monkeypatch, sender):
    """Stop retries actually firing; record that one was scheduled."""
    scheduled = []
    monkeypatch.setattr(
        sender, "_schedule_retry", lambda record, attempt: scheduled.append((record, attempt))
    )
    return scheduled



def _pending_entry(sender):
    """The single pending retry, as (entry_id, timer, record, attempt)."""
    assert len(sender._timers) == 1, sender._timers
    entry_id, (timer, record, attempt) = next(iter(sender._timers.items()))
    return entry_id, timer, record, attempt


# --------------------------------------------------------------------------
# Request construction
# --------------------------------------------------------------------------
class TestRequestConstruction:
    def test_url_path_and_query(self, sender, monkeypatch):
        calls = _patch_post(monkeypatch, _FakeResponse(200, {"isSuccess": True}))
        sender._attempt(_record(status="Cancelled"), 1)
        assert len(calls) == 1
        assert calls[0]["url"] == (
            f"https://tamweely.example/v1/data/add-call-result/by-tracking-id/{GUID}"
        )
        # §4: status is a query-string parameter, bound by name.
        assert calls[0]["params"] == {"status": "Cancelled"}

    def test_api_key_and_content_type_headers(self, sender, monkeypatch):
        calls = _patch_post(monkeypatch, _FakeResponse(200, {"isSuccess": True}))
        sender._attempt(_record(), 1)
        headers = calls[0]["headers"]
        assert headers["X-Api-Key"] == "test-key-do-not-log"
        assert headers["Content-Type"] == "application/json"

    def test_timeout_is_passed(self, sender, monkeypatch):
        calls = _patch_post(monkeypatch, _FakeResponse(200, {"isSuccess": True}))
        sender._attempt(_record(), 1)
        assert calls[0]["timeout"] == 10.0

    def test_tracking_id_is_percent_encoded(self, sender, monkeypatch):
        """main.py only checks tracking_id is non-empty, so a value containing
        a path separator or query character must not escape its path segment."""
        calls = _patch_post(monkeypatch, _FakeResponse(200, {"isSuccess": True}))
        sender._attempt(_record(tracking_id="../../evil?x=1"), 1)
        assert calls[0]["url"].endswith("/by-tracking-id/..%2F..%2Fevil%3Fx%3D1")

    def test_base_url_trailing_slash_does_not_double(self, monkeypatch):
        s = AddCallResultSender(_FakeSettings(base_url="https://tamweely.example/"))
        calls = _patch_post(monkeypatch, _FakeResponse(200, {"isSuccess": True}))
        s._attempt(_record(), 1)
        assert "//v1/data" not in calls[0]["url"]
        s.shutdown()


# --------------------------------------------------------------------------
# Response handling -- one test per row of the table in ADD_CALL_RESULT.md
# --------------------------------------------------------------------------
class TestResponseLogging:
    """The endpoint returns HTTP 200 for both success and failure, so the body
    is the only evidence of which one happened. It is logged on every attempt,
    whatever the outcome."""

    def test_body_is_logged_on_success(self, sender, monkeypatch, caplog):
        _patch_post(monkeypatch, _FakeResponse(200, {"isSuccess": True, "data": True}))
        with caplog.at_level("INFO"):
            sender._attempt(_record(), 1)
        assert '"isSuccess": true' in caplog.text
        assert "HTTP 200" in caplog.text

    def test_body_is_logged_when_not_accepted(self, sender, monkeypatch, caplog):
        _patch_post(
            monkeypatch,
            _FakeResponse(200, {"isSuccess": False, "errorMessage": "No BatchCallDetail found"}),
        )
        _capture_retries(monkeypatch, sender)
        with caplog.at_level("INFO"):
            sender._attempt(_record(), 1)
        assert "No BatchCallDetail found" in caplog.text

    def test_non_json_body_is_logged(self, sender, monkeypatch, caplog):
        """The case the old code was blindest to: a proxy's HTML error page
        retried five times with nothing in the log saying what it was."""
        _patch_post(monkeypatch, _FakeResponse(200, raw="<html>login required</html>"))
        _capture_retries(monkeypatch, sender)
        with caplog.at_level("INFO"):
            sender._attempt(_record(), 1)
        assert "login required" in caplog.text

    def test_oversized_body_is_truncated(self, sender, monkeypatch, caplog):
        _patch_post(monkeypatch, _FakeResponse(200, raw="x" * 50_000))
        _capture_retries(monkeypatch, sender)
        with caplog.at_level("INFO"):
            sender._attempt(_record(), 1)
        assert "truncated, 50000 chars" in caplog.text
        assert len(caplog.text) < 5_000

    def test_body_is_logged_on_error_status(self, sender, monkeypatch, caplog):
        _patch_post(monkeypatch, _FakeResponse(500, raw="upstream exploded"))
        _capture_retries(monkeypatch, sender)
        with caplog.at_level("INFO"):
            sender._attempt(_record(), 1)
        assert "HTTP 500" in caplog.text
        assert "upstream exploded" in caplog.text


class TestResponseFieldCasing:
    """The guide documents camelCase; the deployed endpoint answers in
    PascalCase. Both must read the same, because which one arrives is a
    deployment detail on their side."""

    # Copied verbatim from service.log, 2026-08-23 18:30:35.
    LIVE_SUCCESS_BODY = {
        "IsSuccess": True,
        "Data": True,
        "ErrorMessage": None,
        "ValidationErrors": [],
        "Message": None,
    }

    def test_pascal_case_success_does_not_retry(self, sender, monkeypatch):
        """The regression: this exact body was retried 5x and dead-lettered
        as permanently failed, after Tamweely had already accepted it."""
        _patch_post(monkeypatch, _FakeResponse(200, self.LIVE_SUCCESS_BODY))
        scheduled = _capture_retries(monkeypatch, sender)
        sender._attempt(_record(), 1)
        assert scheduled == []
        assert _dead_letters(sender) == []

    def test_pascal_case_failure_still_retries(self, sender, monkeypatch):
        _patch_post(
            monkeypatch,
            _FakeResponse(200, {"IsSuccess": False, "ErrorMessage": "No BatchCallDetail found"}),
        )
        scheduled = _capture_retries(monkeypatch, sender)
        sender._attempt(_record(), 1)
        assert len(scheduled) == 1

    def test_pascal_case_validation_errors_are_permanent(self, sender, monkeypatch):
        _patch_post(
            monkeypatch,
            _FakeResponse(
                200,
                {
                    "IsSuccess": False,
                    "ErrorMessage": "Validation failed",
                    "ValidationErrors": ["trackingId must be a GUID"],
                },
            ),
        )
        scheduled = _capture_retries(monkeypatch, sender)
        sender._attempt(_record(), 1)
        assert scheduled == []
        assert _dead_letters(sender)[0]["_reason"] == "validation_error"

    def test_message_field_is_used_when_error_message_is_absent(
        self, sender, monkeypatch, caplog
    ):
        _patch_post(monkeypatch, _FakeResponse(200, {"IsSuccess": False, "Message": "row locked"}))
        _capture_retries(monkeypatch, sender)
        with caplog.at_level("INFO"):
            sender._attempt(_record(), 1)
        assert "row locked" in caplog.text

    def test_truthy_non_true_is_still_not_success(self, sender, monkeypatch):
        """Case-insensitivity must not weaken the `is True` check."""
        for value in ("true", 1, "True", [1]):
            _patch_post(monkeypatch, _FakeResponse(200, {"IsSuccess": value}))
            scheduled = _capture_retries(monkeypatch, sender)
            sender._attempt(_record(), 1)
            assert len(scheduled) == 1, f"{value!r} must not read as success"

    def test_non_object_body_is_not_success(self, sender, monkeypatch):
        for body in (True, [{"IsSuccess": True}], "IsSuccess"):
            _patch_post(monkeypatch, _FakeResponse(200, body))
            scheduled = _capture_retries(monkeypatch, sender)
            sender._attempt(_record(), 1)
            assert len(scheduled) == 1, f"{body!r} must not read as success"


class TestResponseHandling:
    def test_success_does_not_retry(self, sender, monkeypatch):
        _patch_post(monkeypatch, _FakeResponse(200, {"isSuccess": True, "data": True}))
        scheduled = _capture_retries(monkeypatch, sender)
        sender._attempt(_record(), 1)
        assert scheduled == []
        assert _dead_letters(sender) == []

    def test_is_success_false_retries(self, sender, monkeypatch):
        """'No BatchCallDetail found' is the row-arrives-late race, not a
        permanent failure -- see CALL_STATUS_TRACKING.md."""
        _patch_post(
            monkeypatch,
            _FakeResponse(
                200,
                {
                    "isSuccess": False,
                    "errorMessage": f"No BatchCallDetail found for TrackingId: {GUID}",
                    "validationErrors": [],
                },
            ),
        )
        scheduled = _capture_retries(monkeypatch, sender)
        sender._attempt(_record(), 1)
        assert len(scheduled) == 1

    def test_validation_errors_are_permanent(self, sender, monkeypatch):
        """A malformed request rebuilds identically on every retry, and each
        attempt re-forces FinalOutcome on their side. Dead-letter at once."""
        _patch_post(
            monkeypatch,
            _FakeResponse(
                200,
                {
                    "isSuccess": False,
                    "errorMessage": "Validation failed",
                    "validationErrors": ["trackingId must be a GUID"],
                },
            ),
        )
        scheduled = _capture_retries(monkeypatch, sender)
        sender._attempt(_record(), 1)
        assert scheduled == []
        assert _dead_letters(sender)[0]["_reason"] == "validation_error"

    @pytest.mark.parametrize("body", [
        {"isSuccess": "false"},   # string, not bool
        {"isSuccess": None},
        {"isSuccess": 1},         # truthy but not True
        {},                       # field absent entirely
        ["not", "a", "dict"],
    ])
    def test_only_boolean_true_counts_as_success(self, sender, monkeypatch, body):
        _patch_post(monkeypatch, _FakeResponse(200, body))
        scheduled = _capture_retries(monkeypatch, sender)
        sender._attempt(_record(), 1)
        assert len(scheduled) == 1, f"{body!r} must not be read as success"

    def test_non_json_200_retries(self, sender, monkeypatch):
        _patch_post(monkeypatch, _FakeResponse(200, raw="<html>gateway</html>"))
        scheduled = _capture_retries(monkeypatch, sender)
        sender._attempt(_record(), 1)
        assert len(scheduled) == 1

    def test_401_does_not_retry_and_dead_letters(self, sender, monkeypatch):
        _patch_post(monkeypatch, _FakeResponse(401, {}))
        scheduled = _capture_retries(monkeypatch, sender)
        sender._attempt(_record(), 1)
        assert scheduled == []
        assert _dead_letters(sender)[0]["_reason"] == "unauthorized"

    def test_500_retries(self, sender, monkeypatch):
        _patch_post(monkeypatch, _FakeResponse(500, {}))
        scheduled = _capture_retries(monkeypatch, sender)
        sender._attempt(_record(), 1)
        assert len(scheduled) == 1

    @pytest.mark.parametrize("code", [408, 409, 425, 429])
    def test_transient_4xx_retries(self, sender, monkeypatch, code):
        _patch_post(monkeypatch, _FakeResponse(code, {}))
        scheduled = _capture_retries(monkeypatch, sender)
        sender._attempt(_record(), 1)
        assert len(scheduled) == 1

    @pytest.mark.parametrize("code", [400, 403, 404])
    def test_permanent_4xx_dead_letters(self, sender, monkeypatch, code):
        _patch_post(monkeypatch, _FakeResponse(code, {}))
        scheduled = _capture_retries(monkeypatch, sender)
        sender._attempt(_record(), 1)
        assert scheduled == []
        assert _dead_letters(sender)[0]["_reason"] == f"http_{code}"

    def test_transport_error_retries(self, sender, monkeypatch):
        _patch_post(monkeypatch, exc=requests.ConnectionError("refused"))
        scheduled = _capture_retries(monkeypatch, sender)
        sender._attempt(_record(), 1)
        assert len(scheduled) == 1

    def test_timeout_retries(self, sender, monkeypatch):
        _patch_post(monkeypatch, exc=requests.Timeout("too slow"))
        scheduled = _capture_retries(monkeypatch, sender)
        sender._attempt(_record(), 1)
        assert len(scheduled) == 1


# --------------------------------------------------------------------------
# Retry budget
# --------------------------------------------------------------------------
class TestRetryBudget:
    def test_gives_up_at_max_attempts(self, sender):
        """max_retries counts TOTAL attempts: 5 means the 5th is the last."""
        sender._schedule_retry(_record(), 5)
        assert _dead_letters(sender)[0]["_reason"] == "retries_exhausted"

    def test_gives_up_past_max_age(self, sender):
        old = time.time() - 7200  # older than the 3600 s budget
        sender._schedule_retry(_record(first_attempt_at=old), 1)
        assert _dead_letters(sender)[0]["_reason"] == "retries_exhausted"

    def test_schedules_a_live_timer_within_budget(self, sender):
        sender._schedule_retry(_record(), 1)
        assert len(sender._timers) == 1
        timer, record, next_attempt = sender._timers[0]
        assert timer.is_alive()
        # The record rides along so shutdown() can dead-letter what it cancels.
        assert record["tracking_id"] == GUID
        assert next_attempt == 2
        assert _dead_letters(sender) == []

    def test_entry_is_removed_when_its_timer_fires(self, sender, monkeypatch):
        """Bookkeeping must not grow forever in a long-running service. Each
        entry now removes ITSELF when its timer fires, which is also what
        makes shutdown able to tell fired from pending."""
        monkeypatch.setattr(sender._executor, "submit", lambda fn, *a: None)
        sender._schedule_retry(_record(), 1)
        entry_id, timer, record, attempt = _pending_entry(sender)
        timer.cancel()  # stop the real one; drive the callback by hand
        sender._submit_retry(entry_id, record, attempt)
        assert sender._timers == {}

    def test_gives_up_when_the_next_backoff_would_outlast_the_budget(self, tmp_path):
        """The age check used to run BEFORE the wait, so a record just inside
        the budget could schedule an attempt ~37 s beyond it."""
        s = AddCallResultSender(_FakeSettings(
            max_retry_age_seconds=2.0,
            dead_letter_path=str(tmp_path / "age.jsonl"),
        ))
        # 1.5 s old, and attempt 1 backs off ~1 s -> 2.5 s > the 2.0 s budget.
        s._schedule_retry(_record(first_attempt_at=time.time() - 1.5), 1)
        assert s._timers == {}
        assert _dead_letters(s)[0]["_reason"] == "retries_exhausted"
        s.shutdown()


# --------------------------------------------------------------------------
# Configuration gating
# --------------------------------------------------------------------------
class TestDisabled:
    @pytest.mark.parametrize("overrides", [
        {"base_url": ""},
        {"api_key": ""},
    ])
    def test_missing_config_makes_no_request(self, monkeypatch, overrides):
        """There is no on/off flag -- blanking the URL or the key IS the
        off-switch, so each alone must fully disable the push."""
        s = AddCallResultSender(_FakeSettings(**overrides))
        calls = _patch_post(monkeypatch, _FakeResponse(200, {"isSuccess": True}))
        s.push_async(GUID, "Timeout")
        assert s.enabled is False
        assert calls == []
        s.shutdown()

    def test_empty_tracking_id_makes_no_request(self, sender, monkeypatch):
        calls = _patch_post(monkeypatch, _FakeResponse(200, {"isSuccess": True}))
        sender.push_async("", "Timeout")
        assert calls == []


# --------------------------------------------------------------------------
# Shutdown
# --------------------------------------------------------------------------
class TestShutdown:
    def test_push_after_shutdown_is_dead_lettered_not_sent(self, sender, monkeypatch):
        """A call worker outliving shutdown_grace_seconds still reaches
        _record_call_ended. Its result must be recorded, not dropped."""
        calls = _patch_post(monkeypatch, _FakeResponse(200, {"isSuccess": True}))
        sender.shutdown()
        sender.push_async(GUID, "Timeout")
        assert calls == []
        assert _dead_letters(sender)[0]["_reason"] == "shutting_down"

    def test_retry_after_shutdown_is_dead_lettered(self, sender):
        sender.shutdown()
        sender._schedule_retry(_record(), 1)
        assert _dead_letters(sender)[0]["_reason"] == "shutting_down"
        assert sender._timers == {}

    def test_shutdown_cancels_pending_timers(self, sender):
        sender._schedule_retry(_record(), 1)
        timer = sender._timers[0][0]
        sender.shutdown()
        assert timer.finished.is_set()  # cancelled

    def test_shutdown_dead_letters_the_retries_it_cancels(self, sender):
        """Cancelling a Timer throws away the retry it was holding. Without
        this the outcome is neither delivered nor recoverable -- which is
        exactly what the dead-letter file exists to prevent."""
        sender._schedule_retry(_record(tracking_id=GUID, status="Cancelled"), 1)
        assert _dead_letters(sender) == []
        sender.shutdown()
        rows = _dead_letters(sender)
        assert len(rows) == 1
        assert rows[0]["tracking_id"] == GUID
        assert rows[0]["status"] == "Cancelled"
        assert rows[0]["_reason"] == "shutdown_cancelled_retry"

    def test_shutdown_with_no_pending_retries_writes_nothing(self, sender):
        sender.shutdown()
        assert _dead_letters(sender) == []

    def test_fired_retry_is_not_dead_lettered_by_a_later_shutdown(self, sender, monkeypatch):
        """The interleaving that a Timer.is_alive() check gets WRONG: a Timer
        is a Thread and stays alive while its callback runs, so shutdown
        would read an executing retry as still pending, dead-letter it, and
        the attempt submitted a moment later could still succeed -- putting a
        DELIVERED result into the manual re-push worklist.

        Driven through the real _submit_retry rather than a never-started
        timer, which is the only way to exercise this."""
        submitted = []
        monkeypatch.setattr(sender._executor, "submit", lambda fn, *a: submitted.append(a))
        sender._schedule_retry(_record(), 1)
        entry_id, timer, record, attempt = _pending_entry(sender)
        timer.cancel()
        sender._submit_retry(entry_id, record, attempt)  # the timer "fires"
        assert len(submitted) == 1, "the retry must have been handed to the pool"

        sender.shutdown()
        assert _dead_letters(sender) == [], "delivered work must not enter the worklist"

    def test_shutdown_claims_a_retry_its_timer_never_got_to(self, sender, monkeypatch):
        """The mirror image: shutdown wins the claim, so the record IS saved
        -- and the timer firing afterwards must not write a second line."""
        submitted = []
        monkeypatch.setattr(sender._executor, "submit", lambda fn, *a: submitted.append(a))
        sender._schedule_retry(_record(), 1)
        entry_id, timer, record, attempt = _pending_entry(sender)

        sender.shutdown()
        assert len(_dead_letters(sender)) == 1

        sender._submit_retry(entry_id, record, attempt)  # fires after shutdown
        assert submitted == [], "must not submit to a closed executor"
        assert len(_dead_letters(sender)) == 1, "must not double-write the worklist"

    def test_every_scheduled_retry_is_accounted_for_exactly_once(self, sender, monkeypatch):
        """Torture the real race: many live timers with a genuine shutdown
        landing among them. Each record must end up either submitted or
        dead-lettered -- never both, never neither."""
        submitted = []
        lock = threading.Lock()

        def fake_submit(fn, *a):
            with lock:
                submitted.append(a[0]["tracking_id"])

        monkeypatch.setattr(sender._executor, "submit", fake_submit)
        monkeypatch.setattr(add_call_result.random, "uniform", lambda a, b: 0.0)

        ids = [f"id-{i:03d}" for i in range(60)]
        for tid in ids:
            # attempt 0 -> backoff 2**-1 = 0.5 s, so real timers really fire
            sender._schedule_retry(_record(tracking_id=tid), 0)
        time.sleep(0.25)      # let roughly half of them fire
        sender.shutdown()
        time.sleep(0.6)       # let any survivors run

        lettered = [r["tracking_id"] for r in _dead_letters(sender)]
        assert len(lettered) == len(set(lettered)), "a record was dead-lettered twice"
        overlap = set(lettered) & set(submitted)
        assert not overlap, f"submitted AND dead-lettered: {sorted(overlap)}"
        assert set(lettered) | set(submitted) == set(ids), "a record vanished"

    def test_sender_built_after_shutdown_is_born_shut_down(self, monkeypatch):
        """get_sender() used an unlocked pre-check, so it could race
        shutdown(): reader sees None, shutdown() takes the lock and finds
        nothing to stop, reader then builds a LIVE sender nothing will ever
        shut down. Anything built after shutdown must be inert."""
        monkeypatch.setattr(add_call_result, "_sender", None)
        monkeypatch.setattr(add_call_result, "_shutdown_called", False)
        add_call_result.shutdown()  # nothing exists yet -- the racy window
        built = add_call_result.get_sender()
        assert built._shutting_down is True
        calls = _patch_post(monkeypatch, _FakeResponse(200, {"isSuccess": True}))
        add_call_result.push_async(GUID, "Timeout")
        assert calls == []

    def test_module_shutdown_keeps_the_shut_down_sender(self, monkeypatch):
        """get_sender() must not build a fresh LIVE sender after shutdown --
        that would push results the operator has already stopped."""
        monkeypatch.setattr(add_call_result, "_sender", None)
        first = add_call_result.get_sender()
        add_call_result.shutdown()
        assert add_call_result.get_sender() is first
        assert first._shutting_down is True


# --------------------------------------------------------------------------
# Secret hygiene
# --------------------------------------------------------------------------
class TestNoSecretLeak:
    def test_api_key_never_reaches_the_dead_letter_file(self, sender, monkeypatch):
        _patch_post(monkeypatch, _FakeResponse(401, {}))
        sender._attempt(_record(), 1)
        with open(sender._dead_letter_path, encoding="utf-8") as f:
            content = f.read()
        assert "test-key-do-not-log" not in content
        assert GUID in content  # the useful part IS there

    def test_api_key_never_reaches_the_log(self, sender, monkeypatch, caplog):
        _patch_post(monkeypatch, _FakeResponse(401, {}))
        with caplog.at_level("DEBUG"):
            sender._attempt(_record(), 1)
        assert "test-key-do-not-log" not in caplog.text

    def test_server_echoed_key_is_redacted_from_the_log(self, sender, monkeypatch, caplog):
        """errorMessage is written by the far end. A proxy echoing the request
        headers back in an error body must not put a live credential in
        service.log."""
        _patch_post(
            monkeypatch,
            _FakeResponse(200, {
                "isSuccess": False,
                "errorMessage": "upstream rejected X-Api-Key: test-key-do-not-log",
                "validationErrors": [],
            }),
        )
        _capture_retries(monkeypatch, sender)
        with caplog.at_level("DEBUG"):
            sender._attempt(_record(), 1)
        assert "test-key-do-not-log" not in caplog.text
        assert "<redacted>" in caplog.text

    def test_server_echoed_key_is_redacted_from_validation_errors(self, sender, monkeypatch, caplog):
        _patch_post(
            monkeypatch,
            _FakeResponse(200, {
                "isSuccess": False,
                "errorMessage": "invalid",
                "validationErrors": ["bad header test-key-do-not-log"],
            }),
        )
        with caplog.at_level("DEBUG"):
            sender._attempt(_record(), 1)
        assert "test-key-do-not-log" not in caplog.text

    def test_transport_exception_text_is_redacted(self, sender, monkeypatch, caplog):
        _patch_post(
            monkeypatch,
            exc=requests.ConnectionError("failed sending X-Api-Key: test-key-do-not-log"),
        )
        _capture_retries(monkeypatch, sender)
        with caplog.at_level("DEBUG"):
            sender._attempt(_record(), 1)
        assert "test-key-do-not-log" not in caplog.text

    def test_dead_letter_record_carries_only_the_useful_fields(self, sender, monkeypatch):
        _patch_post(monkeypatch, _FakeResponse(401, {}))
        sender._attempt(_record(status="Cancelled"), 1)
        rec = _dead_letters(sender)[0]
        assert rec["tracking_id"] == GUID
        assert rec["status"] == "Cancelled"
        assert "_first_attempt_at" not in rec


# --------------------------------------------------------------------------
# Which SIP codes may honestly be called "customer no answer"
# --------------------------------------------------------------------------
class TestCustomerNoAnswerCodes:
    def test_486_busy_is_customer_side(self):
        assert add_call_result.is_customer_no_answer(486) is True

    def test_503_is_not_customer_side(self):
        """db maps 503 to Cancelled for our own column, but 503 is the PBX or
        a trunk failing -- pushing it as 202 would tell Tamweely the customer
        did not answer a call their phone never received."""
        import db
        assert 503 in db._CANCELLED_SIP_CODES
        assert add_call_result.is_customer_no_answer(503) is False

    @pytest.mark.parametrize("code", [None, 408, 480, 600, 603, 604, 200])
    def test_unresolved_codes_are_excluded(self, code):
        assert add_call_result.is_customer_no_answer(code) is False
