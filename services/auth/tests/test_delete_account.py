from helpers import PASSWORD, login, signup


async def _delete(client, password: str = PASSWORD):
    return await client.request("DELETE", "/api/auth/me", json={"password": password})


async def test_delete_requires_session(client):
    r = await _delete(client)
    assert r.status_code == 401


async def test_delete_with_wrong_password_is_403(client):
    await signup(client)
    await login(client)
    r = await _delete(client, password="wrong-password")
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"
    assert (await client.get("/api/auth/me")).status_code == 200


async def test_delete_logs_out_every_device_and_publishes_event(make_client, settings, client, rdb):
    user = await signup(client)
    await login(client)
    async with make_client(settings) as other_device:
        await login(other_device)
        other_sid = other_device.cookies.get("sid")

        r = await _delete(client)
        assert r.status_code == 204
        assert "sid=" in r.headers["set-cookie"]

        assert (await other_device.get("/api/auth/me")).status_code == 401
    assert await rdb.exists(f"session:{other_sid}") == 0
    assert await rdb.exists(f"user_sessions:{user['id']}") == 0

    events = await rdb.xrange("user:events")
    assert len(events) == 1
    assert events[0][1] == {"type": "user_deleted", "user_id": user["id"]}


async def test_deleted_user_cannot_login_and_email_can_be_reused(client):
    await signup(client)
    await login(client)
    assert (await _delete(client)).status_code == 204
    assert (await login(client)).status_code == 401
    again = await signup(client)
    assert again["email"] == "alice@example.com"
    assert again["nickname"] == "alice"


async def test_retry_after_partial_failure_completes(client, settings, rdb):
    """DB 삭제 후 Redis 단계가 실패했다고 가정: 같은 세션으로 다시 요청하면 나머지 단계를 마친다."""
    from auth import users
    from auth.infra import make_engine

    user = await signup(client)
    await login(client)
    engine = make_engine(settings.database_url)
    assert await users.soft_delete(engine, user["id"])  # DB 단계만 끝난 상태
    await engine.dispose()

    r = await _delete(client)
    assert r.status_code == 204
    assert len(await rdb.xrange("user:events")) == 1


async def test_publish_failure_keeps_session_and_retry_publishes_once(client, rdb, monkeypatch):
    from redis.exceptions import RedisError

    from auth import routes

    real = routes.publish_user_deleted
    calls = {"n": 0}

    async def flaky(redis, user_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RedisError("boom")
        await real(redis, user_id)

    monkeypatch.setattr(routes, "publish_user_deleted", flaky)
    await signup(client)
    await login(client)

    first = await _delete(client)
    assert first.status_code == 503
    assert (await client.get("/api/auth/me")).status_code == 200

    retry = await _delete(client)
    assert retry.status_code == 204
    events = await rdb.xrange("user:events")
    assert len(events) == 1
    assert events[0][1]["type"] == "user_deleted"
