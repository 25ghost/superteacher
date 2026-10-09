"""The live classroom's message quota (Phase 3, slice 3D, §13).

One participant of one class may commit at most
``WS_MESSAGE_RATE_LIMIT`` (10) new messages per sliding
``WS_MESSAGE_RATE_WINDOW_SECONDS`` (10 s). The rule exists to stop a
scripted flood of an in-progress class, so it lives next to the service
that commits messages and is consulted exactly there — only for a
genuinely NEW message, after authorization and idempotency have already
answered:

- a heartbeat or any other control frame never reaches this limiter;
- an idempotent retry (same ``client_message_id``, same body) never
  reaches it either, so a client that resends until it sees an
  acknowledgement cannot burn its own quota;
- a rejected submission is refused BEFORE any insert or publish.

Honest scope — this is deliberately process-local
--------------------------------------------------
State is an in-process sliding window keyed by ``(class_id, user_id)``
with a hard bound on tracked keys (``max_keys``, least-recently-used
eviction), so memory cannot grow with classes or with time. It does NOT
claim cross-worker guarantees:

- Slice 3C allows only ONE live connection per participant per class
  across all workers (the registry plus the segment's unique open
  index), so one participant's frames arrive at one worker and cannot
  race around the counter through connection duplication;
- a participant who disconnects and reconnects to a DIFFERENT worker
  starts a fresh window there — a known, accepted limitation of an MVP
  without Redis (which slice 3D is forbidden to introduce).

No broad rate-limiting framework is involved: the existing slowapi
primitives throttle HTTP requests keyed by a Starlette ``Request`` and
cannot see a WebSocket frame, which is why this small dedicated counter
exists instead.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict, deque

from app.core.config import get_settings

#: Upper bound on tracked (class, participant) keys. Reaching it evicts
#: the least recently active key, never the one being checked.
DEFAULT_MAX_KEYS = 10_000

MessageKey = tuple[uuid.UUID, uuid.UUID]


class MessageRateLimiter:
    """Sliding-window quota over ``(class_id, user_id)``, thread-safe."""

    def __init__(self, *, max_keys: int = DEFAULT_MAX_KEYS) -> None:
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._windows: OrderedDict[MessageKey, deque[float]] = OrderedDict()

    def allow(self, class_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Consume one quota slot; ``False`` when the window is full.

        Limit and window are re-read from settings on every call so tests
        can shrink them live (the same pattern the heartbeat watchdog
        uses).
        """
        settings = get_settings()
        limit = max(1, int(settings.WS_MESSAGE_RATE_LIMIT))
        window = max(0.0, float(settings.WS_MESSAGE_RATE_WINDOW_SECONDS))
        now = time.monotonic()
        key = (class_id, user_id)

        with self._lock:
            timestamps = self._windows.get(key)
            if timestamps is None:
                timestamps = deque()
                self._windows[key] = timestamps
            cutoff = now - window
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            self._windows.move_to_end(key)
            if len(timestamps) >= limit:
                return False
            timestamps.append(now)
            while len(self._windows) > self._max_keys:
                # The key just used sits at the end (move_to_end above),
                # so the oldest entry is always a different one.
                self._windows.popitem(last=False)
            return True

    def reset(self) -> None:
        """Drop every window (test isolation between cases)."""
        with self._lock:
            self._windows.clear()


#: The process-wide limiter (one per worker, matching one event loop).
message_rate_limiter = MessageRateLimiter()
