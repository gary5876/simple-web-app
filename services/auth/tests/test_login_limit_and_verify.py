from urllib.parse import unquote

from helpers import BROKEN_REDIS_URL, login, signup


async def test_tenth_failure_blocks_following_attempts(client):
    await signup(client)
    for _ in range(10):
        assert (await login(client, password="wrong-password")).status_code == 401
    r = await login(client)  # 올바른 비밀번호여도 차단
    assert r.status_code == 429
    assert r.json()["code"] == "TOO_MANY_ATTEMPTS"
    assert 1 <= int(r.headers["Retry-After"]) <= 900


async def test_success_resets_failure_counter(client, rdb):
    await signup(client)
    for _ in range(9):
        await login(client, password="wrong-password")
    assert (await login(client)).status_code == 200
    assert await rdb.exists("login_fail:alice@example.com") == 0


async def test_limit_is_per_account(client):
    await signup(client)
    await signup(client, email="bob@example.com", nickname="bob")
    for _ in range(10):
        await login(client, password="wrong-password")
    assert (await login(client, email="bob@example.com")).status_code == 200


async def test_login_is_503_when_redis_down(make_client, settings, client):
    await signup(client)
    async with make_client(settings.model_copy(update={"redis_url": BROKEN_REDIS_URL})) as c:
        r = await login(c)
    assert r.status_code == 503
    assert r.json()["code"] == "UNAVAILABLE"
    assert r.headers["Retry-After"] == "5"


async def test_verify_without_cookie_is_anonymous(client):
    r = await client.get("/internal/verify")
    assert r.status_code == 200
    assert "X-User-Id" not in r.headers
    assert "X-Auth-Degraded" not in r.headers


async def test_verify_with_unknown_sid_is_anonymous(client):
    client.cookies.set("sid", "does-not-exist")
    r = await client.get("/internal/verify")
    assert r.status_code == 200
    assert "X-User-Id" not in r.headers


async def test_verify_returns_user_headers(client):
    user = await signup(client, nickname="한글닉네임")
    await login(client)
    r = await client.get("/internal/verify")
    assert r.status_code == 200
    assert r.headers["X-User-Id"] == user["id"]
    assert r.headers["X-User-Nickname"] != "한글닉네임"  # ASCII 로 인코딩되어 있어야 한다
    assert unquote(r.headers["X-User-Nickname"]) == "한글닉네임"


async def test_verify_reports_degraded_when_redis_down(make_client, settings):
    async with make_client(settings.model_copy(update={"redis_url": BROKEN_REDIS_URL})) as c:
        c.cookies.set("sid", "any")
        r = await c.get("/internal/verify")
    assert r.status_code == 200
    assert r.headers["X-Auth-Degraded"] == "1"
    assert "X-User-Id" not in r.headers


async def test_verify_reports_degraded_on_corrupt_session(client, rdb):
    await rdb.set("session:corrupt", "{not json")
    client.cookies.set("sid", "corrupt")
    r = await client.get("/internal/verify")
    assert r.status_code == 200
    assert r.headers["X-Auth-Degraded"] == "1"
    assert "X-User-Id" not in r.headers
