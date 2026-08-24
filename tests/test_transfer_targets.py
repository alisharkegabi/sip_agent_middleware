"""
Tests for TransferTargets, the chooser that replaced ExtensionPool.

THE BUG THESE EXIST FOR: ExtensionPool treated transfer targets as a pool of
scarce resources and quarantined one for TRANSFER_EXTENSION_BUSY_SECONDS
(300 s by default) after every successful handoff. With a small
TRANSFER_EXTENSIONS list -- production had a handful of hotline numbers --
the second transfer inside five minutes found the pool empty, and the
middleware refused it: the caller, who had just been told
"هيتم تحويل المكالمة دلوقتي", instead heard "all lines are busy, we'll
contact you within two days" and the call was recorded TranFail. Meanwhile
the PBX queue on the other end was idle and would have taken them.

The target is a queue. A queue holds callers; it does not fill up. So the
whole point of this module is that it CANNOT refuse. Every test below is
some form of that one assertion.
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transfer_targets import TransferTargets  # noqa: E402


class TestNeverRefuses:
    def test_same_target_handed_out_repeatedly(self):
        """The exact production case: one queue number, many transfers. The
        pool refused the second one within its busy window."""
        targets = TransferTargets(["201"])
        assert [targets.next_target() for _ in range(50)] == ["201"] * 50

    def test_no_state_carries_between_calls(self):
        """Nothing a previous transfer did may withhold a target from the
        next one -- there is no release(), so there is nothing to forget to
        call."""
        targets = TransferTargets(["201", "202"])
        for _ in range(200):
            assert targets.next_target() is not None

    def test_concurrent_callers_all_get_a_target(self):
        """Two live calls transferring at the same moment. Under the pool,
        the second one raised ExtensionPoolExhausted."""
        targets = TransferTargets(["201"])
        results: list = []
        lock = threading.Lock()

        def _grab():
            got = targets.next_target()
            with lock:
                results.append(got)

        threads = [threading.Thread(target=_grab) for _ in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 32
        assert all(r == "201" for r in results)


class TestRotation:
    def test_cycles_in_configured_order(self):
        targets = TransferTargets(["201", "202", "203"])
        got = [targets.next_target() for _ in range(7)]
        assert got == ["201", "202", "203", "201", "202", "203", "201"]

    def test_duplicates_removed_but_order_kept(self):
        """A repeated entry would otherwise take a bigger share of the
        rotation for no stated reason."""
        targets = TransferTargets(["202", "201", "202"])
        assert [targets.next_target() for _ in range(4)] == ["202", "201", "202", "201"]

    def test_whitespace_and_blanks_dropped(self):
        """TRANSFER_EXTENSIONS is a hand-edited CSV; a trailing comma or a
        space after one is normal."""
        targets = TransferTargets([" 201 ", "", "   ", "202"])
        assert [targets.next_target() for _ in range(2)] == ["201", "202"]


class TestNothingConfigured:
    def test_falsy_when_empty(self):
        """CallSession reads the truthiness to tell "misconfigured" from
        "busy" -- the one case where the prompt is still correct."""
        assert not TransferTargets([])
        assert not TransferTargets(["", "  "])

    def test_truthy_when_configured(self):
        assert TransferTargets(["201"])

    def test_next_target_returns_none_rather_than_raising(self):
        """None, not an exception: the caller is the SIP thread mid-call,
        and an exception there is a dropped call."""
        assert TransferTargets([]).next_target() is None


class TestStats:
    def test_reports_count_only(self):
        """Health output is logged and forwarded; there is no operational
        reason to spray PBX hotline numbers through it."""
        stats = TransferTargets(["201", "202"]).stats()
        assert stats == {"configured": 2}

    def test_zero_when_unconfigured(self):
        assert TransferTargets([]).stats() == {"configured": 0}
