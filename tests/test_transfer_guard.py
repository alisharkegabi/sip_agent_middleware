"""
Unit tests for CallSession._claim_transfer -- the one-shot guard that
prevents a single call from ever sending more than one REFER, and that
(combined with on_agent_response being a closure bound to one CallSession
per call) prevents two different calls from ever touching each other's
transfer state.

CallSession.__init__ does no socket/file I/O (sockets are only opened later,
inside _dial()), so real CallSession instances are constructed directly
here rather than mocked -- this exercises the actual guard, not a stand-in
for it. No network, no sockets, no PBX.
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from call_session import CallSession  # noqa: E402
from config import Settings  # noqa: E402
from extension_pool import ExtensionPool  # noqa: E402
from port_allocator import PortAllocator  # noqa: E402


def _make_session(**overrides) -> CallSession:
    settings = Settings()
    kwargs = dict(
        phone_number="+201234567890",
        dynamic_variables={"tracking_id": "test-tracking-id"},
        settings=settings,
        port_allocator=PortAllocator(10000, 10999, 1.0),
        extension_pool=ExtensionPool(["406"], cooldown_seconds=1.0),
        tracking_id="test-tracking-id",
    )
    kwargs.update(overrides)
    return CallSession(**kwargs)


class TestSingleCallGuard:
    def test_claim_returns_true_once(self):
        session = _make_session()
        assert session._claim_transfer() is True
        assert session._claim_transfer() is False
        assert session._claim_transfer() is False

    def test_claim_under_concurrency_grants_exactly_one(self):
        session = _make_session()
        results = []
        results_lock = threading.Lock()
        start = threading.Event()

        def worker():
            start.wait()
            claimed = session._claim_transfer()
            with results_lock:
                results.append(claimed)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 20
        assert results.count(True) == 1
        assert results.count(False) == 19


class TestGuardIsPerInstance:
    def test_two_sessions_each_get_their_own_claim(self):
        """The anti-mixup guarantee: one call successfully claiming a
        transfer must have zero effect on another call's ability to claim
        its own."""
        session_a = _make_session(tracking_id="tracking-a")
        session_b = _make_session(tracking_id="tracking-b")

        assert session_a._claim_transfer() is True
        # B is completely unaffected by A having already claimed.
        assert session_b._claim_transfer() is True

        # Both are now independently exhausted.
        assert session_a._claim_transfer() is False
        assert session_b._claim_transfer() is False

    def test_state_lives_on_self_not_the_class(self):
        session_a = _make_session(tracking_id="tracking-a")
        session_b = _make_session(tracking_id="tracking-b")
        session_a._claim_transfer()
        assert session_a._transfer_started is True
        assert session_b._transfer_started is False


class TestTransferFailedFlag:
    def test_mark_transfer_failed_is_sticky(self):
        session = _make_session()
        assert session._transfer_failed is False
        session._mark_transfer_failed()
        assert session._transfer_failed is True
        # Calling it again is a no-op, not an error.
        session._mark_transfer_failed()
        assert session._transfer_failed is True

    def test_transfer_failed_is_per_instance(self):
        session_a = _make_session(tracking_id="tracking-a")
        session_b = _make_session(tracking_id="tracking-b")
        session_a._mark_transfer_failed()
        assert session_a._transfer_failed is True
        assert session_b._transfer_failed is False
