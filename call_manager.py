"""
CallManager owns:
  - the worker thread pool that calls actually run on
  - the registry of CallSession objects (active + recently completed)
  - the RTP port allocator
  - graceful shutdown

Concurrency model unchanged from the original design rationale below --
still a bounded ThreadPoolExecutor, still the right choice for this
workload (blocking sockets + a blocking SDK). See §1/§4 of the work order
for why a per-call thread count reduction (F-11, the shared RTP scheduler)
is the scalability lever, not a different concurrency model entirely.

Hardening applied here (see PRODUCTION_HARDENING_WORK_ORDER.md):
  - F-07: the reaper thread (and every other long-lived daemon thread) must
    survive an unexpected exception in its loop body instead of dying
    silently for the rest of the process lifetime.
  - F-18: webhook delivery is re-enabled (it was dead code -- commented
    out -- forcing the .NET client into polling, which F-03's bug turned
    into a 500 on every single poll).
  - F-19: unbounded batches/queues are bounded: MAX_QUEUE_DEPTH rejects new
    calls with a distinct QueueFullError (main.py maps this to 429 +
    Retry-After) once the executor's pending queue is already full of
    scheduled-but-not-yet-running calls; queued_at + MAX_QUEUE_WAIT_SECONDS
    is enforced inside CallSession.run() itself.
  - F-21: counters are mutated only under self._lock (a bare += from a
    done-callback running on a different thread than health() reads it
    from is not atomic).
  - F-22: queued/active counts are tracked with explicit counters instead of
    reaching into ThreadPoolExecutor._work_queue (a private attribute whose
    shape isn't stable across CPython versions).
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from call_session import CallSession
from config import Settings
from conversation_analysis import AnalysisFetcher
from extension_pool import ExtensionPool
from logging_config import get_logger, _DroppingQueueHandler
from models import CallStatus
from port_allocator import PortAllocator
from webhook_client import WebhookSender

logger = get_logger("call_manager")


class QueueFullError(RuntimeError):
    """Distinct from a plain shutdown-rejection RuntimeError so main.py can
    return 429 + Retry-After instead of silently cancelling the recipient."""
    pass


class CallManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._executor = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_calls,
            thread_name_prefix="call-worker",
        )
        self._port_allocator = PortAllocator(
            settings.rtp_port_min, settings.rtp_port_max, settings.rtp_port_cooldown_seconds
        )
        transfer_extensions = [e.strip() for e in settings.transfer_extensions.split(",") if e.strip()]
        self._extension_pool = ExtensionPool(
            transfer_extensions, cooldown_seconds=settings.transfer_extension_cooldown_seconds
        )
        self._webhook_sender = WebhookSender(settings)
        self._analysis_fetcher = AnalysisFetcher(settings)
        self._sessions: dict[str, CallSession] = {}
        self._lock = threading.RLock()
        self._shutdown_event = threading.Event()
        self._started_at = time.time()

        # F-21/F-22: explicit, lock-guarded counters.
        self._total_scheduled = 0
        self._total_completed = 0
        self._total_failed = 0
        self._queued_count = 0

        self._reaper_alive = threading.Event()
        self._reaper_alive.set()
        self._reaper_thread = threading.Thread(
            target=self._reap_old_sessions, daemon=True, name="call-reaper"
        )
        self._reaper_thread.start()

    # ------------------------------------------------------------------
    def submit_call(self, *, phone_number: str, dynamic_variables: dict, tracking_id: Optional[str]) -> CallSession:
        if self._shutdown_event.is_set():
            raise RuntimeError("service is shutting down; not accepting new calls")

        with self._lock:
            max_queue_depth = self.settings.max_concurrent_calls * self.settings.max_queue_depth_multiplier
            if self._queued_count >= max_queue_depth:
                raise QueueFullError(
                    f"queue depth {self._queued_count} >= limit {max_queue_depth}; "
                    "retry later (F-19 backpressure)"
                )
            self._queued_count += 1
            self._total_scheduled += 1

        session = CallSession(
            phone_number=phone_number,
            dynamic_variables=dynamic_variables,
            settings=self.settings,
            port_allocator=self._port_allocator,
            extension_pool=self._extension_pool,
            tracking_id=tracking_id,
        )
        with self._lock:
            self._sessions[session.call_id] = session

        future = self._executor.submit(self._run_session, session)
        future.add_done_callback(lambda f, s=session: self._on_call_done(s, f))
        logger.info(f"scheduled call {session.call_id} to {phone_number}")
        return session

    def _run_session(self, session: CallSession) -> None:
        # This runs on the worker thread the moment the executor actually
        # dequeues the job -- i.e. exactly when it stops being "queued" and
        # starts being "active" (F-22).
        with self._lock:
            self._queued_count = max(0, self._queued_count - 1)
        session.run()

    def _on_call_done(self, session: CallSession, future) -> None:
        exc = future.exception() if not future.cancelled() else None
        with self._lock:
            if exc is not None:
                logger.error(f"call {session.call_id} worker raised: {exc}")
                self._total_failed += 1
            elif session.status == CallStatus.FAILED:
                self._total_failed += 1
            else:
                self._total_completed += 1

        # F-18: re-enabled. Was dead code (commented out), which forced the
        # .NET client into polling GET /calls/{id} -- straight into the
        # F-03 500-on-every-poll bug. Non-blocking and isolated; see
        # WebhookSender for retry/dead-letter handling.
        self._webhook_sender.send_async(session.to_webhook_payload())

        # Post-call analysis is not available at this instant -- ElevenLabs
        # is still computing it -- so it gets its own notification rather
        # than delaying the completion one above. Fire-and-forget; a failure
        # to fetch never affects the call result already delivered.
        self._analysis_fetcher.fetch_async(
            call_id=session.call_id,
            conversation_id=session.conversation_id,
            on_result=lambda analysis, s=session: self._on_analysis_ready(s, analysis),
        )

    def _on_analysis_ready(self, session: CallSession, analysis: dict) -> None:
        session.set_analysis(analysis)
        self._webhook_sender.send_async(session.to_webhook_payload(event="call_analysis"))

    # ------------------------------------------------------------------
    def get_call(self, call_id: str) -> Optional[CallSession]:
        with self._lock:
            return self._sessions.get(call_id)

    def list_calls(self, status: Optional[CallStatus] = None) -> list[CallSession]:
        with self._lock:
            sessions = list(self._sessions.values())
        if status is not None:
            sessions = [s for s in sessions if s.status == status]
        return sessions

    def hangup(self, call_id: str) -> bool:
        session = self.get_call(call_id)
        if session is None:
            return False
        session.request_hangup()
        return True

    def get_call_by_tracking_id(self, tracking_id: str) -> Optional[CallSession]:
        """tracking_id is caller-supplied (dynamic_variables) and is what
        ElevenLabs already has on hand mid-call, unlike our internal
        call_id. Not guaranteed unique across the process lifetime (e.g. a
        retried batch could reuse one) -- prefer the most recently created
        matching session, since that's the one still plausibly in-flight."""
        with self._lock:
            candidates = [s for s in self._sessions.values() if s.tracking_id == tracking_id]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.created_at)

    def transfer_by_tracking_id(self, tracking_id: str) -> Optional[dict]:
        """Blocks until the transfer resolves or times out -- call off the
        event loop (see main.py's use of run_in_threadpool)."""
        session = self.get_call_by_tracking_id(tracking_id)
        if session is None:
            return None
        return session.request_transfer()

    # ------------------------------------------------------------------
    def health(self) -> dict:
        with self._lock:
            active = sum(
                1 for s in self._sessions.values()
                if s.status in (CallStatus.PENDING, CallStatus.DIALING, CallStatus.RINGING, CallStatus.CONNECTED)
            )
            total_scheduled = self._total_scheduled
            total_completed = self._total_completed
            total_failed = self._total_failed
            queued = self._queued_count
            oldest_queued = 0.0
            if queued:
                queued_sessions = [
                    s for s in self._sessions.values() if s.status == CallStatus.PENDING
                ]
                if queued_sessions:
                    oldest_queued = max(0.0, time.time() - min(s.queued_at for s in queued_sessions))

        port_stats = self._port_allocator.stats()
        extension_stats = self._extension_pool.stats()
        return {
            "status": "ok" if not self._shutdown_event.is_set() else "shutting_down",
            "active_calls": active,
            "capacity": self.settings.max_concurrent_calls,
            "queued_calls": queued,
            "total_scheduled": total_scheduled,
            "total_completed": total_completed,
            "total_failed": total_failed,
            "rtp_ports_free": port_stats["free"],
            "rtp_ports_in_use": port_stats["in_use"],
            "transfer_extensions_free": extension_stats["free"],
            "transfer_extensions_in_use": extension_stats["in_use"],
            "uptime_seconds": time.time() - self._started_at,
            "queue_depth": queued,
            "oldest_queued_seconds": oldest_queued,
            "reaper_alive": self._reaper_alive.is_set(),
            "dropped_log_records": _DroppingQueueHandler.dropped_count,
        }

    # ------------------------------------------------------------------
    def _reap_old_sessions(self) -> None:
        """Evict terminal sessions older than the retention window so the
        registry doesn't grow unbounded across ~500 calls/day.

        F-07: previously an unguarded loop body -- one unexpected exception
        (e.g. a dict-changed-size race, or a logging failure) would kill
        this thread for the entire process lifetime with no alert, silently
        disabling the only thing that bounds registry/RAM growth."""
        while not self._shutdown_event.wait(timeout=60):
            try:
                cutoff = time.time() - self.settings.call_retention_seconds
                with self._lock:
                    stale = [
                        cid for cid, s in self._sessions.items()
                        if s.ended_at is not None and s.ended_at < cutoff
                    ]
                    for cid in stale:
                        del self._sessions[cid]
                if stale:
                    logger.info(f"reaped {len(stale)} completed call record(s)")
            except Exception:
                logger.exception("reaper loop error, continuing")
        self._reaper_alive.clear()

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        logger.info("shutdown requested: hanging up active calls")
        self._shutdown_event.set()
        for session in self.list_calls():
            if session.status in (CallStatus.PENDING, CallStatus.DIALING, CallStatus.RINGING, CallStatus.CONNECTED):
                session.request_hangup()

        deadline = time.time() + self.settings.shutdown_grace_seconds
        while time.time() < deadline:
            still_active = [
                s for s in self.list_calls()
                if s.status in (CallStatus.PENDING, CallStatus.DIALING, CallStatus.RINGING, CallStatus.CONNECTED)
            ]
            if not still_active:
                break
            time.sleep(0.5)

        # F-25: bounded wait even here -- if a call is still stuck despite
        # every timeout in F-02 (shouldn't happen, but defense in depth),
        # don't let executor.shutdown(wait=True) block forever. Python's
        # ThreadPoolExecutor.shutdown() has no timeout parameter, so run it
        # on a helper thread and abandon it at the deadline; worker threads
        # are not daemonic today (F-25 note: consider making them daemon
        # threads at pool-creation time as a second line of defense).
        shutdown_done = threading.Event()

        def _do_shutdown():
            self._executor.shutdown(wait=True, cancel_futures=False)
            shutdown_done.set()

        threading.Thread(target=_do_shutdown, daemon=True, name="executor-shutdown").start()
        if not shutdown_done.wait(timeout=self.settings.shutdown_grace_seconds):
            logger.warning(
                "executor did not finish draining within shutdown_grace_seconds; "
                "abandoning wait (process exit will reclaim remaining threads)"
            )

        self._analysis_fetcher.shutdown()
        self._webhook_sender.shutdown()
        logger.info("call manager shut down")
