"""
Tests for CallSession._perform_transfer -- the SIP REFER state machine.

This is the part of the transfer feature that talks to the PBX, and it was
the only part with no test at all. The three bugs pinned here were all
"treat a perfectly normal SIP message as a fatal error":

  1. A "100 Trying" response to the REFER was read as a rejection, so a PBX
     that sends one before "202 Accepted" aborted every transfer on the
     first message it sent.
  2. A NOTIFY carrying a "180 Ringing" sipfrag was read as a final failure.
     RFC 3515 has the notifier relay the referred-to leg's provisionals, so
     this fired on every transfer to an extension that rings before a human
     picks up -- i.e. the normal case.
  3. ANY in-dialog BYE during the REFER wait was reported as a successful
     transfer. A caller hanging up while on hold therefore wrote
     Status=Transfer for a call no human ever took, and quarantined the
     extension for TRANSFER_EXTENSION_BUSY_SECONDS.

The peer here is a real TCP socket on loopback driven by the test, not a
mock, because the thing under test is SipStream framing + message dispatch
and a mock would assume the answer. No PBX, no ElevenLabs, no RTP:
_close_el_session() short-circuits when self._conversation is None, which is
the case for a CallSession that was never bridged.
"""
from __future__ import annotations

import os
import socket
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sip_protocol as sip  # noqa: E402
from call_session import CallSession, _TransferRequest  # noqa: E402
from config import Settings  # noqa: E402
from extension_pool import ExtensionPool, ExtensionPoolExhausted  # noqa: E402
from port_allocator import PortAllocator  # noqa: E402

EXTENSION = "406"


@pytest.fixture
def peer():
    """A connected TCP socket pair: (session_side, pbx_side)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    session_side = socket.create_connection(listener.getsockname())
    pbx_side, _ = listener.accept()
    listener.close()

    yield session_side, pbx_side

    for s in (session_side, pbx_side):
        try:
            s.close()
        except Exception:
            pass


@pytest.fixture
def session(peer, tmp_path):
    session_side, _ = peer
    settings = Settings()
    # Keep the failure/timeout tests quick.
    object.__setattr__(settings, "transfer_wait_seconds", 2.0)

    s = CallSession(
        phone_number="+201000000000",
        dynamic_variables={},
        settings=settings,
        port_allocator=PortAllocator(10000, 10999, 1.0),
        extension_pool=ExtensionPool([EXTENSION], cooldown_seconds=0.0),
        tracking_id=None,  # keeps _db_call a no-op: no SQL Server in tests
    )
    s._sock = session_side
    s._stream = sip.SipStream(session_side)
    return s


def _dialog_kwargs(session: CallSession) -> dict:
    return dict(
        local_ip="127.0.0.1",
        local_port=5060,
        pbx_ip="127.0.0.1",
        target_number=session.phone_number,
        ext_user="9999",
        call_id=session._sip_call_id,
        from_tag=session._from_tag,
    )


def _run_transfer(session: CallSession) -> dict:
    """Run _perform_transfer on its own thread (it is the SIP thread in
    production) and hand back a dict of what it returned.

    The extension is taken from the pool exactly as the bridge loop takes
    it, not hand-written into the request: ExtensionPool.release() is a
    silent no-op for an extension that was never acquired, so a hand-built
    request makes every release/quarantine assertion vacuously pass."""
    extension = session._extension_pool.acquire()
    assert extension == EXTENSION
    request = _TransferRequest(extension=extension, source="agent_phrase")
    out: dict = {}

    def _go():
        cseq, outcome = session._perform_transfer(
            request, dialog_kwargs=_dialog_kwargs(session), remote_tag=";tag=pbx1", cseq=10
        )
        out["cseq"] = cseq
        out["outcome"] = outcome
        out["result"] = request.result

    t = threading.Thread(target=_go, name="perform-transfer")
    t.start()
    out["thread"] = t
    return out


def _read_refer(pbx: socket.socket) -> str:
    """Read the REFER the session just sent and return it."""
    pbx.settimeout(5.0)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = pbx.recv(4096)
        assert chunk, "session closed the socket instead of sending a REFER"
        data += chunk
    text = data.decode()
    assert text.startswith("REFER "), f"expected a REFER, got: {text.splitlines()[0]}"
    return text


def _refer_cseq(refer_text: str) -> int:
    for line in refer_text.split("\r\n"):
        if line.upper().startswith("CSEQ:"):
            return int(line.split()[1])
    raise AssertionError("no CSeq in REFER")


def _response(code: int, reason: str, cseq: int) -> bytes:
    return (
        f"SIP/2.0 {code} {reason}\r\n"
        f"Via: SIP/2.0/TCP 127.0.0.1:5060;branch=z9hG4bKtest\r\n"
        f"From: <sip:9999@127.0.0.1>;tag=local\r\n"
        f"To: <sip:+201000000000@127.0.0.1>;tag=pbx1\r\n"
        f"Call-ID: test-call-id\r\n"
        f"CSeq: {cseq} REFER\r\n"
        f"Content-Length: 0\r\n"
        f"\r\n"
    ).encode()


def _notify(frag_code: int, frag_reason: str) -> bytes:
    body = f"SIP/2.0 {frag_code} {frag_reason}\r\n"
    return (
        f"NOTIFY sip:9999@127.0.0.1 SIP/2.0\r\n"
        f"Via: SIP/2.0/TCP 127.0.0.1:5060;branch=z9hG4bKnotify\r\n"
        f"From: <sip:+201000000000@127.0.0.1>;tag=pbx1\r\n"
        f"To: <sip:9999@127.0.0.1>;tag=local\r\n"
        f"Call-ID: test-call-id\r\n"
        f"CSeq: 1 NOTIFY\r\n"
        f"Event: refer\r\n"
        f"Subscription-State: active;expires=60\r\n"
        f"Content-Type: message/sipfrag;version=2.0\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"\r\n"
        f"{body}"
    ).encode()


def _bye() -> bytes:
    return (
        f"BYE sip:9999@127.0.0.1 SIP/2.0\r\n"
        f"Via: SIP/2.0/TCP 127.0.0.1:5060;branch=z9hG4bKbye\r\n"
        f"From: <sip:+201000000000@127.0.0.1>;tag=pbx1\r\n"
        f"To: <sip:9999@127.0.0.1>;tag=local\r\n"
        f"Call-ID: test-call-id\r\n"
        f"CSeq: 2 BYE\r\n"
        f"Content-Length: 0\r\n"
        f"\r\n"
    ).encode()


class TestProvisionalResponsesAreNotFailures:
    def test_100_trying_before_202_still_transfers(self, session, peer):
        """THE BUG: `if 200 <= code < 300 ... else: failed` treated a 100
        Trying for the REFER as a rejection, killing the transfer on the
        first message a PBX that sends one ever emitted."""
        _, pbx = peer
        run = _run_transfer(session)

        cseq = _refer_cseq(_read_refer(pbx))
        pbx.sendall(_response(100, "Trying", cseq))
        pbx.sendall(_response(202, "Accepted", cseq))
        pbx.sendall(_notify(200, "OK"))

        run["thread"].join(timeout=10)
        assert not run["thread"].is_alive()
        assert run["outcome"] == "transferred"
        assert run["result"]["success"] is True
        assert session.transferred_to == EXTENSION

    def test_180_ringing_notify_is_not_a_final_outcome(self, session, peer):
        """THE BUG: only frag_code == 100 was treated as provisional, so
        the 180 Ringing that RFC 3515 has the notifier relay while the
        extension is alerting was read as a final failure."""
        _, pbx = peer
        run = _run_transfer(session)

        cseq = _refer_cseq(_read_refer(pbx))
        pbx.sendall(_response(202, "Accepted", cseq))
        pbx.sendall(_notify(100, "Trying"))
        pbx.sendall(_notify(180, "Ringing"))
        pbx.sendall(_notify(200, "OK"))

        run["thread"].join(timeout=10)
        assert not run["thread"].is_alive()
        assert run["outcome"] == "transferred"
        assert run["result"]["success"] is True

    def test_183_session_progress_is_not_a_final_outcome(self, session, peer):
        _, pbx = peer
        run = _run_transfer(session)

        cseq = _refer_cseq(_read_refer(pbx))
        pbx.sendall(_response(202, "Accepted", cseq))
        pbx.sendall(_notify(183, "Session Progress"))
        pbx.sendall(_notify(200, "OK"))

        run["thread"].join(timeout=10)
        assert run["outcome"] == "transferred"

    def test_final_non_2xx_notify_still_fails(self, session, peer):
        """The fix must not swallow real failures: a 486 sipfrag means the
        extension really is busy."""
        _, pbx = peer
        run = _run_transfer(session)

        cseq = _refer_cseq(_read_refer(pbx))
        pbx.sendall(_response(202, "Accepted", cseq))
        pbx.sendall(_notify(180, "Ringing"))
        pbx.sendall(_notify(486, "Busy Here"))

        run["thread"].join(timeout=10)
        assert run["outcome"] == "failed"
        assert run["result"]["success"] is False
        assert session.transferred_to is None
        assert session._transfer_failed is True

    def test_4xx_response_to_refer_still_fails(self, session, peer):
        """A real rejection of the REFER itself is still a rejection."""
        _, pbx = peer
        run = _run_transfer(session)

        cseq = _refer_cseq(_read_refer(pbx))
        pbx.sendall(_response(403, "Forbidden", cseq))

        run["thread"].join(timeout=10)
        assert run["outcome"] == "failed"
        assert session._transfer_failed is True


class TestByeDuringTransfer:
    def test_bye_after_ringing_is_a_completed_transfer(self, session, peer):
        """Some PBXes complete a blind transfer by BYEing our leg instead
        of sending a final NOTIFY. Once the referred-to leg has alerted,
        that reading is sound and must keep working."""
        _, pbx = peer
        run = _run_transfer(session)

        cseq = _refer_cseq(_read_refer(pbx))
        pbx.sendall(_response(202, "Accepted", cseq))
        pbx.sendall(_notify(180, "Ringing"))
        pbx.sendall(_bye())

        run["thread"].join(timeout=10)
        assert run["outcome"] == "transferred"
        assert run["result"]["success"] is True
        assert session.transferred_to == EXTENSION

    def test_bye_with_no_progress_is_a_caller_hangup_not_a_transfer(self, session, peer):
        """THE BUG: the BYE arm was unconditional, so a caller hanging up
        while on hold -- before anything indicated the referred-to leg had
        gone anywhere -- was recorded as a successful transfer."""
        _, pbx = peer
        run = _run_transfer(session)

        cseq = _refer_cseq(_read_refer(pbx))
        pbx.sendall(_response(202, "Accepted", cseq))
        pbx.sendall(_bye())

        run["thread"].join(timeout=10)
        assert not run["thread"].is_alive()
        assert run["outcome"] == "remote_bye"
        assert run["result"]["success"] is False
        assert session.transferred_to is None, "a hangup must not be recorded as a transfer"
        assert session._transfer_failed is True

    def test_caller_hangup_does_not_quarantine_the_extension(self, session, peer):
        """The old behaviour marked the extension busy for
        TRANSFER_EXTENSION_BUSY_SECONDS (300s by default) over a transfer
        that never happened -- under load that starves the pool."""
        _, pbx = peer
        run = _run_transfer(session)

        cseq = _refer_cseq(_read_refer(pbx))
        pbx.sendall(_response(202, "Accepted", cseq))
        pbx.sendall(_bye())
        run["thread"].join(timeout=10)

        # cooldown_seconds=0.0 on this pool, so a plain release() makes the
        # extension immediately reusable; a busy_seconds release would not.
        assert session._extension_pool.acquire() == EXTENSION

    def test_completed_transfer_does_quarantine_the_extension(self, session, peer):
        """The mirror of the test above -- a human really is on that
        extension now, so it must NOT come straight back to the pool."""
        _, pbx = peer
        run = _run_transfer(session)

        cseq = _refer_cseq(_read_refer(pbx))
        pbx.sendall(_response(202, "Accepted", cseq))
        pbx.sendall(_notify(180, "Ringing"))
        pbx.sendall(_bye())
        run["thread"].join(timeout=10)
        assert run["outcome"] == "transferred"

        with pytest.raises(ExtensionPoolExhausted):
            session._extension_pool.acquire()

    def test_bye_is_answered_with_200_ok(self, session, peer):
        """Whichever way the BYE is interpreted, it must be acknowledged --
        the bridge loop breaks without sending one of its own."""
        _, pbx = peer
        run = _run_transfer(session)

        cseq = _refer_cseq(_read_refer(pbx))
        pbx.sendall(_response(202, "Accepted", cseq))
        pbx.sendall(_bye())
        run["thread"].join(timeout=10)

        pbx.settimeout(5.0)
        reply = pbx.recv(4096).decode()
        assert reply.startswith("SIP/2.0 200"), reply.splitlines()[0]


class TestTimeout:
    def test_no_response_at_all_times_out_as_failed(self, session, peer):
        _, pbx = peer
        run = _run_transfer(session)
        _read_refer(pbx)  # ...and then say nothing

        run["thread"].join(timeout=10)
        assert run["outcome"] == "failed"
        assert run["result"]["success"] is False
        assert session._transfer_failed is True


def _cseq_of(text: str, method: str) -> int:
    """CSeq number of a message, asserting it belongs to `method`."""
    for line in text.split("\r\n"):
        if line.upper().startswith("CSEQ:"):
            number, seen = line.split()[1], line.split()[2]
            assert seen.upper() == method.upper(), f"expected a {method} CSeq, got {seen}"
            return int(number)
    raise AssertionError(f"no CSeq in:\n{text}")


class TestByeCseq:
    """RFC 3261 §12.2.1.1: a new request within a dialog MUST carry a CSeq
    strictly greater than the previous one. Same number + different method
    lets a strict peer answer 500, and the PBX-side dialog then outlives our
    own cleanup."""

    def test_send_bye_increments_the_cseq(self, session, peer):
        _, pbx = peer
        used = session._send_bye(
            dialog_kwargs=_dialog_kwargs(session), remote_tag=";tag=pbx1", cseq=10
        )
        assert used == 11

        pbx.settimeout(5.0)
        msg = pbx.recv(4096).decode()
        assert msg.startswith("BYE "), msg.splitlines()[0]
        assert _cseq_of(msg, "BYE") == 11

    def test_bye_after_a_failed_refer_does_not_reuse_the_refer_cseq(self, session, peer):
        """THE BUG: the hangup path built its BYE with whatever `cseq` it
        happened to be holding, and after a failed transfer that is the
        REFER's own number. This branch turned a failed transfer from rare
        into routine, so it stopped being theoretical."""
        _, pbx = peer
        run = _run_transfer(session)
        refer_cseq = _refer_cseq(_read_refer(pbx))
        pbx.sendall(_response(403, "Forbidden", refer_cseq))
        run["thread"].join(timeout=10)
        assert run["outcome"] == "failed"

        # Exactly what the bridge loop does on the way out: hang up using
        # the cseq _perform_transfer handed back.
        session._send_bye(
            dialog_kwargs=_dialog_kwargs(session), remote_tag=";tag=pbx1", cseq=run["cseq"]
        )

        pbx.settimeout(5.0)
        msg = pbx.recv(4096).decode()
        assert _cseq_of(msg, "BYE") > refer_cseq, "BYE reused the REFER's CSeq"
