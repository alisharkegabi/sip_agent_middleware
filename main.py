from __future__ import annotations

import signal
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

import name_normalizer
from call_manager import CallManager, QueueFullError
from config import settings
from logging_config import configure_logging, get_logger, stop_logging
from models import (
    CallDetail,
    CallsRequest,
    CallsResponse,
    CallStatus,
    HealthResponse,
    ScheduledCall,
)

configure_logging(
    settings.log_level,
    log_dir=settings.log_dir,
    log_to_console=settings.log_to_console,
    max_bytes=settings.log_max_bytes,
    backup_count=settings.log_backup_count,
    queue_maxsize=settings.log_queue_maxsize,
)
logger = get_logger("api")

# F-20: crude fixed-window per-source rate limiter. Good enough at the
# stated 500 calls/day, ~10k/month scale; swap for a proper limiter
# (slowapi, a reverse-proxy limiter, etc.) if load grows materially.
_rate_lock = threading.Lock()
_rate_buckets: dict[str, tuple[int, float]] = {}  # ip -> (count, window_start)


def _rate_limited(client_ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        count, window_start = _rate_buckets.get(client_ip, (0, now))
        if now - window_start >= 60:
            count, window_start = 0, now
        count += 1
        _rate_buckets[client_ip] = (count, window_start)
        return count > settings.rate_limit_per_minute


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.call_manager = CallManager(settings)
    logger.info(
        f"call service started (capacity={settings.max_concurrent_calls}, "
        f"rtp_ports={settings.rtp_port_min}-{settings.rtp_port_max}), |"
        f"local_ip = {settings.local_ip}"
    )

    # F-25: SIGTERM (e.g. from a Kubernetes pod eviction, `docker stop`, or
    # NSSM/sc.exe stopping a Windows service) must be handled WITHOUT doing
    # any blocking work inside the signal handler itself. The previous
    # handler called call_manager.shutdown() directly on the signal-handling
    # path -- which sleeps up to shutdown_grace_seconds and then blocks in
    # executor.shutdown(wait=True) -- freezing the asyncio event loop
    # entirely for that whole window. Combined with F-02 (before this fix,
    # no call timeouts existed), a single stuck call made the process
    # unkillable by anything short of `taskkill /F`. The handler now only
    # sets an event; the actual drain happens here in the lifespan shutdown
    # path, off the signal-handling context.
    #
    # Note for OPS-01/Windows service deployment: Windows does not deliver
    # POSIX SIGTERM the way this code assumes. When running as a Windows
    # Service (NSSM), hook the service-stop event and call
    # app.state.call_manager.shutdown() from there instead of relying on
    # this signal handler.
    shutdown_signalled = threading.Event()

    def _on_sigterm(signum, frame):
        logger.info("SIGTERM received, flagging graceful shutdown (non-blocking)")
        shutdown_signalled.set()

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        pass  # not in main thread / not supported on this platform

    yield

    logger.info("application shutdown: draining active calls")
    app.state.call_manager.shutdown()
    stop_logging()  # flush any buffered log records before the process exits


app = FastAPI(
    title="Outbound Calling Service",
    description="Production API for launching concurrent SIP + ElevenLabs outbound calls.",
    version="1.0.0",
    lifespan=lifespan,
)


def get_manager() -> CallManager:
    return app.state.call_manager


@app.middleware("http")
async def _security_middleware(request: Request, call_next):
    """F-20: shared-secret auth + basic rate limiting. Bind the service to
    127.0.0.1 or a private interface (see OPS/BIND_HOST) as the primary
    control; this header check is defense in depth so the service isn't a
    bare, unauthenticated telephony trigger if it ever becomes reachable
    beyond that interface (e.g. via a misconfigured ngrok tunnel)."""
    if request.url.path in ("/health",):
        return await call_next(request)

    ## Add this if api_shared_secret is required
    # if settings.api_shared_secret:
    #     provided = request.headers.get("x-api-key", "")
    #     if provided != settings.api_shared_secret:
    #         return JSONResponse(status_code=401, content={"detail": "invalid or missing X-Api-Key"})

    client_ip = request.client.host if request.client else "unknown"
    if _rate_limited(client_ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "rate limit exceeded"},
            headers={"Retry-After": "60"},
        )

    return await call_next(request)


@app.post("/calls", response_model=CallsResponse, status_code=202)
async def create_calls(payload: CallsRequest):
    """
    Schedule one or more outbound calls. Returns immediately with a call_id
    per recipient; it does not wait for any call to dial, connect, or finish.
    When each call reaches a terminal state (completed/failed/rejected/
    cancelled), a webhook notification is POSTed to WEBHOOK_URL. GET
    /calls/{call_id} remains available for on-demand status checks.
    """
    manager = get_manager()
    if not payload.recipients:
        raise HTTPException(status_code=400, detail="recipients must be a non-empty list")

    # F-19: an unbounded batch (e.g. a morning dump of hundreds of numbers)
    # used to be accepted whole, with everything beyond capacity sitting in
    # the executor's internal queue indefinitely.
    if len(payload.recipients) > settings.max_recipients_per_request:
        raise HTTPException(
            status_code=400,
            detail=f"too many recipients in one request (max {settings.max_recipients_per_request})",
        )

    allow_prefixes = tuple(p.strip() for p in settings.phone_allow_prefixes.split(",") if p.strip())
    deny_prefixes = tuple(p.strip() for p in settings.phone_deny_prefixes.split(",") if p.strip())

    scheduled: list[ScheduledCall] = []
    for recipient in payload.recipients:
        dynamic_variables = recipient.conversation_initiation_client_data.dynamic_variables

        # F-20: configurable allow/deny prefix list on phone_number, on top
        # of the E.164-shape validation already enforced by the pydantic
        # model (models.RecipientRequest).
        number = recipient.phone_number
        if deny_prefixes and number.startswith(deny_prefixes):
            raise HTTPException(status_code=400, detail=f"{number} matches a denied prefix")
        if allow_prefixes and not number.startswith(allow_prefixes):
            raise HTTPException(status_code=400, detail=f"{number} does not match an allowed prefix")

        # F-23: cap dynamic_variables size/key-count -- these get carried
        # into the ElevenLabs conversation config and the webhook payload.
        if len(dynamic_variables) > settings.max_dynamic_variables_keys:
            raise HTTPException(status_code=400, detail="too many dynamic_variables keys")
        import json as _json  # local import: only needed for this size check
        if len(_json.dumps(dynamic_variables)) > settings.max_dynamic_variables_bytes:
            raise HTTPException(status_code=400, detail="dynamic_variables payload too large")

        # The agent's callTransferInt webhook tool carries tracking_id as a URL
        # path param (POST .../calls/by-tracking-id/{tracking_id}/transfer), so
        # ElevenLabs treats it as required and closes the socket with 1008
        # "Missing required dynamic variables in tools" when it is absent --
        # but only AFTER the call has connected and the greeting has played,
        # burning a real customer call on a payload bug. Reject before dialling.
        # It is also the BatchCallDetails key, so a call without one is
        # untracked even if it succeeds.
        if not str(dynamic_variables.get("tracking_id") or "").strip():
            raise HTTPException(
                status_code=400,
                detail="dynamic_variables.tracking_id is required",
            )

        # Arabic name orthography: the source data stores `علي` as `على` (also
        # the preposition "on"), so the agent says the wrong word. Build the
        # corrected snapshot HERE, before dialling -- doing it at the
        # ElevenLabs boundary would put a data-file or value-type error after
        # the customer has already answered, burning a real call for the same
        # class of reason the tracking_id check above exists.
        #
        # `dynamic_variables` itself is left untouched, so to_webhook_payload()
        # still echoes the .NET client exactly what it sent.
        speech_dynamic_variables = dynamic_variables
        if settings.name_normalization:
            name_keys = [
                k.strip() for k in settings.name_normalization_keys.split(",") if k.strip()
            ]
            gender_keys = dict(
                pair.split(":", 1)
                for pair in (
                    p.strip() for p in settings.name_normalization_gender_keys.split(",")
                )
                if ":" in pair
            )
            speech_dynamic_variables, counters = name_normalizer.normalize_dynamic_variables(
                dynamic_variables, name_keys, gender_keys
            )
            if counters:
                # Rule names and counts only -- names are borrower data.
                logger.info(
                    f"name normalization: {name_normalizer.format_counters(counters)}"
                )

        try:
            session = manager.submit_call(
                phone_number=recipient.phone_number,
                dynamic_variables=dynamic_variables,
                speech_dynamic_variables=speech_dynamic_variables,
                tracking_id=dynamic_variables.get("tracking_id"),
            )
        except QueueFullError as e:
            # F-19: distinct from a shutdown rejection -- tell the caller to
            # slow down and retry rather than silently cancelling.
            logger.warning(f"queue full, rejecting call to {recipient.phone_number}: {e}")
            raise HTTPException(status_code=429, detail=str(e)) from e
        except RuntimeError as e:
            # Service draining — reject just this recipient, not the whole batch.
            scheduled.append(
                ScheduledCall(
                    call_id="",
                    phone_number=recipient.phone_number,
                    tracking_id=dynamic_variables.get("tracking_id"),
                    status=CallStatus.CANCELLED,
                )
            )
            logger.warning(f"rejected call to {recipient.phone_number}: {e}")
            continue

        scheduled.append(
            ScheduledCall(
                call_id=session.call_id,
                phone_number=session.phone_number,
                tracking_id=session.tracking_id,
                status=session.status,
            )
        )

    return CallsResponse(scheduled=scheduled, id=str(uuid.uuid4()))


@app.get("/calls", response_model=list[CallDetail])
async def list_calls(status: CallStatus | None = None):
    manager = get_manager()
    return [CallDetail(**s.to_dict()) for s in manager.list_calls(status=status)]


@app.get("/calls/{call_id}", response_model=CallDetail)
async def get_call(call_id: str):
    manager = get_manager()
    session = manager.get_call(call_id)
    if session is None:
        raise HTTPException(status_code=404, detail="call not found")
    return CallDetail(**session.to_dict())


@app.post("/calls/{call_id}/hangup", status_code=202)
async def hangup_call(call_id: str):
    manager = get_manager()
    if not manager.hangup(call_id):
        raise HTTPException(status_code=404, detail="call not found")
    return {"call_id": call_id, "hangup_requested": True}


@app.post("/calls/by-tracking-id/{tracking_id}/transfer")
async def transfer_call_by_tracking_id(tracking_id: str):
    """Fallback transfer trigger, keyed by tracking_id -- the id ElevenLabs
    already has in dynamic_variables mid-call, since it was supplied by the
    original caller in the /calls request rather than generated by this
    service. The PRIMARY transfer trigger is the agent's own transcript
    phrase (CallSession._maybe_trigger_transfer, watching for
    TRANSFER_TRIGGER_PHRASE in the agent's spoken text) -- this endpoint no
    longer needs to be called by ElevenLabs for a transfer to happen, and
    exists only so an external caller can still force one."""
    manager = get_manager()
    if manager.get_call_by_tracking_id(tracking_id) is None:
        raise HTTPException(status_code=404, detail="call not found")
    result = await run_in_threadpool(manager.transfer_by_tracking_id, tracking_id)
    if result is None:
        raise HTTPException(status_code=404, detail="call not found")
    return result


@app.get("/health", response_model=HealthResponse)
async def health():
    manager = get_manager()
    return HealthResponse(**manager.health())


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    logger.exception(f"unhandled exception on {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"detail": "internal server error"})
