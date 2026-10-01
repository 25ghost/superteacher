"""Rate limiting infrastructure for the SuperTeacher backend.

Shared ``Limiter`` instance used by the FastAPI app and individual
endpoint modules. Extracted here to avoid circular imports between
``main.py`` and the endpoint modules that apply rate-limit decorators.

The storage backend comes from ``RATE_LIMIT_STORAGE_URI`` (default
``memory://``): per-process counters, which is correct for a single worker
and for tests, and wrong for a multi-worker deployment — point it at a
shared store there. The URI is passed straight to slowapi, which loads the
matching ``limits`` backend on demand (no extra dependency is required just
to declare it).
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import get_settings

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=get_settings().RATE_LIMIT_STORAGE_URI,
)
