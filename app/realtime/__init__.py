"""WebSocket classroom transport plumbing (Phase 3, slice 3C).

Split by concern, kept free of service imports so it can never form a
cycle with the business layer:

- ``close_codes`` — the single documented vocabulary of close codes,
  refusal reasons, event names and client-facing error codes;
- ``connections`` — the in-process registry of live sockets, the opaque
  ``participant_ref`` identity, and per-connection send/cleanup hooks;
- ``event_bus`` — publish-on-commit via PostgreSQL LISTEN/NOTIFY plus
  the one lazy listener thread, dispatching to local connections only.
"""
