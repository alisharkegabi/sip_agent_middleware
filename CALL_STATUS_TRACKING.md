# Call Status Tracking (dbo.BatchCallDetails)

Writes call lifecycle timestamps and status to a SQL Server table,
`dbo.BatchCallDetails`, keyed by `TrackingId` (the `tracking_id` carried in
a call's `dynamic_variables`). Four new PascalCase columns are written:
`RingingAt`, `AnsweredAt`, `EndedAt` (all `datetime`), and `Status`
(`nvarchar`).

## Status values (PascalCase strings)

- `Ringing` — authenticated INVITE sent, waiting for the callee to answer.
- `Answered` — 200 OK received for the INVITE.
- `Cancelled` — SIP final response `486` or `503`, or the middleware
  cancelled the dialog itself before the callee answered (e.g. an API-driven
  hangup mid-ring).
- `Timeout` — no answer within `MAX_RING_SECONDS`; the middleware sent
  CANCEL for the unconfirmed dialog (`_send_cancel()`, `exit_reason ==
  "ring_timeout"`).

For a normal end-of-call (BYE sent or received after the call was answered,
max call duration, RTP inactivity, or a successful transfer), only
`EndedAt` is stamped — `Status` is left as whatever it already was
(`Answered`), since none of those are one of the four tracked statuses.

## Files changed

### `db.py` (new file)
- `DbSettings` — dataclass reading `SQL_DRIVER`, `SQL_SERVER`,
  `SQL_DATABASE`, `SQL_UID`, `SQL_PWD` from `.env` (same `python-dotenv`
  pattern as `config.py`). No credentials hardcoded, unlike the old
  `sqlservertest.py` scratch script.
- `get_connection()` — opens a `pyodbc` connection
  (`TrustServerCertificate=yes`).
- `sip_response_to_status(sip_code)` — maps SIP `486`/`503` to
  `STATUS_CANCELLED`; returns `None` for anything else.
- `mark_ringing(tracking_id, ringing_at=None)`
- `mark_answered(tracking_id, answered_at=None)`
- `mark_ended(tracking_id, ended_at=None, status=None)` — `status` is only
  written when explicitly passed; otherwise only `EndedAt` is updated.
- Every function opens its own connection and explicitly `conn.close()`s in
  a `finally` block — `pyodbc.Connection`'s context manager only
  commits/rolls back, it does **not** close the connection, so relying on
  `with get_connection() as conn:` alone would leak a connection on every
  call.
- All timestamps default to `datetime.now(timezone.utc)` if not passed.

### `requirements.txt`
Added `pyodbc==5.3.0` (matches the version already installed in `venv`;
was previously used only by the untracked `sqlservertest.py` script and
never pinned).

### `call_session.py`
- `import db`, plus a `_db_call(func, *args, **kwargs)` helper: no-ops if
  `self.tracking_id` is unset, and swallows/logs any DB exception so a
  database hiccup can never break a live call.
- `_last_reject_code: Optional[int]` added to `__init__` and set in
  `_wait_for_answer()` for every SIP rejection branch (300s redirect,
  `486/603/600/604`, and the generic 4xx/5xx/6xx catch-all that `503` falls
  into) so the exact code is available later.
- **Ringing**: `self._db_call(db.mark_ringing)` where `CallStatus.RINGING`
  is set, right after the authenticated INVITE is sent.
- **Answered**: `self._db_call(db.mark_answered)` where `self.answered =
  True` is set (200 OK received).
- **Ended**: centralized in `_finish()` (called from the `finally` block of
  `run()`, so it fires exactly once per call on every exit path) via a new
  `_record_call_ended(exit_reason)`:
  - `exit_reason == "ring_timeout"` → `Status = 'Timeout'`
  - `exit_reason == "local_hangup"` **and call was never answered** →
    `Status = 'Cancelled'`
  - `_last_reject_code` maps via `sip_response_to_status()` to
    `'Cancelled'` (i.e. `486`/`503`) → `Status = 'Cancelled'`
  - `exit_reason` in `("remote_bye", "agent_ended", "local_hangup"` *after
    answered*`, "max_duration", "rtp_timeout", "transferred")` →
    `EndedAt` only, `Status` untouched
  - Anything else (auth failures, connect timeouts, port exhaustion, etc.)
    — no DB write at all; not one of the four tracked statuses, so nothing
    is guessed.

## `.env` additions (add these yourself; not committed)

```bash
SQL_DRIVER=ODBC Driver 18 for SQL Server
SQL_SERVER=<server-ip-or-host>
SQL_DATABASE=VoxEngineDB
SQL_UID=<username>
SQL_PWD=<password>
```

## Required action outside this repo

`dbo.BatchCallDetails` needs the four new columns added:
`RingingAt datetime`, `AnsweredAt datetime`, `EndedAt datetime`, `Status
nvarchar(...)`. The existing `TrackingId` column is used as-is to find the
row to update.

## Not yet done / possible follow-ups

- No automated tests were added (no existing test harness for SIP dialogs
  in this repo to extend).
- SIP rejection codes other than `486`/`503` (e.g. `603`, `600`, `604`,
  redirects) don't write a DB status — only `EndedAt` would be skipped
  entirely for those today, since they don't map to one of the four
  tracked statuses. Confirm whether any of those should also count as
  `Cancelled`.
- Writes are synchronous, on the SIP thread that also drives the dialog —
  a slow/unreachable SQL Server adds latency to the call flow (bounded only
  by the ODBC driver's own connect/query timeout, not `MAX_CALL_SECONDS`
  or similar). Consider moving these to a background thread/queue if that
  becomes a problem in production.
