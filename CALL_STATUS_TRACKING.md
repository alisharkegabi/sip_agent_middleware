# Call Status Tracking (dbo.BatchCallDetails)

Writes call lifecycle timestamps and status to a SQL Server table,
`dbo.BatchCallDetails`, keyed by `TrackingId` (the `tracking_id` carried in
a call's `dynamic_variables`). Four new PascalCase columns are written:
`RingingAt`, `AnsweredAt`, `EndedAt` (all `datetime`), and `Status`
(`nvarchar(20)`, confirmed live via `INFORMATION_SCHEMA.COLUMNS`).

## Status values (PascalCase strings)

This is now a **six**-status scheme (originally four; `Transfer`/`TranFail`
were added alongside the phrase-triggered internal transfer feature — see
`TRANSFER_FEATURE.md`):

- `Ringing` — authenticated INVITE sent, waiting for the callee to answer.
- `Answered` — 200 OK received for the INVITE.
- `Cancelled` — SIP final response `486` or `503`, or the middleware
  cancelled the dialog itself before the callee answered (e.g. an API-driven
  hangup mid-ring).
- `Timeout` — no answer within `MAX_RING_SECONDS`; the middleware sent
  CANCEL for the unconfirmed dialog (`_send_cancel()`, `exit_reason ==
  "ring_timeout"`).
- `Transfer` — the agent said the internal-transfer trigger phrase and the
  SIP REFER completed successfully; the caller landed on a human extension
  (`exit_reason == "transferred"`).
- `TranFail` — the agent **announced** a transfer (said the trigger phrase)
  and it did **not** complete, for any reason: no extension was free
  (`exit_reason == "transfer_unavailable"`, caller heard the "all lines
  busy" prompt), or the PBX rejected/timed out the REFER and the call
  continued normally afterward. The short spelling is deliberate — both new
  values fit the existing `nvarchar(20)` column with room to spare, no
  schema change needed. This status exists because the caller was told
  "we'll transfer you now" and it didn't happen (or, for the busy case, was
  explicitly promised a callback within two days) — the row has to be
  findable to build that follow-up list, which a plain `Answered` would not
  allow.

For a normal end-of-call that involved no transfer attempt at all (BYE sent
or received after the call was answered, max call duration, or RTP
inactivity), only `EndedAt` is stamped — `Status` is left as whatever it
already was (`Answered`), since none of those are one of the six tracked
statuses.

### Ordering in `_record_call_ended()` (call_session.py)

The check order matters and is intentional:

1. **`exit_reason == "transferred"` first.** A call that failed one
   transfer attempt and then succeeded on a retry (the agent said the
   trigger phrase again after a rejected REFER) must record `Transfer`, not
   `TranFail`. An actual success always wins over an earlier failure on the
   same call.
2. **A sticky `self._transfer_failed` flag second** — checked before
   `ring_timeout` / `local_hangup` / the 486/503 reject-code mapping. Those
   three are all pre-answer states where no transfer could have been
   announced, so there's no real conflict with them; but this position also
   means a failed transfer followed by e.g. `sip_disconnect` (a reason
   nothing else in the table writes a status for) still records `TranFail`.
   That's intended — the transfer failure is a known fact, not a guess, and
   the callback obligation holds regardless of how the call finally ended.

See `tests/test_call_status_mapping.py` for the full decision table exercised
against every row.

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
- `STATUS_TRANSFERRED = "Transfer"` / `STATUS_TRANSFER_FAILED = "TranFail"` —
  added for the internal transfer feature (see `TRANSFER_FEATURE.md`).
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
  `_record_call_ended(exit_reason)`. See the "Ordering" subsection above for
  the full, order-dependent chain — summary:
  - `exit_reason == "transferred"` → `Status = 'Transfer'`
  - `exit_reason == "transfer_unavailable"` **or** the sticky
    `self._transfer_failed` flag is set → `Status = 'TranFail'`
  - `exit_reason == "ring_timeout"` → `Status = 'Timeout'`
  - `exit_reason == "local_hangup"` **and call was never answered** →
    `Status = 'Cancelled'`
  - `_last_reject_code` maps via `sip_response_to_status()` to
    `'Cancelled'` (i.e. `486`/`503`) → `Status = 'Cancelled'`
  - `exit_reason` in `("remote_bye", "agent_ended", "local_hangup"` *after
    answered*`, "max_duration", "rtp_timeout")` → `EndedAt` only, `Status`
    untouched
  - Anything else (auth failures, connect timeouts, port exhaustion, etc.)
    **and no transfer was ever announced** — no DB write at all; not one of
    the six tracked statuses, so nothing is guessed.

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

**Column width, confirmed:** queried live —
`SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME='BatchCallDetails' AND COLUMN_NAME='Status'` returned
`nvarchar(20)`. `Transfer` (8 chars) and `TranFail` (8 chars) both fit with
plenty of room; no schema change was needed for the new statuses. If the
.NET consumer treats `Status` as a closed enum on its side, it still needs
both new values added there.

## Not yet done / possible follow-ups

- No automated tests were added for the SIP-dialog status writes themselves
  (no existing test harness for SIP dialogs in this repo to extend) — but
  `tests/test_call_status_mapping.py` does exercise the full
  `_record_call_ended()` decision table (including the new `Transfer` /
  `TranFail` branches) against a stub, with no real DB connection.
- SIP rejection codes other than `486`/`503` (e.g. `603`, `600`, `604`,
  redirects) don't write a DB status — only `EndedAt` would be skipped
  entirely for those today, since they don't map to one of the six tracked
  statuses. Confirm whether any of those should also count as `Cancelled`.
- Writes are synchronous, on the SIP thread that also drives the dialog —
  a slow/unreachable SQL Server adds latency to the call flow (bounded only
  by the ODBC driver's own connect/query timeout, not `MAX_CALL_SECONDS`
  or similar). Consider moving these to a background thread/queue if that
  becomes a problem in production.
- A rejected/timed-out transfer resets the one-shot transfer guard so the
  agent can legitimately retry once the trigger phrase is said again; there
  is no cap on how many `TranFail`-then-retry cycles one call can go
  through before either succeeding or ending some other way.
