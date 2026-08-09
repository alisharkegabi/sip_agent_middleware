"""
ElevenLabs <-> RTP audio bridge.

Per Rule 1 of the work order: the G.711 mu-law <-> PCM16k transcoding,
audioop.ratecv state handling, 20ms/160-byte framing, RMS VAD threshold
logic, and the 4-stage latency math are UNCHANGED and must produce
identical values to the reference implementation. Only robustness,
lifecycle, and I/O-placement issues below were touched.

What changed here (see PRODUCTION_HARDENING_WORK_ORDER.md):
  - F-07: the receive loop no longer dies for the rest of the call on the
    first transient error.
  - F-08: `except Exception: break` -> log-and-continue; only a closed
    socket or is_running=False ends the loop. On Windows, ICMP
    port-unreachable surfaces as WSAECONNRESET on a connectionless UDP
    socket -- explicitly suppressed via SIO_UDP_CONNRESET.
  - F-09: start() is now idempotent so CallSession can start RTP
    transmission (silence frames) immediately after ACK, before the
    ElevenLabs SDK later calls start() itself from inside start_session().
  - F-10: output() computes the latency numbers under the lock, then
    releases the lock BEFORE doing any file I/O -- the previous code held
    _latency_lock across a blocking open()/write()/close(), which under
    Windows Defender real-time scanning cost tens of milliseconds on the
    TTS delivery thread, once per turn, every call.
  - F-12: the RTP transmit destination can now be retargeted after
    construction (`retarget()`), for when the SDP media address differs
    from the PBX signalling IP, and the receive loop implements symmetric
    RTP / latching: if inbound RTP arrives from somewhere other than the
    configured destination, the transmit destination switches to match.
  - F-13: proper RTP header parsing (version/CC/extension/padding) and a
    payload-type filter -- only PT 0 (PCMU) is decoded as audio; RFC 2833
    telephone-event and comfort-noise packets are no longer fed to STT as
    mu-law garbage.
  - last_rx_monotonic is exposed for CallSession's RTP-inactivity watchdog
    (F-02).
"""
from __future__ import annotations

import os
import socket
import struct
import threading
import time
from collections import deque
from typing import Callable, Optional

try:
    import audioop  # stdlib, removed in Python 3.13+
except ImportError:
    import audioop_lts as audioop  # pip install audioop-lts

from elevenlabs.conversational_ai.conversation import AudioInterface


class RtpAudioInterface(AudioInterface):
    def __init__(
        self,
        local_port: int,
        remote_ip: str,
        remote_port: int,
        *,
        call_id: str,
        frame_ms: int = 20,
        frame_bytes: int = 160,
        log_dir: str = "./logs",
        rms_threshold: int = 500,
        on_turn_latency: Optional[Callable[[dict], None]] = None,
        logger=None,
    ):
        self.call_id = call_id
        self.remote_ip = remote_ip
        self.remote_port = remote_port
        self._latched = False  # True once symmetric-RTP has retargeted us
        self.is_running = False
        self._started = False  # F-09: guards idempotent start()
        self.input_callback = None
        self._frame_ms = frame_ms
        self._frame_bytes = frame_bytes
        self._on_turn_latency = on_turn_latency
        self._logger = logger
        self.last_rx_monotonic: Optional[float] = None  # F-02 watchdog hook

        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if os.name == "nt":
            # F-16B: on Windows, SO_REUSEADDR permits a DIFFERENT socket to
            # bind the same port and steal traffic (unlike POSIX semantics).
            # SO_EXCLUSIVEADDRUSE is the correct flag for exclusive
            # ownership of an RTP port.
            self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            # F-08: suppress WSAECONNRESET from ICMP port-unreachable on a
            # connectionless socket -- otherwise a stray ICMP kills recvfrom
            # with an exception that looks like a real transport error.
            # NOTE: Python's socket.ioctl() only accepts SIO_RCVALL /
            # SIO_KEEPALIVE_VALS / SIO_LOOPBACK_FAST_PATH -- it rejects any
            # other control code with ValueError("invalid ioctl command"),
            # SIO_UDP_CONNRESET included. Call WSAIoctl directly via ctypes
            # instead of going through that restricted wrapper.
            try:
                import ctypes

                SIO_UDP_CONNRESET = 0x9800000C
                in_buf = ctypes.c_bool(False)
                bytes_returned = ctypes.c_ulong(0)
                ret = ctypes.windll.ws2_32.WSAIoctl(
                    self.udp_sock.fileno(),
                    SIO_UDP_CONNRESET,
                    ctypes.byref(in_buf), ctypes.sizeof(in_buf),
                    None, 0,
                    ctypes.byref(bytes_returned),
                    None, None,
                )
                if ret != 0 and self._logger:
                    self._logger.warning(
                        f"WSAIoctl(SIO_UDP_CONNRESET) returned {ret}; "
                        f"WSAGetLastError={ctypes.windll.ws2_32.WSAGetLastError()}"
                    )
            except Exception:
                # Never let this best-effort Windows quirk-fix take down
                # call setup; the F-08 exception guard in _rtp_recv_loop
                # still protects against ECONNRESET even if this fails.
                if self._logger:
                    self._logger.exception("failed to set SIO_UDP_CONNRESET, continuing without it")
        else:
            self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp_sock.bind(("0.0.0.0", local_port))
        self.udp_sock.settimeout(0.5)

        self.seq_num = 0
        self.timestamp = 0
        self.ssrc = int.from_bytes(os.urandom(4), "big")  # unique per call, was a fixed constant
        self._ulaw_silence_byte = audioop.lin2ulaw(b"\x00\x00", 2)

        self._play_queue = deque()
        self._play_cv = threading.Condition()

        self._down_state = None
        self._up_state = None

        # --- End-to-End Latency Instrumentation State (per-call, unchanged logic) ---
        self._turn_number = 0
        self._utterance_start = None
        self._stt_time = None
        self._llm_time = None
        self._awaiting_agent = False
        self._latency_lock = threading.Lock()
        self._rms_threshold = rms_threshold
        os.makedirs(log_dir, exist_ok=True)
        self._latency_log_path = os.path.join(log_dir, f"{call_id}.latency.log")
        # F-10: open the handle once and keep it, instead of open/write/close
        # per turn while holding _latency_lock.
        self._latency_log_fh = open(self._latency_log_path, "a", encoding="utf-8")
        # ------------------------------------------------------------------------

    def retarget(self, remote_ip: str, remote_port: int) -> None:
        """F-12: point RTP transmission at the address from the answer SDP's
        `c=` line (which may be a media server/SBC distinct from the PBX
        signalling IP), instead of the hard-coded PBX IP."""
        self.remote_ip = remote_ip
        self.remote_port = remote_port
        self._latched = False

    def _pcm16k_to_ulaw8k(self, pcm_16k: bytes) -> bytes:
        down, self._down_state = audioop.ratecv(pcm_16k, 2, 1, 16000, 8000, self._down_state)
        return audioop.lin2ulaw(down, 2)

    def _ulaw8k_to_pcm16k(self, ulaw_8k: bytes) -> bytes:
        lin = audioop.ulaw2lin(ulaw_8k, 2)
        up, self._up_state = audioop.ratecv(lin, 2, 1, 8000, 16000, self._up_state)
        return up

    def start(self, input_callback: Callable[[bytes], None]):
        """F-09: idempotent. CallSession calls this immediately after ACK so
        silence frames flow right away (no dead air while ElevenLabs' own
        start_session() is still in flight); the SDK's later call to
        start() with the real callback just wires up input_callback without
        spawning duplicate threads."""
        self.input_callback = input_callback
        if self._started:
            return
        self._started = True
        self.is_running = True
        threading.Thread(target=self._rtp_recv_loop, daemon=True, name=f"rtp-recv-{self.call_id}").start()
        threading.Thread(target=self._rtp_send_loop, daemon=True, name=f"rtp-send-{self.call_id}").start()

    def stop(self):
        self.is_running = False
        with self._play_cv:
            self._play_cv.notify_all()
        try:
            self.udp_sock.close()
        except Exception:
            pass
        try:
            self._latency_log_fh.close()
        except Exception:
            pass

    # --- Callbacks for intermediate latency tracking ---
    def record_stt_complete(self):
        """Called when the user's transcript is finalized by the SDK."""
        with self._latency_lock:
            if self._awaiting_agent and self._stt_time is None:
                self._stt_time = time.perf_counter()

    def record_llm_first_text(self):
        """Called when the first piece of text arrives from the LLM."""
        with self._latency_lock:
            if self._awaiting_agent and self._llm_time is None:
                self._llm_time = time.perf_counter()
    # ---------------------------------------------------

    def output(self, audio: bytes):
        """Receives TTS audio chunks from ElevenLabs."""

        # --- Latency Instrumentation (compute under lock, write off lock) ---
        log_entry = None
        latency_payload = None
        with self._latency_lock:
            if self._awaiting_agent and self._utterance_start is not None:
                tts_time = time.perf_counter()
                self._turn_number += 1

                t0 = self._utterance_start
                t1 = self._stt_time if self._stt_time else t0
                t2 = self._llm_time if self._llm_time else t1
                t3 = tts_time

                total_latency = (t3 - t0) * 1000
                stt_latency = (t1 - t0) * 1000 if self._stt_time else 0
                llm_latency = (t2 - t1) * 1000 if self._llm_time else 0
                tts_latency = (t3 - t2) * 1000 if self._llm_time or self._stt_time else 0

                log_entry = (
                    f"\n========== [{self.call_id}] TURN #{self._turn_number} END-TO-END DELAY ==========\n"
                    f"1. VAD Audio Detected : 0 ms (Base: {t0:.4f})\n"
                )
                if self._stt_time:
                    log_entry += f"2. User STT Complete  : +{stt_latency:.0f} ms\n"
                if self._llm_time:
                    log_entry += f"3. Agent LLM Reply    : +{llm_latency:.0f} ms\n"

                log_entry += (
                    f"4. Agent TTS Audio    : +{tts_latency:.0f} ms\n"
                    f"--------------------------------------------------\n"
                    f"Total Turn Latency    : {total_latency:.0f} ms\n"
                    f"====================================================\n"
                )

                latency_payload = {
                    "turn": self._turn_number,
                    "total_ms": round(total_latency),
                    "stt_ms": round(stt_latency),
                    "llm_ms": round(llm_latency),
                    "tts_ms": round(tts_latency),
                }

                # Reset state for the next conversational turn
                self._awaiting_agent = False
                self._utterance_start = None
                self._stt_time = None
                self._llm_time = None
        # ------------------------------------------------------------------
        # F-10: everything below runs OUTSIDE _latency_lock. _rtp_recv_loop
        # only needs the lock briefly to flip _awaiting_agent/_utterance_start
        # at utterance start, so releasing it here removes the audio-thread
        # stall that file I/O (and, on Windows with Defender scanning, a
        # fresh open()) used to cause once per turn.
        if log_entry is not None:
            if self._logger:
                self._logger.info(
                    "turn_latency",
                    extra={"call_id": self.call_id, "turn": latency_payload["turn"],
                           "total_ms": latency_payload["total_ms"]},
                )
            try:
                self._latency_log_fh.write(log_entry)
                self._latency_log_fh.flush()
            except Exception:
                pass
            if self._on_turn_latency:
                try:
                    self._on_turn_latency(latency_payload)
                except Exception:
                    pass

        if not self.is_running or not self.remote_port:
            return
        payload = self._pcm16k_to_ulaw8k(audio)
        frames = [payload[i:i + self._frame_bytes] for i in range(0, len(payload), self._frame_bytes)]
        with self._play_cv:
            self._play_queue.extend(frames)
            self._play_cv.notify()

    def interrupt(self):
        with self._play_cv:
            self._play_queue.clear()

    def _rtp_send_loop(self):
        next_send = time.monotonic()
        while self.is_running:
            try:
                with self._play_cv:
                    if self._play_queue:
                        chunk = self._play_queue.popleft()
                    else:
                        chunk = None
                        # Don't block indefinitely when there's nothing to play.
                        # RTP must keep flowing at a steady cadence even during
                        # silence between/after turns -- many SBCs/PBXs treat a
                        # gap in the RTP stream as the call having dropped,
                        # which is heard as an abrupt "line closed" cut right
                        # after the agent stops speaking. Wait at most one
                        # frame interval so we can emit a silence frame on
                        # schedule instead.
                        self._play_cv.wait(timeout=self._frame_ms / 1000)
                        if self._play_queue:
                            chunk = self._play_queue.popleft()

                    if not self.is_running:
                        return

                if chunk is None:
                    # Comfort/silence frame -- keeps the RTP stream continuous.
                    chunk = self._ulaw_silence_byte * self._frame_bytes
                elif len(chunk) < self._frame_bytes:
                    chunk = chunk + self._ulaw_silence_byte * (self._frame_bytes - len(chunk))

                header = struct.pack("!BBHII", 0x80, 0x00, self.seq_num, self.timestamp, self.ssrc)
                try:
                    if self.remote_port:
                        self.udp_sock.sendto(header + chunk, (self.remote_ip, self.remote_port))
                except Exception:
                    pass

                self.seq_num = (self.seq_num + 1) & 0xFFFF
                self.timestamp = (self.timestamp + self._frame_bytes) & 0xFFFFFFFF

                next_send += self._frame_ms / 1000
                sleep_for = next_send - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_send = time.monotonic()
            except Exception:
                # F-07: a transient error in the pacing loop must not kill
                # RTP transmission for the rest of the call.
                if self._logger:
                    self._logger.exception("rtp send loop error, continuing")
                next_send = time.monotonic()
                continue

    def _rtp_recv_loop(self):
        while self.is_running:
            try:
                data, addr = self.udp_sock.recvfrom(2048)
                self.last_rx_monotonic = time.monotonic()

                # F-12: symmetric RTP / latching. If the far end is sending
                # from an address other than the one the SDP told us, and
                # we haven't latched yet, switch our transmit destination to
                # match the observed source -- standard NAT-traversal
                # behaviour, and required whenever a NAT/ngrok sits in the
                # media path.
                src_ip, src_port = addr
                if not self._latched and (src_ip != self.remote_ip or src_port != self.remote_port):
                    self.remote_ip, self.remote_port = src_ip, src_port
                    self._latched = True
                    if self._logger:
                        self._logger.info(f"RTP latched to observed source {src_ip}:{src_port}")

                # F-13: parse the RTP header properly instead of assuming a
                # bare 12-byte header with no CSRCs/extension/padding, and
                # only decode PT 0 (PCMU) as audio. RFC 2833 telephone-event
                # (commonly PT 101) and comfort-noise (PT 13) packets are
                # skipped rather than fed to STT as mu-law garbage.
                if len(data) < 12:
                    continue
                b0, b1 = data[0], data[1]
                version = (b0 >> 6) & 0x03
                padding = bool(b0 & 0x20)
                extension = bool(b0 & 0x10)
                cc = b0 & 0x0F
                payload_type = b1 & 0x7F

                if version != 2:
                    continue

                offset = 12 + (cc * 4)
                if extension:
                    if len(data) < offset + 4:
                        continue
                    ext_len_words = struct.unpack("!H", data[offset + 2:offset + 4])[0]
                    offset += 4 + (ext_len_words * 4)

                if len(data) < offset:
                    continue

                payload = data[offset:]
                if padding and payload:
                    pad_len = payload[-1]
                    if 0 < pad_len <= len(payload):
                        payload = payload[:-pad_len]

                if payload_type != 0:
                    # Not PCMU -- e.g. RFC 2833 DTMF or comfort noise.
                    # Deliberately not decoded as speech audio.
                    continue

                pcm_payload = self._ulaw8k_to_pcm16k(payload)

                # --- Latency Instrumentation (Detect User Speaking) ---
                if not self._awaiting_agent:
                    rms = audioop.rms(pcm_payload, 2)
                    if rms > self._rms_threshold:
                        with self._latency_lock:
                            if not self._awaiting_agent:
                                self._utterance_start = time.perf_counter()
                                self._awaiting_agent = True
                # ------------------------------------------------------

                if self.input_callback:
                    self.input_callback(pcm_payload)
            except socket.timeout:
                continue
            except OSError:
                # Socket closed (normal shutdown path) -- exit quietly.
                if not self.is_running:
                    break
                if self._logger:
                    self._logger.warning("rtp recv OSError, continuing")
                continue
            except Exception:
                # F-08: previously `except Exception: break` -- one decode
                # error or one WSAECONNRESET (common on Windows UDP sockets
                # after an ICMP port-unreachable) silently ended inbound
                # audio for the rest of the call while everything else kept
                # running, i.e. one-way audio nobody noticed. Log and keep
                # listening; only is_running=False or a closed socket ends
                # this loop.
                if self._logger:
                    self._logger.exception("rtp recv loop error, continuing")
                continue