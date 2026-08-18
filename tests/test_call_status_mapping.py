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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from call_session import CallSession  # noqa: E402


class _FakeSession:
    """Duck-typed stand-in for CallSession, exposing only what
    _record_call_ended actually reads, plus a recording _db_call."""

    def __init__(self, *, answered=True, last_reject_code=None, transfer_failed=False):
        self.answered = answered
        self._last_reject_code = last_reject_code
        self._transfer_failed = transfer_failed
        self.calls: list[tuple] = []

    def _db_call(self, func, *args, **kwargs):
        self.calls.append((func, args, kwargs))


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
