"""
SIP message construction / parsing helpers.

Message-building logic (headers, digest algorithm, 401 handshake) is
unchanged from the original per Rule 2 of the work order, except where the
work order explicitly requires a change:
  - branch-per-transaction (F-14): every builder takes an explicit `branch`
    argument; CallSession is responsible for generating a fresh one per
    transaction rather than reusing one value for the whole dialog.
  - digest qop=auth support (F-05).
  - a new build_cancel() (F-15).
  - build_ok_response() no longer emits a literal "Header:" placeholder for
    a missing header, and copies ALL Via lines, not just the first (F-26).
  - recv_full() is replaced by SipStream, a stateful framer that retains
    unconsumed bytes across calls and reads the body by Content-Length
    (F-04).
"""
from __future__ import annotations

import hashlib
import os
import re
import socket
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


def new_branch() -> str:
    """A fresh RFC 3261 §8.1.1.7 branch id for a NEW client transaction.
    Must be called for every INVITE (incl. the authenticated re-send), every
    ACK to a 2xx, every BYE, and every CANCEL (CANCEL is the one exception:
    it reuses the branch of the INVITE it's cancelling -- see build_cancel)."""
    return f"z9hG4bK-{uuid.uuid4().hex}"


def build_sdp(local_ip: str, local_rtp_port: int) -> str:
    return (
        "v=0\r\n"
        f"o=tester 0 0 IN IP4 {local_ip}\r\n"
        "s=Test Call\r\n"
        f"c=IN IP4 {local_ip}\r\n"
        "t=0 0\r\n"
        f"m=audio {local_rtp_port} RTP/AVP 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
    )


def build_invite(
    *,
    local_ip: str,
    local_port: int,
    pbx_ip: str,
    target_number: str,
    ext_user: str,
    call_id: str,
    branch: str,
    from_tag: str,
    sdp: str,
    cseq: int = 1,
    extra_headers: str = "",
) -> str:
    return (
        f"INVITE sip:{target_number}@{pbx_ip} SIP/2.0\r\n"
        f"Via: SIP/2.0/TCP {local_ip}:{local_port};branch={branch}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: \"{ext_user}\" <sip:{ext_user}@{pbx_ip}>;tag={from_tag}\r\n"
        f"To: <sip:{target_number}@{pbx_ip}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} INVITE\r\n"
        f"Contact: <sip:{ext_user}@{local_ip}:{local_port};transport=tcp>\r\n"
        f"{extra_headers}"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {len(sdp)}\r\n"
        f"\r\n"
        f"{sdp}"
    )


def build_ack(
    *,
    local_ip: str,
    local_port: int,
    pbx_ip: str,
    target_number: str,
    ext_user: str,
    call_id: str,
    branch: str,
    from_tag: str,
    cseq: int,
    remote_tag: str = "",
) -> str:
    return (
        f"ACK sip:{target_number}@{pbx_ip} SIP/2.0\r\n"
        f"Via: SIP/2.0/TCP {local_ip}:{local_port};branch={branch}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: \"{ext_user}\" <sip:{ext_user}@{pbx_ip}>;tag={from_tag}\r\n"
        f"To: <sip:{target_number}@{pbx_ip}>{remote_tag}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} ACK\r\n"
        f"Content-Length: 0\r\n"
        f"\r\n"
    )


def build_bye(
    *,
    local_ip: str,
    local_port: int,
    pbx_ip: str,
    target_number: str,
    ext_user: str,
    call_id: str,
    branch: str,
    from_tag: str,
    cseq: int,
    remote_tag: str = "",
) -> str:
    return (
        f"BYE sip:{target_number}@{pbx_ip} SIP/2.0\r\n"
        f"Via: SIP/2.0/TCP {local_ip}:{local_port};branch={branch}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: \"{ext_user}\" <sip:{ext_user}@{pbx_ip}>;tag={from_tag}\r\n"
        f"To: <sip:{target_number}@{pbx_ip}>{remote_tag}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} BYE\r\n"
        f"Content-Length: 0\r\n"
        f"\r\n"
    )


def build_cancel(
    *,
    local_ip: str,
    local_port: int,
    pbx_ip: str,
    target_number: str,
    ext_user: str,
    call_id: str,
    branch: str,
    from_tag: str,
    cseq: int,
) -> str:
    """CANCEL for a not-yet-confirmed INVITE (F-15). Per RFC 3261 §9.1, a
    CANCEL MUST use the SAME branch (and CSeq number) as the INVITE it
    cancels -- it is matched to that transaction, not a new one."""
    return (
        f"CANCEL sip:{target_number}@{pbx_ip} SIP/2.0\r\n"
        f"Via: SIP/2.0/TCP {local_ip}:{local_port};branch={branch}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: \"{ext_user}\" <sip:{ext_user}@{pbx_ip}>;tag={from_tag}\r\n"
        f"To: <sip:{target_number}@{pbx_ip}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} CANCEL\r\n"
        f"Content-Length: 0\r\n"
        f"\r\n"
    )


def build_refer(
    *,
    local_ip: str,
    local_port: int,
    pbx_ip: str,
    target_number: str,
    ext_user: str,
    call_id: str,
    branch: str,
    from_tag: str,
    cseq: int,
    refer_to_extension: str,
    remote_tag: str = "",
) -> str:
    """In-dialog REFER (RFC 3515) asking the PBX to blind-transfer this call
    to an internal extension. No attended/consultation leg -- Refer-To
    points straight at the extension on the PBX."""
    return (
        f"REFER sip:{target_number}@{pbx_ip} SIP/2.0\r\n"
        f"Via: SIP/2.0/TCP {local_ip}:{local_port};branch={branch}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: \"{ext_user}\" <sip:{ext_user}@{pbx_ip}>;tag={from_tag}\r\n"
        f"To: <sip:{target_number}@{pbx_ip}>{remote_tag}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} REFER\r\n"
        f"Refer-To: <sip:{refer_to_extension}@{pbx_ip}>\r\n"
        f"Referred-By: <sip:{ext_user}@{pbx_ip}>\r\n"
        f"Contact: <sip:{ext_user}@{local_ip}:{local_port};transport=tcp>\r\n"
        f"Content-Length: 0\r\n"
        f"\r\n"
    )


def build_ok_response(request_text: str, *, sdp: Optional[str] = None, to_tag: str = "") -> str:
    """Build a 200 OK for an in-dialog request (BYE/OPTIONS/INFO/NOTIFY/
    re-INVITE ack, etc). F-26 fix: previously, a missing header emitted the
    literal string "Via:" as a header line (malformed SIP), and only the
    FIRST Via was ever copied even when the request traversed a proxy chain
    and carried several. Both are corrected: headers that aren't present are
    omitted entirely, and every Via line is echoed back, in order.

    F-14B: pass `sdp` to answer a re-INVITE with the current session
    description (required so a PBX with session timers enabled doesn't
    tear the call down thinking we rejected the refresh)."""
    lines = request_text.split("\r\n")

    def find_all(prefix: str) -> list[str]:
        return [l for l in lines if l.lower().startswith(prefix.lower())]

    def find_one(prefix: str) -> Optional[str]:
        matches = find_all(prefix)
        return matches[0] if matches else None

    via_lines = find_all("Via:")
    from_line = find_one("From:")
    to_line = find_one("To:")
    call_id_line = find_one("Call-ID:")
    cseq_line = find_one("CSeq:")

    if to_line is not None and to_tag and "tag=" not in to_line:
        to_line = f"{to_line};tag={to_tag}"

    header_block = "".join(f"{v}\r\n" for v in via_lines)
    for line in (from_line, to_line, call_id_line, cseq_line):
        if line is not None:
            header_block += f"{line}\r\n"

    if sdp:
        header_block += "Content-Type: application/sdp\r\n"
        header_block += f"Content-Length: {len(sdp)}\r\n\r\n{sdp}"
        return "SIP/2.0 200 OK\r\n" f"{header_block}"

    return "SIP/2.0 200 OK\r\n" f"{header_block}" "Content-Length: 0\r\n" "\r\n"


def parse_status_line(data: str) -> Optional[tuple[int, str]]:
    """Return (status_code, reason_phrase) for a SIP response, or None if
    `data` isn't a status line (e.g. it's a request like BYE/OPTIONS)."""
    first_line = data.split("\r\n", 1)[0] if data else ""
    m = re.match(r"SIP/2\.0\s+(\d{3})\s+(.*)", first_line)
    if not m:
        return None
    return int(m.group(1)), m.group(2).strip()


def parse_method(data: str) -> Optional[str]:
    """Return the method name if `data` is a SIP request, else None."""
    first_line = data.split("\r\n", 1)[0] if data else ""
    m = re.match(r"([A-Z]+) sip:", first_line)
    return m.group(1) if m else None


def parse_www_auth(data: str) -> tuple[str, str, Optional[str], Optional[str]]:
    """Returns (realm, nonce, qop, opaque). qop/opaque are None if absent."""
    line = next(l for l in data.split("\r\n") if l.lower().startswith("www-authenticate"))
    realm = line.split('realm="')[1].split('"')[0]
    nonce = line.split('nonce="')[1].split('"')[0]
    qop = None
    if "qop=" in line:
        # qop may be quoted ("auth") or a bare token depending on server
        m = re.search(r'qop=("?)([^",]+)\1', line)
        if m:
            qop = m.group(2)
    opaque = None
    if 'opaque="' in line:
        opaque = line.split('opaque="')[1].split('"')[0]
    return realm, nonce, qop, opaque


def digest_response(
    username: str,
    password: str,
    realm: str,
    nonce: str,
    method: str,
    uri: str,
    *,
    qop: Optional[str] = None,
    nc: str = "00000001",
    cnonce: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """RFC 2617 digest auth. Supports both the legacy RFC 2069 form (no qop)
    and qop="auth" (F-05) -- some PBXs advertise qop and reject a response
    computed the old way, which otherwise causes an infinite 401 retry loop.
    Returns (response_hash, cnonce_used_or_None)."""
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    if qop:
        cnonce = cnonce or uuid.uuid4().hex[:16]
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
        return response, cnonce
    return hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest(), None


class FrameKind(Enum):
    MESSAGE = auto()
    TIMEOUT = auto()   # no complete message available yet, socket didn't close
    CLOSED = auto()     # peer closed the connection


@dataclass
class Frame:
    kind: FrameKind
    text: Optional[str] = None


class SipStream:
    """Stateful SIP framer over a single TCP socket (F-04).

    Replaces the old module-level recv_full(), which read until the first
    "\\r\\n\\r\\n" and returned everything it had -- discarding nothing back
    to a buffer. That silently dropped whichever message didn't come first
    when the PBX coalesced two messages into one TCP segment (very common
    under load/retransmission), and truncated any body split across reads
    (breaking Content-Length parsing for 200 OK's SDP).

    This class keeps a persistent bytearray buffer per socket/dialog and:
      1. reads until a header terminator is buffered;
      2. parses Content-Length and keeps reading until the full body exists;
      3. returns exactly ONE message and leaves any remaining bytes buffered;
      4. serves already-buffered complete messages without touching the
         socket at all;
      5. reports three distinct outcomes (message / timeout / closed)
         instead of overloading "" and None.
    """

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = bytearray()

    def read_message(self, timeout: float) -> Frame:
        # First, see if a full message is already sitting in the buffer from
        # a previous coalesced read.
        msg = self._try_extract()
        if msg is not None:
            return Frame(FrameKind.MESSAGE, msg)

        self._sock.settimeout(timeout)
        try:
            chunk = self._sock.recv(4096)
        except socket.timeout:
            return Frame(FrameKind.TIMEOUT)
        except OSError:
            return Frame(FrameKind.CLOSED)

        if chunk == b"":
            return Frame(FrameKind.CLOSED)

        self._buf.extend(chunk)
        msg = self._try_extract()
        if msg is not None:
            return Frame(FrameKind.MESSAGE, msg)
        return Frame(FrameKind.TIMEOUT)

    def _try_extract(self) -> Optional[str]:
        sep = b"\r\n\r\n"
        idx = self._buf.find(sep)
        if idx == -1:
            return None

        header_bytes = bytes(self._buf[:idx])
        header_text = header_bytes.decode(errors="ignore")
        content_length = 0
        for line in header_text.split("\r\n"):
            if line.lower().startswith("content-length:"):
                try:
                    content_length = int(line.split(":", 1)[1].strip())
                except ValueError:
                    content_length = 0
                break

        body_start = idx + len(sep)
        body_end = body_start + content_length
        if len(self._buf) < body_end:
            # Body not fully arrived yet; wait for more bytes on the next
            # read instead of returning a truncated message.
            return None

        full = bytes(self._buf[:body_end]).decode(errors="ignore")
        del self._buf[:body_end]
        return full


# --------------------------------------------------------------------------
# Backwards-compatible shim. Nothing in this codebase should call this any
# more (CallSession uses SipStream), but kept so any external tooling/tests
# built against the old signature don't break outright.
# --------------------------------------------------------------------------
def recv_full(sock: socket.socket, timeout: float = 1.0) -> str | None:
    stream = SipStream(sock)
    frame = stream.read_message(timeout)
    if frame.kind == FrameKind.CLOSED:
        return None
    if frame.kind == FrameKind.TIMEOUT:
        return ""
    return frame.text
