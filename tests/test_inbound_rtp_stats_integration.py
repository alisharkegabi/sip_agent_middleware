"""
End-to-end check that inbound RTP accounting is actually wired into the
receive loop, over a real UDP socket.

The unit tests in test_inbound_rtp_stats.py prove the arithmetic. These
prove the counters are fed from the real packet path -- including the two
placement decisions that are easy to get wrong:

  1. accounting runs BEFORE the payload-type filter, so DTMF and
     comfort-noise packets are not mistaken for loss
  2. accounting survives a malformed/short packet without desyncing
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio_bridge import RtpAudioInterface  # noqa: E402

PCMU = 0
DTMF = 101
PAYLOAD = b"\xff" * 160


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def rtp_packet(seq: int, ts: int, *, pt: int = PCMU, ssrc: int = 0xDEADBEEF,
               payload: bytes = PAYLOAD) -> bytes:
    return struct.pack("!BBHII", 0x80, pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc) + payload


@pytest.fixture
def rtp(tmp_path):
    port = _free_port()
    iface = RtpAudioInterface(
        port, "127.0.0.1", _free_port(),
        call_id="rtp-stats-test", log_dir=str(tmp_path),
    )
    iface.local_port = port
    yield iface
    try:
        iface.stop()
    except Exception:
        pass


def send_all(port: int, packets, pace: float = 0.001):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for p in packets:
        s.sendto(p, ("127.0.0.1", port))
        time.sleep(pace)
    s.close()
    time.sleep(0.25)  # let the receive thread drain


class TestWiring:
    def test_clean_stream_reports_no_loss(self, rtp):
        rtp.start(lambda _pcm: None)
        send_all(rtp.local_port,
                 [rtp_packet(i, i * 160) for i in range(40)])
        s = rtp.rtp_stats()
        assert s["received"] == 40
        assert s["lost"] == 0
        assert s["loss_pct"] == 0.0

    def test_dropped_packets_are_counted_from_the_real_socket(self, rtp):
        rtp.start(lambda _pcm: None)
        seqs = [i for i in range(40) if i not in (10, 11, 12, 25)]
        send_all(rtp.local_port, [rtp_packet(i, i * 160) for i in seqs])
        s = rtp.rtp_stats()
        assert s["received"] == 36
        assert s["expected"] == 40
        assert s["lost"] == 4
        assert s["max_gap"] == 3
        assert s["gap_buckets"]["3-5"] == 1
        assert s["gap_buckets"]["1"] == 1


class TestPlacement:
    def test_dtmf_packets_do_not_read_as_loss(self, rtp):
        """RFC 2833 telephone-event packets share the sequence space. They
        are skipped as audio but must still be counted as received, or a
        caller pressing a key looks like packet loss."""
        rtp.start(lambda _pcm: None)
        packets = []
        for i in range(30):
            pt = DTMF if 10 <= i < 15 else PCMU
            packets.append(rtp_packet(i, i * 160, pt=pt, payload=b"\x00" * 4))
        send_all(rtp.local_port, packets)
        s = rtp.rtp_stats()
        assert s["received"] == 30
        assert s["lost"] == 0, "DTMF was counted as loss -- accounting is after the PT filter"

    def test_audio_still_reaches_the_callback(self, rtp):
        """The counters must not have displaced the audio path."""
        got = []
        rtp.start(lambda pcm: got.append(pcm))
        send_all(rtp.local_port, [rtp_packet(i, i * 160) for i in range(20)])
        assert len(got) == 20
        # 160 mu-law bytes in -> 320 samples of PCM16 at 16 kHz -> 640 bytes
        assert all(len(p) == 640 for p in got)

    def test_short_packet_does_not_desync_accounting(self, rtp):
        rtp.start(lambda _pcm: None)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for i in range(10):
            s.sendto(rtp_packet(i, i * 160), ("127.0.0.1", rtp.local_port))
            time.sleep(0.001)
        s.sendto(b"\x80\x00\x00", ("127.0.0.1", rtp.local_port))  # 3 bytes
        time.sleep(0.001)
        for i in range(10, 20):
            s.sendto(rtp_packet(i, i * 160), ("127.0.0.1", rtp.local_port))
            time.sleep(0.001)
        s.close()
        time.sleep(0.25)
        st = rtp.rtp_stats()
        assert st["received"] == 20
        assert st["lost"] == 0


class TestSsrcChange:
    def test_ssrc_change_resets_rather_than_reporting_huge_loss(self, rtp):
        """A re-INVITE or transfer restarts the sequence space. Without
        this the call would report tens of thousands of lost packets."""
        rtp.start(lambda _pcm: None)
        send_all(rtp.local_port,
                 [rtp_packet(i, i * 160, ssrc=0x11111111) for i in range(20)])
        send_all(rtp.local_port,
                 [rtp_packet(i, i * 160, ssrc=0x22222222) for i in range(20)])
        s = rtp.rtp_stats()
        assert s["ssrc_changes"] == 1
        assert s["lost"] == 0
        assert s["received"] == 20  # counters restarted with the new source


class TestSummaryIsLoggedExactlyOnce:
    def test_repeated_stop_logs_one_line(self, rtp, caplog):
        """stop() is reached twice on the normal path -- the SDK closes the
        audio interface from end_session(), then CallSession._cleanup()
        calls it again. Observed on a live call: the summary appeared twice,
        which would double-count in any aggregation over the service log."""
        import logging

        logger = logging.getLogger("rtp-once-test")
        logger.setLevel(logging.INFO)
        rtp._logger = logger
        rtp.start(lambda _pcm: None)
        send_all(rtp.local_port, [rtp_packet(i, i * 160) for i in range(10)])

        with caplog.at_level(logging.INFO, logger="rtp-once-test"):
            rtp.stop()
            rtp.stop()
            rtp.stop()

        lines = [r for r in caplog.records if "rtp_in" in r.getMessage()]
        assert len(lines) == 1, f"summary logged {len(lines)} times, expected 1"


class TestLogSafety:
    def test_summary_line_carries_no_payload(self, rtp):
        rtp.start(lambda _pcm: None)
        send_all(rtp.local_port, [rtp_packet(i, i * 160) for i in range(10)])
        line = rtp._rtp_stats.format_line()
        assert "rtp_in" in line
        assert "\xff" not in line
        assert all(ord(c) < 128 for c in line), "log line must stay ASCII"
