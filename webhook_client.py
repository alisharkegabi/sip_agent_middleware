"""
Fire-and-forget webhook delivery for terminal call events.

This replaces external polling of GET /calls/{id}: instead of a client
repeatedly asking "is it done yet?", the service POSTs a single notification
to a configured URL the moment a call reaches a terminal status (Completed,
Failed, Rejected, Cancelled).

F-18 fix: the previous WebhookSender put `time.sleep(backoff)` INSIDE the
worker function running on a 4-thread pool. A handful of slow/unreachable
endpoints (exactly what you'd see during the "network is bad" scenario this
whole work order is about) would occupy all 4 workers sleeping, stalling
every other pending notification behind them. Retries are now scheduled via
threading.Timer (no worker thread blocks waiting), backoff has jitter, and a
retry gives up after webhook_max_retry_age_seconds and is appended to a
dead-letter file instead of being silently lost -- an outage of the
receiving endpoint no longer loses call results.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import requests

from config import Settings
from logging_config import get_logger

logger = get_logger("webhook")


# --------------------------------------------------------------------------
# Call submission (POST /calls) -- triggers a new outbound call.
#
# This is the same request test.py makes against the calling service; it's
# provided here so callers (e.g. the external server, or any script) can
# trigger a call from code instead of hand-building the request each time.
# It is unrelated to WebhookSender below, which handles delivery of the
# *completion* notification once the call this function starts has ended.
# --------------------------------------------------------------------------

# Exact sample payload from test.py, reused as-is so this function's default
# behavior matches the known-working manual test.
SAMPLE_CALL_PAYLOAD: dict[str, Any] = {
    "recipients": [
        {
            "phone_number": "01153411597",
            "conversation_initiation_client_data": {
                "dynamic_variables": {
                    "is_guarantor": "false",
                    "cr_gender": "male",
                    "call_receiver": "أحمد محمد",
                    "contract_ref": "CTR-00123",
                    "gr_phone_number": "+201000000000",
                    "br_phone_number": "+201111111111",
                    "user_name": "أحمد محمد",
                    "user_name_full": "أحمد محمد علي",
                    "br_gender": "Male",
                    "guarantor_name": "",
                    "guarantor_name_full": "",
                    "gr_gender": "",
                    "payment_amount": "1500",
                    "due_date": "01/07/2026",
                    "outstanding_balance": "12000",
                    "Today_date": "14/07/2026",
                    "penalty": "150",
                    "remain_installments": "8",
                    "last_payment_date": "20/06/2026",
                    "last_paid_amount": "1500",
                    "total_number_installments": "24",
                    "total_loan_amount": "36000",
                }
            },
        },
    ]
}


def submit_call(
    base_url: str,
    payload: Optional[dict] = None,
    timeout: float = 10,
    api_shared_secret: str = "",
) -> dict:
    """
    Trigger a new outbound call by POSTing to the calling service's
    /calls endpoint -- the same request test.py makes.

    Args:
        base_url: root URL of the calling service, e.g. "http://localhost:8000".
        payload: request body matching CallsRequest (see models.py). Defaults
                 to SAMPLE_CALL_PAYLOAD (the exact data from test.py) if omitted.
        timeout: request timeout in seconds.
        api_shared_secret: if set, sent as the X-Api-Key header (F-20).

    Returns:
        The parsed JSON response (a CallsResponse: {"scheduled": [...]}).

    Raises:
        requests.RequestException if the request fails or the service
        returns a non-2xx status.
    """
    body = payload if payload is not None else SAMPLE_CALL_PAYLOAD
    url = f"{base_url.rstrip('/')}/calls"
    headers = {"X-Api-Key": api_shared_secret} if api_shared_secret else {}

    logger.info(f"submitting call to {url}")
    resp = requests.post(url, json=body, timeout=timeout, headers=headers)
    resp.raise_for_status()

    result = resp.json()
    for call in result.get("scheduled", []):
        logger.info(f"scheduled call_id={call.get('call_id')} status={call.get('status')}")
    return result


class WebhookSender:
    def __init__(self, settings: Settings):
        self._url = settings.webhook_url
        self._timeout = settings.webhook_timeout_seconds
        self._max_retries = max(1, settings.webhook_max_retries)
        self._max_retry_age = settings.webhook_max_retry_age_seconds
        self._dead_letter_path = settings.webhook_dead_letter_path
        # Deliberately separate from CallManager's call-worker executor --
        # webhook delivery must never compete for or block call-handling
        # threads. Small and bounded; this is fire-and-forget notification
        # traffic, not the hot path. No thread ever sleeps in this pool now
        # (F-18) -- retries are rescheduled via threading.Timer instead.
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webhook-sender")
        self._timers: list[threading.Timer] = []
        self._timers_lock = threading.Lock()
        self._shutting_down = False
        if self._dead_letter_path:
            os.makedirs(os.path.dirname(self._dead_letter_path) or ".", exist_ok=True)

    def send_async(self, payload: dict) -> None:
        """Schedule delivery and return immediately. Never raises/blocks the caller."""
        if not self._url:
            return
        payload = dict(payload)
        payload.setdefault("_first_attempt_at", time.time())
        try:
            self._executor.submit(self._attempt, payload, 1)
        except Exception as e:
            logger.error(f"failed to schedule webhook for call {payload.get('call_id', '-')}: {e}")

    def _attempt(self, payload: dict, attempt: int) -> None:
        call_id = payload.get("call_id", "-")
        # Strip our bookkeeping field before sending over the wire.
        wire_payload = {k: v for k, v in payload.items() if k != "_first_attempt_at"}
        try:
            resp = requests.post(self._url, json=wire_payload, timeout=self._timeout)
            if resp.status_code < 300:
                logger.info(f"webhook delivered for call {call_id} (attempt {attempt})")
                return
            logger.warning(
                f"webhook for call {call_id} got HTTP {resp.status_code} (attempt {attempt}/{self._max_retries})"
            )
        except requests.RequestException as e:
            logger.warning(f"webhook delivery failed for call {call_id} (attempt {attempt}/{self._max_retries}): {e}")
        except Exception as e:
            logger.error(f"unexpected error sending webhook for call {call_id}: {e}")
            return

        self._schedule_retry(payload, attempt)

    def _schedule_retry(self, payload: dict, attempt: int) -> None:
        call_id = payload.get("call_id", "-")
        age = time.time() - payload.get("_first_attempt_at", time.time())
        if attempt >= self._max_retries or age >= self._max_retry_age:
            logger.error(f"webhook delivery permanently failed for call {call_id} after {attempt} attempt(s)")
            self._dead_letter(payload)
            return

        if self._shutting_down:
            self._dead_letter(payload)
            return

        base_backoff = min(2 ** (attempt - 1), 30)
        backoff = base_backoff + random.uniform(0, base_backoff * 0.25)  # jitter

        timer = threading.Timer(
            backoff, lambda: self._executor.submit(self._attempt, payload, attempt + 1)
        )
        timer.daemon = True
        with self._timers_lock:
            self._timers.append(timer)
        timer.start()

    def _dead_letter(self, payload: dict) -> None:
        if not self._dead_letter_path:
            return
        try:
            record = {k: v for k, v in payload.items() if k != "_first_attempt_at"}
            record["_dead_lettered_at"] = time.time()
            with open(self._dead_letter_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception:
            logger.exception("failed to write webhook dead-letter record")

    def shutdown(self) -> None:
        self._shutting_down = True
        with self._timers_lock:
            for t in self._timers:
                t.cancel()
        self._executor.shutdown(wait=False)
