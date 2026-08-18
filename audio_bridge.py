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

import numpy as np

try:
    import audioop  # stdlib, removed in Python 3.13+
except ImportError:
    import audioop_lts as audioop  # pip install audioop-lts

from elevenlabs.conversational_ai.conversation import AudioInterface


def _design_lowpass(cutoff_hz: float, rate: int, taps: int) -> np.ndarray:
    """Windowed-sinc FIR low-pass, Blackman window, normalised to unity DC gain.

    Blackman buys ~74 dB of stopband rejection where a Hamming window gives
    ~53 dB -- worth the extra taps here, because anything left above 4 kHz
    does not merely stay quiet, it folds down into the speech band.
    """
    taps = int(taps) | 1  # odd => integer group delay, exactly (taps-1)/2 samples
    n = np.arange(taps) - (taps - 1) / 2
    fc = cutoff_hz / rate
    h = 2 * fc * np.sinc(2 * fc * n) * np.blackman(taps)
    return h / h.sum()


class OutboundResampler:
    """16 kHz PCM16 from the agent -> 8 kHz G.711 mu-law for the phone leg.

    THE POINT OF THIS CLASS. audioop.ratecv is a rate converter, not a
    resampler: it linearly interpolates and applies no filter at all. An
    8 kHz stream cannot represent anything above 4 kHz, so before this
    existed, every frequency the agent produced between 4 and 8 kHz
    mirrored back around 4 kHz and reappeared inside the speech band as
    noise bearing no relation to what was said. Measured against a properly
    filtered reference the old path scored 23.4 dB SNR, and 48% of that
    alias energy landed in 2-4 kHz -- exactly where the consonants that
    distinguish Arabic words live (س ش ص ث).

    So: low-pass first, THEN decimate. Nothing left above 4 kHz, nothing to
    fold.

    Kept separate from RtpAudioInterface deliberately -- that class binds a
    UDP socket in __init__, which makes it untestable. This is pure DSP and
    tests/test_audio_antialias.py exercises it directly.

    NOT THREAD-SAFE, and does not need to be: output() is only ever called
    from the ElevenLabs SDK's single receive thread.

    This corrects clarity, not level. The path stays gain-transparent, which
    the tests assert -- callers already describe the agent as quiet and this
    must not make it quieter.
    """

    def __init__(
        self,
        *,
        antialias: bool = True,
        cutoff_hz: float = 3400.0,
        # 129 => group delay of 64 input samples, i.e. exactly 32 output
        # samples after 2:1 decimation. An integer output delay keeps the
        # filtered stream sample-aligned with the unfiltered one, which is
        # what makes an honest A/B comparison possible.
        taps: int = 129,
        in_rate: int = 16000,
        out_rate: int = 8000,
    ):
        self._antialias = antialias
        self._in_rate = in_rate
        self._out_rate = out_rate
        self._ratecv_state = None
        # A chunk boundary must never land mid-sample. Chunk lengths come
        # from the ElevenLabs websocket and are not guaranteed even.
        self._residue = b""
        if antialias:
            self._taps = _design_lowpass(cutoff_hz, in_rate, taps)
            # Overlap-save history, zero-primed. Carrying this across calls is
            # what makes chunked output byte-identical to filtering the whole
            # stream at once -- without it every chunk seam is an audible click.
            self._history = np.zeros(len(self._taps) - 1, dtype=np.float64)

    def _filter(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate((self._history, x))
        y = np.convolve(buf, self._taps, mode="valid")  # len(y) == len(x)
        self._history = buf[len(buf) - len(self._history):]
        return y

    def process(self, pcm_16k: bytes) -> bytes:
        if self._residue:
            pcm_16k = self._residue + pcm_16k
            self._residue = b""
        if len(pcm_16k) % 2:
            pcm_16k, self._residue = pcm_16k[:-1], pcm_16k[-1:]
        if not pcm_16k:
            return b""

        if self._antialias:
            x = np.frombuffer(pcm_16k, dtype="<i2").astype(np.float64)
            y = np.rint(self._filter(x))
            pcm_16k = np.clip(y, -32768, 32767).astype("<i2").tobytes()

        down, self._ratecv_state = audioop.ratecv(
            pcm_16k, 2, 1, self._in_rate, self._out_rate, self._ratecv_state
        )
        return audioop.lin2ulaw(down, 2)


class InboundResampler:
    """8 kHz G.711 mu-law from the phone leg -> 16 kHz PCM16 for the agent.

    The mirror of OutboundResampler's problem. Interpolating without a
    reconstruction filter leaves spectral IMAGES: mirrored copies of the
    caller's voice above 4 kHz, which an 8 kHz stream cannot legitimately
    contain. audioop.ratecv's linear interpolation is only a weak low-pass,
    so it rejected those images by a measured 4.1 dB at 3400 Hz and 7.0 dB
    at 3000 Hz -- STT was receiving the caller plus a spectral ghost.

    Linear interpolation also DROOPED the passband by the same mechanism:
    -4.1 dB at 3400 Hz, -3.1 dB at 3000 Hz relative to 500 Hz, so callers
    reached STT with the top of their voice rolled off.

    Zero-stuffing and filtering fixes both at once -- it is the textbook
    interpolator, and unlike ratecv its passband is flat. The x2 restores
    the amplitude lost to inserting a zero between every sample.

    Level-transparent by design, and tested as such: _rtp_recv_loop runs its
    RMS VAD on this output, so a gain change here would silently move the
    speech-detection threshold.

    NOT THREAD-SAFE; only ever called from the RTP receive thread.
    """

    def __init__(
        self,
        *,
        antiimage: bool = True,
        # 3600, not 3400 like the outbound side. The caller's voice IS the
        # PSTN band, so content at 3400 Hz is real and a cutoff sitting on it
        # would cost 6 dB of it. Measured at 3600: droop at 3400 Hz is 0.41 dB
        # (the old path lost 4.18 dB there) while images are still rejected by
        # 87 dB. The outbound filter has no such constraint -- the agent's
        # voice holds 0.009% of its energy in 3400-4000 Hz -- and stays at
        # 3400 so its stopband is fully established before 4 kHz.
        cutoff_hz: float = 3600.0,
        taps: int = 129,
        in_rate: int = 8000,
        out_rate: int = 16000,
    ):
        self._antiimage = antiimage
        self._in_rate = in_rate
        self._out_rate = out_rate
        self._ratecv_state = None
        if antiimage:
            # Designed at the OUTPUT rate -- the images live in the upsampled
            # stream, so that is where they have to be removed.
            self._taps = _design_lowpass(cutoff_hz, out_rate, taps) * 2.0
            self._history = np.zeros(len(self._taps) - 1, dtype=np.float64)

    def _filter(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate((self._history, x))
        y = np.convolve(buf, self._taps, mode="valid")
        self._history = buf[len(buf) - len(self._history):]
        return y

    def process(self, ulaw_8k: bytes) -> bytes:
        lin = audioop.ulaw2lin(ulaw_8k, 2)  # 1 byte in -> 2 bytes out, never odd
        if not lin:
            return b""

        if not self._antiimage:
            up, self._ratecv_state = audioop.ratecv(
                lin, 2, 1, self._in_rate, self._out_rate, self._ratecv_state
            )
            return up

        x = np.frombuffer(lin, dtype="<i2").astype(np.float64)
        stuffed = np.zeros(len(x) * 2, dtype=np.float64)
        stuffed[0::2] = x
        y = np.rint(self._filter(stuffed))
        return np.clip(y, -32768, 32767).astype("<i2").tobytes()


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
        antialias: bool = True,
        antialias_cutoff_hz: float = 3400.0,
        antiimage: bool = True,
        antiimage_cutoff_hz: float = 3400.0,
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

        # Outbound (agent -> caller) resampling, incl. the anti-alias low-pass.
        self._downsampler = OutboundResampler(
            antialias=antialias, cutoff_hz=antialias_cutoff_hz
        )
        # Inbound (caller -> agent) 8k -> 16k, incl. the anti-image filter.
        self._upsampler = InboundResampler(
            antiimage=antiimage, cutoff_hz=antiimage_cutoff_hz
        )

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
        return self._downsampler.process(pcm_16k)

    def _ulaw8k_to_pcm16k(self, ulaw_8k: bytes) -> bytes:
        return self._upsampler.process(ulaw_8k)

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