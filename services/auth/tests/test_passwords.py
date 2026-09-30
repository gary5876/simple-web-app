import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from auth import passwords


@pytest.fixture(autouse=True)
def _restore_concurrency():
    yield
    passwords.configure(4)


async def test_hash_password_bounds_concurrent_hashing(monkeypatch):
    passwords.configure(2)
    lock = threading.Lock()
    state = {"in_flight": 0, "max": 0}

    def slow_hash(password: str) -> str:
        with lock:
            state["in_flight"] += 1
            state["max"] = max(state["max"], state["in_flight"])
        time.sleep(0.2)
        with lock:
            state["in_flight"] -= 1
        return "hashed:" + password

    monkeypatch.setattr(passwords, "_hasher", SimpleNamespace(hash=slow_hash))

    results = await asyncio.gather(*(passwords.hash_password(f"pw{i}") for i in range(6)))

    assert results == [f"hashed:pw{i}" for i in range(6)]
    assert state["max"] == 2


async def test_verify_password_bounds_concurrent_verification(monkeypatch):
    passwords.configure(2)
    lock = threading.Lock()
    state = {"in_flight": 0, "max": 0}

    def slow_verify(password_hash: str, password: str) -> bool:
        with lock:
            state["in_flight"] += 1
            state["max"] = max(state["max"], state["in_flight"])
        time.sleep(0.2)
        with lock:
            state["in_flight"] -= 1
        return True

    monkeypatch.setattr(passwords, "_hasher", SimpleNamespace(verify=slow_verify))

    results = await asyncio.gather(*(passwords.verify_password("h", "p") for _ in range(6)))

    assert results == [True] * 6
    assert state["max"] == 2


async def test_new_hashes_use_single_lane_and_legacy_hashes_still_verify():
    from argon2 import PasswordHasher

    new_hash = await passwords.hash_password("pw-12345678")
    assert "p=1" in new_hash
    legacy = PasswordHasher().hash("pw-12345678")  # 기본값 p=4
    assert "p=4" in legacy
    assert await passwords.verify_password(legacy, "pw-12345678") is True
    assert await passwords.verify_password(legacy, "wrong") is False
