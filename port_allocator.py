"""
Thread-safe allocator for local RTP ports.

F-16 fixes three related bugs in the original allocator:

  A. LIFO reuse (`list.pop()` / `list.append()`): a port freed by a call that
     just ended was the very next port handed to a new call. The far end
     frequently keeps sending RTP for a second or two after BYE, so the new
     call would receive the previous call's residual audio. Fixed with a
     FIFO deque plus an explicit cooldown/quarantine window before a
     released port becomes eligible again.
  B. Odd ports handed out indiscriminately. By RFC 3550 convention, RTP
     media uses an even port with RTCP on the next (odd) port above it.
     Fixed: only even ports are ever allocated; `port + 1` is implicitly
     reserved for RTCP and never handed out on its own.
  C. `SO_REUSEADDR` on the RTP socket meant something more dangerous than
     intended on Windows (lets another socket steal the same port). That
     part of the fix lives in audio_bridge.py, which owns the actual UDP
     socket -- this allocator just needs to hand out a hygienic port list.

This remains the only piece of shared mutable state in the service and is
still guarded by a single narrow lock with O(1)-ish acquire/release.
"""
from __future__ import annotations

import threading
import time
from collections import deque


class PortPoolExhausted(RuntimeError):
    pass


class PortAllocator:
    def __init__(self, port_min: int, port_max: int, cooldown_seconds: float = 5.0):
        if port_max <= port_min:
            raise ValueError("port_max must be greater than port_min")
        # Even ports only (F-16C); port+1 is implicitly reserved for RTCP.
        first_even = port_min if port_min % 2 == 0 else port_min + 1
        self._free: deque[int] = deque(range(first_even, port_max + 1, 2))
        self._in_use: set[int] = set()
        # port -> time it becomes eligible again after release (F-16A)
        self._quarantined: dict[int, float] = {}
        self._cooldown = cooldown_seconds
        self._lock = threading.Lock()

    def acquire(self) -> int:
        with self._lock:
            now = time.monotonic()
            # Promote any quarantined ports whose cooldown has elapsed.
            ready = [p for p, until in self._quarantined.items() if until <= now]
            for p in ready:
                del self._quarantined[p]
                self._free.append(p)

            if not self._free:
                raise PortPoolExhausted(
                    "No free RTP ports available — increase RTP_PORT_MIN/RTP_PORT_MAX "
                    "or raise MAX_CONCURRENT_CALLS capacity planning."
                )
            port = self._free.popleft()  # FIFO: oldest-released port goes out first
            self._in_use.add(port)
            return port

    def release(self, port: int) -> None:
        with self._lock:
            if port in self._in_use:
                self._in_use.discard(port)
                self._quarantined[port] = time.monotonic() + self._cooldown

    def reject(self, port: int) -> None:
        """Call this instead of release() if bind() on the port itself
        failed (F-16C fallback path) -- quarantines it without ever having
        counted it as in-use, so callers can just try the next port."""
        with self._lock:
            self._in_use.discard(port)
            self._quarantined[port] = time.monotonic() + self._cooldown

    def stats(self) -> dict:
        with self._lock:
            return {
                "free": len(self._free),
                "in_use": len(self._in_use),
                "quarantined": len(self._quarantined),
            }
