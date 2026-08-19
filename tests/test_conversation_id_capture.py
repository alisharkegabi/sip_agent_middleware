"""
Unit tests for early ElevenLabs conversation-id capture -- the path that
preserves post-call analysis for calls whose SIP leg ends before the SDK's
wait_for_session_end() returns.

CallSession.__init__ does no socket/file I/O (sockets are only opened later,
inside _dial()), so real CallSession instances are constructed directly
here rather than mocked -- this exercises the actual lock-protected session
state and webhook payload. No network, no sockets, no ElevenLabs session.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from call_session import CallSession, _read_conversation_id  # noqa: E402
from config import Settings  # noqa: E402
from extension_pool import ExtensionPool  # noqa: E402
from port_allocator import PortAllocator  # noqa: E402


def _make_session(**overrides) -> CallSession:
    settings = Settings()
    kwargs = dict(
        phone_number="+201234567890",
        dynamic_variables={"tracking_id": "test-tracking-id"},
        settings=settings,
        port_allocator=PortAllocator(10000, 10999, 1.0),
        extension_pool=ExtensionPool(["406"], cooldown_seconds=1.0),
        tracking_id="test-tracking-id",
    )
    kwargs.update(overrides)
    return CallSession(**kwargs)


class _ConversationStub:
    def __init__(self, conversation_id):
        self._conversation_id = conversation_id


class TestReadConversationId:
    def test_returns_id_from_private_sdk_attribute(self):
        assert _read_conversation_id(_ConversationStub("conversation-123")) == "conversation-123"

    def test_returns_none_when_id_is_unavailable(self):
        assert _read_conversation_id(None) is None
        assert _read_conversation_id(object()) is None
        assert _read_conversation_id(_ConversationStub(None)) is None
        assert _read_conversation_id(_ConversationStub("")) is None


class TestCaptureConversationId:
    def test_populates_session_and_webhook_payload(self):
        session = _make_session()
        session._conversation = _ConversationStub("conversation-123")

        session._capture_conversation_id()

        assert session.conversation_id == "conversation-123"
        assert session.to_webhook_payload()["conversation_id"] == "conversation-123"

    def test_does_not_overwrite_an_already_captured_id(self):
        session = _make_session()
        session._conversation = _ConversationStub("conversation-first")
        session._capture_conversation_id()

        session._conversation = _ConversationStub("conversation-second")
        session._capture_conversation_id()

        assert session.conversation_id == "conversation-first"

    def test_none_conversation_is_a_no_op(self):
        session = _make_session()
        session._conversation = None

        session._capture_conversation_id()

        assert session.conversation_id is None


class TestCleanupCapturesBeforeDroppingTheSession:
    """The regression this whole change exists for.

    Every exit path except "agent_ended" (transferred, local_hangup,
    remote_bye, rtp_timeout, max_duration) leaves _bridge()'s loop while the
    el-wait thread is still blocked in wait_for_session_end(), so that thread
    has not assigned conversation_id yet. _cleanup() then drops the live SDK
    object, and CallManager._on_call_done() reads conversation_id as None --
    at which point AnalysisFetcher.fetch_async() returns at its falsy guard
    and the post-call analysis is lost permanently, with nothing to retry.
    """

    def test_cleanup_captures_the_id_before_dropping_the_conversation(self):
        session = _make_session()
        session._conversation = _ConversationStub("conversation-transferred")

        session._cleanup()

        assert session._conversation is None, "cleanup must still drop the SDK object"
        assert session.conversation_id == "conversation-transferred"
        assert session.to_webhook_payload()["conversation_id"] == "conversation-transferred"
