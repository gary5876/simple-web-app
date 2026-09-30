import uuid
from collections.abc import Iterator

import httpx
import pytest

from conftest import BASE_URL, compose, stack_ready, wait_until


def auth_redis_ready() -> bool:
    # Redis가 살아 있으면 잘못된 세션에 401, 죽어 있으면 503을 돌려준다.
    res = httpx.get(f"{BASE_URL}/api/auth/me", headers={"Cookie": "sid=probe"}, timeout=5)
    return res.status_code == 401


@pytest.fixture
def redis_down() -> Iterator[None]:
    # stop/start는 컨테이너를 유지하고 redis:7-alpine은 종료 시 /data 볼륨에 RDB를 저장하므로
    # 세션은 재시작 후에도 남는다. 그래도 복구는 auth-svc가 다시 Redis에 닿을 때까지 기다린다.
    compose("stop", "redis")
    try:
        yield
    finally:
        compose("start", "redis")
        wait_until(auth_redis_ready)


@pytest.fixture
def auth_down() -> Iterator[None]:
    compose("stop", "auth")
    try:
        yield
    finally:
        compose("start", "auth")
        # 컨테이너 IP가 바뀌었을 수 있으므로 upstream을 다시 해석하게 nginx를 재시작한다.
        compose("restart", "nginx")
        wait_until(stack_ready)


def test_reads_still_work_when_redis_is_down(anon: httpx.Client, redis_down: None) -> None:
    res = anon.get("/api/board/posts")
    assert res.status_code == 200, res.text
    assert "items" in res.json()


def test_writes_get_503_when_redis_is_down(session_client: httpx.Client, redis_down: None) -> None:
    res = session_client.post(
        "/api/board/posts",
        json={"title": "degraded", "body": "redis down"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert res.status_code == 503, res.text
    assert res.json()["code"] == "UNAVAILABLE"
    assert "retry-after" in res.headers


def test_board_returns_json_503_when_auth_is_down(anon: httpx.Client, auth_down: None) -> None:
    res = anon.get("/api/board/posts")
    assert res.status_code == 503
    assert res.json()["code"] == "UNAVAILABLE"
    assert res.headers["retry-after"] == "5"


def test_auth_me_returns_json_503_when_auth_is_down(anon: httpx.Client, auth_down: None) -> None:
    res = anon.get("/api/auth/me")
    assert res.status_code == 503
    assert res.json()["code"] == "UNAVAILABLE"
    assert res.headers["retry-after"] == "5"
