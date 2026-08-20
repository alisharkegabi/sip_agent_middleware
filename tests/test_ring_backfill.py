"""
Unit tests for the RingAt backfill in db.py.

The bug these cover: the BatchCallDetails row is created by the .NET client,
but mark_ringing fires a median of 131 ms after POST /calls is accepted --
before that row necessarily exists -- while mark_answered cannot fire before
3.5 s. An UPDATE matching zero rows raises nothing and commits, so the
ringing write was silently lost while every later write landed, leaving rows
with RingAt NULL but AnsweredAt, EndedAt and Status all populated.

Drives the REAL db.mark_ringing / db._backfill_ringing against a stubbed
_execute and a stubbed threading.Timer -- no SQL Server connection, and no
wall-clock waiting.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402


class _FakeTimer:
    """Stand-in for threading.Timer that records rather than waits.

    Every instance is appended to `created`, so a test can assert on the
    delay that was requested and then fire the callback itself.
    """

    created: list["_FakeTimer"] = []

    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.started = False
        _FakeTimer.created.append(self)

    def start(self):
        self.started = True

    def fire(self):
        self.callback()


class _ExecuteRecorder:
    """Replaces db._execute, returning a scripted rowcount per call."""

    def __init__(self, *rowcounts):
        self.rowcounts = list(rowcounts)
        self.calls: list[tuple] = []

    def __call__(self, sql, *params):
        self.calls.append((sql, params))
        return self.rowcounts.pop(0) if self.rowcounts else 0

    @property
    def statements(self):
        return [sql for sql, _ in self.calls]


def _install(monkeypatch, *rowcounts) -> _ExecuteRecorder:
    recorder = _ExecuteRecorder(*rowcounts)
    monkeypatch.setattr(db, "_execute", recorder)
    monkeypatch.setattr(db.threading, "Timer", _FakeTimer)
    _FakeTimer.created = []
    return recorder


class TestNoRetryWhenTheRowExists:
    def test_single_update_and_no_timer(self, monkeypatch):
        """The normal case: the row is there, one UPDATE, nothing scheduled."""
        recorder = _install(monkeypatch, 1)
        db.mark_ringing("11111111-1111-1111-1111-111111111111")
        assert len(recorder.calls) == 1
        assert _FakeTimer.created == []


class TestBackfillWhenTheRowIsNotThereYet:
    def test_zero_rows_schedules_a_retry_without_blocking(self, monkeypatch):
        """mark_ringing runs on the SIP thread that is about to read the
        PBX's response to the INVITE, so the retry must be handed to a
        Timer, never slept inline."""
        _install(monkeypatch, 0)
        db.mark_ringing("11111111-1111-1111-1111-111111111111")
        assert len(_FakeTimer.created) == 1
        timer = _FakeTimer.created[0]
        assert timer.delay == db.RING_BACKFILL_DELAYS[0]
        assert timer.daemon is True
        assert timer.started is True

    def test_retry_lands_and_stops_retrying(self, monkeypatch):
        recorder = _install(monkeypatch, 0, 1)
        db.mark_ringing("11111111-1111-1111-1111-111111111111")
        _FakeTimer.created[0].fire()
        assert len(recorder.calls) == 2
        # One scheduled timer, already fired; no further one queued behind it.
        assert len(_FakeTimer.created) == 1

    def test_retries_walk_the_configured_delays_then_give_up(self, monkeypatch):
        recorder = _install(monkeypatch, *([0] * (len(db.RING_BACKFILL_DELAYS) + 1)))
        db.mark_ringing("11111111-1111-1111-1111-111111111111")
        for i in range(len(db.RING_BACKFILL_DELAYS)):
            assert _FakeTimer.created[i].delay == db.RING_BACKFILL_DELAYS[i]
            _FakeTimer.created[i].fire()
        # Bounded: the initial write plus one per configured delay, no more.
        assert len(recorder.calls) == 1 + len(db.RING_BACKFILL_DELAYS)
        assert len(_FakeTimer.created) == len(db.RING_BACKFILL_DELAYS)

    def test_a_failing_retry_is_rescheduled_not_dropped(self, monkeypatch):
        """A DB error mid-backfill must not silently end the chain."""
        _install(monkeypatch, 0)

        def _boom(sql, *params):
            raise RuntimeError("connection reset")

        db.mark_ringing("11111111-1111-1111-1111-111111111111")
        monkeypatch.setattr(db, "_execute", _boom)
        _FakeTimer.created[0].fire()
        assert len(_FakeTimer.created) == 2
        assert _FakeTimer.created[1].delay == db.RING_BACKFILL_DELAYS[1]


class TestTheBackfillStatementIsSafeToRunLate:
    def test_guards_against_clobbering_a_later_status(self, monkeypatch):
        """By the time a retry fires the call is usually answered. The
        backfill must set RingAt without dragging Status back to 'Ringing'."""
        recorder = _install(monkeypatch, 0, 1)
        db.mark_ringing("11111111-1111-1111-1111-111111111111")
        _FakeTimer.created[0].fire()
        backfill_sql = recorder.statements[1]
        assert "WHERE TrackingId = ? AND RingAt IS NULL" in backfill_sql
        assert "CASE WHEN AnsweredAt IS NULL AND EndedAt IS NULL" in backfill_sql

    def test_backfill_writes_the_original_ring_time_not_the_retry_time(self, monkeypatch):
        """RingAt has to be when the phone actually started ringing, not
        whenever the retry happened to succeed."""
        recorder = _install(monkeypatch, 0, 1)
        db.mark_ringing("11111111-1111-1111-1111-111111111111")
        first_ring_at = recorder.calls[0][1][0]
        _FakeTimer.created[0].fire()
        assert recorder.calls[1][1][0] == first_ring_at
