"""
Loads a pre-rendered WAV file into RTP-ready mu-law frames, for the "all
lines busy" prompt played when a caller-facing transfer is announced but no
internal extension is free (see call_session.py's
_play_busy_prompt_and_close).

Deliberately separate from audio_bridge.py: RtpAudioInterface binds a UDP
socket in __init__ and cannot be constructed in a unit test, whereas this
module does no I/O beyond reading one WAV file and is fully testable.
"""
from __future__ import annotations

import threading
import wave
from typing import Dict, List

try:
    import audioop  # stdlib, removed in Python 3.13+
except ImportError:
    import audioop_lts as audioop  # pip install audioop-lts

from audio_bridge import OutboundResampler

_cache: Dict[str, List[bytes]] = {}
_cache_lock = threading.Lock()


def load_ulaw_frames(wav_path: str, frame_bytes: int = 160) -> List[bytes]:
    """Load a mono 16-bit PCM WAV (8 kHz or 16 kHz) and return a list of
    frame_bytes-sized mu-law chunks, ready for
    RtpAudioInterface.play_static_frames().

    Cached at module level, keyed by path -- this is meant to run once per
    process (called from CallManager.__init__), not once per call: 30
    concurrent calls must not each re-decode and re-filter the same file.

    Raises FileNotFoundError / wave.Error / ValueError on a missing or
    unsuitable file. Callers that want a soft failure (a missing prompt
    should never crash a live call) must catch and log -- see
    CallManager._load_busy_frames.
    """
    with _cache_lock:
        cached = _cache.get(wav_path)
        if cached is not None:
            return cached

        with wave.open(wav_path, "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            rate = wf.getframerate()
            pcm = wf.readframes(wf.getnframes())

        if channels != 1:
            raise ValueError(f"{wav_path}: expected mono audio, got {channels} channels")
        if sampwidth != 2:
            raise ValueError(f"{wav_path}: expected 16-bit PCM, got {sampwidth * 8}-bit")
        if rate not in (8000, 16000):
            raise ValueError(f"{wav_path}: expected 8000 or 16000 Hz, got {rate} Hz")

        if rate == 16000:
            # Fresh instance every load, not shared with any live call's
            # resampler -- OutboundResampler carries FIR overlap-save
            # history across process() calls, and that history belongs to
            # whichever single stream is feeding it.
            ulaw = OutboundResampler().process(pcm)
        else:
            ulaw = audioop.lin2ulaw(pcm, 2)

        silence_byte = audioop.lin2ulaw(b"\x00\x00", 2)
        frames = [ulaw[i:i + frame_bytes] for i in range(0, len(ulaw), frame_bytes)]
        if frames and len(frames[-1]) < frame_bytes:
            frames[-1] = frames[-1] + silence_byte * (frame_bytes - len(frames[-1]))

        _cache[wav_path] = frames
        return frames
