"""
Configuration for the outbound calling service.

All values are sourced from environment variables (with defaults matching the
original script) so that credentials and per-environment tuning never live in
source code. Load a .env file with `python-dotenv` or via your process
manager / container orchestrator.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

from transfer_trigger import normalize_arabic

load_dotenv(override=True)


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val is not None else default


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val is not None else default


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # --- SIP / PBX ---
    pbx_ip: str = field(default_factory=lambda: _env_str("PBX_IP", "10.3.1.250"))
    pbx_port: int = field(default_factory=lambda: _env_int("PBX_PORT", 5060))
    ext_user: str = field(default_factory=lambda: _env_str("EXT_USER", "409"))
    ext_pass: str = field(default_factory=lambda: _env_str("EXT_PASS", ""))
    local_ip: str = field(default_factory=lambda: _env_str("LOCAL_IP", "10.1.2.20"))
    # NOTE: the original fixed LOCAL_PORT=5060 is intentionally NOT reused for
    # outbound legs. Every concurrent TCP connection to the same PBX:5060
    # would otherwise collide on an identical 4-tuple. Each call now binds an
    # ephemeral local port; see CallSession for details.
    sip_connect_timeout: float = field(default_factory=lambda: _env_float("SIP_CONNECT_TIMEOUT", 5.0))
    sip_recv_timeout: float = field(default_factory=lambda: _env_float("SIP_RECV_TIMEOUT", 5.0))
    sip_bridge_poll_timeout: float = 0.3

    # --- Call lifecycle timeouts (F-02) — every long-running loop in
    # CallSession must terminate on one of these even if no SIP/RTP event
    # ever arrives. This is what makes a dead PBX / dead ElevenLabs endpoint
    # / forever-ringing callee release its thread, port, and RAM instead of
    # leaking until someone Ctrl+C's the process.
    max_ring_seconds: float = field(default_factory=lambda: _env_float("MAX_RING_SECONDS", 45.0))
    max_call_seconds: float = field(default_factory=lambda: _env_float("MAX_CALL_SECONDS", 600.0))
    rtp_inactivity_seconds: float = field(default_factory=lambda: _env_float("RTP_INACTIVITY_SECONDS", 15.0))
    el_start_timeout_seconds: float = field(default_factory=lambda: _env_float("EL_START_TIMEOUT_SECONDS", 10.0))
    max_auth_attempts: int = field(default_factory=lambda: _env_int("MAX_AUTH_ATTEMPTS", 2))
    cancel_wait_seconds: float = field(default_factory=lambda: _env_float("CANCEL_WAIT_SECONDS", 5.0))
    # How long to wait for the final response to a BYE before giving up and
    # closing the socket. The BYE used to be fire-and-forget, so a rejected
    # one was invisible and could leave our leg up on the PBX -- see
    # CallSession._await_bye_response.
    bye_response_timeout_seconds: float = field(
        default_factory=lambda: _env_float("BYE_RESPONSE_TIMEOUT_SECONDS", 2.0)
    )

    # --- Call transfer (SIP REFER to an internal extension) ---
    # CSV of extensions the agent may blind-transfer a call to, e.g. "201,202,203".
    transfer_extensions: str = field(default_factory=lambda: _env_str("TRANSFER_EXTENSIONS", ""))
    transfer_wait_seconds: float = field(default_factory=lambda: _env_float("TRANSFER_WAIT_SECONDS", 15.0))
    transfer_extension_cooldown_seconds: float = field(
        default_factory=lambda: _env_float("TRANSFER_EXTENSION_COOLDOWN_SECONDS", 2.0)
    )
    # How long a successfully-transferred extension is assumed busy before
    # it's eligible again -- see extension_pool.py's caveat about no real
    # presence signal from the PBX.
    transfer_extension_busy_seconds: float = field(
        default_factory=lambda: _env_float("TRANSFER_EXTENSION_BUSY_SECONDS", 300.0)
    )

    # Phrase the agent speaks to trigger an internal transfer. Detection is
    # on the agent's own transcript (CallSession._maybe_trigger_transfer,
    # fed by callback_agent_response) -- ElevenLabs no longer POSTs to this
    # service, and no client tool call is required, to start a transfer.
    transfer_trigger_phrase: str = field(
        default_factory=lambda: _env_str("TRANSFER_TRIGGER_PHRASE", "هيتم تحويل المكالمة دلوقتي")
    )
    # CSV of additional phrases that also trigger a transfer if spoken.
    transfer_trigger_extra_phrases: str = field(
        default_factory=lambda: _env_str("TRANSFER_TRIGGER_EXTRA_PHRASES", "")
    )
    # How long to let the trigger sentence finish playing out to the caller
    # before sending the REFER -- callback_agent_response fires when the
    # LLM's text arrives, seconds before the caller actually hears it.
    transfer_playout_timeout_seconds: float = field(
        default_factory=lambda: _env_float("TRANSFER_PLAYOUT_TIMEOUT_SECONDS", 10.0)
    )
    transfer_playout_quiet_seconds: float = field(
        default_factory=lambda: _env_float("TRANSFER_PLAYOUT_QUIET_SECONDS", 0.6)
    )
    # How long to wait for the trigger sentence's TTS audio to START
    # arriving before giving up and transferring anyway. Needed because
    # callback_agent_response fires on the LLM's text, ahead of any audio --
    # without this the drain check below sees an empty queue and a stale
    # last-output timestamp and returns instantly. See
    # CallSession._wait_for_playout.
    transfer_playout_start_timeout_seconds: float = field(
        default_factory=lambda: _env_float("TRANSFER_PLAYOUT_START_TIMEOUT_SECONDS", 2.0)
    )
    # Bounded wait when closing the ElevenLabs websocket on the transfer
    # path -- end_session() must never be allowed to stall the SIP thread.
    el_end_session_timeout_seconds: float = field(
        default_factory=lambda: _env_float("EL_END_SESSION_TIMEOUT_SECONDS", 3.0)
    )

    # --- "All lines busy" static prompt (played when the transfer trigger
    # fires but no extension is free) ---
    busy_prompt_enabled: bool = field(default_factory=lambda: _env_bool("BUSY_PROMPT_ENABLED", True))
    busy_prompt_audio_path: str = field(
        default_factory=lambda: _env_str("BUSY_PROMPT_AUDIO_PATH", "./assets/audio/all_lines_busy.wav")
    )
    busy_prompt_tail_seconds: float = field(
        default_factory=lambda: _env_float("BUSY_PROMPT_TAIL_SECONDS", 0.8)
    )

    # Normalised, blank-filtered phrase tuple, computed once at Settings
    # construction (see __post_init__) rather than per transcript line --
    # on_agent_response runs on the ElevenLabs SDK's websocket receive
    # thread for every live call and must stay cheap. An empty entry here
    # (e.g. a trailing comma in TRANSFER_TRIGGER_EXTRA_PHRASES) is filtered
    # out deliberately: an empty phrase is a substring of everything, which
    # would transfer on the agent's first word.
    transfer_trigger_phrases_normalized: tuple[str, ...] = field(default_factory=tuple, init=False)

    # --- RTP ---
    rtp_port_min: int = field(default_factory=lambda: _env_int("RTP_PORT_MIN", 10000))
    rtp_port_max: int = field(default_factory=lambda: _env_int("RTP_PORT_MAX", 10999))
    rtp_port_cooldown_seconds: float = field(default_factory=lambda: _env_float("RTP_PORT_COOLDOWN_SECONDS", 5.0))
    frame_ms: int = 20
    frame_bytes: int = 160  # 8kHz PCMU, 20ms/frame

    # Anti-alias low-pass on the outbound (agent -> caller) leg. audioop.ratecv
    # does not filter, so without this everything the agent produces above
    # 4 kHz folds back into the speech band -- measured at 23.4 dB SNR against
    # a filtered reference. Flagged so it can be A/B'd against real calls;
    # AUDIO_ANTIALIAS=false restores the previous behaviour exactly.
    audio_antialias: bool = field(default_factory=lambda: _env_bool("AUDIO_ANTIALIAS", True))
    audio_antialias_cutoff_hz: float = field(
        default_factory=lambda: _env_float("AUDIO_ANTIALIAS_CUTOFF_HZ", 3400.0)
    )

    # The mirror of the above on the inbound (caller -> agent) leg. ratecv's
    # linear interpolation leaves spectral images above 4 kHz in what STT
    # hears -- rejected by only 4.1 dB at 3400 Hz -- and droops the passband
    # by 3.1 dB at 3000 Hz. AUDIO_ANTIIMAGE=false restores the old path.
    audio_antiimage: bool = field(default_factory=lambda: _env_bool("AUDIO_ANTIIMAGE", True))
    audio_antiimage_cutoff_hz: float = field(
        default_factory=lambda: _env_float("AUDIO_ANTIIMAGE_CUTOFF_HZ", 3600.0)
    )

    # --- ElevenLabs ---
    agent_id: str = field(default_factory=lambda: _env_str("AGENT_ID", ""))
    elevenlabs_api_key: str = field(default_factory=lambda: _env_str("ELEVENLABS_API_KEY", ""))

    # --- Post-call conversation analysis (conversation_analysis.py) ---
    # After a call ends, fetch the ElevenLabs conversation record so the
    # evaluation-criteria results and data-collection fields (e.g.
    # "PaymentDate") reach the .NET client. ElevenLabs computes these
    # asynchronously, hence the poll.
    fetch_conversation_analysis: bool = field(
        default_factory=lambda: _env_bool("FETCH_CONVERSATION_ANALYSIS", True)
    )
    analysis_poll_interval_seconds: float = field(
        default_factory=lambda: _env_float("ANALYSIS_POLL_INTERVAL_SECONDS", 3.0)
    )
    analysis_max_wait_seconds: float = field(
        default_factory=lambda: _env_float("ANALYSIS_MAX_WAIT_SECONDS", 90.0)
    )
    analysis_request_timeout_seconds: float = field(
        default_factory=lambda: _env_float("ANALYSIS_REQUEST_TIMEOUT_SECONDS", 15.0)
    )
    # Dump the COMPLETE conversation record (including the turn-by-turn
    # transcript -- real customer speech) to logs/conversations/*.json.
    # Off by default; enable for debugging/verification only.
    log_conversation_json: bool = field(
        default_factory=lambda: _env_bool("LOG_CONVERSATION_JSON", False)
    )

    # --- Concurrency / capacity ---
    # F-29: the stated requirement is 30 concurrent calls on 4 vCPU. 50 was
    # an untested default; keep 30 as the production default and only raise
    # it after the soak/jitter tests in the work order pass at the new value.
    max_concurrent_calls: int = field(default_factory=lambda: _env_int("MAX_CONCURRENT_CALLS", 30))
    call_retention_seconds: int = field(default_factory=lambda: _env_int("CALL_RETENTION_SECONDS", 900))
    shutdown_grace_seconds: int = field(default_factory=lambda: _env_int("SHUTDOWN_GRACE_SECONDS", 20))

    # --- Request/queue backpressure (F-19) ---
    max_recipients_per_request: int = field(default_factory=lambda: _env_int("MAX_RECIPIENTS_PER_REQUEST", 100))
    max_queue_depth_multiplier: int = field(default_factory=lambda: _env_int("MAX_QUEUE_DEPTH_MULTIPLIER", 2))
    max_queue_wait_seconds: float = field(default_factory=lambda: _env_float("MAX_QUEUE_WAIT_SECONDS", 300.0))

    # --- Logging (F-01) ---
    log_dir: str = field(default_factory=lambda: _env_str("LOG_DIR", "./logs"))
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO"))
    log_to_console: bool = field(default_factory=lambda: _env_bool("LOG_TO_CONSOLE", False))
    log_transcripts: bool = field(default_factory=lambda: _env_bool("LOG_TRANSCRIPTS", False))
    log_max_bytes: int = field(default_factory=lambda: _env_int("LOG_MAX_BYTES", 50 * 1024 * 1024))
    log_backup_count: int = field(default_factory=lambda: _env_int("LOG_BACKUP_COUNT", 10))
    log_queue_maxsize: int = field(default_factory=lambda: _env_int("LOG_QUEUE_MAXSIZE", 100_000))

    # --- Webhook notifications (replaces client-side polling of GET /calls/{id}) ---
    # Empty string disables webhook delivery entirely.
    webhook_url: str = field(default_factory=lambda: _env_str("WEBHOOK_URL", ""))
    webhook_timeout_seconds: float = field(default_factory=lambda: _env_float("WEBHOOK_TIMEOUT_SECONDS", 5.0))
    webhook_max_retries: int = field(default_factory=lambda: _env_int("WEBHOOK_MAX_RETRIES", 3))
    webhook_max_retry_age_seconds: float = field(
        default_factory=lambda: _env_float("WEBHOOK_MAX_RETRY_AGE_SECONDS", 3600.0)
    )
    webhook_dead_letter_path: str = field(
        default_factory=lambda: _env_str("WEBHOOK_DEAD_LETTER_PATH", "./logs/webhook_dead_letter.jsonl")
    )

    # --- Tamweely AddCallResult push (see ADD_CALL_RESULT.md) ---
    # Pushes the outcome of calls that were never answered to Tamweely's
    # AddCallResultByTrackingId endpoint. Those calls produce no ElevenLabs
    # conversation, so the post-call webhook that normally delivers a result
    # never fires for them. Off by default: enabling it makes this service
    # write to a customer-facing system, so it must be a deliberate act.
    # A push is a no-op unless the flag is on AND both URL and key are set.
    add_call_result_enabled: bool = field(
        default_factory=lambda: _env_bool("ADD_CALL_RESULT_ENABLED", False)
    )
    # Origin only, e.g. https://host -- the documented route is appended in
    # add_call_result.py.
    add_call_result_base_url: str = field(
        default_factory=lambda: _env_str("ADD_CALL_RESULT_BASE_URL", "")
    )
    add_call_result_api_key: str = field(
        default_factory=lambda: _env_str("ADD_CALL_RESULT_API_KEY", "")
    )
    add_call_result_timeout_seconds: float = field(
        default_factory=lambda: _env_float("ADD_CALL_RESULT_TIMEOUT_SECONDS", 10.0)
    )
    # Higher than the webhook's 3 on purpose: the .NET client can create the
    # BatchCallDetails row up to ~3.4 s after POST /calls is accepted (see
    # CALL_STATUS_TRACKING.md, "The RingAt race"), and a 486 Busy can resolve
    # a call in ~1 s, so the first pushes may legitimately find no row. Five
    # attempts with the backoff below span ~15 s, comfortably past that.
    add_call_result_max_retries: int = field(
        default_factory=lambda: _env_int("ADD_CALL_RESULT_MAX_RETRIES", 5)
    )
    add_call_result_max_retry_age_seconds: float = field(
        default_factory=lambda: _env_float("ADD_CALL_RESULT_MAX_RETRY_AGE_SECONDS", 3600.0)
    )
    add_call_result_dead_letter_path: str = field(
        default_factory=lambda: _env_str(
            "ADD_CALL_RESULT_DEAD_LETTER_PATH", "./logs/add_call_result_dead_letter.jsonl"
        )
    )

    # --- API security (F-20, F-23) ---
    # Shared-secret header the .NET client must present. Empty disables the
    # check (NOT recommended in production -- set this before exposing the
    # service beyond localhost).
    api_shared_secret: str = field(default_factory=lambda: _env_str("API_SHARED_SECRET", ""))
    bind_host: str = field(default_factory=lambda: _env_str("BIND_HOST", "127.0.0.1"))
    bind_port: int = field(default_factory=lambda: _env_int("BIND_PORT", 8000))
    rate_limit_per_minute: int = field(default_factory=lambda: _env_int("RATE_LIMIT_PER_MINUTE", 120))
    phone_allow_prefixes: str = field(default_factory=lambda: _env_str("PHONE_ALLOW_PREFIXES", ""))  # CSV, empty = allow all
    phone_deny_prefixes: str = field(default_factory=lambda: _env_str("PHONE_DENY_PREFIXES", ""))  # CSV
    max_dynamic_variables_bytes: int = field(default_factory=lambda: _env_int("MAX_DYNAMIC_VARIABLES_BYTES", 8192))
    max_dynamic_variables_keys: int = field(default_factory=lambda: _env_int("MAX_DYNAMIC_VARIABLES_KEYS", 64))

    # --- Arabic name normalization for TTS ---
    # The source data stores `علي` (Ali) as `على`, which is also the preposition
    # "on", so the agent says the wrong word. name_normalizer.py repairs the
    # spelling of the keys listed below before the call is placed. Only the
    # ElevenLabs side sees the correction -- the webhook still echoes the raw
    # payload the client sent. Off restores the previous behaviour exactly.
    name_normalization: bool = field(default_factory=lambda: _env_bool("NAME_NORMALIZATION", True))
    name_normalization_keys: str = field(  # CSV, parsed like phone_allow_prefixes
        default_factory=lambda: _env_str(
            "NAME_NORMALIZATION_KEYS",
            "user_name,user_name_full,call_receiver,guarantor_name,guarantor_name_full",
        )
    )
    # CSV of `name_key:gender_key`. A few names are female with a final ى and
    # male with a final ي (`يسرى` Yosra vs `يسري` Yosri), so the payload's own
    # gender column decides. Only the first token of a name consults it -- the
    # tokens after it are the father's and grandfather's names.
    name_normalization_gender_keys: str = field(
        default_factory=lambda: _env_str(
            "NAME_NORMALIZATION_GENDER_KEYS",
            "user_name:br_gender,user_name_full:br_gender,call_receiver:cr_gender,"
            "guarantor_name:gr_gender,guarantor_name_full:gr_gender",
        )
    )

    def __post_init__(self) -> None:
        # Settings is frozen (immutable after construction, like every other
        # field here) -- object.__setattr__ is the documented escape hatch
        # dataclasses itself uses internally for exactly this case.
        raw_phrases = [self.transfer_trigger_phrase] + self.transfer_trigger_extra_phrases.split(",")
        normalized = tuple(
            normalize_arabic(p) for p in raw_phrases if normalize_arabic(p)
        )
        object.__setattr__(self, "transfer_trigger_phrases_normalized", normalized)


settings = Settings()
