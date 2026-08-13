# Call Transfer Feature (SIP REFER to an internal extension)

Adds a `transfer_call` ElevenLabs client tool: the agent can hand a live
call off to a human on an internal PBX extension mid-conversation, using a
blind SIP REFER on the existing dialog. If no extension is currently free,
the tool returns a JSON result telling ElevenLabs all lines are busy,
instead of attempting a transfer.

## Files changed

### `sip_protocol.py`
Added `build_refer()` — builds an in-dialog SIP REFER (RFC 3515) request:
`Refer-To: <sip:{extension}@{pbx_ip}>`, reusing the same Call-ID/From-tag
as the rest of the dialog, with a fresh branch and an incremented CSeq.
This is a **blind transfer** — no attended/consultation leg.

### `extension_pool.py` (new file)
Thread-safe FIFO pool of internal extensions, modeled directly on the
existing `port_allocator.py` (same acquire/release/cooldown/quarantine
pattern).

- `acquire()` pops the next free extension, or raises
  `ExtensionPoolExhausted` if none are free.
- `release(extension, busy_seconds=None)` returns an extension to the
  pool after a quarantine window — a short `cooldown_seconds` after a
  *failed* transfer attempt, or a longer `busy_seconds` after a
  *successful* one.
- **Known limitation**: this pool has no real visibility into PBX
  presence/registration state. It only tracks what this middleware itself
  handed out. After a successful transfer it assumes the extension stays
  busy for `TRANSFER_EXTENSION_BUSY_SECONDS` and then makes it available
  again automatically — there's no signal for when that human call
  actually ends.

### `config.py`
New settings (env-var backed, see `.env` block below):
- `transfer_extensions` (CSV) / `TRANSFER_EXTENSIONS`
- `transfer_wait_seconds` / `TRANSFER_WAIT_SECONDS`
- `transfer_extension_cooldown_seconds` / `TRANSFER_EXTENSION_COOLDOWN_SECONDS`
- `transfer_extension_busy_seconds` / `TRANSFER_EXTENSION_BUSY_SECONDS`

### `call_manager.py`
- Builds one shared `ExtensionPool` from `settings.transfer_extensions` at
  startup and injects it into every `CallSession` (same pattern as the
  existing `PortAllocator`).
- Surfaces `transfer_extensions_free` / `transfer_extensions_in_use` in
  `GET /health`.

### `models.py`
- `HealthResponse` gained `transfer_extensions_free` /
  `transfer_extensions_in_use`.
- `CallDetail` gained `transferred_to: Optional[str]`.

### `call_session.py` (core of the feature)
- `CallSession.__init__` now accepts `extension_pool`, stores
  `self.transferred_to`, and a `self._transfer_requests` queue used to
  hand work between threads (see concurrency note below).
- Inside `_bridge()`, before building the `Conversation`, a `ClientTools()`
  instance is created and `transfer_call` is registered on it, pointing at
  `self._handle_transfer_tool_call`. That `ClientTools` instance is passed
  into `Conversation(..., client_tools=client_tools, ...)`.
- **`_handle_transfer_tool_call(parameters) -> dict`**: runs on
  ElevenLabs SDK's `ClientTools` executor thread (NOT the SIP thread).
  - If no extension pool is configured, or `ExtensionPool.acquire()`
    raises `ExtensionPoolExhausted`, it returns immediately:
    ```json
    {"success": false, "status": "busy", "message": "All lines are currently busy."}
    ```
    This is the exact JSON that reaches ElevenLabs as the client-tool
    result, satisfying the "all lines busy" requirement.
  - Otherwise it queues a `_TransferRequest` (extension + a
    `threading.Event`) onto `self._transfer_requests` and blocks on that
    event (bounded by `transfer_wait_seconds + 5s` slack) for the real
    outcome, which is produced by `_perform_transfer` below.
- **`_perform_transfer(request, ...)`**: runs on the SIP thread, inside
  `_bridge()`'s existing polling loop — this is the *only* thread allowed
  to touch `self._sock` / `self._stream`, so the actual REFER always goes
  out from there, picked up once per loop iteration via
  `self._transfer_requests.get_nowait()`.
  - Sends the REFER.
  - Waits for the 202/4xx response matched by CSeq, then for the
    transfer-progress NOTIFY's `message/sipfrag` body (or a BYE sent by
    the PBX itself, treated as an implicit success).
  - On success: releases the extension into the pool with the long
    `busy_seconds` quarantine, sets `self.transferred_to`, sends our own
    BYE to end this leg (unless the PBX already sent one), and unblocks
    the waiting tool-call thread with a success result.
  - On failure/timeout: releases the extension with the short cooldown
    and unblocks the tool-call thread with a failure result. The call
    continues normally — the agent can retry, escalate, or keep talking.
- The main `_bridge()` loop now checks the transfer queue each iteration;
  if a transfer completes, `exit_reason = "transferred"`, which was added
  to the set of exit reasons that map to `CallStatus.COMPLETED`.
- `to_dict()` / `to_webhook_payload()` now include `transferred_to`.

## Concurrency design (why it's split into two methods)

The ElevenLabs SDK invokes client tools on its own thread pool
(`ClientTools`), separate from the thread running `CallSession._bridge()`'s
SIP read loop. Only one thread may ever read from `SipStream` /
`self._sock` at a time. So the tool-call handler never touches the socket
directly — it just picks an extension and blocks on an `Event`; the actual
REFER send + response handling happens back on the SIP thread, which is
the only thread already looping on `self._stream.read_message()`.

## `.env` additions

```bash
# --- Call transfer (SIP REFER to an internal extension) ---
# Comma-separated internal extensions the agent may blind-transfer a call
# to, e.g. "201,202,203". Empty = transfer_call tool always reports busy.
TRANSFER_EXTENSIONS=201,202,203
TRANSFER_WAIT_SECONDS=15
TRANSFER_EXTENSION_COOLDOWN_SECONDS=2
# How long a successfully-transferred extension is assumed busy before it's
# eligible for another transfer again (no real PBX presence signal exists).
TRANSFER_EXTENSION_BUSY_SECONDS=300
```

Add it under the `--- SIP / PBX ---` / `--- Call lifecycle timeouts ---`
section of the existing `.env`. Replace the extension list with your real
internal extensions.

## Required action outside this repo

The ElevenLabs agent's own configuration (dashboard/API) must also define
a **client tool named `transfer_call`** for the agent to be able to invoke
it — registering the handler in `call_session.py` only wires up the
middleware side. No parameters are required from the agent; the
middleware picks the extension from the pool itself.

## Tool result contract (what the agent sees)

```json
// Success
{"success": true, "status": "transferred", "message": "Call transferred to extension 201."}

// No extensions free
{"success": false, "status": "busy", "message": "All lines are currently busy."}

// REFER sent but rejected / timed out / NOTIFY reported failure
{"success": false, "status": "failed", "message": "Transfer to extension 201 failed."}

// Feature not configured (no extension_pool wired up) or internal error
{"success": false, "status": "unavailable" | "error", "message": "..."}
```

## Not yet done / possible follow-ups

- No automated tests were added for the transfer path (no existing test
  harness for SIP dialogs in this repo to extend).
- Extension availability is inferred, not observed (see limitation above).
  If the PBX exposes presence/BLF (e.g. via SIP SUBSCRIBE/NOTIFY on
  dialog state, or a vendor API), that would let `ExtensionPool` track
  real busy/free state instead of a fixed timeout guess.
- Only blind transfer is implemented (no attended/consultation leg where
  the middleware waits for the human to accept before dropping the
  caller).
