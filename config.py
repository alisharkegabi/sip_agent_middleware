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

    # --- RTP ---
    rtp_port_min: int = field(default_factory=lambda: _env_int("RTP_PORT_MIN", 10000))
    rtp_port_max: int = field(default_factory=lambda: _env_int("RTP_PORT_MAX", 10999))
    rtp_port_cooldown_seconds: float = field(default_factory=lambda: _env_float("RTP_PORT_COOLDOWN_SECONDS", 5.0))
    frame_ms: int = 20
    frame_bytes: int = 160  # 8kHz PCMU, 20ms/frame

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


settings = Settings()
