"""
Round-robin chooser over the configured call-transfer targets.

REPLACES ExtensionPool, AND WHY IT IS NOT A POOL
------------------------------------------------
The old ExtensionPool modelled transfer targets as scarce resources: acquire
one, quarantine it for TRANSFER_EXTENSION_BUSY_SECONDS after a successful
handoff, and refuse the transfer outright once none were free. That model was
wrong for what TRANSFER_EXTENSIONS actually points at -- a PBX queue / hotline
number, which HOLDS callers rather than filling up. Worse, the pool had no PBX
presence or BLF visibility at all: it only ever tracked what this middleware
had itself handed out, so "all lines are busy" was a guess made from no
evidence about the PBX's real state.

The observable failure: a second transfer to the same queue within
TRANSFER_EXTENSION_BUSY_SECONDS (default 300 s) was refused by the middleware,
and the caller -- who had just been told "هيتم تحويل المكالمة دلوقتي" -- heard
"all lines are busy, we'll contact you within two days" instead, while the
queue sat there ready to take them.

So there is nothing to allocate here. next_target() hands out the configured
numbers in rotation, never blocks, never raises, and has no notion of a target
being in use or on cooldown. Whether the queue can take the call is the PBX's
answer to give, and it gives it in the REFER response -- see
CallSession._perform_transfer.

Rotation, rather than always returning the first entry, only matters when
several hotline numbers are configured: it spreads calls across them instead
of hammering one. It is NOT a capacity mechanism, and nothing downstream may
treat it as one.
"""
from __future__ import annotations

import threading
from typing import Optional


class TransferTargets:
    def __init__(self, targets: list[str]):
        # dict.fromkeys de-duplicates while preserving configured order --
        # a repeated number in TRANSFER_EXTENSIONS would otherwise just get
        # a bigger share of the rotation for no stated reason.
        self._targets: list[str] = list(dict.fromkeys(t.strip() for t in targets if t.strip()))
        self._cursor = 0
        self._lock = threading.Lock()

    def __bool__(self) -> bool:
        """False only when nothing is configured.

        That is the one remaining case in which a transfer cannot be
        attempted at all, and it is a misconfiguration rather than a busy
        line. CallSession reads this to decide whether to fall back to the
        busy prompt."""
        return bool(self._targets)

    def next_target(self) -> Optional[str]:
        """The next configured target, or None if none are configured.

        Never raises and never refuses: a queue does not run out. None means
        TRANSFER_EXTENSIONS is empty."""
        with self._lock:
            if not self._targets:
                return None
            target = self._targets[self._cursor]
            self._cursor = (self._cursor + 1) % len(self._targets)
            return target

    def stats(self) -> dict:
        """Count only, deliberately not the numbers themselves -- health
        output is logged and forwarded, and there is no operational reason
        to spray PBX hotline numbers through it. The chosen target is
        already logged per transfer, under that call's own logger."""
        with self._lock:
            return {"configured": len(self._targets)}
