"""
SQL Server connectivity for call status tracking.

Writes call lifecycle timestamps/status to dbo.BatchCallDetails, keyed by
TrackingId (the tracking_id carried in a call's dynamic_variables). Connection
settings are sourced from environment variables via python-dotenv, following
the same pattern as config.py -- never hardcode credentials here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pyodbc
from dotenv import load_dotenv

load_dotenv(override=True)


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class DbSettings:
    sql_driver: str = field(default_factory=lambda: _env_str("SQL_DRIVER", "ODBC Driver 18 for SQL Server"))
    sql_server: str = field(default_factory=lambda: _env_str("SQL_SERVER", ""))
    sql_database: str = field(default_factory=lambda: _env_str("SQL_DATABASE", ""))
    sql_uid: str = field(default_factory=lambda: _env_str("SQL_UID", ""))
    sql_pwd: str = field(default_factory=lambda: _env_str("SQL_PWD", ""))


db_settings = DbSettings()

# Statuses as logged in dbo.BatchCallDetails.Status (nvarchar(20) -- both
# new values below are short by design, but there's ample room either way).
STATUS_RINGING = "Ringing"
STATUS_ANSWERED = "Answered"
STATUS_CANCELLED = "Cancelled"
STATUS_TIMEOUT = "Timeout"
STATUS_TRANSFERRED = "Transfer"      # REFER confirmed, caller landed on the extension
STATUS_TRANSFER_FAILED = "TranFail"  # transfer was announced but never completed

# SIP final responses that mean the callee/PBX rejected or cancelled the dialog.
_CANCELLED_SIP_CODES = {486, 503}


def get_connection() -> pyodbc.Connection:
    """Open a new connection to the BatchCallDetails database."""
    return pyodbc.connect(
        f"Driver={{{db_settings.sql_driver}}};"
        f"Server={db_settings.sql_server};"
        f"Database={db_settings.sql_database};"
        f"UID={db_settings.sql_uid};"
        f"PWD={db_settings.sql_pwd};"
        "TrustServerCertificate=yes;"
    )


def sip_response_to_status(sip_code: int) -> Optional[str]:
    """Map a SIP final-response code to a terminal Status value, if applicable."""
    if sip_code in _CANCELLED_SIP_CODES:
        return STATUS_CANCELLED
    return None


def mark_ringing(tracking_id: str, ringing_at: Optional[datetime] = None) -> None:
    """Record that the callee started ringing (180 Ringing received)."""
    ringing_at = ringing_at or datetime.now(timezone.utc)
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE dbo.BatchCallDetails SET RingAt = ?, Status = ? WHERE TrackingId = ?",
            ringing_at,
            STATUS_RINGING,
            tracking_id,
        )
        conn.commit()
    finally:
        conn.close()


def mark_answered(tracking_id: str, answered_at: Optional[datetime] = None) -> None:
    """Record that the call was answered (200 OK to INVITE received)."""
    answered_at = answered_at or datetime.now(timezone.utc)
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE dbo.BatchCallDetails SET AnsweredAt = ?, Status = ? WHERE TrackingId = ?",
            answered_at,
            STATUS_ANSWERED,
            tracking_id,
        )
        conn.commit()
    finally:
        conn.close()


def mark_ended(
    tracking_id: str,
    ended_at: Optional[datetime] = None,
    status: Optional[str] = None,
) -> None:
    """
    Record that the call ended -- a BYE was sent/received, the dialog was
    cancelled (486/503, or CANCEL sent for an unconfirmed dialog on ring
    timeout), the call was transferred (or an announced transfer failed to
    complete), or otherwise terminated.

    Pass `status=STATUS_CANCELLED`, `status=STATUS_TIMEOUT`,
    `status=STATUS_TRANSFERRED`, or `status=STATUS_TRANSFER_FAILED` for
    those terminal cases. Leave `status` unset for a normal BYE after the
    call was already marked Answered -- Status stays as-is and only EndedAt
    is set.
    """
    ended_at = ended_at or datetime.now(timezone.utc)
    conn = get_connection()
    try:
        if status is not None:
            conn.execute(
                "UPDATE dbo.BatchCallDetails SET EndedAt = ?, Status = ? WHERE TrackingId = ?",
                ended_at,
                status,
                tracking_id,
            )
        else:
            conn.execute(
                "UPDATE dbo.BatchCallDetails SET EndedAt = ? WHERE TrackingId = ?",
                ended_at,
                tracking_id,
            )
        conn.commit()
    finally:
        conn.close()
