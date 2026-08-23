# Tamweely AddCallResult push

Pushes the outcome of **calls that were never answered** to Tamweely's
`AddCallResultByTrackingId` endpoint, keyed by the same `TrackingId` that
[`db.py`](db.py) already uses for `dbo.BatchCallDetails`.

**Configuring it turns it on.** There is no separate enable flag: set
`ADD_CALL_RESULT_BASE_URL` and `ADD_CALL_RESULT_API_KEY` in `.env` and every
unanswered call is reported from then on. Leave either blank and nothing is sent.

## Why it exists

Tamweely's `AddCallResult` push is normally driven by the **ElevenLabs post-call
webhook**: a conversation finishes, ElevenLabs notifies the .NET side, and it
POSTs the result.

A call that is never answered — busy, no answer, ring timeout, hangup mid-ring —
produces no ElevenLabs conversation at all. That webhook never fires, and **no
result ever reaches Tamweely.** This middleware is the only component that knows
those outcomes, so it pushes them itself.

## What is pushed, and what is not

The push set is deliberately **narrower** than the `Status` values written to
`BatchCallDetails`. A pushed `Cancelled`/`Timeout` permanently overwrites
`FinalOutcome` (→ 202 «العميل عدم رد») and `SummaryArabic` on Tamweely's side, so
it is only sent where the statement is true.

| Outcome | `BatchCallDetails.Status` | Pushed? | Why |
|---|---|---|---|
| Ring timeout (`MAX_RING_SECONDS`) | `Timeout` | ✅ `Timeout` | Customer did not pick up |
| SIP `486` Busy | `Cancelled` | ✅ `Cancelled` | Customer's phone rejected the call |
| Client hangup before answer (`POST /calls/{id}/hangup`) | `Cancelled` | ✅ `Cancelled` | The .NET client cancelled this call deliberately |
| SIP `503` Service Unavailable | `Cancelled` | ❌ | **PBX or trunk failure.** The customer's phone was never reached, let alone unanswered |
| Hangup from service **shutdown** | `Cancelled` | ❌ | A deploy or service restart, not customer behaviour |
| Answered call (BYE, max duration, RTP timeout) | `Answered` | ❌ | Produced a conversation — the ElevenLabs webhook already delivers it |
| `Transfer` / `TranFail` | as before | ❌ | Answered calls; same as above |
| `connect_timeout`, `connect_failed`, `internal_error`, auth failure, port exhaustion | *(nothing written)* | ❌ | Never reached the customer |

The two `❌` rows that still write `Cancelled` locally are the important ones: the
`Status` column is ours to record freely, but a push is a claim made to a
customer-facing system.

### Still open with Tamweely

SIP `408`, `480`, `487`, `600`, `603` and `604` push nothing today. Whether any of
them count as "customer no answer" is a Tamweely business question — the same one
already open in [CALL_STATUS_TRACKING.md](CALL_STATUS_TRACKING.md). Nothing is
guessed.

If Tamweely rules on them, **two** places need the code, not one:

1. `db._CANCELLED_SIP_CODES` (or a new mapping in `db.sip_response_to_status`) — so
   the code produces a `Status` at all. Without this the push branch is never reached.
2. `add_call_result.CUSTOMER_NO_ANSWER_SIP_CODES` — so that status is also pushed.

The push rides on the DB branch: `_record_call_ended` only consults
`is_customer_no_answer()` inside the `reject_status is not None` arm. Adding `603` to
the second set alone changes nothing, which is what
`test_widening_the_table_widens_the_push` pins down.

## Why `?status=` is always sent

Guide §5 step 3: with `FinalOutcome` still null the payload falls back to the
**AgentType default** — 206 for Contact, 202 for Collection. A no-answer call on a
Contact agent would therefore push 206. Only `?status=Cancelled` / `?status=Timeout`
force 202 (§4.1). The enum binds by name, and those names match `db.py`'s
`STATUS_*` constants character for character.

## Delivery and retries

`HTTP 200 does not mean success.` Per guide §8, both "tracking id not found" and a
Tamweely-side failure come back 200 — the `isSuccess` field in the body is what
decides, and it is checked with `is True`, not truthiness.

| Response | Action |
|---|---|
| `200` + `isSuccess: true` | Done |
| `200` + `isSuccess: false` | **Retry** |
| `200` + `validationErrors` non-empty | Dead-letter — the request itself is malformed, retries rebuild it identically |
| `200` + non-JSON body | Retry |
| `401` | Dead-letter, log loudly — a wrong key fails identically every time |
| `408` / `409` / `425` / `429` / `5xx` | Retry |
| other `4xx` | Dead-letter |
| transport error / timeout | Retry |

**Why `isSuccess: false` is retried.** "No BatchCallDetail found for TrackingId …"
is the row-arrives-late race measured in
[CALL_STATUS_TRACKING.md](CALL_STATUS_TRACKING.md) ("The RingAt race"): the .NET
client can create the row up to ~3.4 s after `POST /calls` is accepted, while a 486
Busy can resolve a call in ~1 s. The other documented cause is a Tamweely-side
outage — re-sending after exactly that is the endpoint's own stated purpose (§1).

> **This is at-least-once delivery.** A lost response produces a duplicate push, and
> the guide does not state that the receiving API is idempotent. Retries are bounded
> by `ADD_CALL_RESULT_MAX_RETRIES`. See the blockers below before enabling this in
> production.

Retries run on daemon `threading.Timer`s with exponential backoff plus jitter, so no
pool thread ever sleeps. This mirrors [`webhook_client.py`](webhook_client.py) (F-18).

`push_async()` does **no network I/O** — the request always runs on the pool, and the
call measured **0.7 ms** on the SIP thread. It is not unconditionally free: building
the sender runs `os.makedirs()` on the dead-letter directory, and a push arriving
after shutdown is dead-lettered inline. `CallManager` builds the sender at startup
specifically so the first case never lands on a SIP thread. Neither is a network
round trip, which is the thing that must never sit in front of a live call.

## Before enabling in production

Raised in review, not fixable in code. **One remains open.**

### 1. Is a repeated AddCallResult for one `TrackingId` harmless? — OPEN

Delivery is **at-least-once**, and it cannot be made exactly-once from this side. If
Tamweely processes the request and forwards it downstream but the response is lost or
times out, `requests.post()` raises and the sender retries — the work was done, only
the acknowledgement was lost. Guide §5 confirms the status change and the downstream
push both happen *before* the success response and the `IsCallResultPosted` flag are
written, so a retry repeats real work.

Everything avoidable has been avoided: permanent failures are not retried, retries are
bounded, and a delivered result is never placed in the dead-letter worklist. What
remains needs receiver-side idempotency — or confirmation that a duplicate is
harmless.

### 2. Are `tracking_id`s actually unique per call? — answered

Raised in review because `POST /calls` validates only that `tracking_id` is non-empty
([main.py:186](main.py:186)) and `CallManager` notes that IDs are not guaranteed unique
across the process lifetime ([call_manager.py:217](call_manager.py:217)). Two unanswered
sessions sharing an ID would push against the same external record.

**Confirmed by the team on 2026-08-23: the reuse seen during local testing was test
data only — production generates one per recipient**, matching guide §4 ("a GUID
generated per recipient at call-fire time"). Not a blocker.

Still worth knowing that nothing in this service *enforces* it, and that the same
exposure already applies to the `BatchCallDetails` writes in [db.py](db.py), which are
keyed the same way. If a duplicate ever did reach production, the symptom would be one
call's outcome overwriting another's on Tamweely's side.


## Dead letters

Anything undeliverable is appended as one JSON line to
`ADD_CALL_RESULT_DEAD_LETTER_PATH` (default `./logs/add_call_result_dead_letter.jsonl`):

```json
{"tracking_id": "...", "status": "Timeout", "_dead_lettered_at": 1755689000.0, "_reason": "retries_exhausted"}
```

`_reason` is one of `retries_exhausted`, `unauthorized`, `validation_error`,
`http_<code>`, `shutting_down`, `shutdown_cancelled_retry`, `schedule_failed`,
`unexpected_error`.

`shutdown_cancelled_retry` means a retry was still pending when the service
stopped. Cancelling its timer would otherwise throw the outcome away, so it is
written here instead. A retry that fired in the same instant as the cancel can
produce two lines for one `tracking_id` — a duplicate in a manual worklist is
much cheaper than a missing one.

**The API key is never written to this file or to any log line.** A record only
ever carries `tracking_id` and `status`. Server-controlled text — `errorMessage`,
`validationErrors`, and a transport exception's message — is additionally scrubbed
for the key before it is logged, in case a proxy echoes the request headers back
in an error body.

That file is the re-push worklist. The endpoint is built for exactly this, so a
line can be replayed by hand:

```bash
curl -X POST "https://<host>/v1/data/add-call-result/by-tracking-id/<tracking_id>?status=Timeout" -H "X-Api-Key: <key>"
```

There is no automatic replay on startup. A crash before the first attempt loses the
push entirely; the `BatchCallDetails` row still holds the outcome.

## `.env` settings (add these yourself; not committed)

```bash
ADD_CALL_RESULT_BASE_URL=https://<the Tamweely host>
ADD_CALL_RESULT_API_KEY=<the shared API key>
```

Both are required. Setting them **is** the decision to push — there is no separate
on/off flag, by deliberate choice, so there is no state where the service is half
configured and silently doing nothing.

`ADD_CALL_RESULT_BASE_URL` is an **origin only** — the documented route
`/v1/data/add-call-result/by-tracking-id/{trackingId}` is appended in code.

Optional, with defaults:

```bash
ADD_CALL_RESULT_TIMEOUT_SECONDS=10
ADD_CALL_RESULT_MAX_RETRIES=5
ADD_CALL_RESULT_MAX_RETRY_AGE_SECONDS=3600
ADD_CALL_RESULT_DEAD_LETTER_PATH=./logs/add_call_result_dead_letter.jsonl
```

`ADD_CALL_RESULT_MAX_RETRIES` counts **total attempts**, not retries after the
first: `5` means one initial try plus four retries, spanning roughly 15 s of
backoff — comfortably past the ~3.4 s row-creation race.

### Confirming which mode a running service is in

With no flag to read back, the startup log is the source of truth. Exactly one of
these appears once, when the service starts:

```
AddCallResult push ACTIVE -> <hostname> -- unanswered calls WILL be reported to Tamweely
AddCallResult push INACTIVE: ADD_CALL_RESULT_BASE_URL and/or ADD_CALL_RESULT_API_KEY
is not set -- unanswered calls will NOT be reported to Tamweely
```

Check that line after any deployment. The hostname is logged so a wrong environment
is visible immediately; the API key never is.

**If NEITHER line appears**, the service did not load `.env` at all. Under NSSM that
usually means `AppDirectory` is not the folder containing `.env`:

```bash
nssm set OutboundCallingService AppDirectory "C:\path\to\service"
```

`config.py` calls `load_dotenv()` with no explicit path, so python-dotenv searches
upward from the working directory. Same class of failure as a stale `LOCAL_IP`.

**`.env` beats the process environment.** That call passes `override=True`, so a value
in `.env` wins over one exported by NSSM, the shell, or a container. Setting
`ADD_CALL_RESULT_BASE_URL` as a service environment variable will *not* take effect if
`.env` also defines it — edit `.env`, not the service config. This applies to every
setting in `config.py`, not just these.

### Turning it off

Blank `ADD_CALL_RESULT_BASE_URL` (or the key) and restart. That is the only
off-switch — there is no flag to flip and no way to disable it without a restart.

## Files

| File | Change |
|---|---|
| [add_call_result.py](add_call_result.py) | New. `AddCallResultSender` plus module-level `push_async()` / `shutdown()`, and `CUSTOMER_NO_ANSWER_SIP_CODES` |
| [config.py](config.py) | Six `ADD_CALL_RESULT_*` settings (no enable flag) |
| [call_session.py](call_session.py) | `_push_call_result()` beside `_db_call()`; three branches of `_record_call_ended()` set a push status; `request_hangup(reason=...)` records who asked |
| [call_manager.py](call_manager.py) | Tags the shutdown drain `reason="shutdown"`; calls `add_call_result.shutdown()` last |
| [tests/test_add_call_result.py](tests/test_add_call_result.py) | New, 59 tests |
| [tests/test_call_status_mapping.py](tests/test_call_status_mapping.py) | Push / no-push across the whole decision table |

## Testing

```bash
pytest tests/test_add_call_result.py tests/test_call_status_mapping.py -q
```

No test opens a socket or contacts a real endpoint.

### Verified against live calls, 2026-08-23

Both push branches were exercised end to end on the real PBX, with the middleware
pointed at `Claude_files/fake_tamweely_server.py` on localhost rather than Tamweely:

| Trigger | SIP evidence | Pushed |
|---|---|---|
| Busy extension (`201`) | `486 Busy Here` 1.06 s after scheduling | `Cancelled` |
| Unanswered extension (`406`) | `sent CANCEL for unconfirmed dialog` at 45.26 s | `Timeout` |

Three pushes, every one accepted on the first attempt, no dead letters. The push
followed the SIP rejection by 34 ms and the terminal webhook by ~2 s, and the SIP
thread showed no delay between the final response and cleanup.

The `Cancelled`-from-client-hangup branch (`POST /calls/{id}/hangup` before answer) has
not been triggered by a live call; it is covered by the unit tests and by
`Claude_files/fake_endpoint_e2e.py`, which drives the whole decision table through a
real socket.

Note the ~1 s figure above against the ~3.4 s row-creation race in
[CALL_STATUS_TRACKING.md](CALL_STATUS_TRACKING.md): a busy number resolves well inside
the window where the `BatchCallDetails` row may not exist yet, so the
`isSuccess: false` retry path is expected to be exercised routinely in production.

The regression tests for the shutdown races, the dead-lettering of cancelled
retries, and the log redaction were each mutation-checked: the fix was reverted
and the corresponding test confirmed to fail.

## Concurrency notes

- `request_hangup()` is **first-writer-wins** under `_status_lock`. A shutdown
  drain and an API hangup can both fire on one call; last-writer-wins let a
  default-`"api"` call arriving after the drain flip the reason back and push
  202 for a deploy.
- `get_sender()` takes its lock unconditionally — no double-checked fast path.
  The unlocked pre-check raced `shutdown()` and could build a *live* sender
  after the service had stopped. A sender built after shutdown is born shut
  down, so anything handed to it is dead-lettered rather than sent.
- The `_shutting_down` check, the timer registration and `timer.start()` all
  happen under one lock, so a retry timer cannot escape the shutdown cancel
  sweep and submit to a closed executor.
- Lock order is `_timers_lock` → `_dead_letter_lock`, never the reverse, and
  dead-letter I/O is always done after releasing `_timers_lock`.
- A pending retry is **claimed** by whichever of its timer or `shutdown()` first pops
  its `entry_id` from `_timers`. That is what makes each record handled exactly once:
  submitted, or dead-lettered, never both and never neither.
  `Timer.is_alive()` cannot decide this — a `Timer` is a `Thread` and stays alive
  while its callback runs, so shutdown would read an executing retry as still pending,
  dead-letter it, and let the attempt succeed anyway, putting a delivered result into
  the re-push worklist.
- The retry budget is checked against the age the record will have *after* the next
  backoff, not before it, so a wait cannot carry it past
  `ADD_CALL_RESULT_MAX_RETRY_AGE_SECONDS`.
