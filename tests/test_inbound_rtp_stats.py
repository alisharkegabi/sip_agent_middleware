"""
Unit tests for inbound RTP loss/jitter accounting.

The receive loop has always parsed the RTP header and thrown the sequence
number away, so inbound packet loss has never been measurable on this
system. That matters because the symptoms attributed to a noisy caller
environment -- fragmented transcripts, the agent saying the line is
unclear, choppy audio -- are equally consistent with packets never
arriving. A denoiser cannot fix a packet that was dropped, so this has to
be measured before any audio processing is designed.

Accounting follows RFC 3550: extended sequence numbers with cycle
counting, expected-minus-received for loss, and the standard smoothed
interarrival jitter estimator.

Nothing here touches audio. These are counters.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio_bridge import InboundRtpStats  # noqa: E402

# PCMU: 8000 timestamp ticks per second, 160 ticks per 20 ms frame.
CLOCK = 8000
TICKS = 160
FRAME_S = 0.020


def feed(stats, seqs, *, start_t=1000.0, jitter=None, base_seq=0):
    """Feed a sequence of packet numbers with ideal 20 ms pacing.

    `seqs` are offsets from base_seq. Arrival time and RTP timestamp are
    both derived from the offset, so a perfectly paced stream produces
    exactly zero jitter.
    """
    for i, off in enumerate(seqs):
        t = start_t + off * FRAME_S
        if jitter is not None:
            t += jitter[i]
        stats.observe(
            seq=(base_seq + off) & 0xFFFF,
            rtp_timestamp=(off * TICKS) & 0xFFFFFFFF,
            arrival_monotonic=t,
        )


class TestCleanStream:
    def test_no_loss_on_a_perfect_stream(self):
        s = InboundRtpStats()
        feed(s, range(100))
        r = s.summary()
        assert r["received"] == 100
        assert r["expected"] == 100
        assert r["lost"] == 0
        assert r["loss_pct"] == 0.0
        assert r["duplicates"] == 0
        assert r["reordered"] == 0

    def test_perfect_pacing_gives_zero_jitter(self):
        s = InboundRtpStats()
        feed(s, range(100))
        assert s.summary()["jitter_ms"] == pytest.approx(0.0, abs=1e-6)

    def test_empty_stream_summarises_without_dividing_by_zero(self):
        r = InboundRtpStats().summary()
        assert r["received"] == 0
        assert r["loss_pct"] == 0.0
        assert r["jitter_ms"] == 0.0


class TestLoss:
    def test_single_dropped_packet(self):
        s = InboundRtpStats()
        feed(s, [0, 1, 2, 4, 5])  # 3 missing
        r = s.summary()
        assert r["received"] == 5
        assert r["expected"] == 6
        assert r["lost"] == 1
        assert r["max_gap"] == 1

    def test_burst_loss_is_counted_and_bucketed(self):
        s = InboundRtpStats()
        feed(s, [0, 1, 2, 8, 9])  # 3..7 missing = burst of 5
        r = s.summary()
        assert r["lost"] == 5
        assert r["max_gap"] == 5
        # Burst profile is the point: 5 lost as one burst is a very
        # different problem from 5 lost singly.
        assert r["gap_buckets"]["3-5"] == 1
        assert r["gap_buckets"]["1"] == 0

    def test_isolated_losses_bucket_separately_from_bursts(self):
        s = InboundRtpStats()
        feed(s, [0, 2, 4, 6])  # three single-packet gaps
        r = s.summary()
        assert r["lost"] == 3
        assert r["max_gap"] == 1
        assert r["gap_buckets"]["1"] == 3
        assert r["gap_buckets"]["3-5"] == 0

    def test_loss_percentage(self):
        s = InboundRtpStats()
        feed(s, [0, 1, 2, 3, 5, 6, 7, 8, 9, 10])  # 4 missing, 11 expected
        r = s.summary()
        assert r["expected"] == 11
        assert r["received"] == 10
        assert r["loss_pct"] == pytest.approx(100.0 / 11, abs=0.01)


class TestDuplicatesAndReordering:
    def test_duplicate_is_not_counted_as_a_new_packet(self):
        s = InboundRtpStats()
        feed(s, [0, 1, 2, 2, 3])
        r = s.summary()
        assert r["duplicates"] == 1
        assert r["lost"] == 0

    def test_reordered_packet_is_recovered_not_reported_lost(self):
        s = InboundRtpStats()
        feed(s, [0, 1, 3, 2, 4])  # 2 arrives late, after 3
        r = s.summary()
        assert r["reordered"] == 1
        assert r["lost"] == 0, "a late packet still arrived; it is not lost"

    def test_loss_is_never_negative(self):
        s = InboundRtpStats()
        feed(s, [0, 1, 1, 1, 1, 2])
        assert s.summary()["lost"] >= 0


class TestSequenceWraparound:
    def test_wrap_past_65535_is_not_a_massive_loss_event(self):
        s = InboundRtpStats()
        # 65533, 65534, 65535, 0, 1, 2 -- a normal stream across the wrap
        feed(s, range(6), base_seq=65533)
        r = s.summary()
        assert r["received"] == 6
        assert r["expected"] == 6
        assert r["lost"] == 0

    def test_loss_across_the_wrap_is_still_counted(self):
        s = InboundRtpStats()
        feed(s, [0, 1, 2, 5], base_seq=65534)  # 65536(=0) and 1 missing
        r = s.summary()
        assert r["lost"] == 2
        assert r["max_gap"] == 2


class TestJitter:
    def test_late_arrivals_raise_jitter(self):
        s = InboundRtpStats()
        # Alternate +/- 10 ms around ideal pacing.
        j = [0.010 if i % 2 else -0.010 for i in range(100)]
        feed(s, range(100), jitter=j)
        assert s.summary()["jitter_ms"] > 5.0

    def test_jitter_is_reported_in_milliseconds(self):
        s = InboundRtpStats()
        j = [0.0] * 50
        j[25] = 0.050  # one 50 ms late packet
        feed(s, range(50), jitter=j)
        r = s.summary()
        # One spike, smoothed by the RFC 3550 1/16 estimator -- present but
        # well under the spike itself.
        assert 0.0 < r["jitter_ms"] < 50.0

    def test_max_interarrival_gap_is_tracked(self):
        s = InboundRtpStats()
        j = [0.0] * 20
        j[10] = 0.100  # a 100 ms stall
        feed(s, range(20), jitter=j)
        assert s.summary()["max_interarrival_ms"] > 100.0


class TestSummaryShape:
    def test_summary_is_log_safe(self):
        """No customer data may reach a log line. Counters only."""
        s = InboundRtpStats()
        feed(s, range(10))
        r = s.summary()
        for k, v in r.items():
            assert isinstance(v, (int, float, dict)), f"{k} is {type(v)}"

    def test_one_line_renders(self):
        s = InboundRtpStats()
        feed(s, [0, 1, 2, 4])
        line = s.format_line()
        assert "rtp_in" in line
        assert "loss_pct=" in line
        assert "jitter_ms=" in line
