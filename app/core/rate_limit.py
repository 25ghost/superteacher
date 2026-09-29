"""Rate limiting infrastructure for the SuperTeacher backend.

Shared ``Limiter`` instance used by the FastAPI app and individual
endpoint modules. Extracted here to avoid circular imports between
``main.py`` and the endpoint modules that apply rate-limit decorators.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
