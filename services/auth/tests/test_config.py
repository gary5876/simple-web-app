import pytest
from pydantic import ValidationError

from auth.config import Settings

BASE = {"database_url": "postgresql+asyncpg://u:p@h/db", "redis_url": "redis://h:6379/0"}


def test_hash_settings_defaults():
    s = Settings(**BASE)
    assert s.hash_concurrency == 4
    assert s.hash_queue_timeout_seconds == 5.0


@pytest.mark.parametrize("field,value", [("hash_concurrency", 0), ("hash_queue_timeout_seconds", 0)])
def test_hash_settings_reject_non_positive(field, value):
    with pytest.raises(ValidationError):
        Settings(**BASE, **{field: value})
