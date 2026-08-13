"""
Thread-safe FIFO pool of internal extensions eligible for SIP-REFER call
transfer. Mirrors PortAllocator's acquire/release/cooldown pattern
(see port_allocator.py) for the same reason: an extension that a transfer
just failed on (or that a call was just handed off to) shouldn't be handed
to the very next transfer attempt before the PBX side has settled.

Caveat: this pool has no visibility into the PBX's actual registration/
presence state for an extension -- it only tracks what THIS middleware has
handed out. A successfully transferred extension is assumed busy for
`busy_seconds` (config: TRANSFER_EXTENSION_BUSY_SECONDS) and then becomes
eligible again automatically; there's no signal for when that human call
actually ends.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional


class ExtensionPoolExhausted(RuntimeError):
    pass


class ExtensionPool:
    def __init__(self, extensions: list[str], cooldown_seconds: float = 2.0):
        self._free: deque[str] = deque(dict.fromkeys(e.strip() for e in extensions if e.strip()))
        self._in_use: set[str] = set()
        # extension -> time it becomes eligible again
        self._quarantined: dict[str, float] = {}
        self._cooldown = cooldown_seconds
        self._lock = threading.Lock()

    def acquire(self) -> str:
        with self._lock:
            now = time.monotonic()
            ready = [e for e, until in self._quarantined.items() if until <= now]
            for e in ready:
                del self._quarantined[e]
                self._free.append(e)

            if not self._free:
                raise ExtensionPoolExhausted("no internal extensions available for transfer")
            ext = self._free.popleft()
            self._in_use.add(ext)
            return ext

    def release(self, extension: str, *, busy_seconds: Optional[float] = None) -> None:
        with self._lock:
            if extension in self._in_use:
                self._in_use.discard(extension)
                wait = busy_seconds if busy_seconds is not None else self._cooldown
                self._quarantined[extension] = time.monotonic() + wait

    def stats(self) -> dict:
        with self._lock:
            return {
                "free": len(self._free),
                "in_use": len(self._in_use),
                "quarantined": len(self._quarantined),
            }
