"""Latest perception frame per source for the read-only state Query.

The session endpoint writes and the Query reads from the same event loop, so no
lock is needed: each update replaces an immutable snapshot atomically from the
loop's point of view. A failed capture never leaves the preceding snapshot usable
for longer than the caller's freshness window.

The store keeps exactly the newest frame per source and nothing older: a caller
that needs the recent past has to watch across repeated reads. Every frame carries
its receive time, so it stays aged at read time rather than frozen at capture time.
A read also records each returned digest, so the next read can report
``changed_since_last_read`` and a watcher can skip a vision pass on a frame it has
already judged.
"""

import time

from .contracts import LIMITATIONS, PerceptionFrame, PerceptionStateOutput


class StateStore:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.frames = []
        self.updated = None
        self.active = False
        self.simulated = False
        # Digest returned by the previous successful read, per source. A read is the
        # only writer, so no lock is needed; a failed or stale read leaves it alone
        # so the next successful read still reports its frames as changed.
        self.last_read_sha = {}

    def update(self, frames, *, active, simulated):
        self.frames, self.updated = list(frames), self.clock()
        self.active, self.simulated = active, simulated

    def mark_inactive(self):
        self.active = False

    def snapshot(self, max_age_s):
        """Return the latest state within the freshness window, or None when unusable."""
        if self.updated is None or not self.frames:
            return None
        now = self.clock()
        age = now - self.updated
        if not 0 <= age <= max_age_s:
            return None
        # Age is re-derived at read time; capture-time age would understate it.
        current = []
        for frame in self.frames:
            values = frame.model_dump()
            values["age_ms"] = (now - frame.received_monotonic_s) * 1000
            values["changed_since_last_read"] = self.last_read_sha.get(frame.source) != frame.sha256
            current.append(PerceptionFrame(**values))
        self.last_read_sha = {frame.source: frame.sha256 for frame in self.frames}
        return PerceptionStateOutput(
            updated_monotonic_s=self.updated,
            store_age_ms=age * 1000,
            session_active=self.active,
            frames=current,
            simulated=self.simulated,
            limitations=list(LIMITATIONS),
        )
