"""
Tests for CallSession._wait_for_playout's start gate.

THE BUG THESE EXIST FOR: _wait_for_playout is there so the trigger sentence
("your call is being transferred now") actually reaches the caller before
the REFER goes out. But on_agent_response -- which is what queues the
transfer -- fires on the LLM's TEXT, ahead of any audio for that sentence.
At that instant the previous turn's audio has long since drained, so
playout_pending() is 0 and last_output_monotonic is seconds old: both exit
conditions were already true and the wait returned in one poll interval,
transferring the caller before they heard a word.

The fix waits for the sentence's audio to START before it begins measuring
drain/quiet. A fake RTP interface is used rather than a real one because
what is under test is purely the timing logic over two attributes;
RtpAudioInterface binds a UDP socket and spawns two threads, neither of
which would make these assertions more true.
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from call_session import CallSession  # noqa: E402
from config import Settings  # noqa: E402
from transfer_targets import TransferTargets  # noqa: E402
from port_allocator import PortAllocator  # noqa: E402


class FakeRtp:
    """Just the two attributes _wait_for_playout reads."""

    def __init__(self, *, pending: int = 0, last_output: float | None = None):
        self._pending = pending
        self.last_output_monotonic = last_output

    def playout_pending(self) -> int:
        return self._pending


def _make_session() -> CallSession:
    return CallSession(
        phone_number="+201000000000",
        dynamic_variables={},
        settings=Settings(),
        port_allocator=PortAllocator(10000, 10999, 1.0),
        transfer_targets=TransferTargets(["406"]),
        tracking_id=None,
    )


def _elapsed(fn) -> float:
    t0 = time.monotonic()
    fn()
    return time.monotonic() - t0


class TestStartGate:
    def test_without_the_gate_it_returns_before_audio_begins(self):
        """Documents exactly the behaviour the gate exists to prevent: the
        state at trigger time (empty queue, stale last-output) satisfies
        both exit conditions immediately."""
        session = _make_session()
        # Stale: the previous turn's audio finished 5 seconds ago.
        session._rtp_interface = FakeRtp(pending=0, last_output=time.monotonic() - 5.0)

        took = _elapsed(
            lambda: session._wait_for_playout(quiet_seconds=0.6, timeout=10.0, wait_for_start=0.0)
        )
        assert took < 0.3, "precondition for the bug: it returns essentially instantly"

    def test_gate_waits_for_audio_to_start_then_for_it_to_drain(self):
        session = _make_session()
        rtp = FakeRtp(pending=0, last_output=time.monotonic() - 5.0)
        session._rtp_interface = rtp

        def _speak():
            # TTS for the trigger sentence starts arriving 0.3s after the
            # text did, then streams chunks for another 0.3s -- each chunk
            # pushing last_output_monotonic forward, exactly as output()
            # does, so the quiet window can only start after the last one.
            time.sleep(0.3)
            end = time.monotonic() + 0.3
            while time.monotonic() < end:
                rtp._pending = 5
                rtp.last_output_monotonic = time.monotonic()
                time.sleep(0.02)
            rtp._pending = 0

        t = threading.Thread(target=_speak)
        t.start()
        took = _elapsed(
            lambda: session._wait_for_playout(quiet_seconds=0.2, timeout=10.0, wait_for_start=2.0)
        )
        t.join()

        # Waited for the audio to start (0.3) + stream (0.3) + the quiet
        # window after the last chunk (0.2). Without the gate this returned
        # at ~0.05, before the caller had heard anything at all.
        assert took >= 0.7, f"returned too early ({took:.2f}s) -- the gate did not hold"
        assert took < 5.0, f"took far too long ({took:.2f}s)"

    def test_gate_is_satisfied_by_a_new_chunk_even_if_the_queue_never_fills(self):
        """The send loop can drain frames as fast as output() queues them,
        so a rising last_output_monotonic counts as 'started' too."""
        session = _make_session()
        rtp = FakeRtp(pending=0, last_output=time.monotonic() - 5.0)
        session._rtp_interface = rtp

        def _speak():
            time.sleep(0.3)
            rtp.last_output_monotonic = time.monotonic()

        t = threading.Thread(target=_speak)
        t.start()
        took = _elapsed(
            lambda: session._wait_for_playout(quiet_seconds=0.2, timeout=10.0, wait_for_start=2.0)
        )
        t.join()

        assert took >= 0.4, f"returned before the chunk arrived ({took:.2f}s)"
        assert took < 5.0

    def test_gate_gives_up_after_wait_for_start_not_after_timeout(self):
        """If the agent produced text but no speech, the SIP thread must
        not sit out the whole playout timeout -- it holds the only thread
        that reads the SIP socket."""
        session = _make_session()
        session._rtp_interface = FakeRtp(pending=0, last_output=time.monotonic() - 5.0)

        took = _elapsed(
            lambda: session._wait_for_playout(quiet_seconds=0.6, timeout=10.0, wait_for_start=0.5)
        )
        assert 0.4 <= took < 2.0, f"expected to give up near wait_for_start, took {took:.2f}s"

    def test_no_rtp_interface_returns_immediately(self):
        session = _make_session()
        session._rtp_interface = None
        took = _elapsed(
            lambda: session._wait_for_playout(quiet_seconds=0.6, timeout=10.0, wait_for_start=2.0)
        )
        assert took < 0.2


class TestBusyPromptCallerIsUnaffected:
    def test_zero_start_gate_keeps_the_drain_only_behaviour(self):
        """_play_busy_prompt_and_close queues the frames itself and then
        waits with quiet_seconds=0.0 and no start gate -- it only wants the
        queue drained. That path must keep working unchanged."""
        session = _make_session()
        rtp = FakeRtp(pending=8, last_output=None)
        session._rtp_interface = rtp

        def _drain():
            time.sleep(0.3)
            rtp._pending = 0

        t = threading.Thread(target=_drain)
        t.start()
        took = _elapsed(
            lambda: session._wait_for_playout(quiet_seconds=0.0, timeout=5.0)
        )
        t.join()

        assert took >= 0.25, "returned before the prompt had drained"
        assert took < 2.0
