"""Unit tests: the realtime event bus listener (Phase 3, slice 3E).

No database — pure mechanics of :class:`app.realtime.event_bus.RealtimeEventBus`:

- ``publish`` validation (a type is mandatory; payloads stay under the
  PostgreSQL NOTIFY limit) and the two delivery paths: ``pg_notify`` on a
  PostgreSQL session (fires with the caller's commit, discarded on
  rollback — nothing dispatches locally), and the SQLite/unit-test
  fall-through to local dispatch;
- local dispatch routing: class-scoped fan-out, ``connection.revoke`` by
  offering+student, ``except_ref`` skipping the sender, sockets without a
  handler, malformed routing fields dropped, and a closed loop being a
  no-op instead of a crash;
- the LISTEN thread itself: a no-op off PostgreSQL, a single idempotent
  start, survival across connection failures (reconnect — a NOTIFY fired
  while no backend listens is dropped forever, so the connection must
  outlive idle ticks), malformed NOTIFY payloads dropped without killing
  the loop, and a clean stop that wakes the idle wait.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import types
import uuid
from types import SimpleNamespace

import pytest

from app.realtime import event_bus as event_bus_module
from app.realtime.connections import LiveConnection, registry
from app.realtime.event_bus import CHANNEL, RealtimeEventBus


# --- fakes --------------------------------------------------------------------------------


class _FakeBind:
    def __init__(self, dialect_name: str) -> None:
        self.dialect = SimpleNamespace(name=dialect_name)


class _FakeSession:
    """Just enough session for publish: bind + recorded pg_notify calls."""

    def __init__(self, dialect_name: str = "sqlite") -> None:
        self._bind = _FakeBind(dialect_name)
        self.executed: list[tuple[str, dict]] = []

    def get_bind(self):
        return self._bind

    def execute(self, statement, parameters=None):  # noqa: ANN001
        self.executed.append((str(statement), parameters or {}))


class _FakeNotify:
    def __init__(self, payload: str) -> None:
        self.payload = payload


class _FakePsycopgConnection:
    """One listening connection: batches of notifications, then idle ticks."""

    def __init__(self, batches: list[list[_FakeNotify]]) -> None:
        self._batches = list(batches)
        self.listened: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement: str) -> None:
        self.listened.append(statement)

    def notifies(self, timeout: float | None = None):  # noqa: ANN001
        if self._batches:
            return self._batches.pop(0)
        # Idle: block briefly like the real driver, never spin.
        time.sleep(min(timeout or 0.05, 0.05))
        return []


def _install_fake_psycopg(monkeypatch, connect):
    """``_listen_loop`` imports psycopg inside the function; fake it there."""
    fake = types.ModuleType("psycopg")
    fake.connect = connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    return fake


def _connection(
    class_id: uuid.UUID,
    *,
    offering_id: uuid.UUID | None = None,
    student_id: uuid.UUID | None = None,
    received: list | None = None,
    handle_event="record",
) -> LiveConnection:
    sink = received if received is not None else []
    handler = None
    if handle_event == "record":
        async def handler(payload: dict) -> None:  # noqa: ANN202
            sink.append(payload)

    return LiveConnection(
        websocket=None,
        class_id=class_id,
        user_id=uuid.uuid4(),
        role="student" if student_id else "teacher",
        display_name="Test Participant",
        participant_ref=uuid.uuid4().hex[:32],
        connection_id=uuid.uuid4().hex,
        teaching_offering_id=offering_id or uuid.uuid4(),
        student_id=student_id,
        handle_event=handler,
    )


async def _settle() -> None:
    """A few loop turns: run_coroutine_threadsafe schedules, then runs."""
    for _ in range(5):
        await asyncio.sleep(0)


# --- publish: validation and the two delivery paths --------------------------------------


def test_publish_requires_a_type() -> None:
    bus = RealtimeEventBus()
    with pytest.raises(ValueError, match="type"):
        bus.publish(_FakeSession(), {"class_id": "x"})


def test_publish_rejects_payloads_postgresql_would_refuse() -> None:
    """NOTIFY silently drops payloads over ~8000 bytes; refuse them loudly."""
    bus = RealtimeEventBus()
    event = {"type": "class.ended", "class_id": "x", "blob": "y" * 9000}
    with pytest.raises(ValueError, match="8000"):
        bus.publish(_FakeSession(), event)


def test_publish_on_postgresql_notifies_the_callers_transaction_only() -> None:
    """pg_notify rides the caller's commit — never a local dispatch."""
    bus = RealtimeEventBus()
    session = _FakeSession(dialect_name="postgresql")

    bus.publish(session, {"type": "class.ended", "class_id": "abc"})

    assert len(session.executed) == 1
    statement, parameters = session.executed[0]
    assert "pg_notify" in statement
    assert parameters["channel"] == CHANNEL
    assert json.loads(parameters["payload"]) == {"type": "class.ended", "class_id": "abc"}


def test_publish_on_sqlite_dispatches_locally() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        bus = RealtimeEventBus()
        bus.bind_loop(loop)
        class_id = uuid.uuid4()
        received: list = []
        connection = _connection(class_id, received=received)
        registry.try_register(connection)
        try:
            bus.publish(
                _FakeSession(dialect_name="sqlite"),
                {"type": "class.ended", "class_id": str(class_id)},
            )
            await _settle()
            assert received == [{"type": "class.ended", "class_id": str(class_id)}]
        finally:
            registry.unregister(connection)

    asyncio.run(scenario())


# --- local dispatch routing ---------------------------------------------------------------


def test_class_scoped_events_reach_every_connection_of_that_class() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        bus = RealtimeEventBus()
        bus.bind_loop(loop)
        class_id, other_class = uuid.uuid4(), uuid.uuid4()
        here, elsewhere = [], []
        first = _connection(class_id, received=here)
        second = _connection(class_id, received=here)
        outsider = _connection(other_class, received=elsewhere)
        registry.try_register(first)
        registry.try_register(second)
        registry.try_register(outsider)
        try:
            bus._dispatch_local({"type": "class.ended", "class_id": str(class_id)})
            await _settle()
            assert len(here) == 2
            assert elsewhere == []
        finally:
            for connection in (first, second, outsider):
                registry.unregister(connection)

    asyncio.run(scenario())


def test_a_revoke_reaches_every_offering_connection_of_that_student() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        bus = RealtimeEventBus()
        bus.bind_loop(loop)
        offering, student = uuid.uuid4(), uuid.uuid4()
        target, bystander, teacher = [], [], []
        first = _connection(uuid.uuid4(), offering_id=offering, student_id=student, received=target)
        second = _connection(uuid.uuid4(), offering_id=offering, student_id=student, received=target)
        other_offering = _connection(
            uuid.uuid4(), offering_id=uuid.uuid4(), student_id=student, received=bystander
        )
        teacher_conn = _connection(
            uuid.uuid4(), offering_id=offering, student_id=None, received=teacher
        )
        for connection in (first, second, other_offering, teacher_conn):
            registry.try_register(connection)
        try:
            bus._dispatch_local(
                {
                    "type": "connection.revoke",
                    "teaching_offering_id": str(offering),
                    "student_id": str(student),
                }
            )
            await _settle()
            # Both classes of that offering+student, and nobody else.
            assert len(target) == 2
            assert bystander == []
            assert teacher == []
        finally:
            for connection in (first, second, other_offering, teacher_conn):
                registry.unregister(connection)

    asyncio.run(scenario())


def test_a_revoke_without_routing_keys_reaches_nobody() -> None:
    bus = RealtimeEventBus()
    assert bus._targets({"type": "connection.revoke"}) == []


def test_except_ref_skips_the_sender() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        bus = RealtimeEventBus()
        bus.bind_loop(loop)
        class_id = uuid.uuid4()
        sender_received, other_received = [], []
        sender = _connection(class_id, received=sender_received)
        other = _connection(class_id, received=other_received)
        registry.try_register(sender)
        registry.try_register(other)
        try:
            bus._dispatch_local(
                {
                    "type": "message.new",
                    "class_id": str(class_id),
                    "except_ref": sender.participant_ref,
                }
            )
            await _settle()
            assert sender_received == []
            assert len(other_received) == 1
        finally:
            registry.unregister(sender)
            registry.unregister(other)

    asyncio.run(scenario())


def test_a_connection_without_a_handler_is_skipped() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        bus = RealtimeEventBus()
        bus.bind_loop(loop)
        class_id = uuid.uuid4()
        silent = _connection(class_id, handle_event=None)
        registry.try_register(silent)
        try:
            # Nothing to await: dispatch must simply not crash on it.
            bus._dispatch_local({"type": "class.ended", "class_id": str(class_id)})
            await asyncio.sleep(0)
            assert silent.handle_event is None
        finally:
            registry.unregister(silent)

    asyncio.run(scenario())


def test_bad_routing_fields_are_dropped_not_raised() -> None:
    bus = RealtimeEventBus()
    # Not a UUID: routing raises inside _targets and is swallowed.
    bus._dispatch_local({"type": "class.ended", "class_id": "not-a-uuid"})
    # No class id at all: nothing to route to.
    bus._dispatch_local({"type": "class.ended"})


def test_a_closed_loop_makes_dispatch_a_no_op() -> None:
    loop = asyncio.new_event_loop()
    loop.close()
    bus = RealtimeEventBus()
    bus.bind_loop(loop)
    class_id = uuid.uuid4()
    received: list = []
    connection = _connection(class_id, received=received)
    registry.try_register(connection)
    try:
        bus._dispatch_local({"type": "class.ended", "class_id": str(class_id)})
        assert received == []
    finally:
        registry.unregister(connection)


# --- the LISTEN thread ---------------------------------------------------------------------


def test_ensure_listening_is_a_no_op_off_postgresql(monkeypatch) -> None:
    """Plain HTTP workers and SQLite unit tests never open the extra connection."""
    monkeypatch.setattr(
        event_bus_module,
        "get_settings",
        lambda: SimpleNamespace(database_url="sqlite+pysqlite:///:memory:"),
    )
    bus = RealtimeEventBus()
    bus.ensure_listening()
    assert bus._thread is None


def _connect_stub(failures: int, batches_by_call: dict | None = None):
    """A psycopg.connect stand-in: N hard failures, then listening fakes."""
    calls = {"count": 0}
    made: list[_FakePsycopgConnection] = []

    def _connect(dsn, autocommit=False):  # noqa: ANN001, ANN001
        index = calls["count"]
        calls["count"] += 1
        if index < failures:
            raise ConnectionError("database unavailable")
        batches = (batches_by_call or {}).get(index, [])
        connection = _FakePsycopgConnection(batches)
        made.append(connection)
        return connection

    return _connect, calls, made


def test_the_listener_survives_a_connection_failure_and_reconnects(
    monkeypatch,
) -> None:
    """A dead backend must not kill the listener — the next NOTIFY still lands."""
    delivered: list[dict] = []
    notify = _FakeNotify(json.dumps({"type": "class.ended", "class_id": "abc"}))
    connect, calls, made = _connect_stub(failures=1, batches_by_call={1: [[notify]]})
    _install_fake_psycopg(monkeypatch, connect)
    monkeypatch.setattr(
        event_bus_module,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://user:pw@localhost/app"),
    )

    bus = RealtimeEventBus()

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        bus.bind_loop(loop)

        original_dispatch = bus._dispatch_local

        def _capture(payload: dict) -> None:
            delivered.append(payload)
            original_dispatch(payload)

        monkeypatch.setattr(bus, "_dispatch_local", _capture)
        bus.ensure_listening()  # blocks until the first successful connect
        deadline = time.monotonic() + 5.0
        while not delivered and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        bus._stop.set()
        thread = bus._thread
        await asyncio.to_thread(thread.join, 3.0)
        assert not thread.is_alive()

    asyncio.run(scenario())

    assert calls["count"] >= 2  # one failure, then a successful reconnect
    assert made[-1].listened == [f"LISTEN {CHANNEL}"]
    assert delivered == [{"type": "class.ended", "class_id": "abc"}]


def test_a_malformed_notify_is_dropped_and_the_loop_survives(monkeypatch) -> None:
    delivered: list[dict] = []
    batches = [
        [
            _FakeNotify("this is not json"),
            _FakeNotify(json.dumps({"type": "class.ended", "class_id": "ok"})),
        ]
    ]
    connect, _calls, _made = _connect_stub(failures=0, batches_by_call={0: batches})
    _install_fake_psycopg(monkeypatch, connect)
    monkeypatch.setattr(
        event_bus_module,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://user:pw@localhost/app"),
    )

    bus = RealtimeEventBus()

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        bus.bind_loop(loop)
        monkeypatch.setattr(
            bus, "_dispatch_local", lambda payload: delivered.append(payload)
        )
        bus.ensure_listening()
        deadline = time.monotonic() + 5.0
        while not delivered and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        bus._stop.set()
        thread = bus._thread
        await asyncio.to_thread(thread.join, 3.0)
        assert not thread.is_alive()

    asyncio.run(scenario())
    assert delivered == [{"type": "class.ended", "class_id": "ok"}]


def test_ensure_listening_starts_exactly_one_thread(monkeypatch) -> None:
    connect, calls, _made = _connect_stub(failures=0)
    _install_fake_psycopg(monkeypatch, connect)
    monkeypatch.setattr(
        event_bus_module,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://user:pw@localhost/app"),
    )

    bus = RealtimeEventBus()
    bus.ensure_listening()
    first = bus._thread
    try:
        assert first is not None and first.is_alive()
        bus.ensure_listening()  # idempotent: same thread, not a second one
        assert bus._thread is first
    finally:
        bus._stop.set()
        first.join(timeout=3.0)
    assert not first.is_alive()


def test_stop_wakes_an_idle_listener_immediately(monkeypatch) -> None:
    """The listener waits on notifies()/the stop flag — stop must not hang."""
    connect, _calls, _made = _connect_stub(failures=0)
    _install_fake_psycopg(monkeypatch, connect)
    monkeypatch.setattr(
        event_bus_module,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://user:pw@localhost/app"),
    )

    bus = RealtimeEventBus()
    bus.ensure_listening()
    thread = bus._thread
    assert thread is not None and thread.is_alive()

    started = time.monotonic()
    bus._stop.set()
    thread.join(timeout=3.0)
    assert not thread.is_alive()
    # Woken from the idle tick, not left waiting out a long sleep.
    assert time.monotonic() - started < 2.0
