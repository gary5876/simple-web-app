from helpers import PASSWORD, login, signup


async def test_signup_returns_user(client):
    user = await signup(client)
    assert user["email"] == "alice@example.com"
    assert user["nickname"] == "alice"
    assert len(user["id"]) == 36


async def test_signup_normalizes_email_case(client):
    user = await signup(client, email="Alice@Example.COM")
    assert user["email"] == "alice@example.com"


async def test_duplicate_email_is_409(client):
    await signup(client)
    r = await client.post("/api/auth/signup", json={"email": "alice@example.com", "nickname": "other", "password": PASSWORD})
    assert r.status_code == 409
    assert r.json()["code"] == "EMAIL_TAKEN"


async def test_duplicate_nickname_is_409(client):
    await signup(client)
    r = await client.post("/api/auth/signup", json={"email": "bob@example.com", "nickname": "alice", "password": PASSWORD})
    assert r.status_code == 409
    assert r.json()["code"] == "NICKNAME_TAKEN"


async def test_short_password_is_422(client):
    r = await client.post("/api/auth/signup", json={"email": "a@example.com", "nickname": "al", "password": "short"})
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"


async def test_login_sets_httponly_cookie_and_session(client, rdb):
    user = await signup(client)
    r = await login(client)
    assert r.status_code == 200
    assert r.json() == user
    set_cookie = r.headers["set-cookie"]
    assert set_cookie.startswith("sid=")
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    sid = client.cookies.get("sid")
    assert 86000 < await rdb.ttl(f"session:{sid}") <= 86400
    assert await rdb.sismember(f"user_sessions:{user['id']}", sid)


async def test_me_returns_current_user(client):
    user = await signup(client)
    await login(client)
    r = await client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json() == user


async def test_me_without_session_is_401(client):
    r = await client.get("/api/auth/me")
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"


async def test_wrong_password_is_401(client):
    await signup(client)
    r = await login(client, password="wrong-password")
    assert r.status_code == 401
    assert r.json()["code"] == "INVALID_CREDENTIALS"


async def test_unknown_email_is_401(client):
    r = await login(client, email="ghost@example.com")
    assert r.status_code == 401
    assert r.json()["code"] == "INVALID_CREDENTIALS"


async def test_logout_removes_session(client, rdb):
    user = await signup(client)
    await login(client)
    sid = client.cookies.get("sid")
    r = await client.post("/api/auth/logout")
    assert r.status_code == 204
    assert await rdb.exists(f"session:{sid}") == 0
    assert not await rdb.sismember(f"user_sessions:{user['id']}", sid)
    assert (await client.get("/api/auth/me")).status_code == 401
