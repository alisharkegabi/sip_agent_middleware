"""
Regression tests for the "all lines busy" static prompt playback.

THE BUG THESE EXIST FOR: _play_busy_prompt_and_close() originally closed
the ElevenLabs session BEFORE playing the prompt, to stop the agent talking
over it. But Conversation.end_session() calls stop() on the audio interface
(elevenlabs/conversational_ai/conversation.py: `if self.audio_interface is
not None: self.audio_interface.stop()`), which sets is_running=False and
closes the RTP socket. play_static_frames() then hit its is_running guard
and returned silently -- the caller heard nothing, and nothing was logged.

So: playback must happen while the interface is still running, and the
session is closed afterwards. The agent is kept off the line during
playback by the _static_playback latch instead.

RtpAudioInterface binds a real UDP socket in __init__, so these bind to
ephemeral loopback ports and never actually transmit anywhere meaningful.
"""
from __future__ import annotations

import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio_bridge import RtpAudioInterface  # noqa: E402


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def rtp(tmp_path):
    iface = RtpAudioInterface(
        _free_port(),
        "127.0.0.1",
        _free_port(),
        call_id="test-call",
        log_dir=str(tmp_path),
    )
    yield iface
    try:
        iface.stop()
    except Exception:
        pass


FRAMES = [b"\xff" * 160 for _ in range(10)]


class TestPlaybackRequiresARunningInterface:
    def test_returns_false_when_stopped(self, rtp):
        """This is the exact failure mode the bug produced: end_session()
        had already called stop(), so there was nothing left to play out
        of."""
        rtp.start(lambda _pcm: None)
        rtp.stop()
        assert rtp.play_static_frames(FRAMES) is False
        assert rtp.playout_pending() == 0

    def test_returns_false_when_no_remote_port(self, tmp_path):
        iface = RtpAudioInterface(
            _free_port(), "127.0.0.1", 0, call_id="test-call", log_dir=str(tmp_path)
        )
        try:
            iface.start(lambda _pcm: None)
            assert iface.play_static_frames(FRAMES) is False
        finally:
            iface.stop()

    def test_returns_true_and_queues_when_running(self, rtp):
        rtp.is_running = True  # queued without starting the sender threads
        assert rtp.play_static_frames(FRAMES) is True
        assert rtp.playout_pending() == len(FRAMES)


class TestAgentAudioIsDroppedDuringPlayback:
    def test_output_is_ignored_once_static_playback_latched(self, rtp):
        rtp.is_running = True
        rtp.play_static_frames(FRAMES)
        pending_before = rtp.playout_pending()

        # Agent TTS still arriving on the not-yet-closed websocket must not
        # be queued behind (or interleaved with) the prompt.
        rtp.output(b"\x00\x01" * 1600)

        assert rtp.playout_pending() == pending_before

    def test_output_works_normally_before_playback(self, rtp):
        rtp.is_running = True
        rtp.output(b"\x00\x01" * 1600)
        assert rtp.playout_pending() > 0

    def test_static_frames_replace_queued_agent_audio(self, rtp):
        rtp.is_running = True
        rtp.output(b"\x00\x01" * 1600)
        assert rtp.playout_pending() > 0

        rtp.play_static_frames(FRAMES)
        # Tail-end agent audio is cleared, not played ahead of the prompt.
        assert rtp.playout_pending() == len(FRAMES)
