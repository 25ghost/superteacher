"""Slice 3D — the process-local message quota (§41).

The limiter is a small, honest counter: a sliding window keyed by
``(class_id, user_id)``, consulted once per NEW message. These tests pin
the four properties the classroom depends on — the quota itself, the
window sliding, strict per-class/per-participant scope, and bounded
memory — plus the concurrency claim that makes a process-local counter
acceptable at all: no interleaving of threads may push more successes
through it than the configured limit.
"""
from __future__ import annotations

import threading
import uuid

from app.core.config import get_settings
from app.services.message_rate_limiter import (
    DEFAULT_MAX_KEYS,
    MessageRateLimiter,
    message_rate_limiter,
)


def _keys(n: int) -> list[tuple[uuid.UUID, uuid.UUID]]:
    return [(uuid.uuid4(), uuid.uuid4()) for _ in range(n)]


def test_the_configured_quota_is_the_number_of_allowed_sends() -> None:
    limit = get_settings().WS_MESSAGE_RATE_LIMIT
    limiter = MessageRateLimiter()
    class_id, user_id = uuid.uuid4(), uuid.uuid4()

    allowed = [limiter.allow(class_id, user_id) for _ in range(limit + 5)]
    assert allowed == [True] * limit + [False] * 5


def test_a_refused_send_neither_consumes_nor_extends_the_window() -> None:
    """The 11th frame is refused without adding an 11th timestamp."""
    limiter = MessageRateLimiter()
    class_id, user_id = uuid.uuid4(), uuid.uuid4()
    limit = get_settings().WS_MESSAGE_RATE_LIMIT

    for _ in range(limit):
        assert limiter.allow(class_id, user_id) is True
    window = limiter._windows[(class_id, user_id)]
    assert len(window) == limit
    assert limiter.allow(class_id, user_id) is False
    assert len(window) == limit  # refusal recorded nothing


def test_the_window_slides_with_time(monkeypatch) -> None:
    """Shrinking the window live (settings are re-read) frees the quota."""
    limiter = MessageRateLimiter()
    class_id, user_id = uuid.uuid4(), uuid.uuid4()
    limit = get_settings().WS_MESSAGE_RATE_LIMIT

    for _ in range(limit):
        assert limiter.allow(class_id, user_id) is True
    assert limiter.allow(class_id, user_id) is False

    monkeypatch.setattr(get_settings(), "WS_MESSAGE_RATE_WINDOW_SECONDS", 0)
    assert limiter.allow(class_id, user_id) is True
    assert len(limiter._windows[(class_id, user_id)]) == 1


def test_the_quota_is_per_class_and_per_participant(monkeypatch) -> None:
    """Ten messages in class A say nothing about class B or user B."""
    monkeypatch.setattr(get_settings(), "WS_MESSAGE_RATE_LIMIT", 2)
    limiter = MessageRateLimiter()
    class_a, class_b = uuid.uuid4(), uuid.uuid4()
    user_a, user_b = uuid.uuid4(), uuid.uuid4()

    assert limiter.allow(class_a, user_a) is True
    assert limiter.allow(class_a, user_a) is True
    assert limiter.allow(class_a, user_a) is False  # class A, user A: full

    assert limiter.allow(class_b, user_a) is True  # same user, other class
    assert limiter.allow(class_a, user_b) is True  # same class, other user


def test_memory_is_bounded_and_the_active_key_survives_eviction() -> None:
    """Reaching ``max_keys`` evicts the LEAST recently used entry only."""
    limiter = MessageRateLimiter(max_keys=2)
    (class_a, user_a), (class_b, user_b), (class_c, user_c) = _keys(3)

    assert limiter.allow(class_a, user_a) is True
    assert limiter.allow(class_b, user_b) is True
    assert limiter.allow(class_c, user_c) is True  # evicts A's window

    assert len(limiter._windows) == 2
    assert (class_a, user_a) not in limiter._windows  # oldest was dropped
    assert (class_c, user_c) in limiter._windows  # the key in use stays


def test_the_bound_never_grows_past_the_default() -> None:
    assert DEFAULT_MAX_KEYS == 10_000
    limiter = MessageRateLimiter()  # the process-wide default bound
    for class_id, user_id in _keys(DEFAULT_MAX_KEYS + 50):
        assert limiter.allow(class_id, user_id) is True
    assert len(limiter._windows) <= DEFAULT_MAX_KEYS


def test_reset_drops_every_window() -> None:
    limiter = MessageRateLimiter()
    class_id, user_id = uuid.uuid4(), uuid.uuid4()
    assert limiter.allow(class_id, user_id) is True
    limiter.reset()
    assert limiter._windows == {}
    assert limiter.allow(class_id, user_id) is True


def test_concurrent_frames_never_exceed_the_quota(monkeypatch) -> None:
    """Thread-safety: a flood from many threads still yields exactly N.

    The one claim that makes a process-local counter honest for slice 3D
    (single live connection per participant per class): whatever the
    interleaving, the number of allowed sends can never pass the limit.
    """
    monkeypatch.setattr(get_settings(), "WS_MESSAGE_RATE_LIMIT", 10)
    limiter = MessageRateLimiter()
    class_id, user_id = uuid.uuid4(), uuid.uuid4()
    results: list[bool] = []
    results_lock = threading.Lock()
    start = threading.Barrier(8)

    def flood() -> None:
        start.wait()
        for _ in range(5):  # 40 attempts against a quota of 10
            allowed = limiter.allow(class_id, user_id)
            with results_lock:
                results.append(allowed)

    threads = [threading.Thread(target=flood) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 40
    assert sum(results) == 10


def test_the_singleton_is_the_shared_process_counter() -> None:
    assert isinstance(message_rate_limiter, MessageRateLimiter)
    class_id, user_id = uuid.uuid4(), uuid.uuid4()
    try:
        assert message_rate_limiter.allow(class_id, user_id) is True
    finally:
        message_rate_limiter.reset()
