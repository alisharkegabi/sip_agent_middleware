# Call Transfer Feature (SIP REFER to an internal extension)

Hands a live call off to a human on an internal PBX extension mid-
conversation, using a blind SIP REFER on the existing dialog. If no
extension is currently free, the caller hears a pre-rendered static audio
message instead, and the call ends cleanly.

**The trigger is the agent's own speech, not an ElevenLabs tool call or
webhook.** The middleware watches the agent's transcript for one exact
sentence:

> `هيتم تحويل المكالمة دلوقتي`

The moment the agent says it, the middleware starts the transfer itself —
no HTTP request from ElevenLabs is involved, and no client tool needs to be
configured in the ElevenLabs dashboard for this to work. The legacy
HTTP/client-tool entry points still exist as a fallback (see §"Legacy entry
points" below).

## Files changed

### `transfer_trigger.py` (new file)
Pure, socket-free phrase matching, split out for the same reason
`OutboundResampler` was lifted out of `RtpAudioInterface` — it needs to be
unit-testable and it runs on the ElevenLabs SDK's websocket receive thread,
so it has to stay cheap.

- `normalize_arabic(text)` — strips tashkeel/harakat, tatweel, normalises
  alef/hamza forms (`أ إ آ ٱ` → `ا`), `ى` → `ي`, `ة` → `ه`, `ؤ` → `و`,
  `ئ` → `ي`, converts Arabic-Indic digits to ASCII, strips ASCII and Arabic
  punctuation, collapses whitespace, casefolds.
- `matches_transfer_phrase(text, phrases)` — normalises `text` and every
  entry in `phrases`, returns `True` if any normalised phrase is a
  substring of the normalised text. A blank phrase never matches (guards
  against `TRANSFER_TRIGGER_EXTRA_PHRASES=""` splitting into `[""]`, which
  would otherwise transfer on the agent's first word).

### `static_audio.py` (new file)
Loads the "all lines busy" WAV into RTP-ready mu-law frames.

- `load_ulaw_frames(wav_path, frame_bytes=160)` — reads a mono 16-bit WAV
  (8 kHz or 16 kHz); a 16 kHz file is passed through `audio_bridge.py`'s
  `OutboundResampler` (a fresh instance, never the live call's own) so the
  prompt gets the same anti-alias treatment as agent speech; slices the
  result into 160-byte frames, padding the last one with mu-law silence.
- Cached at module level, keyed by path — decoded/filtered once per
  process, not once per call.
- Loaded eagerly in `CallManager.__init__` (`_load_busy_frames`), so a
  missing or corrupt file is a startup-time log error, not a live-call
  crash: the busy path falls back to hanging up silently if frames are
  unavailable.

### `audio_bridge.py`
Three additions to `RtpAudioInterface`, none of which touch the resampling/
VAD/latency math:
- `last_output_monotonic` — stamped at the top of every `output()` call, so
  the SIP thread can tell "the play queue is empty because the sentence
  finished" from "empty because the next TTS chunk hasn't arrived yet".
- `playout_pending()` — current play-queue depth.
- `play_static_frames(frames)` — queues pre-built mu-law frames directly
  (bypassing PCM→mu-law resampling, since the busy prompt is already
  mu-law), clearing whatever was queued first. Returns `False`, and logs
  the specific reason, if the interface can no longer transmit
  (`is_running=False` or no remote port) instead of failing silently.
- `_static_playback` latch — set by `play_static_frames()`; makes
  `output()` drop any agent TTS still arriving on the still-open
  websocket, so the static prompt owns the line without needing the
  session closed first. Never cleared, since the only caller is a terminal
  prompt immediately followed by hangup.

### `config.py` / `.env`
New settings (see the `.env` block below). `transfer_trigger_phrase` and
`transfer_trigger_extra_phrases` (CSV) are normalised **once**, at
`Settings` construction (`__post_init__`, since `Settings` is frozen) into
`transfer_trigger_phrases_normalized` — not per transcript line.

### `db.py`
Two new status constants (`Status` is `nvarchar(20)`, confirmed by querying
`INFORMATION_SCHEMA.COLUMNS` — both fit with room to spare):
- `STATUS_TRANSFERRED = "Transfer"`
- `STATUS_TRANSFER_FAILED = "TranFail"`

See `CALL_STATUS_TRACKING.md` for the full status scheme and the exact
`_record_call_ended()` ordering.

### `call_manager.py`
- Builds one shared `ExtensionPool` from `settings.transfer_extensions` at
  startup (unchanged) and now also eagerly loads the busy-prompt frames via
  `static_audio.load_ulaw_frames`, passing them into every `CallSession`.

### `call_session.py` (core of the feature)
- **`_maybe_trigger_transfer(text)`** — called from `on_agent_response` for
  every agent utterance on every live call. Runs on the ElevenLabs SDK's
  websocket receive thread, so it must return in microseconds: it only
  matches the phrase and, if matched, hands off to the SIP thread. Wrapped
  in try/except — an exception here would otherwise kill the receive
  thread and take the whole conversation down with it.
- **`_claim_transfer()`** — a per-`CallSession` compare-and-set
  (`self._transfer_started`). Returns `True` exactly once per call. Every
  entry point (agent phrase, client tool, HTTP) calls this first, so at
  most one REFER is ever sent per call. This, combined with
  `on_agent_response` being a closure bound to one `CallSession` instance
  created fresh per call, is what makes it structurally impossible for one
  call's trigger phrase to move another call's leg — **see the anti-mixup
  test in the manual verification checklist below.**
- **Extension acquisition moved to the SIP thread.** Every entry point now
  enqueues a `_TransferRequest(extension=None, ...)`; the bridge loop is the
  only place that calls `ExtensionPool.acquire()`. This means "all lines
  busy" behaves identically no matter what triggered the transfer.
- **`_wait_for_playout(quiet_seconds, timeout)`** — blocks the SIP thread
  (not the RTP send/recv threads, which keep running) until the RTP play
  queue has been empty for `quiet_seconds` with no new TTS chunk arriving.
  Used before acquiring an extension, so the trigger sentence itself
  finishes reaching the caller before anything else happens.
- **`_close_el_session()`** — closes the ElevenLabs websocket. Idempotent
  and bounded by `EL_END_SESSION_TIMEOUT_SECONDS` (a daemon thread + timed
  join) so a slow `end_session()` can never stall the SIP thread. Called on
  a confirmed-successful transfer (both the "PBX sent BYE" and the "NOTIFY
  sipfrag 2xx" paths in `_perform_transfer`), and *after* the busy prompt
  finishes playing. **Deliberately not called before the REFER is
  confirmed** — a rejected REFER leaves the call live, and the agent must
  still be able to speak.
- **`_play_busy_prompt_and_close()`** — runs on the SIP thread when the
  transfer trigger fires but no extension is free (pool exhausted, or not
  configured). Plays the busy prompt to completion via
  `play_static_frames` + `_wait_for_playout` + a `BUSY_PROMPT_TAIL_SECONDS`
  hold, **then** closes the ElevenLabs session; the caller sets
  `exit_reason = "transfer_unavailable"` and ends the call with a normal
  BYE, so the prompt is always fully transmitted before the BYE goes out.

  > **Ordering here is load-bearing — do not "tidy" it.** The ElevenLabs
  > SDK's `Conversation.end_session()` calls `stop()` on the audio
  > interface, which sets `is_running = False` and closes the RTP socket.
  > An earlier version closed the session *first* (to stop the agent
  > talking over the prompt) and the caller heard silence:
  > `play_static_frames()` hit its `is_running` guard and returned without
  > queueing anything. The agent is instead kept off the line by the
  > `_static_playback` latch (below), which is what makes it safe to leave
  > the websocket open until the prompt has finished.
  > Covered by `tests/test_busy_prompt_playback.py`.
- **`_mark_transfer_failed()`** — sets a sticky `self._transfer_failed`
  flag whenever an *announced* transfer does not complete (busy, REFER
  rejected, or REFER timed out). Read by `_record_call_ended()` — see
  `CALL_STATUS_TRACKING.md`.
- The bridge loop's transfer-request handling, `_perform_transfer()`, and
  the exit-reason → `CallStatus` mapping were all extended to cover the new
  `"transfer_unavailable"` exit reason alongside the existing
  `"transferred"`.

## Legacy entry points (kept as a fallback)

The ElevenLabs `transfer_call` client tool and the
`POST /calls/by-tracking-id/{tracking_id}/transfer` HTTP endpoint both still
work, routed through the exact same `_claim_transfer()` guard and the same
SIP-thread extension acquisition as the phrase trigger. Neither needs to be
configured for the phrase trigger to work, and configuring both is
harmless — the one-shot guard means only the first one to fire can ever
cause a transfer, no matter which entry point it came through. Removing
them outright is a deliberate follow-up (D7 in the work order), once the
ElevenLabs agent's dashboard configuration is confirmed not to depend on
the tool.

## Concurrency design (why it's split into two methods)

The ElevenLabs SDK invokes callbacks (`on_agent_response`) and client tools
on its own threads, separate from the thread running `CallSession._bridge()`'s
SIP read loop. Only one thread may ever read from `SipStream` / `self._sock`
at a time. So neither the phrase detector nor the tool-call handler ever
touches the socket directly — they enqueue a `_TransferRequest` and (for the
tool/HTTP paths) block on an `Event`; the actual extension acquisition, REFER
send, and response handling happen back on the SIP thread, which is the only
thread already looping on `self._stream.read_message()`.

## "All lines busy" prompt

Text (Arabic, verbatim — do not reword):

> `للأسف جميع الخطوط مشغولة الآن. سيتم التواصل معك خلال يومين.`

Rendered via ElevenLabs TTS, using the same agent voice where possible, and
stored at `BUSY_PROMPT_AUDIO_PATH` (default
`./assets/audio/all_lines_busy.wav`). See the asset's own generation notes
for how it was produced and how to re-record it.

Because this message promises a callback within two days, a call that ends
this way is **not** indistinguishable from a normal completed call in
`BatchCallDetails` — see `Status = 'TranFail'` in `CALL_STATUS_TRACKING.md`.

## `.env` additions

```bash
# --- Call transfer (SIP REFER to an internal extension) ---
TRANSFER_EXTENSIONS=201,202,203
TRANSFER_WAIT_SECONDS=15
TRANSFER_EXTENSION_COOLDOWN_SECONDS=2
TRANSFER_EXTENSION_BUSY_SECONDS=300

# Phrase the agent speaks to trigger an internal transfer. Detection is on
# the agent's own transcript -- ElevenLabs no longer POSTs to this service
# and no client tool call is required to start a transfer.
TRANSFER_TRIGGER_PHRASE=هيتم تحويل المكالمة دلوقتي
TRANSFER_TRIGGER_EXTRA_PHRASES=
TRANSFER_PLAYOUT_TIMEOUT_SECONDS=10
TRANSFER_PLAYOUT_QUIET_SECONDS=0.6
EL_END_SESSION_TIMEOUT_SECONDS=3
# "All lines busy" static prompt, played when the trigger fires but no
# internal extension is free.
BUSY_PROMPT_ENABLED=true
BUSY_PROMPT_AUDIO_PATH=./assets/audio/all_lines_busy.wav
BUSY_PROMPT_TAIL_SECONDS=0.8
```

## Required action outside this repo

1. **The ElevenLabs agent prompt must say the exact trigger sentence.**
   `sara_prompt_v2.md` has been updated to instruct the agent to say
   `هيتم تحويل المكالمة دلوقتي` verbatim instead of calling a tool — but the
   **live prompt configured in the ElevenLabs dashboard must be updated to
   match**. This is a manual step outside the repo; code alone does not
   make the feature work.
2. Optional: remove the `transfer_call` client tool from the agent's
   ElevenLabs configuration once the dashboard prompt change is confirmed
   live. Not required — see "Legacy entry points" above.

## Tool/HTTP result contract (unchanged shape, one new status)

```json
// Success
{"success": true, "status": "transferred", "message": "Call transferred to extension 201."}

// No extensions free
{"success": false, "status": "busy", "message": "All lines are currently busy."}

// REFER sent but rejected / timed out / NOTIFY reported failure
{"success": false, "status": "failed", "message": "Transfer to extension 201 failed."}

// A transfer was already requested/claimed for this call
{"success": false, "status": "already_requested", "message": "A transfer has already been requested for this call."}

// Internal error (e.g. the bridge loop never picked up the request)
{"success": false, "status": "error", "message": "..."}
```

This dict is what the `transfer_call` client tool / HTTP fallback return; it
is not seen by the phrase-trigger path, which has no caller waiting on a
result.

## Manual verification checklist

See `TRANSFER_INTERNAL_PLAN.md` §13 for the full list. The two that matter
most:
- **Anti-mixup**: two concurrent calls, trigger the phrase on one only —
  the other call's `transferred_to`, `Status`, and extension must be
  completely unaffected.
- **Failed-then-retried transfer**: a REFER that fails, followed by the
  agent saying the phrase again and succeeding, must record `Status =
  'Transfer'`, not `'TranFail'` — this is the one ordering the sticky
  `_transfer_failed` flag could get wrong.

## Not yet done / possible follow-ups

- No real PBX presence/BLF for extension availability — the pool still
  guesses via `TRANSFER_EXTENSION_BUSY_SECONDS`.
- Only blind transfer is implemented (no attended/consultation leg where
  the middleware waits for the human to accept before dropping the
  caller).
- Removing the legacy `transfer_call` client tool / HTTP endpoint outright
  (D7) is left for a follow-up once the ElevenLabs agent config is
  confirmed clean.
- A rejected/timed-out REFER resets the one-shot guard so the agent can say
  the trigger phrase again; there's no cap on how many times this can
  happen in one call. If that turns out to matter in practice, add a retry
  counter.
