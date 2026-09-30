import uuid

from helpers import BROKEN_DATABASE_URL, BROKEN_REDIS_URL, new_user_id, post_headers, user_headers

POST = {"title": "대피소 위치", "body": "OO초등학교 체육관이 열려 있습니다."}


async def test_anonymous_write_is_401(client):
    r = await client.post("/api/board/posts", json=POST, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"


async def test_degraded_auth_is_503(client):
    headers = {**post_headers(new_user_id()), "X-Auth-Degraded": "1"}
    r = await client.post("/api/board/posts", json=POST, headers=headers)
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "5"


async def test_missing_idempotency_key_is_400(client):
    r = await client.post("/api/board/posts", json=POST, headers=user_headers(new_user_id()))
    assert r.status_code == 400
    assert r.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_invalid_idempotency_key_is_400(client):
    headers = {**user_headers(new_user_id()), "Idempotency-Key": "not-a-uuid"}
    r = await client.post("/api/board/posts", json=POST, headers=headers)
    assert r.status_code == 400
    assert r.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_title_too_long_is_422(client):
    r = await client.post("/api/board/posts", json={"title": "x" * 101, "body": "b"}, headers=post_headers(new_user_id()))
    assert r.status_code == 422


async def test_accepted_post_is_queued_with_pending_marker(client, rdb):
    uid = new_user_id()
    r = await client.post("/api/board/posts", json=POST, headers=post_headers(uid, "재난봇"))
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending"
    post_id = body["id"]
    assert uuid.UUID(post_id).version == 7

    entries = await rdb.xrange("posts:stream")
    assert len(entries) == 1
    fields = entries[0][1]
    assert fields["id"] == post_id
    assert fields["author_id"] == uid
    assert fields["author_nickname"] == "재난봇"
    assert fields["title"] == POST["title"]
    assert fields["created_at"].endswith("+00:00")
    assert 3500 < await rdb.ttl(f"pending:{post_id}") <= 3600


async def test_same_idempotency_key_returns_same_post(client, rdb):
    headers = post_headers(new_user_id(), key=str(uuid.uuid4()))
    first = await client.post("/api/board/posts", json=POST, headers=headers)
    second = await client.post("/api/board/posts", json=POST, headers=headers)
    assert first.json()["id"] == second.json()["id"]
    assert await rdb.xlen("posts:stream") == 1


async def test_full_queue_is_503_and_does_not_burn_idempotency_key(make_client, settings, rdb):
    uid = new_user_id()
    key = str(uuid.uuid4())
    async with make_client(settings.model_copy(update={"queue_max_len": 1})) as c:
        assert (await c.post("/api/board/posts", json=POST, headers=post_headers(uid))).status_code == 202
        full = await c.post("/api/board/posts", json=POST, headers=post_headers(uid, key=key))
        assert full.status_code == 503
        assert full.json()["code"] == "QUEUE_FULL"
        assert full.headers["Retry-After"] == "5"

        # 워커가 큐를 비운 뒤 같은 키로 재시도하면 새로 큐에 들어가야 한다.
        for entry_id, _ in await rdb.xrange("posts:stream"):
            await rdb.xdel("posts:stream", entry_id)
        retry = await c.post("/api/board/posts", json=POST, headers=post_headers(uid, key=key))
    assert retry.status_code == 202
    assert await rdb.xlen("posts:stream") == 1


async def test_writes_are_accepted_while_db_is_down(make_client, settings):
    async with make_client(settings.model_copy(update={"database_url": BROKEN_DATABASE_URL})) as c:
        r = await c.post("/api/board/posts", json=POST, headers=post_headers(new_user_id()))
    assert r.status_code == 202


async def test_writes_are_503_while_redis_is_down(make_client, settings):
    async with make_client(settings.model_copy(update={"redis_url": BROKEN_REDIS_URL})) as c:
        r = await c.post("/api/board/posts", json=POST, headers=post_headers(new_user_id()))
    assert r.status_code == 503
    assert r.json()["code"] == "UNAVAILABLE"
