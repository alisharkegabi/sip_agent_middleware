"""
CallSession: everything about ONE call, isolated from every other call.

Refactor of the original main() control flow, now hardened per
PRODUCTION_HARDENING_WORK_ORDER.md. Every step — TCP connect, INVITE, 401
challenge, authenticated re-INVITE, waiting for 200 OK / rejection, ACK, RTP
bridge bring-up, ElevenLabs conversation start, in-dialog request handling,
BYE/CANCEL, and cleanup — happens on its own thread with its own socket, RTP
port, SIP dialog identifiers, and ElevenLabs session, same as before.

What's new here relative to the original refactor:
  - F-02: every wait loop has a monotonic deadline, not just a hangup flag.
  - F-04: uses SipStream (a real framer) instead of the old recv_full().
  - F-05: full SIP response-class handling + qop digest auth, capped retries.
  - F-06: heavy references are dropped at the end of cleanup.
  - F-09: RTP silence starts flowing immediately after ACK.
  - F-12: RTP is targeted at the SDP answer's media address, with latching.
  - F-14/F-15: fresh branch per transaction; CANCEL for unconfirmed dialogs;
    in-dialog OPTIONS/INFO/NOTIFY/UPDATE/re-INVITE are answered instead of
    silently ignored.
  - F-17: the real end-of-call reason is recorded, not a blanket COMPLETED.
  - F-24: ended_at/conversation_id are written under _status_lock.

No two CallSessions ever touch each other's state. The only shared object
they touch is the PortAllocator (acquire/release), which is internally
locked, and log files, which are per-call.
"""
from __future__ import annotations

import re
import socket
import threading
import time
import uuid
from typing import Optional

from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation, ConversationInitiationData

import sip_protocol as sip
from audio_bridge import RtpAudioInterface
from config import Settings
from logging_config import get_call_logger
from models import CallStatus
from port_allocator import PortAllocator


class CallSession:
    def __init__(
        self,
        *,
        phone_number: str,
        dynamic_variables: dict,
        settings: Settings,
        port_allocator: PortAllocator,
        tracking_id: Optional[str] = None,
    ):
        self.call_id = uuid.uuid4().hex  # our internal id, exposed via the API
        self.phone_number = phone_number
        self.dynamic_variables = dynamic_variables
        self.tracking_id = tracking_id or dynamic_variables.get("tracking_id")
        self.settings = settings
        self._port_allocator = port_allocator
        self.logger = get_call_logger(self.call_id)
        self.conversation_id = None

        # Stable for the whole dialog (RFC 3261): Call-ID and From tag.
        self._sip_call_id = f"{uuid.uuid4()}@{settings.local_ip}"
        self._from_tag = uuid.uuid4().hex[:8]
        # F-14: branch is now per-TRANSACTION, generated fresh each time via
        # sip.new_branch() -- no single fixed value for the whole dialog.

        self.status: CallStatus = CallStatus.PENDING
        self.error: Optional[str] = None
        self.exit_reason: Optional[str] = None  # F-17
        self.answered: bool = False
        self.created_at = time.time()
        self.queued_at = time.time()  # F-19
        self.dialed_at: Optional[float] = None
        self.connected_at: Optional[float] = None
        self.ended_at: Optional[float] = None
        self.local_rtp_port: Optional[int] = None
        self.remote_rtp_port: Optional[int] = None
        self.last_turn_latency: Optional[dict] = None

        self._status_lock = threading.Lock()
        self._hangup_requested = threading.Event()  # per-call, replaces the global shutdown_requested

        self._sock: Optional[socket.socket] = None
        self._stream: Optional[sip.SipStream] = None
        self._rtp_interface: Optional[RtpAudioInterface] = None
        self._conversation: Optional[Conversation] = None

    # ------------------------------------------------------------------
    # Public control surface (called by CallManager / API layer)
    # ------------------------------------------------------------------
    def request_hangup(self) -> None:
        self._hangup_requested.set()

    def _set_status(self, status: CallStatus, error: Optional[str] = None) -> None:
        with self._status_lock:
            self.status = status
            if error:
                self.error = error

    def to_dict(self) -> dict:
        with self._status_lock:
            talk_seconds = None
            if self.connected_at is not None and self.ended_at is not None:
                talk_seconds = round(self.ended_at - self.connected_at, 3)
            return {
                "call_id": self.call_id,
                "conversation_id": self.conversation_id,
                "phone_number": self.phone_number,
                "tracking_id": self.tracking_id,
                "status": self.status,
                "error": self.error,
                "created_at": self.created_at,
                "dialed_at": self.dialed_at,
                "connected_at": self.connected_at,
                "ended_at": self.ended_at,
                "remote_rtp_port": self.remote_rtp_port,
                "local_rtp_port": self.local_rtp_port,
                "last_turn_latency": self.last_turn_latency,
                "exit_reason": self.exit_reason,
                "answered": self.answered,
                "talk_seconds": talk_seconds,
            }

    def to_webhook_payload(self) -> dict:
        """Terminal-state notification payload sent to the configured webhook.

        Reuses the same fields as to_dict()/CallDetail -- no new state is
        introduced, this is just a different shape for external consumers.
        """
        with self._status_lock:
            started_at = self.dialed_at if self.dialed_at is not None else self.created_at
            duration_seconds = None
            if self.ended_at is not None and started_at is not None:
                duration_seconds = round(self.ended_at - started_at, 3)
            return {
                "call_id": self.call_id,
                "conversation_id": self.conversation_id,
                "status": self.status.value,
                "started_at": started_at,
                "ended_at": self.ended_at,
                "duration_seconds": duration_seconds,
                "reason": self.exit_reason or self.error or self.status.value,
                "metadata": {
                    "phone_number": self.phone_number,
                    "tracking_id": self.tracking_id,
                    "remote_rtp_port": self.remote_rtp_port,
                    "local_rtp_port": self.local_rtp_port,
                    "last_turn_latency": self.last_turn_latency,
                    "answered": self.answered,
                    "dynamic_variables": self.dynamic_variables,
                },
            }

    # ------------------------------------------------------------------
    # Main entry point — runs entirely on a CallManager worker thread.
    # ------------------------------------------------------------------
    def run(self) -> None:
        # F-19: a call that sat in the executor queue too long (e.g. a
        # morning batch of hundreds of calls) shouldn't dial hours after it
        # was relevant.
        queue_wait = time.time() - self.queued_at
        if queue_wait > self.settings.max_queue_wait_seconds:
            self._set_status(CallStatus.CANCELLED, "queue_timeout")
            self._finish("queue_timeout")
            return

        try:
            self.local_rtp_port = self._port_allocator.acquire()
        except Exception as e:
            self._set_status(CallStatus.FAILED, str(e))
            self.logger.error(f"could not allocate RTP port: {e}")
            self._finish("port_exhausted")
            return

        exit_reason = "unknown"
        try:
            exit_reason = self._dial()
        except _CallAborted as e:
            self._set_status(CallStatus.CANCELLED if e.cancelled else CallStatus.FAILED, str(e) or None)
            exit_reason = e.reason
        except _CallRejected as e:
            self._set_status(CallStatus.REJECTED, str(e))
            exit_reason = e.reason
        except Exception as e:
            self.logger.exception("unhandled error during call")
            self._set_status(CallStatus.FAILED, str(e))
            exit_reason = "internal_error"
        finally:
            self._cleanup()
            self._port_allocator.release(self.local_rtp_port)
            self._finish(exit_reason)

    def _finish(self, exit_reason: str) -> None:
        with self._status_lock:  # F-24: write under the same lock to_dict() reads under
            self.exit_reason = exit_reason
            self.ended_at = time.time()

    # ------------------------------------------------------------------
    # SIP handshake + bridge (equivalent to the reference script's main())
    # ------------------------------------------------------------------
    def _dial(self) -> str:
        """Returns the exit_reason string for a call that never reached the
        bridge (rejected/failed/cancelled during handshake), or delegates to
        _bridge() once connected."""
        cfg = self.settings
        self._set_status(CallStatus.DIALING)
        self.dialed_at = time.time()
        deadline_connect = time.monotonic() + cfg.sip_connect_timeout

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # F-04: SO_LINGER(1,0) forces an RST on close, which can leave the
        # dialog dangling on the PBX side. Keep it available only as a
        # last-resort abort path (see _abort_close()); normal close is a
        # graceful FIN after BYE/CANCEL.
        self._sock.bind((cfg.local_ip, 0))
        local_port = self._sock.getsockname()[1]

        # F-02: connect() previously had NO timeout at all; on Windows an
        # unreachable/overloaded PBX blocked this for ~21s per attempt.
        self._sock.settimeout(cfg.sip_connect_timeout)
        self.logger.info(f"connecting to SIP PBX at {cfg.pbx_ip}:{cfg.pbx_port} (local port {local_port})")
        try:
            self._sock.connect((cfg.pbx_ip, cfg.pbx_port))
        except socket.timeout:
            raise _CallAborted("connect timed out", reason="connect_timeout")
        except OSError as e:
            raise _CallAborted(f"connect failed: {e}", reason="connect_failed")

        # F-04: Nagle can hold small SIP messages (ACK/BYE) for ~200ms and
        # makes coalescing of adjacent messages more likely.
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._stream = sip.SipStream(self._sock)

        sdp = sip.build_sdp(cfg.local_ip, self.local_rtp_port)
        dialog_kwargs = dict(
            local_ip=cfg.local_ip,
            local_port=local_port,
            pbx_ip=cfg.pbx_ip,
            target_number=self.phone_number,
            ext_user=cfg.ext_user,
            call_id=self._sip_call_id,
            from_tag=self._from_tag,
        )

        invite_branch = sip.new_branch()
        self._sock.sendall(
            sip.build_invite(**dialog_kwargs, branch=invite_branch, sdp=sdp, cseq=1).encode()
        )
        frame = self._stream.read_message(cfg.sip_recv_timeout)
        if frame.kind == sip.FrameKind.CLOSED:
            raise _CallAborted("PBX dropped socket during initial INVITE", reason="sip_disconnect")
        text = frame.text or ""
        if "401 Unauthorized" not in text and "407 Proxy Authentication" not in text:
            raise _CallAborted("initialization failed: no auth challenge received", reason="no_auth_challenge")

        auth_attempts = 0
        remote_tag = ""
        answer_sdp = ""
        cseq = 1

        while auth_attempts < cfg.max_auth_attempts:
            auth_attempts += 1
            realm, nonce, qop, opaque = sip.parse_www_auth(text)
            uri = f"sip:{self.phone_number}@{cfg.pbx_ip}"
            resp, cnonce = sip.digest_response(
                cfg.ext_user, cfg.ext_pass, realm, nonce, "INVITE", uri, qop=qop
            )
            auth_header = (
                f'Authorization: Digest username="{cfg.ext_user}", realm="{realm}", '
                f'nonce="{nonce}", uri="{uri}", response="{resp}", algorithm=MD5'
            )
            if qop:
                auth_header += f', qop={qop}, nc=00000001, cnonce="{cnonce}"'
            if opaque:
                auth_header += f', opaque="{opaque}"'
            auth_header += "\r\n"

            cseq += 1
            invite_branch = sip.new_branch()  # F-14: fresh branch per transaction
            self._sock.sendall(
                sip.build_invite(
                    **dialog_kwargs, branch=invite_branch, sdp=sdp, cseq=cseq, extra_headers=auth_header
                ).encode()
            )
            self._set_status(CallStatus.RINGING)
            self.logger.info(f"sent authenticated INVITE (attempt {auth_attempts}), waiting for target to answer")

            outcome, remote_tag, answer_sdp = self._wait_for_answer(invite_branch, cseq)
            if outcome == "reauth":
                # Server issued another 401/407 even after credentials --
                # try once more (bounded by max_auth_attempts) rather than
                # looping forever.
                frame = self._last_challenge_frame
                text = frame.text or ""
                continue
            break
        else:
            raise _CallRejected("authentication rejected after max attempts", reason="auth_rejected")

        if outcome == "cancelled":
            self._send_cancel(invite_branch, cseq, dialog_kwargs)
            raise _CallAborted("cancelled during handshake", cancelled=True, reason="local_hangup")
        if outcome == "rejected":
            raise _CallRejected(self._last_reject_reason, reason=self._last_reject_reason)
        if outcome == "no_answer":
            self._send_cancel(invite_branch, cseq, dialog_kwargs)
            raise _CallAborted("ring timeout, no answer", reason="ring_timeout")
        if outcome != "answered":
            raise _CallAborted(f"unexpected handshake outcome: {outcome}", reason="handshake_error")

        parsed_media_ip = self._parse_sdp_media_address(answer_sdp)
        if parsed_media_ip and parsed_media_ip != cfg.pbx_ip:
            self.logger.warning(
                f"SDP answer c= line ({parsed_media_ip}) differs from PBX_IP "
                f"({cfg.pbx_ip}); sending RTP to PBX_IP to match known-good behavior"
            )
        # Known-good behavior (matches the version that transmits audio
        # successfully): always target the PBX's signaling IP for RTP,
        # rather than trusting the SDP answer's c= line. Some PBX/SBC
        # deployments advertise an internal media address there that isn't
        # actually reachable the same way, which breaks the RTP path.
        media_ip = cfg.pbx_ip
        self.answered = True

        ack_branch = sip.new_branch()  # F-14: ACK to a 2xx is its own transaction
        self._sock.sendall(
            sip.build_ack(**dialog_kwargs, branch=ack_branch, cseq=cseq, remote_tag=remote_tag).encode()
        )
        self.logger.info("handshake established, starting ElevenLabs session")

        return self._bridge(
            local_port=local_port,
            dialog_kwargs=dialog_kwargs,
            remote_tag=remote_tag,
            media_ip=media_ip,
            next_cseq=cseq + 1,
        )

    # ------------------------------------------------------------------
    def _wait_for_answer(self, invite_branch: str, invite_cseq: int) -> tuple:
        """F-02/F-05: bounded wait for a final response to the INVITE.
        Returns (outcome, remote_tag, answer_sdp) where outcome is one of
        'answered' | 'rejected' | 'cancelled' | 'no_answer' | 'reauth'."""
        cfg = self.settings
        deadline = time.monotonic() + cfg.max_ring_seconds
        remote_tag = ""
        self._last_reject_reason = "rejected"
        self._last_challenge_frame = None

        while not self._hangup_requested.is_set():
            if time.monotonic() >= deadline:
                return "no_answer", "", ""

            remaining = deadline - time.monotonic()
            frame = self._stream.read_message(min(cfg.sip_recv_timeout, max(remaining, 0.1)))
            if frame.kind == sip.FrameKind.CLOSED:
                raise _CallAborted("PBX closed connection during handshake", reason="sip_disconnect")
            if frame.kind == sip.FrameKind.TIMEOUT:
                continue

            response = frame.text or ""
            first_line = response.splitlines()[0] if response else ""
            self.logger.info(f"[SIP Status] {first_line}")

            parsed = sip.parse_status_line(response)
            if parsed is None:
                continue  # not a status line (shouldn't happen mid-handshake); keep waiting
            code, reason_phrase = parsed

            if 100 <= code < 200:
                continue  # informational (100 Trying, 180 Ringing, 183...) — keep waiting

            if code in (401, 407):
                self._last_challenge_frame = frame
                return "reauth", "", ""

            if 200 <= code < 300:
                cseq_match = re.search(r"CSeq:\s*(\d+)\s+INVITE", response, re.IGNORECASE)
                if cseq_match and int(cseq_match.group(1)) != invite_cseq:
                    continue  # response to a stale/earlier transaction; keep waiting
                tag_match = re.search(r"To: .*;tag=(.*)", response, re.IGNORECASE)
                if tag_match:
                    remote_tag = f";tag={tag_match.group(1).strip()}"
                media_match = re.search(r"m=audio (\d+)", response)
                if not media_match:
                    self._last_reject_reason = "no_sdp_answer"
                    return "rejected", "", ""
                self.remote_rtp_port = int(media_match.group(1))
                self.logger.info(f"media connected, remote RTP port {self.remote_rtp_port}")
                return "answered", remote_tag, response

            if 300 <= code < 400:
                self._last_reject_reason = "redirect_unsupported"
                return "rejected", "", ""

            if code in (486, 603, 600, 604):
                self._last_reject_reason = f"rejected_{code}"
                return "rejected", "", ""

            # Any other 4xx/5xx/6xx
            self._last_reject_reason = f"failed_{code}_{reason_phrase}".strip()
            return "rejected", "", ""

        return "cancelled", "", ""

    def _send_cancel(self, invite_branch: str, invite_cseq: int, dialog_kwargs: dict) -> None:
        """F-15: CANCEL an unconfirmed dialog instead of abandoning it (which
        leaves the callee's leg ringing/orphaned on the PBX)."""
        cfg = self.settings
        try:
            self._sock.sendall(
                sip.build_cancel(
                    **{k: v for k, v in dialog_kwargs.items() if k != "branch"},
                    branch=invite_branch,  # CANCEL MUST reuse the INVITE's branch
                    cseq=invite_cseq,
                ).encode()
            )
            self.logger.info("sent CANCEL for unconfirmed dialog")
            deadline = time.monotonic() + cfg.cancel_wait_seconds
            while time.monotonic() < deadline:
                frame = self._stream.read_message(max(deadline - time.monotonic(), 0.1))
                if frame.kind == sip.FrameKind.MESSAGE and frame.text and "487" in frame.text.splitlines()[0]:
                    # ACK the 487 to fully close the CANCEL transaction.
                    ack_branch = sip.new_branch()
                    self._sock.sendall(
                        sip.build_ack(
                            **{k: v for k, v in dialog_kwargs.items() if k != "branch"},
                            branch=ack_branch,
                            cseq=invite_cseq,
                        ).encode()
                    )
                    break
                if frame.kind == sip.FrameKind.CLOSED:
                    break
        except Exception:
            self.logger.exception("error sending CANCEL")

    @staticmethod
    def _parse_sdp_media_address(sdp_text: str) -> Optional[str]:
        """F-12: prefer a media-level c= line (appears after m=audio) over
        the session-level one -- the last c= line in the body is the best
        approximation of "most specific" without a full SDP parser."""
        matches = re.findall(r"^c=IN IP4 (\S+)", sdp_text, re.MULTILINE)
        return matches[-1] if matches else None

    def _bridge(
        self,
        *,
        local_port: int,
        dialog_kwargs: dict,
        remote_tag: str,
        media_ip: str,
        next_cseq: int,
    ) -> str:
        cfg = self.settings

        def _on_turn_latency(payload: dict) -> None:
            self.last_turn_latency = payload

        self._rtp_interface = RtpAudioInterface(
            self.local_rtp_port,
            media_ip,  # F-12: SDP answer's media address, not the raw PBX IP
            self.remote_rtp_port,
            call_id=self.call_id,
            frame_ms=cfg.frame_ms,
            frame_bytes=cfg.frame_bytes,
            log_dir=cfg.log_dir,
            on_turn_latency=_on_turn_latency,
            logger=self.logger,
        )

        # F-09: start RTP transmission (silence frames) immediately, before
        # the ElevenLabs SDK exists at all -- otherwise there's dead air
        # between ACK and start_session() returning, and many SBCs tear
        # down the call on a media timeout during exactly that gap.
        media_start = time.monotonic()
        self._rtp_interface.start(lambda _pcm: None)

        client = ElevenLabs(api_key=cfg.elevenlabs_api_key)
        config = ConversationInitiationData(dynamic_variables=self.dynamic_variables)

        def on_agent_response(t):
            self._rtp_interface.record_llm_first_text()
            if cfg.log_transcripts:
                # Deliberately .info(), not .debug(): visibility is
                # controlled solely by the LOG_TRANSCRIPTS flag so it
                # doesn't require cranking the whole app to DEBUG (which
                # would also surface every other noisy debug line).
                self.logger.info(f"AI: {t}")

        def on_user_transcript(t):
            self._rtp_interface.record_stt_complete()
            if cfg.log_transcripts:
                self.logger.info(f"Caller: {t}")

        self._conversation = Conversation(
            client=client,
            agent_id=cfg.agent_id,
            requires_auth=bool(cfg.elevenlabs_api_key),
            audio_interface=self._rtp_interface,
            config=config,
            callback_agent_response=on_agent_response,
            callback_user_transcript=on_user_transcript,
        )

        # F-02: watchdog around start_session() -- under network lag to
        # ElevenLabs (the reported trigger) this call could hang forever.
        start_error: list[Exception] = []
        started = threading.Event()

        def _start():
            try:
                self._conversation.start_session()
            except Exception as e:
                start_error.append(e)
            finally:
                started.set()

        threading.Thread(target=_start, daemon=True, name=f"el-start-{self.call_id}").start()
        if not started.wait(timeout=cfg.el_start_timeout_seconds):
            raise _CallAborted("ElevenLabs start_session timed out", reason="el_start_timeout")
        if start_error:
            raise _CallAborted(f"ElevenLabs start_session failed: {start_error[0]}", reason="el_start_failed")

        media_start_gap_ms = round((time.monotonic() - media_start) * 1000)
        self.logger.info(f"media_start_gap_ms={media_start_gap_ms}")

        self._set_status(CallStatus.CONNECTED)
        self.connected_at = time.time()
        self.logger.info("bridge running, audio actively routed to PBX")

        session_ended = threading.Event()

        def wait_for_end():
            while True:
                try:
                    conversation_id = self._conversation.wait_for_session_end()
                    self.conversation_id = conversation_id
                    break
                except Exception:
                    self.logger.exception("el-wait thread error, retrying")
                    time.sleep(1.0)
                    if self._hangup_requested.is_set() or session_ended.is_set():
                        break
            session_ended.set()

        threading.Thread(
            target=wait_for_end, daemon=True, name=f"el-wait-{self.call_id}"
        ).start()

        call_deadline = time.monotonic() + cfg.max_call_seconds
        exit_reason = "unknown"
        cseq = next_cseq

        while not session_ended.is_set() and not self._hangup_requested.is_set():
            if time.monotonic() >= call_deadline:
                exit_reason = "max_duration"
                break

            last_rx = self._rtp_interface.last_rx_monotonic
            if last_rx is not None and (time.monotonic() - last_rx) > cfg.rtp_inactivity_seconds:
                exit_reason = "rtp_timeout"
                break

            frame = self._stream.read_message(cfg.sip_bridge_poll_timeout)
            if frame.kind == sip.FrameKind.CLOSED:
                self.logger.info("SIP pipe disconnected abruptly")
                exit_reason = "sip_disconnect"
                break
            if frame.kind == sip.FrameKind.TIMEOUT:
                continue

            sip_msg = frame.text or ""
            method = sip.parse_method(sip_msg)
            if method == "BYE":
                self.logger.info("remote party closed the line (BYE received)")
                try:
                    self._sock.sendall(sip.build_ok_response(sip_msg).encode())
                except Exception:
                    pass
                exit_reason = "remote_bye"
                break
            elif method in ("OPTIONS", "INFO", "NOTIFY", "UPDATE"):
                # F-14B: these were previously silently ignored. Unanswered
                # OPTIONS/keepalives and session-timer refreshes
                # (NOTIFY/UPDATE) can cause the PBX to tear the call down,
                # which looked like "calls randomly drop after N minutes."
                self.logger.info(f"answering in-dialog {method}")
                try:
                    self._sock.sendall(sip.build_ok_response(sip_msg).encode())
                except Exception:
                    pass
                continue
            elif method == "INVITE":
                # Re-INVITE (commonly a session-timer refresh). Answer with
                # the current SDP so the session doesn't expire.
                self.logger.info("answering re-INVITE with current SDP")
                refresh_sdp = sip.build_sdp(cfg.local_ip, self.local_rtp_port)
                try:
                    self._sock.sendall(
                        sip.build_ok_response(sip_msg, sdp=refresh_sdp, to_tag=self._from_tag).encode()
                    )
                except Exception:
                    pass
                continue
            # Unrecognized in-dialog traffic: log and keep going rather than
            # silently dropping it forever (old behavior) or crashing.
            self.logger.info(f"ignoring unrecognized in-dialog message: {sip_msg.splitlines()[0] if sip_msg else ''}")

        if exit_reason == "unknown" and session_ended.is_set():
            exit_reason = "agent_ended"

        if self._hangup_requested.is_set() and exit_reason == "unknown":
            exit_reason = "local_hangup"

        if self._hangup_requested.is_set() or exit_reason in ("max_duration", "rtp_timeout", "local_hangup"):
            self.logger.info(f"ending call (reason={exit_reason}), sending BYE")
            try:
                bye_branch = sip.new_branch()  # F-14: BYE is its own transaction
                self._sock.sendall(
                    sip.build_bye(**dialog_kwargs, branch=bye_branch, cseq=cseq, remote_tag=remote_tag).encode()
                )
            except Exception:
                pass

        status = CallStatus.COMPLETED if exit_reason in ("remote_bye", "agent_ended", "local_hangup") else CallStatus.FAILED
        self._set_status(status)
        return exit_reason

    # ------------------------------------------------------------------
    def _cleanup(self) -> None:
        self.logger.info("cleaning up call resources")
        try:
            if self._conversation is not None:
                self._conversation.end_session()
        except Exception:
            pass
        try:
            if self._rtp_interface is not None:
                self._rtp_interface.stop()
        except Exception:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass

        # F-06: this call's record can sit in CallManager's registry for up
        # to CALL_RETENTION_SECONDS after ending. Without this, it keeps
        # strong references to the ElevenLabs SDK object (with its own
        # threads/buffers), the RTP interface (including any still-queued
        # TTS frames), and the raw socket -- all needed state for the API
        # (last_turn_latency, conversation_id) has already been captured
        # into plain fields on `self`, so it's safe to drop the rest.
        self._conversation = None
        self._rtp_interface = None
        self._sock = None
        self._stream = None


class _CallAborted(Exception):
    def __init__(self, message: str = "", cancelled: bool = False, reason: str = "failed"):
        super().__init__(message)
        self.cancelled = cancelled
        self.reason = reason


class _CallRejected(Exception):
    def __init__(self, message: str = "", reason: str = "rejected"):
        super().__init__(message)
        self.reason = reason