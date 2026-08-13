from __future__ import annotations

import re
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

# F-23: phone numbers are interpolated directly into a SIP request line and
# headers; a value containing \r\n would permit SIP header injection. This
# is deliberately conservative (E.164-ish digits, optional leading +).
_PHONE_RE = re.compile(r"^\+?[0-9]{6,15}$")


class CallStatus(str, Enum):
    PENDING = "pending"        # accepted, waiting for a free worker slot
    DIALING = "dialing"        # TCP connect + INVITE sent
    RINGING = "ringing"        # 401 handled, authenticated INVITE sent, awaiting answer
    CONNECTED = "connected"    # 200 OK received, RTP + ElevenLabs bridge running
    COMPLETED = "completed"    # ended cleanly (BYE either way)
    FAILED = "failed"          # error at any stage
    REJECTED = "rejected"      # 486 Busy / 603 Decline
    CANCELLED = "cancelled"    # cancelled before/while dialing (e.g. shutdown, queue timeout)


# --------------------------------------------------------------------------
# Inbound request contract (matches the C# application's POST /calls body)
# --------------------------------------------------------------------------
class ConversationInitiationClientData(BaseModel):
    dynamic_variables: dict[str, Any] = Field(default_factory=dict)


class RecipientRequest(BaseModel):
    phone_number: str
    conversation_initiation_client_data: ConversationInitiationClientData

    @field_validator("phone_number")
    @classmethod
    def _validate_phone(cls, v: str) -> str:
        if not _PHONE_RE.match(v):
            raise ValueError(
                "phone_number must match ^\\+?[0-9]{3,15}$ (F-23: prevents SIP header injection)"
            )
        return v


class CallsRequest(BaseModel):
    recipients: list[RecipientRequest]


# --------------------------------------------------------------------------
# Outbound response contracts
# --------------------------------------------------------------------------
class ScheduledCall(BaseModel):
    call_id: str
    phone_number: str
    tracking_id: Optional[str] = None
    status: CallStatus


class CallsResponse(BaseModel):
    id: str
    scheduled: list[ScheduledCall]


class CallDetail(BaseModel):
    call_id: str
    # F-03: this was `str` (required). CallSession.to_dict() returns None
    # until (and unless) the ElevenLabs session ends cleanly, so FastAPI's
    # response-model validation raised on every poll of an in-progress
    # call, which main.py's catch-all handler turned into a bare 500. Every
    # other Optional[...] field below was audited against the same class of
    # bug at the same time.
    conversation_id: Optional[str] = None
    phone_number: str
    tracking_id: Optional[str] = None
    status: CallStatus
    error: Optional[str] = None
    created_at: float
    dialed_at: Optional[float] = None
    connected_at: Optional[float] = None
    ended_at: Optional[float] = None
    remote_rtp_port: Optional[int] = None
    local_rtp_port: Optional[int] = None
    last_turn_latency: Optional[dict] = None
    # F-17: distinguish "customer heard the message" from "media died after
    # two seconds" -- both used to record as CallStatus.COMPLETED.
    exit_reason: Optional[str] = None
    answered: bool = False
    talk_seconds: Optional[float] = None
    transferred_to: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    active_calls: int
    capacity: int
    queued_calls: int
    total_scheduled: int
    total_completed: int
    total_failed: int
    rtp_ports_free: int
    rtp_ports_in_use: int
    transfer_extensions_free: int = 0
    transfer_extensions_in_use: int = 0
    uptime_seconds: float
    # F-19 observability
    queue_depth: int = 0
    oldest_queued_seconds: float = 0.0
    # F-07 thread-liveness observability
    reaper_alive: bool = True
    dropped_log_records: int = 0
