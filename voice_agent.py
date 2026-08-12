"""
Vendor-neutral seam between the SIP/RTP call machinery and whatever
conversational-voice provider actually drives the conversation.

Stage 0 of the provider-abstraction work: nothing in this module knows which
vendor is in use. CallSession builds its agent through make_voice_agent() and
talks to it only through the VoiceAgent contract below; audio_bridge
implements AudioInterface without importing any vendor SDK. Concrete
implementations live in per-provider modules (e.g. elevenlabs_agent), which
are imported lazily inside the factory so that:

  - importing audio_bridge (or this module) does not require any vendor SDK to
    be installed, and
  - only the selected provider's dependencies are ever loaded.

The trade-off of that lazy import is deliberate and inseparable from the
first point: a missing or broken provider package now surfaces on the first
call that builds an agent, rather than at process start.

AudioInterface is method-for-method identical to the ElevenLabs SDK's own
AudioInterface ABC. That SDK duck-types the object it is handed -- it never
does an isinstance() check -- so RtpAudioInterface keeps satisfying it by
shape alone while inheriting from this ABC instead.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    # Import-time only: keeps `import audio_bridge` from pulling in config
    # (and therefore load_dotenv) as a side effect of this seam.
    from config import Settings


class AudioInterface(ABC):
    """Bidirectional audio transport for one conversation.

    Implemented by the RTP bridge; driven by the voice agent.
    """

    @abstractmethod
    def start(self, input_callback: Callable[[bytes], None]) -> None:
        """Start moving audio. `input_callback` is called regularly with
        inbound audio from the caller as 16-bit PCM mono at 16 kHz."""

    @abstractmethod
    def stop(self) -> None:
        """Stop and release the transport. `input_callback` must not be
        invoked after this returns."""

    @abstractmethod
    def output(self, audio: bytes) -> None:
        """Play agent audio (16-bit PCM mono, 16 kHz) toward the caller.
        Must return promptly and must not block the calling thread."""

    @abstractmethod
    def interrupt(self) -> None:
        """Drop any buffered, not-yet-played agent audio (barge-in)."""


class VoiceAgent(ABC):
    """One conversational session with a voice provider.

    The lifecycle mirrors what CallSession already does: construct eagerly
    (so that setup errors surface at construction time, before the RTP media
    gap is measured), start_session() on a watchdogged thread,
    wait_for_session_end() on a second thread, and end_session() during
    cleanup.
    """

    @abstractmethod
    def start_session(self) -> None:
        """Open the session and begin conversing. Returns once the session is
        running; the conversation itself continues in the background."""

    @abstractmethod
    def wait_for_session_end(self) -> Optional[str]:
        """Block until the session ends, then return the provider's
        conversation id if it has one."""

    @abstractmethod
    def end_session(self) -> None:
        """End the session and release provider-side resources. Safe to call
        whether or not the session ever started."""


def _resolve_provider(settings: "Settings") -> type[VoiceAgent]:
    """Import and return the class for the configured provider."""
    provider = (settings.voice_agent_provider or "").strip().lower()

    if provider == "elevenlabs":
        # Lazy on purpose -- see the module docstring.
        from elevenlabs_agent import ElevenLabsVoiceAgent

        return ElevenLabsVoiceAgent

    raise ValueError(
        f"unknown VOICE_AGENT_PROVIDER: {settings.voice_agent_provider!r}"
    )


def warm_provider(settings: "Settings") -> None:
    """Load the provider module once, at startup.

    Two problems this avoids, both caused by the lazy import above firing on
    the first call instead of at process start:

      - the SDK import (~0.6s cold) lands inside the first call's
        media_start_gap_ms window -- the dead-air gap between ACK and
        start_session() that F-09 exists to minimise; and
      - a missing or broken provider package stays hidden until that first
        call, surfacing as a failed call rather than a failed startup.

    Raises whatever the provider import raises, so a misconfigured
    VOICE_AGENT_PROVIDER stops the service from coming up at all.
    """
    _resolve_provider(settings)


def make_voice_agent(
    *,
    settings: "Settings",
    audio_interface: AudioInterface,
    dynamic_variables: dict,
    on_agent_response: Callable[[str], None],
    on_user_transcript: Callable[[str], None],
) -> VoiceAgent:
    """Build the agent for the configured provider.

    Provider-specific credentials and ids are read off `settings` by the
    provider module itself, which keeps this signature vendor-neutral.
    """
    return _resolve_provider(settings)(
        settings=settings,
        audio_interface=audio_interface,
        dynamic_variables=dynamic_variables,
        on_agent_response=on_agent_response,
        on_user_transcript=on_user_transcript,
    )
