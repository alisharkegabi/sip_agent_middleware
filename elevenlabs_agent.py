"""
ElevenLabs implementation of the VoiceAgent seam.

This is the only module in the service that imports the `elevenlabs` package.

Everything here was lifted out of CallSession._bridge() unchanged: the client
construction, the ConversationInitiationData wrapping of dynamic_variables,
the requires_auth rule, and the callback wiring. The callbacks are what feed
the RTP bridge's turn-latency instrumentation, so keeping them attached to
exactly the same SDK hooks is what keeps the reported latency numbers
identical.
"""
from __future__ import annotations

from typing import Callable, Optional

from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation, ConversationInitiationData

from config import Settings
from voice_agent import AudioInterface, VoiceAgent


class ElevenLabsVoiceAgent(VoiceAgent):
    def __init__(
        self,
        *,
        settings: Settings,
        audio_interface: AudioInterface,
        dynamic_variables: dict,
        on_agent_response: Callable[[str], None],
        on_user_transcript: Callable[[str], None],
    ):
        client = ElevenLabs(api_key=settings.elevenlabs_api_key)
        config = ConversationInitiationData(dynamic_variables=dynamic_variables)

        # The SDK duck-types audio_interface (no isinstance check), so an
        # implementation of our own AudioInterface ABC is accepted as-is.
        self._conversation = Conversation(
            client=client,
            agent_id=settings.agent_id,
            requires_auth=bool(settings.elevenlabs_api_key),
            audio_interface=audio_interface,
            config=config,
            callback_agent_response=on_agent_response,
            callback_user_transcript=on_user_transcript,
        )

    def start_session(self) -> None:
        self._conversation.start_session()

    def wait_for_session_end(self) -> Optional[str]:
        return self._conversation.wait_for_session_end()

    def end_session(self) -> None:
        self._conversation.end_session()
