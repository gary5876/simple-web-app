from helpers import BROKEN_DATABASE_URL, BROKEN_REDIS_URL


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
