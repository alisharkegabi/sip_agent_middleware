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

import queue
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import ClientTools, Conversation, ConversationInitiationData

import db
import sip_protocol as sip
from audio_bridge import RtpAudioInterface
from config import Settings
from extension_pool import ExtensionPool, ExtensionPoolExhausted
from logging_config import get_call_logger
from models import CallStatus
from port_allocator import PortAllocator
from transfer_trigger import matches_transfer_phrase


@dataclass
class _TransferRequest:
    """Handoff between whichever thread requested a transfer (the
    ElevenLabs websocket thread for an agent-spoken phrase, ClientTools'
    executor thread for the legacy client tool, or FastAPI's threadpool for
    the HTTP fallback) and the SIP thread running _bridge()'s loop -- the
    only thread allowed to touch self._sock / self._stream. The requesting
    thread blocks on result_event; the bridge loop picks the request off
    the queue, acquires an extension itself (extension starts as None for
    every entry point now -- see CallSession._request_transfer /
    _maybe_trigger_transfer), does the REFER, and sets the result."""
    extension: Optional[str] = None   # None => the SIP thread acquires it
    source: str = "agent_phrase"      # "agent_phrase" | "client_tool" | "http"
    result_event: threading.Event = field(default_factory=threading.Event)
    result: Optional[dict] = None


class CallSession:
    def __init__(
        self,
        *,
        phone_number: str,
        dynamic_variables: dict,
        settings: Settings,
        port_allocator: PortAllocator,
        extension_pool: Optional[ExtensionPool] = None,
        tracking_id: Optional[str] = None,
        busy_frames: Optional[list] = None,
    ):
        self.call_id = uuid.uuid4().hex  # our internal id, exposed via the API
        self.phone_number = phone_number
        self.dynamic_variables = dynamic_variables
        self.tracking_id = tracking_id or dynamic_variables.get("tracking_id")
        self.settings = settings
        self._port_allocator = port_allocator
        self._extension_pool = extension_pool
        self.logger = get_call_logger(self.call_id)
        self.conversation_id = None
        # Filled in after the call by AnalysisFetcher (evaluation criteria +
        # data collection results). None until ElevenLabs finishes analysing.
        self.analysis: Optional[dict] = None
        self.transferred_to: Optional[str] = None
        self._transfer_requests: "queue.Queue[_TransferRequest]" = queue.Queue()

        # Per-call transfer state. Everything here lives on self -- the only
        # shared object across calls is ExtensionPool, which is internally
        # locked -- which is what structurally prevents one call's trigger
        # phrase (or its transfer outcome) from ever touching another call.
        self._transfer_guard = threading.Lock()
        self._transfer_started = False   # one-shot: at most ONE transfer attempt per call
        self._transfer_failed = False    # D6: an announced transfer that never completed
        self._el_session_closed = False  # makes _close_el_session() idempotent
        self._busy_frames = busy_frames  # pre-built mu-law frames, or None if unavailable

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
        self._last_reject_code: Optional[int] = None

    def _db_call(self, func, *args, **kwargs) -> None:
        """Best-effort write to BatchCallDetails -- never let a DB hiccup
        affect the SIP call flow."""
        if not self.tracking_id:
            return
        try:
            func(self.tracking_id, *args, **kwargs)
        except Exception:
            self.logger.exception(f"failed to record call status in BatchCallDetails ({func.__name__})")

    # ------------------------------------------------------------------
    # Public control surface (called by CallManager / API layer)
    # ------------------------------------------------------------------
    def request_hangup(self) -> None:
        self._hangup_requested.set()

    def request_transfer(self) -> dict:
        """HTTP fallback (POST /calls/{call_id}/transfer or
        /calls/by-tracking-id/{tracking_id}/transfer) -- kept for backward
        compatibility (D7). The primary transfer trigger is now the agent's
        own transcript phrase; see _maybe_trigger_transfer. Blocks the
        calling thread until the transfer resolves or times out, so callers
        (main.py) must run this off the event loop."""
        return self._request_transfer(source="http")

    def set_analysis(self, analysis: dict) -> None:
        """Called from an analysis-fetch thread once ElevenLabs' post-call
        record is available; guarded like every other cross-thread write to
        session state (F-24)."""
        with self._status_lock:
            self.analysis = analysis

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
                "transferred_to": self.transferred_to,
                "analysis": self.analysis,
            }

    def to_webhook_payload(self, event: str = "call_status") -> dict:
        """Terminal-state notification payload sent to the configured webhook.

        Reuses the same fields as to_dict()/CallDetail -- no new state is
        introduced, this is just a different shape for external consumers.

        Sent twice per call when post-call analysis is enabled:
          - event="call_status"   immediately at terminal status, analysis=null
          - event="call_analysis" seconds later, once ElevenLabs has produced
                                  the evaluation/data-collection results
        Everything except `event` and `analysis` is identical between the
        two, so a consumer that only cares about completion can keep
        processing the first one exactly as before and ignore the second.
        """
        with self._status_lock:
            started_at = self.dialed_at if self.dialed_at is not None else self.created_at
            duration_seconds = None
            if self.ended_at is not None and started_at is not None:
                duration_seconds = round(self.ended_at - started_at, 3)
            return {
                "event": event,
                "call_id": self.call_id,
                "conversation_id": self.conversation_id,
                "status": self.status.value,
                "started_at": started_at,
                "ended_at": self.ended_at,
                "duration_seconds": duration_seconds,
                "reason": self.exit_reason or self.error or self.status.value,
                "analysis": self.analysis,
                "metadata": {
                    "phone_number": self.phone_number,
                    "tracking_id": self.tracking_id,
                    "remote_rtp_port": self.remote_rtp_port,
                    "local_rtp_port": self.local_rtp_port,
                    "last_turn_latency": self.last_turn_latency,
                    "answered": self.answered,
                    "dynamic_variables": self.dynamic_variables,
                    "transferred_to": self.transferred_to,
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
        self._record_call_ended(exit_reason)

    def _record_call_ended(self, exit_reason: str) -> None:
        """BatchCallDetails.EndedAt is set for every terminal reason. Status
        is only overwritten for terminal outcomes the PBX call-status scheme
        distinguishes in their own right: Cancelled (486/503, or a local
        hangup before the callee answered), Timeout (CANCEL sent after
        max_ring_seconds with no answer), Transfer (an internal transfer
        completed), and TranFail (a transfer was announced -- the agent
        said the trigger phrase -- but never completed, whether because no
        extension was free or because the PBX rejected/timed out the
        REFER). Every other terminal reason (BYE either side, max
        duration, RTP inactivity, or a failure outside this scheme) just
        stamps EndedAt and leaves Status as whatever mark_ringing/
        mark_answered already set.

        Order matters, in this exact sequence:
          1. "transferred" first -- a call that failed one transfer attempt
             and then succeeded on a retry must record Transfer, not
             TranFail. The sticky self._transfer_failed flag must never
             beat an actual success.
          2. self._transfer_failed second, ahead of ring_timeout /
             local_hangup / reject_status. Those three are all pre-answer
             states where no transfer could have been announced, so there
             is no real conflict -- but this position also means a failed
             transfer followed by e.g. sip_disconnect or internal_error
             (reasons nothing else here writes a status for) still records
             TranFail. That's intended: the transfer failure is a known
             fact, not a guess, and the callback obligation holds
             regardless of how the call finally ended.
        """
        reject_status = db.sip_response_to_status(self._last_reject_code) if self._last_reject_code else None
        if exit_reason == "transferred":
            self._db_call(db.mark_ended, status=db.STATUS_TRANSFERRED)
        elif exit_reason == "transfer_unavailable" or self._transfer_failed:
            self._db_call(db.mark_ended, status=db.STATUS_TRANSFER_FAILED)
        elif exit_reason == "ring_timeout":
            self._db_call(db.mark_ended, status=db.STATUS_TIMEOUT)
        elif exit_reason == "local_hangup" and not self.answered:
            self._db_call(db.mark_ended, status=db.STATUS_CANCELLED)
        elif reject_status is not None:
            self._db_call(db.mark_ended, status=reject_status)
        elif exit_reason in (
            "remote_bye", "agent_ended", "local_hangup", "max_duration", "rtp_timeout",
        ):
            self._db_call(db.mark_ended)

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
            self._db_call(db.mark_ringing)
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
        self._db_call(db.mark_answered)

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
        self._last_reject_code = None
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
                self._last_reject_code = code
                return "rejected", "", ""

            if code in (486, 603, 600, 604):
                self._last_reject_reason = f"rejected_{code}"
                self._last_reject_code = code
                return "rejected", "", ""

            # Any other 4xx/5xx/6xx (e.g. 503)
            self._last_reject_reason = f"failed_{code}_{reason_phrase}".strip()
            self._last_reject_code = code
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

    # ------------------------------------------------------------------
    # Call transfer (SIP REFER to an internal extension)
    # ------------------------------------------------------------------
    def _claim_transfer(self) -> bool:
        """Compare-and-set. Returns True exactly once per CallSession, no
        matter which entry point (agent phrase / client tool / HTTP) calls
        it first or how many times it's called after. This, plus the fact
        that on_agent_response is a closure bound to one CallSession
        instance created fresh per call in _bridge(), is what structurally
        prevents one call's trigger from ever moving another call's leg or
        writing another call's tracking_id."""
        with self._transfer_guard:
            if self._transfer_started:
                return False
            self._transfer_started = True
            return True

    def _mark_transfer_failed(self) -> None:
        """The agent told the caller they were being transferred and it did
        not happen -- no free line, or the PBX rejected/timed out the REFER
        (D6). Sticky for the rest of the call: a later successful transfer
        still wins over this, because _record_call_ended() checks
        exit_reason == "transferred" before it checks this flag."""
        with self._status_lock:
            self._transfer_failed = True

    def _maybe_trigger_transfer(self, text: str) -> None:
        """Called from on_agent_response for every agent utterance on every
        live call -- runs on the ElevenLabs SDK's websocket receive thread,
        NOT the SIP thread, so this must return in microseconds: no I/O, no
        extension acquisition, no socket work, no DB work. It only matches
        the transcript against the configured trigger phrase(s) and, if
        matched, hands off to the SIP thread via self._transfer_requests --
        the actual REFER happens in _bridge()'s loop. Wrapped in try/except
        because an exception raised inside an SDK callback can kill the
        receive thread and take the whole conversation down with it."""
        try:
            if not matches_transfer_phrase(text, self.settings.transfer_trigger_phrases_normalized):
                return
            if not self._claim_transfer():
                self.logger.info("transfer phrase seen again, already in progress; ignoring")
                return
            self.logger.info(
                f"transfer phrase detected; queueing internal transfer "
                f"(call_id={self.call_id} tracking_id={self.tracking_id})"
            )
            self._transfer_requests.put(_TransferRequest(extension=None, source="agent_phrase"))
        except Exception:
            self.logger.exception("transfer trigger check failed")

    def _handle_transfer_tool_call(self, parameters: dict) -> dict:
        """ElevenLabs client tool "transfer_call" -- legacy entry point,
        kept as a fallback (D7): the primary trigger is now the agent's own
        transcript phrase (see _maybe_trigger_transfer). Runs on
        ClientTools' own executor thread, not the SIP thread."""
        return self._request_transfer(source="client_tool")

    def _request_transfer(self, *, source: str) -> dict:
        """Shared body for every transfer entry point (agent phrase, client
        tool, HTTP fallback). Claims the one-shot guard, enqueues a request
        for the SIP thread -- which alone may acquire an extension and send
        SIP, so "all lines busy" behaves identically no matter which entry
        point triggered it -- and blocks for the outcome. The dict returned
        here becomes the client_tool_result the agent sees (for the
        "agent_phrase" and "client_tool" sources) or the HTTP response body
        (for "http")."""
        if not self._claim_transfer():
            return {
                "success": False,
                "status": "already_requested",
                "message": "A transfer has already been requested for this call.",
            }

        request = _TransferRequest(extension=None, source=source)
        self._transfer_requests.put(request)

        # A little slack over transfer_wait_seconds so a normal timeout
        # inside the bridge loop's handling always wins the race and
        # produces a proper result dict instead of this fallback firing
        # first.
        if not request.result_event.wait(timeout=self.settings.transfer_wait_seconds + 5.0):
            self.logger.warning(f"transfer ({source}) never picked up by the bridge loop")
            with self._transfer_guard:
                self._transfer_started = False
            return {
                "success": False,
                "status": "error",
                "message": "Transfer could not be started; the call may already be ending.",
            }

        return request.result

    def _close_el_session(self) -> None:
        """Close the ElevenLabs websocket. Idempotent (also called from
        _cleanup()) and bounded -- end_session() joins the SDK's own
        threads and must never be allowed to stall the SIP thread mid-
        transfer (D3: only called once a transfer is confirmed successful,
        or right before the busy prompt plays)."""
        with self._status_lock:
            if self._el_session_closed:
                return
            self._el_session_closed = True

        conversation = self._conversation
        if conversation is None:
            return

        done = threading.Event()

        def _end():
            try:
                conversation.end_session()
            except Exception:
                pass
            finally:
                done.set()

        threading.Thread(target=_end, daemon=True, name=f"el-end-{self.call_id}").start()
        if not done.wait(timeout=self.settings.el_end_session_timeout_seconds):
            self.logger.warning("ElevenLabs end_session did not return within timeout, abandoning wait")

    def _wait_for_playout(self, *, quiet_seconds: float, timeout: float) -> None:
        """Block until the RTP play queue has been empty, with no new TTS
        chunk having arrived, for `quiet_seconds`, or until `timeout`
        elapses. Runs on the SIP thread; the RTP send/recv loops are
        separate threads and keep flowing throughout -- this only waits, it
        does not pace anything itself. Used so a just-announced sentence
        (the transfer trigger phrase, or the busy prompt itself) actually
        finishes reaching the caller before the next thing happens. Bounded
        by `timeout` so a stuck TTS stream can never wedge the SIP thread."""
        if self._rtp_interface is None:
            return
        deadline = time.monotonic() + timeout
        poll_interval = 0.05
        while time.monotonic() < deadline:
            pending = self._rtp_interface.playout_pending()
            last_output = self._rtp_interface.last_output_monotonic
            quiet_for = (time.monotonic() - last_output) if last_output is not None else quiet_seconds
            if pending == 0 and quiet_for >= quiet_seconds:
                return
            time.sleep(poll_interval)

    def _play_busy_prompt_and_close(self) -> None:
        """Runs on the SIP thread, from _bridge()'s loop. Plays the "all
        lines are busy" static prompt to completion, then closes the
        ElevenLabs session. Returns either way -- the caller sets
        exit_reason = "transfer_unavailable" and ends the call with a BYE
        (D5), so the prompt is always fully transmitted before the BYE.

        ORDER IS LOAD-BEARING: the ElevenLabs session must be closed AFTER
        the prompt has played, not before. Conversation.end_session() calls
        stop() on our audio interface, which sets is_running=False and
        closes the RTP socket -- closing the session first left
        play_static_frames() with nothing to transmit and the caller heard
        silence. The agent can't talk over the prompt in the meantime
        because play_static_frames() latches _static_playback, which makes
        RtpAudioInterface.output() drop any TTS still arriving."""
        cfg = self.settings
        if not cfg.busy_prompt_enabled or self._busy_frames is None or self._rtp_interface is None:
            self.logger.warning(
                f"busy prompt not played: enabled={cfg.busy_prompt_enabled} "
                f"frames_loaded={self._busy_frames is not None} "
                f"rtp_interface={self._rtp_interface is not None} "
                f"(path={cfg.busy_prompt_audio_path!r}); hanging up without it"
            )
            self._close_el_session()
            return

        approx_prompt_seconds = len(self._busy_frames) * cfg.frame_ms / 1000
        self.logger.info(
            f"playing 'all lines busy' prompt to caller "
            f"({len(self._busy_frames)} frames, ~{approx_prompt_seconds:.2f}s)"
        )
        if not self._rtp_interface.play_static_frames(self._busy_frames):
            # play_static_frames has already logged the specific reason.
            self.logger.warning("busy prompt could not be queued; hanging up without playing it")
            self._close_el_session()
            return

        # Wait for the play queue to drain (quiet_seconds=0: we only care
        # that every frame has been handed to the sender), then hold for
        # the tail so those last frames are actually on the wire before the
        # BYE goes out.
        self._wait_for_playout(quiet_seconds=0.0, timeout=approx_prompt_seconds + 5.0)
        time.sleep(cfg.busy_prompt_tail_seconds)
        self.logger.info("'all lines busy' prompt finished playing, ending call")

        self._close_el_session()

    def _perform_transfer(
        self, request: "_TransferRequest", *, dialog_kwargs: dict, remote_tag: str, cseq: int
    ) -> tuple[int, bool]:
        """Runs on the SIP thread, inside _bridge()'s loop. Sends the
        in-dialog REFER, then handles subsequent SIP traffic itself
        (matching the REFER's own CSeq, answering the transfer-progress
        NOTIFY(s)) until a final outcome or timeout, and reports it back to
        the waiting ClientTools thread. Returns (next_cseq, transferred)."""
        cfg = self.settings
        cseq += 1
        refer_branch = sip.new_branch()
        self.logger.info(f"transferring call to extension {request.extension} via SIP REFER")

        try:
            self._sock.sendall(
                sip.build_refer(
                    **dialog_kwargs,
                    branch=refer_branch,
                    cseq=cseq,
                    refer_to_extension=request.extension,
                    remote_tag=remote_tag,
                ).encode()
            )
        except Exception as e:
            self._extension_pool.release(request.extension)
            self._mark_transfer_failed()
            request.result = {
                "success": False,
                "status": "error",
                "message": f"Failed to send transfer request: {e}",
            }
            request.result_event.set()
            return cseq, False

        deadline = time.monotonic() + cfg.transfer_wait_seconds
        refer_accepted = False
        outcome: Optional[str] = None  # "success" | "failed" | None (== timeout)

        while time.monotonic() < deadline:
            frame = self._stream.read_message(max(deadline - time.monotonic(), 0.1))
            if frame.kind == sip.FrameKind.CLOSED:
                outcome = "failed"
                break
            if frame.kind == sip.FrameKind.TIMEOUT:
                continue

            msg = frame.text or ""
            first_line = msg.splitlines()[0] if msg else ""

            if not refer_accepted:
                parsed = sip.parse_status_line(msg)
                if parsed is not None:
                    code, _ = parsed
                    cseq_match = re.search(r"CSeq:\s*(\d+)\s+REFER", msg, re.IGNORECASE)
                    if cseq_match and int(cseq_match.group(1)) == cseq:
                        if 200 <= code < 300:
                            refer_accepted = True
                            self.logger.info(f"REFER accepted ({code}), waiting for transfer outcome")
                        else:
                            outcome = "failed"
                            self.logger.info(f"REFER rejected: {first_line}")
                            break
                    continue

            method = sip.parse_method(msg)
            if method == "NOTIFY" and "refer" in msg.lower():
                try:
                    self._sock.sendall(sip.build_ok_response(msg).encode())
                except Exception:
                    pass
                body = msg.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in msg else ""
                frag = sip.parse_status_line(body.strip())
                if frag is None:
                    continue
                frag_code, _ = frag
                if frag_code == 100:
                    continue  # trying -- keep waiting for the final fragment
                outcome = "success" if 200 <= frag_code < 300 else "failed"
                break
            elif method == "BYE":
                # PBX tore down our leg itself as part of completing the
                # transfer -- ACK it and treat this as success; no BYE of
                # our own is needed below.
                try:
                    self._sock.sendall(sip.build_ok_response(msg).encode())
                except Exception:
                    pass
                self._extension_pool.release(request.extension, busy_seconds=cfg.transfer_extension_busy_seconds)
                self.transferred_to = request.extension
                self._close_el_session()  # D3: only now, transfer is confirmed
                request.result = {
                    "success": True,
                    "status": "transferred",
                    "message": f"Call transferred to extension {request.extension}.",
                }
                request.result_event.set()
                return cseq, True
            elif method in ("OPTIONS", "INFO", "UPDATE"):
                try:
                    self._sock.sendall(sip.build_ok_response(msg).encode())
                except Exception:
                    pass
                continue
            # anything else in-dialog: ignore, keep waiting for the outcome

        if outcome is None:
            outcome = "failed"
            self.logger.info(f"transfer to extension {request.extension} timed out")

        if outcome == "success":
            self.logger.info(f"call transferred to extension {request.extension}, ending our leg")
            self._extension_pool.release(request.extension, busy_seconds=cfg.transfer_extension_busy_seconds)
            self.transferred_to = request.extension
            self._close_el_session()  # D3: only now, transfer is confirmed
            try:
                bye_branch = sip.new_branch()
                cseq += 1
                self._sock.sendall(
                    sip.build_bye(**dialog_kwargs, branch=bye_branch, cseq=cseq, remote_tag=remote_tag).encode()
                )
            except Exception:
                pass
            request.result = {
                "success": True,
                "status": "transferred",
                "message": f"Call transferred to extension {request.extension}.",
            }
            request.result_event.set()
            return cseq, True

        self._extension_pool.release(request.extension)
        self._mark_transfer_failed()
        request.result = {
            "success": False,
            "status": "failed",
            "message": f"Transfer to extension {request.extension} failed.",
        }
        request.result_event.set()
        return cseq, False

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
            antialias=cfg.audio_antialias,
            antialias_cutoff_hz=cfg.audio_antialias_cutoff_hz,
            antiimage=cfg.audio_antiimage,
            antiimage_cutoff_hz=cfg.audio_antiimage_cutoff_hz,
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

        # Registers "transfer_call" as an ElevenLabs client tool -- kept as
        # a fallback entry point (D7). The primary transfer trigger is now
        # the agent's own transcript phrase, detected below in
        # on_agent_response -> _maybe_trigger_transfer; this tool no longer
        # needs to be configured in the ElevenLabs dashboard for transfers
        # to work, though it's harmless to leave registered there. Runs on
        # ClientTools' own executor thread, so the actual SIP REFER is
        # handed off to the _bridge() loop below via
        # self._transfer_requests -- see _handle_transfer_tool_call /
        # _perform_transfer.
        client_tools = ClientTools()
        client_tools.register("transfer_call", self._handle_transfer_tool_call)

        def on_agent_response(t):
            self._rtp_interface.record_llm_first_text()
            if cfg.log_transcripts:
                # Deliberately .info(), not .debug(): visibility is
                # controlled solely by the LOG_TRANSCRIPTS flag so it
                # doesn't require cranking the whole app to DEBUG (which
                # would also surface every other noisy debug line).
                self.logger.info(f"AI: {t}")
            # Primary transfer trigger: the agent's own transcript, not an
            # ElevenLabs tool call or webhook POST. This closure is bound to
            # `self` (one CallSession per call), which is what keeps this
            # per-call -- see _maybe_trigger_transfer's docstring.
            self._maybe_trigger_transfer(t)

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
            client_tools=client_tools,
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

            try:
                transfer_request = self._transfer_requests.get_nowait()
            except queue.Empty:
                transfer_request = None

            if transfer_request is not None:
                if transfer_request.extension is None:
                    # Every entry point (agent phrase, client tool, HTTP)
                    # arrives here with no extension yet -- acquisition
                    # happens on the SIP thread, in one place, so "all
                    # lines busy" behaves identically no matter what
                    # triggered the transfer. First let whatever was just
                    # said (the trigger phrase itself) finish reaching the
                    # caller.
                    self._wait_for_playout(
                        quiet_seconds=cfg.transfer_playout_quiet_seconds,
                        timeout=cfg.transfer_playout_timeout_seconds,
                    )
                    acquired_extension = None
                    if self._extension_pool is not None:
                        try:
                            acquired_extension = self._extension_pool.acquire()
                        except ExtensionPoolExhausted:
                            acquired_extension = None
                    if acquired_extension is None:
                        self.logger.info("transfer requested but no extensions are available")
                        transfer_request.result = {
                            "success": False,
                            "status": "busy",
                            "message": "All lines are currently busy.",
                        }
                        transfer_request.result_event.set()
                        self._mark_transfer_failed()
                        self._play_busy_prompt_and_close()
                        exit_reason = "transfer_unavailable"
                        break
                    transfer_request.extension = acquired_extension

                cseq, transferred = self._perform_transfer(
                    transfer_request, dialog_kwargs=dialog_kwargs, remote_tag=remote_tag, cseq=cseq
                )
                if transferred:
                    exit_reason = "transferred"
                    break
                # Failed (REFER rejected/timed out) -- the call continues,
                # so allow a legitimate retry (e.g. the agent says the
                # trigger phrase again).
                with self._transfer_guard:
                    self._transfer_started = False
                continue

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

        if self._hangup_requested.is_set() or exit_reason in (
            "max_duration", "rtp_timeout", "local_hangup", "agent_ended", "transfer_unavailable",
        ):
            # "transferred" deliberately stays out of this set:
            # _perform_transfer already sent our BYE (or the PBX sent one
            # itself), so sending a second one here would be a stray
            # in-dialog request on a now-dead dialog.
            self.logger.info(f"ending call (reason={exit_reason}), sending BYE")
            try:
                bye_branch = sip.new_branch()  # F-14: BYE is its own transaction
                self._sock.sendall(
                    sip.build_bye(**dialog_kwargs, branch=bye_branch, cseq=cseq, remote_tag=remote_tag).encode()
                )
            except Exception:
                pass

        status = (
            CallStatus.COMPLETED
            if exit_reason in (
                "remote_bye", "agent_ended", "local_hangup", "transferred", "transfer_unavailable",
            )
            else CallStatus.FAILED
        )
        self._set_status(status)
        return exit_reason

    # ------------------------------------------------------------------
    def _cleanup(self) -> None:
        self.logger.info("cleaning up call resources")
        try:
            # Idempotent + bounded (see _close_el_session): a transfer may
            # already have closed this; if not, this is the normal-path
            # close for every other exit reason.
            self._close_el_session()
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