"""
Post-call retrieval of the ElevenLabs conversation record.

Until now the only thing this service kept from ElevenLabs was the
conversation_id (CallSession.conversation_id, set by wait_for_session_end).
Everything ElevenLabs computes *after* the call -- the evaluation criteria
results and the data-collection results (e.g. a "PaymentDate" field defined
in the agent's Analysis config) -- was never fetched, so it never reached
the .NET client.

Why pull instead of ElevenLabs' post-call webhook: the push route needs a
publicly reachable HTTPS endpoint (this service binds 127.0.0.1 by default,
see Settings.bind_host) plus HMAC verification and dashboard configuration.
The pull route uses the API key and SDK already in the process and returns
the same payload -- GET /v1/convai/conversations/{id}.

Timing: ElevenLabs computes the analysis asynchronously, so the record is
`status="processing"` for a few seconds after the call ends and the
`analysis` block is absent until `status="done"`. This module therefore
polls.

Threading follows the F-18 rule learned in webhook_client.py: **no worker
thread ever sleeps.** A poll that isn't ready yet is rescheduled on a
threading.Timer, so a slow ElevenLabs backend can never occupy the pool and
stall other calls' fetches.
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from elevenlabs.client import ElevenLabs

from config import Settings
from logging_config import get_logger

logger = get_logger("analysis")

# Terminal states of the conversation record: nothing further will change.
_TERMINAL_STATES = ("done", "failed")


def _model_to_dict(obj: Any) -> dict:
    """SDK response (pydantic v2) -> plain JSON-safe dict."""
    if isinstance(obj, dict):
        return obj
    for attr, kwargs in (("model_dump", {"mode": "json"}), ("dict", {})):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn(**kwargs)
            except Exception:
                continue
    return dict(getattr(obj, "__dict__", {}) or {})


def extract_analysis(raw: dict) -> dict:
    """Flatten the conversation record into the compact block that goes to
    the .NET client (webhook + GET /calls/{id}).

    Deliberately does NOT include `transcript`: it is turn-by-turn customer
    speech, and the client asked for the analysis, not the recording. The
    full record is still available in the raw dump (LOG_CONVERSATION_JSON).
    """
    analysis = raw.get("analysis") or {}
    metadata = raw.get("metadata") or {}

    evaluation: dict[str, dict] = {}
    for key, item in (analysis.get("evaluation_criteria_results") or {}).items():
        item = item or {}
        evaluation[key] = {
            "result": item.get("result"),
            "rationale": item.get("rationale"),
            "score": item.get("score"),
            "max_score": item.get("max_score"),
        }

    collected: dict[str, dict] = {}
    flat: dict[str, Any] = {}
    for key, item in (analysis.get("data_collection_results") or {}).items():
        item = item or {}
        value = item.get("value")
        collected[key] = {
            "value": value,
            "rationale": item.get("rationale"),
            "type": (item.get("json_schema") or {}).get("type"),
        }
        # Flat name -> value map so the client can read
        # analysis.data_collection.PaymentDate without walking the detail.
        flat[key] = value

    return {
        "conversation_id": raw.get("conversation_id"),
        "status": raw.get("status"),
        "agent_id": raw.get("agent_id"),
        # Which agent branch/version actually served the call. The dashboard
        # traffic split decides this, not the middleware (it requests a
        # signed URL by agent_id only), so recording it here is the only way
        # to know after the fact which configuration produced these results.
        "branch_id": raw.get("branch_id"),
        "version_id": raw.get("version_id"),
        "call_successful": analysis.get("call_successful"),
        "call_success_score": analysis.get("call_success_score"),
        "call_summary_title": analysis.get("call_summary_title"),
        "transcript_summary": analysis.get("transcript_summary"),
        "evaluation_criteria_results": evaluation,
        "data_collection_results": collected,
        "data_collection": flat,
        "call_duration_secs": metadata.get("call_duration_secs"),
        "termination_reason": metadata.get("termination_reason"),
        "cost": metadata.get("cost"),
        "main_language": metadata.get("main_language"),
        "fetched_at": time.time(),
    }


class AnalysisFetcher:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._enabled = bool(
            settings.fetch_conversation_analysis and settings.elevenlabs_api_key
        )
        self._poll_interval = max(1.0, settings.analysis_poll_interval_seconds)
        self._max_wait = max(0.0, settings.analysis_max_wait_seconds)
        self._timeout = settings.analysis_request_timeout_seconds
        self._dump_dir = (
            os.path.join(settings.log_dir, "conversations")
            if settings.log_conversation_json
            else ""
        )

        # Separate from both the call-worker pool and the webhook pool: an
        # ElevenLabs API stall must not consume a call slot or delay a
        # status notification.
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="analysis-fetch")
        self._client: Optional[ElevenLabs] = None
        self._client_lock = threading.Lock()
        self._timers: list[threading.Timer] = []
        self._timers_lock = threading.Lock()
        self._shutting_down = False

        if self._dump_dir:
            os.makedirs(self._dump_dir, exist_ok=True)

        # Logged at startup because these flags are read from .env exactly
        # once, at import: editing .env under a running service changes
        # nothing until it is restarted, and the only symptom otherwise is
        # "the JSON dump never appeared" with no explanation anywhere.
        logger.info(
            f"post-call analysis enabled={self._enabled} "
            f"poll={self._poll_interval}s max_wait={self._max_wait}s "
            f"json_dump={self._dump_dir or 'off'}"
        )

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    def fetch_async(
        self,
        *,
        call_id: str,
        conversation_id: Optional[str],
        on_result: Callable[[dict], None],
    ) -> None:
        """Schedule retrieval; returns immediately and never raises.

        on_result is invoked at most once, on an analysis-fetch thread, with
        the extracted block. It is not called if the record never reaches a
        terminal state within analysis_max_wait_seconds.
        """
        if not self._enabled or not conversation_id or self._shutting_down:
            return
        try:
            self._executor.submit(
                self._attempt, call_id, conversation_id, on_result, time.monotonic(), 1
            )
        except Exception as e:
            logger.error(f"failed to schedule analysis fetch for call {call_id}: {e}")

    # ------------------------------------------------------------------
    def _get_client(self) -> ElevenLabs:
        with self._client_lock:
            if self._client is None:
                self._client = ElevenLabs(api_key=self._settings.elevenlabs_api_key)
            return self._client

    def fetch_once(self, conversation_id: str) -> Optional[dict]:
        """One GET of the conversation record. Returns the raw dict, or None
        if the call failed (network error, 404 while ElevenLabs is still
        indexing the conversation, ...)."""
        try:
            resp = self._get_client().conversational_ai.conversations.get(
                conversation_id,
                request_options={"timeout_in_seconds": int(self._timeout)},
            )
        except Exception as e:
            logger.warning(f"conversation {conversation_id} fetch failed: {e}")
            return None
        return _model_to_dict(resp)

    def _attempt(
        self,
        call_id: str,
        conversation_id: str,
        on_result: Callable[[dict], None],
        started_at: float,
        attempt: int,
    ) -> None:
        raw = self.fetch_once(conversation_id)
        status = (raw or {}).get("status")

        if raw is not None and status in _TERMINAL_STATES:
            self._dump_raw(call_id, conversation_id, raw)
            analysis = extract_analysis(raw)
            criteria = len(analysis["evaluation_criteria_results"])
            fields = ", ".join(analysis["data_collection"].keys()) or "-"
            logger.info(
                f"call {call_id} analysis ready after {attempt} attempt(s): "
                f"status={status} call_successful={analysis['call_successful']} "
                f"criteria={criteria} data_collection=[{fields}]"
            )
            try:
                on_result(analysis)
            except Exception:
                logger.exception(f"analysis callback failed for call {call_id}")
            return

        elapsed = time.monotonic() - started_at
        if elapsed + self._poll_interval > self._max_wait:
            logger.warning(
                f"call {call_id} analysis not ready after {attempt} attempt(s) / "
                f"{elapsed:.0f}s (last status={status or 'unavailable'}); giving up"
            )
            return

        self._schedule_retry(call_id, conversation_id, on_result, started_at, attempt)

    def _schedule_retry(
        self,
        call_id: str,
        conversation_id: str,
        on_result: Callable[[dict], None],
        started_at: float,
        attempt: int,
    ) -> None:
        if self._shutting_down:
            return
        timer = threading.Timer(
            self._poll_interval,
            lambda: self._submit_retry(call_id, conversation_id, on_result, started_at, attempt + 1),
        )
        timer.daemon = True
        with self._timers_lock:
            # Drop already-fired timers so a long-running process doesn't
            # accumulate one dead Timer object per poll.
            self._timers = [t for t in self._timers if t.is_alive()]
            self._timers.append(timer)
        timer.start()

    def _submit_retry(self, call_id, conversation_id, on_result, started_at, attempt) -> None:
        if self._shutting_down:
            return
        try:
            self._executor.submit(
                self._attempt, call_id, conversation_id, on_result, started_at, attempt
            )
        except Exception as e:
            logger.error(f"failed to reschedule analysis fetch for call {call_id}: {e}")

    def _dump_raw(self, call_id: str, conversation_id: str, raw: dict) -> None:
        """Write the complete ElevenLabs record -- analysis, metadata AND the
        full transcript -- to its own file. Off by default: transcripts are
        real customer speech and don't belong in the shared app log."""
        if not self._dump_dir:
            return
        path = os.path.join(self._dump_dir, f"{call_id}_{conversation_id}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2, ensure_ascii=False, default=str)
            logger.info(f"call {call_id} full conversation JSON written to {path}")
        except Exception:
            logger.exception(f"failed to write conversation JSON for call {call_id}")

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        self._shutting_down = True
        with self._timers_lock:
            for t in self._timers:
                t.cancel()
            self._timers.clear()
        self._executor.shutdown(wait=False)
