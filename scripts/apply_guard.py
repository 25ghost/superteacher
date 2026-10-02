"""Shared production-APPLY guard for the data-loading CLIs.

Two scripts can write reference data:

- ``scripts/seed_reference_data.py`` — refuses production APPLY outright
  (no bypass, by design);
- ``scripts/load_catalog.py`` — the supported production path, but only
  with ``--allow-production``.

Both use the predicate and the refusal wording in this module so an
operator sees one message, not two slightly different ones. The helper
deliberately reads nothing but the environment string: the guard uses the
existing configuration system, never hard-coded environment detection.
"""
from __future__ import annotations

#: Refusal wording, single source of truth for every data-loading CLI.
PRODUCTION_APPLY_REFUSAL = (
    "REFUSED: production APPLY is not permitted. "
    "Use --dry-run for read-only inspection, or run in "
    "development/testing environment."
)


def production_apply_refusal(environment: str) -> str | None:
    """Return the refusal message when APPLY is forbidden, else ``None``.

    Case-insensitive: ``PRODUCTION`` behaves exactly like ``production``.
    """
    if environment.lower() == "production":
        return PRODUCTION_APPLY_REFUSAL
    return None
