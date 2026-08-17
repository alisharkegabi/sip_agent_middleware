"""
Unit tests for the inbound anti-image filter.

The mirror of the outbound anti-alias problem. Going 8 kHz -> 16 kHz,
audioop.ratecv interpolates linearly and applies no reconstruction filter,
which leaves spectral IMAGES -- mirrored copies of the caller's voice above
4 kHz that were never in the signal, because an 8 kHz stream cannot carry
anything up there. Measured image rejection on the old path was only
-4.1 dB at 3400 Hz and -7.0 dB at 3000 Hz.

Linear interpolation also DROOPS the passband: the old path lost 4.1 dB at
3400 Hz and 3.1 dB at 3000 Hz relative to 500 Hz, so the caller reached STT
with the top of their voice rolled off. InboundResampler zero-stuffs and
filters instead, which fixes both.
"""
from __future__ import annotations

import math
import os
import struct
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import audioop
except ImportError:
    import audioop_lts as audioop

from audio_bridge import InboundResampler  # noqa: E402


def ulaw_tone(freq: float, secs: float = 1.0, amp: float = 0.2) -> bytes:
    n = int(8000 * secs)
    pcm = struct.pack(
        f"<{n}h",
        *(int(amp * 32767 * math.sin(2 * math.pi * freq * i / 8000)) for i in range(n)),
    )
    return audioop.lin2ulaw(pcm, 2)


def band_dbfs(pcm16: bytes, lo: float, hi: float, skip: int = 2048) -> float:
    x = np.frombuffer(pcm16, dtype="<i2").astype(np.float64)[skip:]
    N = 1 << 12
    w = np.hanning(N)
    acc = np.zeros(N // 2 + 1)
    hops = 0
    for i in range(0, len(x) - N, N // 2):
        acc += np.abs(np.fft.rfft(x[i:i + N] * w)) ** 2
        hops += 1
    assert hops, "signal too short to analyse"
    acc /= hops
    f = np.fft.rfftfreq(N, 1 / 16000)
    e = acc[(f >= lo) & (f < hi)].sum()
    return -math.inf if e <= 0 else 20 * math.log10(math.sqrt(e) / (N / 2) / 32768)


class TestImageRejection:
    """Content mirrored above 4 kHz must not reach STT."""

    @pytest.mark.parametrize("freq", [1000, 2000, 3000, 3400])
    def test_image_is_suppressed(self, freq):
        out = InboundResampler().process(ulaw_tone(freq))
        fund = band_dbfs(out, freq - 150, freq + 150)
        image = band_dbfs(out, (8000 - freq) - 150, (8000 - freq) + 150)
        assert image - fund < -40.0, (
            f"{freq} Hz leaves an image at {8000 - freq} Hz only "
            f"{fund - image:.1f} dB down"
        )

    def test_nothing_fabricated_above_4k(self):
        out = InboundResampler().process(ulaw_tone(3000))
        assert band_dbfs(out, 4000, 8000) - band_dbfs(out, 100, 4000) < -40.0


class TestPassbandIsFlat:
    """Fixes the linear-interpolation droop, and stays gain-transparent."""

    @pytest.mark.parametrize("freq", [500, 1000, 2000, 3000])
    def test_no_droop_across_the_speech_band(self, freq):
        ref = band_dbfs(InboundResampler().process(ulaw_tone(500)), 350, 650)
        got = band_dbfs(InboundResampler().process(ulaw_tone(freq)), freq - 150, freq + 150)
        assert abs(got - ref) < 1.5, f"{freq} Hz is {got - ref:+.2f} dB off 500 Hz"

    def test_droop_is_better_than_the_old_path(self):
        """The old path lost 3.1 dB at 3 kHz. Guard against regressing to it."""
        def droop(r):
            a = band_dbfs(r().process(ulaw_tone(500)), 350, 650)
            b = band_dbfs(r().process(ulaw_tone(3000)), 2850, 3150)
            return a - b

        assert droop(InboundResampler) < droop(lambda: InboundResampler(antiimage=False)) - 1.5

    def test_level_is_preserved(self):
        """_rtp_recv_loop runs its VAD on this output, so a gain change here
        would silently move the speech-detection threshold."""
        src = ulaw_tone(1000)
        before = audioop.rms(audioop.ulaw2lin(src, 2), 2)
        after = audioop.rms(InboundResampler().process(src)[4096:], 2)
        assert abs(20 * math.log10(after / before)) < 1.0


class TestStreamingContinuity:
    """Called once per 20 ms RTP packet, so state must carry across calls."""

    def test_chunked_matches_whole(self):
        src = ulaw_tone(1000)
        whole = InboundResampler().process(src)
        r = InboundResampler()
        chunked = b"".join(r.process(src[i:i + 160]) for i in range(0, len(src), 160))
        assert chunked == whole

    def test_output_is_exactly_two_samples_per_input_sample(self):
        r = InboundResampler()
        for size in (160, 160, 80, 240):
            assert len(r.process(b"\xff" * size)) == size * 4

    def test_no_discontinuity_at_packet_seams(self):
        src = ulaw_tone(1000, secs=0.5)
        r = InboundResampler()
        out = b"".join(r.process(src[i:i + 160]) for i in range(0, len(src), 160))
        v = struct.unpack(f"<{len(out) // 2}h", out)[400:]
        assert max(abs(b - a) for a, b in zip(v, v[1:])) < 4000


class TestDisabled:
    def test_bypass_is_byte_identical_to_the_original_path(self):
        src = ulaw_tone(1000, secs=0.3)
        up, _ = audioop.ratecv(audioop.ulaw2lin(src, 2), 2, 1, 8000, 16000, None)
        assert InboundResampler(antiimage=False).process(src) == up

    def test_bypass_restores_the_old_images(self):
        out = InboundResampler(antiimage=False).process(ulaw_tone(3000))
        fund = band_dbfs(out, 2850, 3150)
        assert band_dbfs(out, 4850, 5150) - fund > -15.0
