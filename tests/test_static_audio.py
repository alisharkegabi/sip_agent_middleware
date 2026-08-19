"""
Unit tests for static_audio.load_ulaw_frames -- turning a pre-rendered WAV
(the "all lines busy" prompt) into RTP-ready mu-law frames.

No network, no sockets: writes real WAV files to pytest's tmp_path and reads
them back, same style as the audio_bridge resampler tests.
"""
from __future__ import annotations

import importlib
import math
import os
import struct
import sys
import wave

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import audioop  # noqa: F401
except ImportError:
    import audioop_lts as audioop  # noqa: F401

import static_audio  # noqa: E402


def _write_wav(path: str, *, rate: int, seconds: float = 0.5, freq: float = 440.0) -> None:
    n = int(rate * seconds)
    samples = struct.pack(
        f"<{n}h",
        *(int(8000 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)),
    )
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples)


@pytest.fixture(autouse=True)
def _clear_cache():
    """load_ulaw_frames caches by path at module level -- clear it between
    tests so cache-behaviour tests aren't polluted by earlier ones reusing
    the same tmp_path-derived filename."""
    static_audio._cache.clear()
    yield
    static_audio._cache.clear()


class TestFrameShape:
    def test_16k_input_produces_160_byte_frames(self, tmp_path):
        path = str(tmp_path / "busy_16k.wav")
        _write_wav(path, rate=16000, seconds=0.5)
        frames = static_audio.load_ulaw_frames(path, frame_bytes=160)
        assert all(len(f) == 160 for f in frames)

    def test_16k_frame_count_matches_duration(self, tmp_path):
        path = str(tmp_path / "busy_16k.wav")
        _write_wav(path, rate=16000, seconds=1.0)
        frames = static_audio.load_ulaw_frames(path, frame_bytes=160)
        # 1s of 8kHz mu-law audio in 20ms (160-byte) frames == 50 frames.
        assert 48 <= len(frames) <= 52

    def test_last_frame_is_padded_not_short(self, tmp_path):
        path = str(tmp_path / "busy_16k_odd.wav")
        # A duration that won't land on an exact 20ms/160-byte boundary.
        _write_wav(path, rate=16000, seconds=0.513)
        frames = static_audio.load_ulaw_frames(path, frame_bytes=160)
        assert len(frames[-1]) == 160

    def test_8k_input_skips_resampling_same_frame_shape(self, tmp_path):
        path = str(tmp_path / "busy_8k.wav")
        _write_wav(path, rate=8000, seconds=0.5)
        frames = static_audio.load_ulaw_frames(path, frame_bytes=160)
        assert all(len(f) == 160 for f in frames)
        assert 23 <= len(frames) <= 27  # 0.5s / 20ms == 25 frames


class TestErrorHandling:
    def test_import_does_not_touch_disk(self):
        # Re-importing the module must not raise even if no busy-prompt
        # file exists anywhere -- loading only happens when
        # load_ulaw_frames() is actually called.
        importlib.reload(static_audio)

    def test_missing_file_raises_cleanly(self, tmp_path):
        missing = str(tmp_path / "does_not_exist.wav")
        with pytest.raises(Exception):
            static_audio.load_ulaw_frames(missing)

    def test_stereo_input_rejected(self, tmp_path):
        path = str(tmp_path / "stereo.wav")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00\x00\x00" * 100)
        with pytest.raises(ValueError):
            static_audio.load_ulaw_frames(path)

    def test_unsupported_rate_rejected(self, tmp_path):
        path = str(tmp_path / "wrong_rate.wav")
        _write_wav(path, rate=44100, seconds=0.1)
        with pytest.raises(ValueError):
            static_audio.load_ulaw_frames(path)


class TestModuleLevelCache:
    def test_second_load_returns_identical_object(self, tmp_path):
        path = str(tmp_path / "busy_cached.wav")
        _write_wav(path, rate=16000, seconds=0.3)
        first = static_audio.load_ulaw_frames(path)
        second = static_audio.load_ulaw_frames(path)
        assert first is second

    def test_different_paths_are_not_conflated(self, tmp_path):
        path_a = str(tmp_path / "a.wav")
        path_b = str(tmp_path / "b.wav")
        _write_wav(path_a, rate=16000, seconds=0.3, freq=440.0)
        _write_wav(path_b, rate=16000, seconds=0.3, freq=880.0)
        frames_a = static_audio.load_ulaw_frames(path_a)
        frames_b = static_audio.load_ulaw_frames(path_b)
        assert frames_a is not frames_b
