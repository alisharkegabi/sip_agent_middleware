# Hardened Outbound Calling Service — what changed and how to run it

This is your codebase with the P0 findings from `PRODUCTION_HARDENING_WORK_ORDER.md`
fully applied, plus the P1/P2 items that were safe to bundle in one pass. The
public HTTP contract (`POST /calls`, `GET /calls/{id}`, `GET /calls`,
`POST /calls/{id}/hangup`, `GET /health`) is unchanged and backward compatible.
The DSP pipeline (μ-law⇄PCM transcoding, `audioop.ratecv` state, 20ms/160-byte
framing, RMS VAD, the 4-stage latency math) was **not** touched — those values
are identical to before.

## What was fixed (by finding ID)

**P0 — the three causes of the Ctrl+C symptom, plus the HTTP 500 bug:**
- **F-01** `logging_config.py` — logging is now `QueueHandler`→`QueueListener`→
  `RotatingFileHandler`, non-blocking with a bounded, drop-on-full queue. No
  call-carrying thread can ever block on a console write again. Console
  logging is opt-in (`LOG_TO_CONSOLE=1`) and should stay off in production.
- **F-02** `call_session.py`, `config.py` — every wait loop (ring, bridge,
  ElevenLabs start) now has a monotonic deadline: `MAX_RING_SECONDS`,
  `MAX_CALL_SECONDS`, `RTP_INACTIVITY_SECONDS`, `EL_START_TIMEOUT_SECONDS`.
  `sock.connect()` finally uses `SIP_CONNECT_TIMEOUT` (it existed in config
  but was never applied).
- **F-03** `models.py` — `conversation_id` (and every other field that could
  legitimately be null mid-call) is now `Optional`. This alone was making
  every poll of an in-progress call return `500`.
- **F-04** `sip_protocol.py` (`SipStream`), `call_session.py` — a real
  stateful framer replaces `recv_full()`: it buffers across reads, parses
  `Content-Length`, and never silently drops a coalesced message (this is
  what was eating BYEs and causing calls to hang forever). `TCP_NODELAY` is
  now set; the previous unconditional `SO_LINGER(1,0)` RST-on-close is gone
  from the normal path.
- **F-05** full SIP response-class handling (1xx/2xx/3xx/4xx/5xx/6xx) plus
  RFC 2617 `qop=auth` digest support, capped at `MAX_AUTH_ATTEMPTS`.

**P1/P2 also applied:** F-06 (heavy refs dropped after cleanup), F-07 (every
long-lived thread — reaper, RTP send/recv, ElevenLabs wait — survives an
exception instead of dying silently), F-08 (RTP recv loop no longer dies on
the first transient error; Windows `WSAECONNRESET` suppressed), F-09 (RTP
silence starts immediately after ACK, before ElevenLabs session setup),
F-10 (latency file I/O moved off the audio thread's lock), F-12 (RTP targets
the SDP answer's media address, with symmetric-RTP latching), F-13 (RTP
payload-type filtering — DTMF/comfort-noise no longer decoded as speech),
F-14 (fresh SIP branch per transaction; OPTIONS/INFO/NOTIFY/UPDATE/re-INVITE
answered instead of ignored), F-15 (CANCEL for unconfirmed dialogs), F-16
(FIFO port reuse with a cooldown, even-ports-only, `SO_EXCLUSIVEADDRUSE` on
Windows), F-17 (real exit-reason tracking instead of blanket `COMPLETED`),
F-18 (webhook delivery re-enabled with non-blocking scheduled retries +
dead-letter file), F-19 (batch size cap, queue-depth cap → `429`, queue-wait
timeout), F-20 (shared-secret header + basic rate limit + phone
allow/deny-prefix list), F-21/F-22 (atomic, explicit counters), F-23 (E.164
validation, header-injection prevention, dynamic_variables size cap), F-24
(status fields written under the lock), F-25 (SIGTERM handler is now
non-blocking), F-26 (fixed malformed/truncated `200 OK` builder), F-28/F-29
(dead config removed, unused native-audio deps dropped, default capacity
set to 30 to match your stated hardware budget).

## What was deliberately NOT bundled in

- **F-11 (shared RTP transmit/receive scheduler).** The work order's own
  commit sequencing (§7) puts this in its own commit, after everything
  else, specifically because it changes the threading model for every
  active call and needs the dedicated jitter test in §6 before it ships.
  Bundling it into the same pass as the correctness fixes above would make
  it much harder to tell which change caused a regression if the jitter
  test fails. As a partial, safe mitigation, add `sys.setswitchinterval(0.001)`
  at process start and run the process at `HIGH_PRIORITY_CLASS` on Windows
  (both zero-risk); the actual thread-count reduction is a good next PR.
- **F-27 (`/metrics` endpoint).** `/health` now exposes the new counters
  (`queue_depth`, `oldest_queued_seconds`, `reaper_alive`,
  `dropped_log_records`), but a full per-call metrics endpoint (RTP
  loss/jitter estimates, media_start_gap histogram, etc.) is a larger,
  separate piece of work best done once you have a metrics sink to send it
  to (Prometheus, CloudWatch, whatever you're standardizing on).
- **RFC 2833 DTMF surfaced as a callback.** F-13's payload-type filter is in
  place (DTMF packets are correctly *not* decoded as audio), but wiring
  digits into an IVR-navigation callback is new functionality, not a fix —
  left as a follow-up per the work order's own phrasing ("useful for IVR
  navigation later").

## New configuration

See `.env.example` for the full list with defaults and comments. Copy it to
`.env` and fill in real values — **`API_SHARED_SECRET` and `EXT_PASS` in
particular must not ship with placeholder values.**

Internal call transfer (SIP REFER, triggered when the agent says
`هيتم تحويل المكالمة دلوقتي`, with a static "all lines busy" fallback) is a
separate feature — see `TRANSFER_FEATURE.md` and `CALL_STATUS_TRACKING.md`.

## Post-call analysis from ElevenLabs (evaluation criteria + data collection)

After a call ends, ElevenLabs asynchronously produces a conversation record
containing the agent's **evaluation criteria results** and **data collection
results** — the fields defined in the agent's *Analysis* tab, e.g.
`PaymentDate`. `conversation_analysis.py` fetches that record
(`GET /v1/convai/conversations/{id}`, via the SDK already in the process) and
hands it to the client. No public endpoint or webhook signature verification
is needed, unlike ElevenLabs' own post-call webhook.

The client now receives **two** notifications per call, identical except for
`event` and `analysis`:

| `event` | when | `analysis` |
|---|---|---|
| `call_status` | immediately at terminal status, exactly as before | `null` |
| `call_analysis` | once ElevenLabs has finished analysing (seconds later) | populated |

A consumer that only cares about completion keeps handling the first message
unchanged and can ignore the second. `GET /calls/{id}` also carries `analysis`
once it lands.

```json
"analysis": {
  "conversation_id": "conv_...",
  "status": "done",
  "branch_id": "agtbrch_...",
  "call_successful": "success",
  "transcript_summary": "...",
  "evaluation_criteria_results": { "<criterion>": { "result": "success", "rationale": "..." } },
  "data_collection_results": { "PaymentDate": { "value": "2026-08-20", "rationale": "...", "type": "string" } },
  "data_collection": { "PaymentDate": "2026-08-20" }
}
```

`data_collection_results` contains **only fields configured on the agent** —
if `PaymentDate` is not defined in the agent's Analysis → Data collection
settings, it will not appear here no matter what was said on the call.

Configuration:

| Variable | Default | Meaning |
|---|---|---|
| `FETCH_CONVERSATION_ANALYSIS` | `true` | Master switch |
| `ANALYSIS_POLL_INTERVAL_SECONDS` | `3.0` | Gap between readiness polls |
| `ANALYSIS_MAX_WAIT_SECONDS` | `90.0` | Give up after this; no second webhook is sent |
| `ANALYSIS_REQUEST_TIMEOUT_SECONDS` | `15.0` | Per-request HTTP timeout |
| `LOG_CONVERSATION_JSON` | `false` | Dump the **complete** record, transcript included, to `logs/conversations/<call_id>_<conversation_id>.json` |

`LOG_CONVERSATION_JSON` writes real customer speech to disk — keep it off in
production and use it only for verification. The transcript is never included
in the webhook payload or in `GET /calls/{id}`.

`fetch_conversation.py` prints the same record on demand for one conversation:

```
python fetch_conversation.py --latest --summary
python fetch_conversation.py conv_01k...
```

Threading follows the F-18 rule: polling is rescheduled on `threading.Timer`,
so no pool thread ever sleeps waiting for ElevenLabs, and the fetcher has its
own executor separate from both call workers and webhook delivery.

## Running it in production (Windows Server 2019+, per your prerequisites doc)

This is the section that actually eliminates the Ctrl+C ritual — the code
fixes above stop the *leaks*, but Cause A (console QuickEdit mode) is fixed
by **never running this in an interactive console**, full stop.

1. **Install as a Windows Service** — don't run `uvicorn` in a cmd/PowerShell
   window and leave it. Use [NSSM](https://nssm.cc/):
   ```powershell
   nssm install OutboundCallingService "C:\path\to\venv\Scripts\python.exe" `
     "-m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1"
   nssm set OutboundCallingService AppDirectory "C:\path\to\service"
   nssm set OutboundCallingService AppStdout "C:\path\to\service\logs\stdout.log"
   nssm set OutboundCallingService AppStderr "C:\path\to\service\logs\stderr.log"
   nssm set OutboundCallingService Start SERVICE_AUTO_START
   nssm start OutboundCallingService
   ```
   Redirecting stdout/stderr to files here is belt-and-suspenders — with
   `LOG_TO_CONSOLE=0` (the default) the app itself never writes to a console
   handle, but NSSM still needs *somewhere* to send anything printed before
   logging is configured.

2. **Exactly one worker.** `CallManager`, `PortAllocator`, and the session
   registry are all in-process, single-instance state. `--workers 2+` gives
   each worker its own port allocator and session dict → RTP port collisions
   and `404`s for calls owned by the other worker. Always `--workers 1`.

3. **If instead hosted under IIS + HttpPlatformHandler:** set the app pool's
   **Idle Time-out = 0**, **Regular Time Interval = 0** (disable recycling),
   **Start Mode = AlwaysRunning**, and disable Rapid-Fail Protection on long
   requests. Default IIS recycling will kill live calls mid-conversation.

4. **Bind privately.** Set `BIND_HOST=127.0.0.1` (default) unless the .NET
   client genuinely runs on a different host, in which case bind to a
   private interface — never `0.0.0.0` behind a public-facing ngrok tunnel.
   Set `API_SHARED_SECRET` and have the .NET client send it as `X-Api-Key`
   on every request.

5. **Windows Defender:** exclude the application directory and `LOG_DIR`
   from real-time scanning. Per-turn file writes and per-call log files
   otherwise cost tens of milliseconds on the audio path (this is the OS
   half of what F-10 fixed in code).

6. **Firewall/NAT:** open UDP `RTP_PORT_MIN`–`RTP_PORT_MAX` both directions
   between this server and the PBX/media node, and TCP 5060 outbound. RTP
   must take a direct route — ngrok (if used at all) is for the HTTP control
   plane only, never the media path.

7. **Capacity:** `MAX_CONCURRENT_CALLS=30` is the validated default for 4
   vCPU. 30 PCMU calls is ~5 Mbps, trivial for your 1 Gbps NIC — the real
   constraint is CPU/GIL contention (see the F-11 note above), not
   bandwidth. Budget 6–8 GB RSS; RSS should now stay flat after the first
   30 minutes of load (F-06/F-02 were the leaks).

8. **`.env` file permissions:** restrict the ACL to the service account, and
   confirm IIS (if used) isn't serving the `.env` file itself.

## Before go-live

Run the verification plan in §6 of the work order — framing unit tests
(concatenated messages / split body / peer-close mid-message), a 4-hour
soak at 30 concurrent calls with RSS/thread-count/port-count assertions, the
five chaos tests (PBX killed, PBX blackholed, ElevenLabs blackholed,
deliberately-coalesced BYE, callee never answers), and the console-freeze
regression test (confirm clicking in any terminal window has zero effect on
call quality once running as a service). None of the code changes above
substitute for actually running that plan once against your PBX.
