"""
Standalone FastAPI app, meant to run on port 9000, that does two jobs:

  1. Triggers outbound calls -- GET "/" serves a plain HTML form; submitting
     it (or POSTing JSON to /trigger-call) calls the calling service's
     POST /calls endpoint on your behalf, exactly like test.py / submit_call()
     do, but reachable from a browser instead of a Python script.
  2. Receives the completion webhook -- POST /call-status, called by the
     calling service once a call reaches a terminal state.

Run:
    uvicorn webhook_receiver:app --host 0.0.0.0 --port 9000

Configure where the calling service lives (defaults to localhost:8000):
    CALL_SERVICE_URL=http://localhost:8000

Point the calling service's WEBHOOK_URL at this app's /call-status endpoint,
e.g. WEBHOOK_URL=http://localhost:9000/call-status
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from webhook_client import submit_call
from dotenv import load_dotenv

load_dotenv(override=True)

CALL_SERVICE_URL = os.getenv("CALL_SERVICE_URL", "http://localhost:8000")

app = FastAPI(
    title="Call Trigger + Status Webhook Receiver",
    description="Triggers outbound calls and receives their terminal-status webhook, both on one port.",
    version="1.0.0",
)


class CallStatusWebhook(BaseModel):
    # "call_status" (sent at terminal status) or "call_analysis" (sent once
    # ElevenLabs' post-call analysis is available). Defaults to call_status
    # so payloads from an older build still validate.
    event: str = "call_status"
    call_id: str
    conversation_id: Optional[str] = None
    status: str
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    duration_seconds: Optional[float] = None
    reason: Optional[str] = None
    analysis: Optional[dict[str, Any]] = None
    metadata: dict[str, Any] = {}


# --------------------------------------------------------------------------
# 1. Trigger a call, from a browser
# --------------------------------------------------------------------------
_FORM_PAGE = """
<!DOCTYPE html>
<html>
<head><title>Trigger a Call</title></head>
<body style="font-family: sans-serif; max-width: 480px; margin: 40px auto;">
  <h2>Trigger a Call</h2>
  <form action="/trigger-call" method="post">
    <label>Phone number:<br>
      <input type="text" name="phone_number" required style="width:100%">
    </label><br><br>
    <label>Tracking ID (optional):<br>
      <input type="text" name="tracking_id" style="width:100%">
    </label><br><br>
    <label>Dynamic variables (JSON, optional):<br>
      <textarea name="dynamic_variables_json" rows="6" style="width:100%">{}</textarea>
    </label><br><br>
    <button type="submit">Call</button>
  </form>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def trigger_form():
    return _FORM_PAGE


@app.post("/trigger-call")
async def trigger_call(
    phone_number: str = Form(...),
    tracking_id: str = Form(""),
    dynamic_variables_json: str = Form("{}"),
):
    try:
        dynamic_variables = json.loads(dynamic_variables_json) if dynamic_variables_json.strip() else {}
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"detail": "dynamic_variables_json is not valid JSON"})

    if tracking_id:
        dynamic_variables.setdefault("tracking_id", tracking_id)

    payload = {
        "recipients": [
            {
                "phone_number": phone_number,
                "conversation_initiation_client_data": {
                    "dynamic_variables": dynamic_variables
                },
            }
        ]
    }

    try:
        # submit_call() uses the blocking `requests` library -- run it off
        # the event loop so this endpoint doesn't stall other requests
        # (including incoming /call-status webhooks) while it waits on the
        # calling service.
        result = await run_in_threadpool(submit_call, CALL_SERVICE_URL, payload)
    except requests.RequestException as e:
        return JSONResponse(status_code=502, content={"detail": f"could not reach calling service: {e}"})

    return result


# --------------------------------------------------------------------------
# 2. Receive the completion webhook
# --------------------------------------------------------------------------
def _log_webhook(payload: dict) -> None:
    """
    Development/debug console logging, deliberately isolated in its own
    function so it can be swapped for structured logging (e.g. the
    logging_config module's logger, or a metrics/alerting sink) in
    production without touching the endpoint logic.

    Prints the raw body rather than the validated model so that fields this
    receiver doesn't model yet are still visible.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    print("=" * 42)
    print(f"Webhook Received  [{payload.get('event', 'call_status')}]")
    print(f"Time: {timestamp}")
    print()
    print(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    print()
    print("Webhook processed successfully.")
    print("=" * 42)


@app.post("/call-status", status_code=200)
async def receive_call_status(request: Request):
    try:
        raw_body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "invalid JSON body"})

    try:
        webhook = CallStatusWebhook(**raw_body)
    except ValidationError as e:
        return JSONResponse(status_code=422, content={"detail": e.errors()})

    try:
        _log_webhook(raw_body)
    except Exception as e:
        # Logging must never fail the request -- the sender already
        # succeeded in delivering the payload at this point.
        print(f"warning: failed to log webhook payload: {e}")

    return {"received": True, "call_id": webhook.call_id, "event": webhook.event}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    print(f"unhandled exception on {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"detail": "internal server error"})