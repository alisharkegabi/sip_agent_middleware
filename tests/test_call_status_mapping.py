"""
Unit tests for CallSession._record_call_ended -- the decision table that
maps an exit_reason (plus a couple of pieces of call state) onto the
BatchCallDetails.Status value written at end of call.

This chain became order-dependent once the transfer statuses (D6) were
added: "transferred" must win over a sticky _transfer_failed flag from an
earlier failed attempt on the same call, and _transfer_failed must be
checked ahead of the pre-answer statuses (ring_timeout/local_hangup/
reject_status) so a transfer failure is never silently dropped by whatever
the call happens to do afterward.

Drives the REAL CallSession._record_call_ended implementation against a
lightweight duck-typed stub instead of a real CallSession -- no sockets, no
SQL Server connection, no ElevenLabs SDK. The stub's _db_call just records
what it was called with, exactly like the real _db_call would forward to
db.mark_ended, but never actually opens a database connection.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import add_call_result  # noqa: E402
import db  # noqa: E402
from call_session import CallSession  # noqa: E402


class _FakeSession:
    """Duck-typed stand-in for CallSession, exposing only what
    _record_call_ended actually reads, plus a recording _db_call."""

    def __init__(self, *, answered=True, last_reject_code=None, transfer_failed=False,
                 hangup_reason="api"):
        self.answered = answered
        self._last_reject_code = last_reject_code
        self._transfer_failed = transfer_failed
        self._hangup_reason = hangup_reason
        self.calls: list[tuple] = []
        self.pushes: list[str] = []
        # One ordered log of both effects, so "DB write before push" is
        # testable rather than just asserted in a comment.
        self.sequence: list[str] = []

    def _db_call(self, func, *args, **kwargs):
        self.calls.append((func, args, kwargs))
        self.sequence.append("db")

    def _push_call_result(self, status):
        self.pushes.append(status)
        self.sequence.append("push")


def _record(exit_reason, **session_kwargs):
    fake = _FakeSession(**session_kwargs)
    CallSession._record_call_ended(fake, exit_reason)
    return fake


class TestTransferredWinsOverStickyFailure:
    def test_transferred_not_failed(self):
        fake = _record("transferred", transfer_failed=False)
        assert len(fake.calls) == 1
        _, _, kwargs = fake.calls[0]
        assert kwargs["status"] == db.STATUS_TRANSFERRED == "Transfer"

    def test_transferred_after_earlier_failed_attempt(self):
        """The ordering this whole table exists to get right: a call that
        failed one transfer attempt and then succeeded on a retry must
        record Transfer, not TranFail."""
        fake = _record("transferred", transfer_failed=True)
        assert len(fake.calls) == 1
        _, _, kwargs = fake.calls[0]
        assert kwargs["status"] == db.STATUS_TRANSFERRED == "Transfer"


class TestTransferFailedIsSticky:
    def test_transfer_unavailable(self):
        fake = _record("transfer_unavailable", transfer_failed=True)
        assert len(fake.calls) == 1
        _, _, kwargs = fake.calls[0]
        assert kwargs["status"] == db.STATUS_TRANSFER_FAILED == "TranFail"

    def test_remote_bye_after_failed_transfer(self):
        fake = _record("remote_bye", transfer_failed=True)
        assert len(fake.calls) == 1
        _, _, kwargs = fake.calls[0]
        assert kwargs["status"] == db.STATUS_TRANSFER_FAILED

    def test_rtp_timeout_after_failed_transfer(self):
        fake = _record("rtp_timeout", transfer_failed=True)
        assert len(fake.calls) == 1
        _, _, kwargs = fake.calls[0]
        assert kwargs["status"] == db.STATUS_TRANSFER_FAILED

    def test_sip_disconnect_after_failed_transfer(self):
        """sip_disconnect writes nothing at all in the non-transfer case
        (see TestNoWriteCases below) -- but a known transfer failure must
        still be recorded regardless of how the call finally ended."""
        fake = _record("sip_disconnect", transfer_failed=True)
        assert len(fake.calls) == 1
        _, _, kwargs = fake.calls[0]
        assert kwargs["status"] == db.STATUS_TRANSFER_FAILED


class TestPreExistingStatusesUnaffected:
    def test_remote_bye_without_transfer_failure_is_endedat_only(self):
        fake = _record("remote_bye", transfer_failed=False)
        assert len(fake.calls) == 1
        _, _, kwargs = fake.calls[0]
        assert "status" not in kwargs

    def test_ring_timeout(self):
        fake = _record("ring_timeout", transfer_failed=False, answered=False)
        assert len(fake.calls) == 1
        _, _, kwargs = fake.calls[0]
        assert kwargs["status"] == db.STATUS_TIMEOUT

    def test_local_hangup_before_answer(self):
        fake = _record("local_hangup", transfer_failed=False, answered=False)
        assert len(fake.calls) == 1
        _, _, kwargs = fake.calls[0]
        assert kwargs["status"] == db.STATUS_CANCELLED

    def test_reject_code_486_maps_to_cancelled(self):
        fake = _record("agent_ended", transfer_failed=False, last_reject_code=486)
        assert len(fake.calls) == 1
        _, _, kwargs = fake.calls[0]
        assert kwargs["status"] == db.STATUS_CANCELLED


class TestNoWriteCases:
    def test_sip_disconnect_without_transfer_failure_writes_nothing(self):
        fake = _record("sip_disconnect", transfer_failed=False)
        assert fake.calls == []

    def test_internal_error_writes_nothing(self):
        fake = _record("internal_error", transfer_failed=False)
        assert fake.calls == []


class TestStatusValuesFitWithoutTruncation:
    """dbo.BatchCallDetails.Status is nvarchar(20); both new values were
    chosen short by design (D6) so no schema change is required."""

    def test_transfer_status_length(self):
        assert db.STATUS_TRANSFERRED == "Transfer"
        assert len(db.STATUS_TRANSFERRED) <= 20

    def test_tranfail_status_length(self):
        assert db.STATUS_TRANSFER_FAILED == "TranFail"
        assert len(db.STATUS_TRANSFER_FAILED) <= 20


# --------------------------------------------------------------------------
# Which of those statuses are additionally PUSHED to Tamweely's AddCallResult
# API (add_call_result.py). The push set is narrower than the DB set at both
# edges -- see _record_call_ended's docstring.
# --------------------------------------------------------------------------
class TestPushedToTamweely:
    def test_ring_timeout_pushes_timeout(self):
        fake = _record("ring_timeout", answered=False)
        assert fake.pushes == [db.STATUS_TIMEOUT]

    def test_client_hangup_before_answer_pushes_cancelled(self):
        fake = _record("local_hangup", answered=False, hangup_reason="api")
        assert fake.pushes == [db.STATUS_CANCELLED]

    def test_486_busy_pushes_cancelled(self):
        fake = _record("agent_ended", last_reject_code=486)
        assert fake.pushes == [db.STATUS_CANCELLED]

    def test_db_write_happens_before_the_push(self):
        """mark_ended owns EndedAt and is the record we keep even when
        Tamweely is unreachable, so it must not depend on the push."""
        fake = _record("ring_timeout", answered=False)
        assert fake.sequence == ["db", "push"]


class TestNotPushedToTamweely:
    def test_503_writes_cancelled_but_does_not_push(self):
        """503 is the PBX or a trunk failing. Recording Cancelled in our own
        column is fine; telling Tamweely the customer did not answer a call
        their phone never received is not."""
        fake = _record("agent_ended", last_reject_code=503)
        _, _, kwargs = fake.calls[0]
        assert kwargs["status"] == db.STATUS_CANCELLED
        assert fake.pushes == []

    def test_shutdown_hangup_writes_cancelled_but_does_not_push(self):
        """CallManager.shutdown() hangs up every ringing session so the
        service can stop. That is a deploy, not customer behaviour."""
        fake = _record("local_hangup", answered=False, hangup_reason="shutdown")
        _, _, kwargs = fake.calls[0]
        assert kwargs["status"] == db.STATUS_CANCELLED
        assert fake.pushes == []

    def test_answered_call_does_not_push(self):
        """An answered call produced an ElevenLabs conversation, so the
        post-call webhook already delivers its result."""
        fake = _record("remote_bye", answered=True)
        assert fake.pushes == []

    def test_transferred_does_not_push(self):
        fake = _record("transferred")
        assert fake.pushes == []

    def test_transfer_failed_does_not_push(self):
        fake = _record("transfer_unavailable", transfer_failed=True)
        assert fake.pushes == []

    def test_local_hangup_after_answer_does_not_push(self):
        fake = _record("local_hangup", answered=True)
        assert fake.pushes == []

    @pytest.mark.parametrize("exit_reason", [
        "sip_disconnect", "internal_error", "connect_timeout", "connect_failed",
    ])
    def test_failure_reasons_push_nothing(self, exit_reason):
        """Those calls never reached the customer at all."""
        fake = _record(exit_reason)
        assert fake.calls == []
        assert fake.pushes == []

    @pytest.mark.parametrize("exit_reason", ["max_duration", "rtp_timeout", "agent_ended"])
    def test_endedat_only_reasons_push_nothing(self, exit_reason):
        fake = _record(exit_reason, answered=True)
        assert fake.pushes == []


class TestPushSetMatchesTheModule:
    """The exclusion must live in ONE place: if CUSTOMER_NO_ANSWER_SIP_CODES
    changes, _record_call_ended must follow without being edited.

    Asserting is_customer_no_answer() returns its own constants proves
    nothing -- it passes even if _record_call_ended stopped consulting it.
    These drive the real decision table instead."""

    def test_widening_the_table_widens_the_push(self, monkeypatch):
        monkeypatch.setattr(
            add_call_result, "CUSTOMER_NO_ANSWER_SIP_CODES", frozenset({486, 603})
        )
        fake = _record("agent_ended", last_reject_code=603)
        # 603 writes no DB status (it is not in db._CANCELLED_SIP_CODES), so
        # there is nothing to push either -- the push rides on the DB branch.
        assert fake.pushes == []

        fake = _record("agent_ended", last_reject_code=486)
        assert fake.pushes == [db.STATUS_CANCELLED]

    def test_narrowing_the_table_stops_the_push(self, monkeypatch):
        """Empty the table and 486 must stop being pushed -- while still
        writing Cancelled locally. Fails if the branch hardcodes 486."""
        monkeypatch.setattr(
            add_call_result, "CUSTOMER_NO_ANSWER_SIP_CODES", frozenset()
        )
        fake = _record("agent_ended", last_reject_code=486)
        _, _, kwargs = fake.calls[0]
        assert kwargs["status"] == db.STATUS_CANCELLED
        assert fake.pushes == []


# --------------------------------------------------------------------------
# Finding 7 (Codex): every test above hand-builds `hangup_reason`, so none of
# them proves CallManager.shutdown() actually tags real sessions, nor that the
# tag survives a concurrent API hangup. That gap is why the last-writer-wins
# race in request_hangup() went unnoticed. These drive the real methods.
# --------------------------------------------------------------------------
class TestHangupProvenanceOnARealSession:
    @staticmethod
    def _bare_session():
        """A real CallSession with only the attributes request_hangup touches,
        built without running __init__ (which opens no sockets, but does want
        a full Settings, port allocator and logger)."""
        import threading as _t
        s = CallSession.__new__(CallSession)
        s._status_lock = _t.Lock()
        s._hangup_requested = _t.Event()
        s._hangup_reason = "api"
        return s

    def test_default_reason_is_api(self):
        s = self._bare_session()
        s.request_hangup()
        assert s._hangup_reason == "api"
        assert s._hangup_requested.is_set()

    def test_shutdown_reason_is_recorded(self):
        s = self._bare_session()
        s.request_hangup(reason="shutdown")
        assert s._hangup_reason == "shutdown"

    def test_later_api_hangup_cannot_overwrite_shutdown(self):
        """THE finding: shutdown drains every live session, and an API hangup
        for the same call can land immediately after. Last-writer-wins let the
        default "api" flip it back and push 202 for a deploy."""
        s = self._bare_session()
        s.request_hangup(reason="shutdown")
        s.request_hangup()  # defaults to "api"
        assert s._hangup_reason == "shutdown"

    def test_later_shutdown_cannot_overwrite_a_real_api_cancel(self):
        """The converse: a client genuinely cancelled this call first, so it
        stays pushable even though a shutdown followed."""
        s = self._bare_session()
        s.request_hangup(reason="api")
        s.request_hangup(reason="shutdown")
        assert s._hangup_reason == "api"

    def test_reason_never_changes_once_the_event_is_set(self):
        """A torture run for torn state. This deliberately does NOT assert
        WHICH caller wins -- with both racing, either may take the lock
        first, and both answers are correct. What must hold is that the
        reason stops changing the moment the event is set, so the value
        _record_call_ended later reads is the one the winner wrote.

        The two sequential tests above are what pin down the ordering rule;
        this one is here for interleaving damage they cannot see."""
        import threading as _t
        for _ in range(200):
            s = self._bare_session()
            start = _t.Event()
            observed = []

            def shutdown_caller():
                start.wait()
                s.request_hangup(reason="shutdown")
                observed.append(s._hangup_reason)

            def api_caller():
                start.wait()
                s.request_hangup()
                observed.append(s._hangup_reason)

            threads = [_t.Thread(target=shutdown_caller), _t.Thread(target=api_caller)]
            for t in threads:
                t.start()
            start.set()
            for t in threads:
                t.join()

            assert s._hangup_requested.is_set()
            assert s._hangup_reason in ("shutdown", "api")
            # Both callers returned AFTER the event was set, so each must have
            # seen the settled value -- not one value then a different one.
            assert set(observed) == {s._hangup_reason}, (
                f"reason changed after the event was set: {observed}"
            )


class TestCallManagerTagsItsOwnDrain:
    """Codex: asserting a source-code substring would pass even if the call
    sat in unreachable code. These drive the real CallManager.shutdown()."""

    @staticmethod
    def _manager_with(session):
        """A real CallManager with __init__ skipped -- shutdown() only needs
        these few attributes, and building one for real would start a worker
        pool, a webhook sender and a reaper thread."""
        import threading as _t
        import call_manager as cm
        from models import CallStatus

        m = cm.CallManager.__new__(cm.CallManager)
        m._shutdown_event = _t.Event()
        m._lock = _t.Lock()
        m._sessions = {"c1": session}
        m.settings = type("S", (), {"shutdown_grace_seconds": 0})()

        class _Executor:
            def shutdown(self, **kwargs):
                pass

        m._executor = _Executor()
        m._analysis_fetcher = type("A", (), {"shutdown": lambda self: None})()
        m._webhook_sender = type("W", (), {"shutdown": lambda self: None})()
        return m, CallStatus

    def test_shutdown_tags_a_ringing_session_as_shutdown(self, monkeypatch):
        import add_call_result

        class _Session:
            def __init__(self):
                self.status = None
                self.reasons = []

            def request_hangup(self, reason="api"):
                self.reasons.append(reason)

        session = _Session()
        manager, CallStatus = self._manager_with(session)
        session.status = CallStatus.RINGING
        monkeypatch.setattr(manager, "list_calls", lambda: [session])
        monkeypatch.setattr(add_call_result, "shutdown", lambda: None)

        manager.shutdown()

        assert session.reasons == ["shutdown"], (
            "the drain must tag its hangups, or every ringing call in flight "
            "during a deploy gets pushed to Tamweely as customer no-answer"
        )

    def test_api_hangup_still_defaults_to_api(self, monkeypatch):
        """The other caller of request_hangup must NOT be tagged."""
        import call_manager as cm

        class _Session:
            def __init__(self):
                self.reasons = []

            def request_hangup(self, reason="api"):
                self.reasons.append(reason)

        session = _Session()
        manager, _ = self._manager_with(session)
        monkeypatch.setattr(manager, "get_call", lambda call_id: session)

        assert cm.CallManager.hangup(manager, "c1") is True
        assert session.reasons == ["api"]
