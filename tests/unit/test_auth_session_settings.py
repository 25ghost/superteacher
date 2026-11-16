"""AUTH-REF-001 configuration boundaries."""
import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_session_limit_default(monkeypatch):
    monkeypatch.delenv('MAX_SESSIONS_PER_USER', raising=False)
    assert Settings(_env_file=None).MAX_SESSIONS_PER_USER == 5


@pytest.mark.parametrize('value', [1, 5])
def test_session_limit_accepts_supported_values(value):
    assert Settings(_env_file=None, MAX_SESSIONS_PER_USER=value).MAX_SESSIONS_PER_USER == value


@pytest.mark.parametrize('value', [0, -1, 6, 10])
def test_session_limit_rejects_out_of_range(value):
    with pytest.raises(ValidationError, match='MAX_SESSIONS_PER_USER'):
        Settings(_env_file=None, MAX_SESSIONS_PER_USER=value)
