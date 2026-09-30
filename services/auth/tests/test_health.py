import time

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from auth.app import create_app
from auth.infra import make_engine
from helpers import BROKEN_DATABASE_URL, BROKEN_REDIS_URL, PASSWORD


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_readyz_ok(client):
    r = await client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"redis": True, "db": True}


async def test_readyz_503_when_redis_down(make_client, settings):
    async with make_client(settings.model_copy(update={"redis_url": BROKEN_REDIS_URL})) as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"redis": False, "db": True}


async def test_readyz_503_when_db_down(make_client, settings):
    async with make_client(settings.model_copy(update={"database_url": BROKEN_DATABASE_URL})) as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"redis": True, "db": False}


async def test_unknown_route_uses_error_format(client):
    r = await client.get("/nope")
    assert r.status_code == 404
    assert r.json()["code"] == "NOT_FOUND"


async def test_request_id_is_echoed(client):
    r = await client.get("/healthz", headers={"X-Request-ID": "req-123"})
    assert r.headers["X-Request-ID"] == "req-123"


async def test_request_id_is_generated_when_missing(client):
    r = await client.get("/healthz")
    assert len(r.headers["X-Request-ID"]) == 32


async def test_metrics_exposes_http_counters(client):
    await client.get("/healthz")
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert 'http_requests_total{method="GET",route="/healthz",status="200"}' in r.text


async def test_unhandled_error_is_500_json(settings, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr("auth.users.create_user", boom)
    app = create_app(settings)
    async with LifespanManager(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            r = await c.post("/api/auth/signup", json={"email": "a@example.com", "nickname": "alice", "password": PASSWORD})
    assert r.status_code == 500
    assert r.json()["code"] == "INTERNAL"
    assert r.json()["message"]


async def test_engine_enforces_statement_timeout(settings):
    engine = make_engine(settings.database_url)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):  # OSError 하위라 503 핸들러가 잡는다
            async with engine.connect() as conn:
                await conn.execute(text("SELECT pg_sleep(10)"))
    finally:
        await engine.dispose()
    assert time.monotonic() - started < 8


async def test_readyz_200_when_db_down_but_not_required(make_client, settings):
    # auth-verify 배포는 DB 없이 Redis만으로 검증하므로 DB 장애가 readiness를 깨지 않아야 한다.
    degraded = settings.model_copy(update={"database_url": BROKEN_DATABASE_URL, "ready_requires_db": False})
    async with make_client(degraded) as c:
        r = await c.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"redis": True, "db": False}
