"""
Unit tests for the outbound anti-alias filter.

No network, no sockets: these exercise OutboundResampler directly, which is
why the resampling was lifted out of RtpAudioInterface (that class binds a
UDP socket in __init__ and cannot be constructed in a unit test).

Background. The phone leg is G.711 at 8 kHz, so it can only represent
frequencies below 4 kHz. The agent speaks at 16 kHz. audioop.ratecv does
linear interpolation and NO filtering, so before this filter existed
everything the agent produced between 4 and 8 kHz mirrored back around
4 kHz and landed on top of the speech as noise -- measured at 23.4 dB SNR
against a filtered reference, with 48% of the alias energy in the 2-4 kHz
consonant band.
"""
from __future__ import annotations

import math
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import audioop  # noqa: F401
except ImportError:
    import audioop_lts as audioop  # noqa: F401

from audio_bridge import OutboundResampler  # noqa: E402

RATE = 16000


def tone(freq: float, secs: float = 0.5, amp: float = 0.2, rate: int = RATE) -> bytes:
    n = int(rate * secs)
    return struct.pack(
        f"<{n}h",
        *(int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)),
    )


def dbfs(rms: float) -> float:
    return -math.inf if rms <= 0 else 20 * math.log10(rms / 32768.0)


def out_dbfs(resampler: OutboundResampler, pcm: bytes, *, skip_ms: int = 40) -> float:
    """Level after the full outbound path, mu-law decoded back to linear.

    The first `skip_ms` is dropped: an FIR starts with a zero-filled history,
    so the leading samples are a ramp, not steady state.
    """
    lin = audioop.ulaw2lin(resampler.process(pcm), 2)
    return dbfs(audioop.rms(lin[skip_ms * 8 * 2:], 2))


class TestAliasRejection:
    """The whole point: content above 4 kHz must not survive decimation."""

    @pytest.mark.parametrize("freq", [4500, 5000, 6000, 7000])
    def test_above_nyquist_is_rejected(self, freq):
        level = out_dbfs(OutboundResampler(), tone(freq))
        assert level < -55.0, (
            f"{freq} Hz came through at {level:.1f} dBFS -- it should be filtered "
            f"out, otherwise it aliases to {abs(8000 - freq)} Hz"
        )

    def test_alias_would_land_in_the_consonant_band(self):
        """7 kHz folds to 1 kHz. Regression guard for the original bug, where
        this tone arrived at full level having moved 6 kHz down the spectrum."""
        src = tone(7000)
        assert out_dbfs(OutboundResampler(), src) < dbfs(audioop.rms(src, 2)) - 40.0


class TestPassbandIsIntact:
    """Filtering must not cost level or dull the speech band."""

    @pytest.mark.parametrize("freq", [300, 500, 1000, 2000])
    def test_speech_band_passes_unattenuated(self, freq):
        src = tone(freq)
        loss = dbfs(audioop.rms(src, 2)) - out_dbfs(OutboundResampler(), src)
        assert abs(loss) < 1.5, f"{freq} Hz lost {loss:.2f} dB in the passband"

    def test_no_broadband_gain_change(self):
        """The path stays level-transparent -- callers already report the
        agent as quiet, so this fix must not make it quieter still."""
        src = tone(1000, amp=0.5)
        loss = dbfs(audioop.rms(src, 2)) - out_dbfs(OutboundResampler(), src)
        assert abs(loss) < 1.0


class TestStreamingContinuity:
    """output() is called once per websocket chunk, so filter state must
    carry across calls or every chunk boundary is an audible click."""

    def test_chunked_matches_whole(self):
        src = tone(1000, secs=1.0)
        whole = OutboundResampler().process(src)

        chunked = OutboundResampler()
        pieces = [chunked.process(src[i:i + 4096]) for i in range(0, len(src), 4096)]
        assert b"".join(pieces) == whole, "chunk boundaries change the output"

    def test_odd_sized_chunks_do_not_desync(self):
        """Chunk lengths are server-chosen and need not be even byte counts
        of whole samples; a stray odd byte must not shift every later sample."""
        src = tone(1000, secs=1.0)
        r = OutboundResampler()
        out = b"".join(r.process(src[i:i + 999]) for i in range(0, len(src), 999))
        assert abs(len(out) - len(OutboundResampler().process(src))) <= 2

    def test_no_discontinuity_at_chunk_seams(self):
        """A steady tone split across chunks must not produce a sample-to-sample
        jump larger than the tone's own slew rate."""
        src = tone(1000, secs=0.5)
        r = OutboundResampler()
        out = b"".join(r.process(src[i:i + 3200]) for i in range(0, len(src), 3200))
        lin = audioop.ulaw2lin(out, 2)
        vals = struct.unpack(f"<{len(lin) // 2}h", lin)[80:]
        jumps = [abs(b - a) for a, b in zip(vals, vals[1:])]
        assert max(jumps) < 6000, f"discontinuity of {max(jumps)} at a chunk seam"


class TestDisabled:
    """The filter is behind a flag so it can be A/B'd on real calls."""

    def test_bypass_restores_old_behaviour(self):
        level = out_dbfs(OutboundResampler(antialias=False), tone(7000))
        assert level > -30.0, "with the filter off, 7 kHz should alias through"

    def test_bypass_is_byte_identical_to_the_original_path(self):
        src = tone(1000, secs=0.3)
        down, _ = audioop.ratecv(src, 2, 1, 16000, 8000, None)
        assert OutboundResampler(antialias=False).process(src) == audioop.lin2ulaw(down, 2)
