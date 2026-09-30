import asyncio

from board import cache
from helpers import (
    BROKEN_DATABASE_URL,
    BROKEN_REDIS_URL,
    insert_posts,
    make_row,
    new_user_id,
    user_headers,
)


async def test_empty_list(client):
    r = await client.get("/api/board/posts")
    assert r.status_code == 200
    assert r.json() == {"items": [], "next_cursor": None}


async def test_cursor_pagination_newest_first(client, engine):
    uid = new_user_id()
    rows = [make_row(uid, title=f"글 {i}") for i in range(25)]
    await insert_posts(engine, rows)

    first = (await client.get("/api/board/posts")).json()
    assert [p["title"] for p in first["items"]] == [f"글 {i}" for i in range(24, 4, -1)]
    assert first["next_cursor"] == first["items"][-1]["id"]

    second = (await client.get("/api/board/posts", params={"cursor": first["next_cursor"]})).json()
    assert [p["title"] for p in second["items"]] == [f"글 {i}" for i in range(4, -1, -1)]
    assert second["next_cursor"] is None


async def test_post_json_shape(client, engine):
    uid = new_user_id()
    row = make_row(uid, title="t", body="b", nickname="닉")
    await insert_posts(engine, [row])
    item = (await client.get("/api/board/posts")).json()["items"][0]
    assert item == {
        "id": str(row["id"]),
        "author_id": uid,
        "author_nickname": "닉",
        "title": "t",
        "body": "b",
        "created_at": row["created_at"].isoformat(),
    }


async def test_limit_validation(client):
    assert (await client.get("/api/board/posts", params={"limit": 51})).status_code == 422
    assert (await client.get("/api/board/posts", params={"cursor": "nope"})).status_code == 422


async def test_first_page_is_cached_briefly(client, engine, rdb):
    uid = new_user_id()
    await insert_posts(engine, [make_row(uid, title="처음 글")])
    await client.get("/api/board/posts")
    assert 0 < await rdb.ttl("cache:posts:first") <= 3
    assert 0 < await rdb.ttl("stale:posts:first") <= 60

    await insert_posts(engine, [make_row(uid, title="새 글")])
    cached = (await client.get("/api/board/posts")).json()
    assert [p["title"] for p in cached["items"]] == ["처음 글"]

    await rdb.delete("cache:posts:first")
    fresh = (await client.get("/api/board/posts")).json()
    assert [p["title"] for p in fresh["items"]] == ["새 글", "처음 글"]


async def test_concurrent_cache_misses_hit_db_once(rdb, settings):
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        return {"items": [], "next_cursor": None}

    results = await asyncio.gather(*(cache.first_page(rdb, loader, settings) for _ in range(10)))
    assert calls == 1
    assert all(r == {"items": [], "next_cursor": None} for r in results)


async def test_list_reads_db_directly_when_redis_down(make_client, settings, engine):
    await insert_posts(engine, [make_row(new_user_id(), title="DB 글")])
    async with make_client(settings.model_copy(update={"redis_url": BROKEN_REDIS_URL})) as c:
        r = await c.get("/api/board/posts")
        detail_id = r.json()["items"][0]["id"]
        detail = await c.get(f"/api/board/posts/{detail_id}")
    assert r.status_code == 200
    assert r.json()["items"][0]["title"] == "DB 글"
    assert detail.json()["status"] == "published"


async def test_list_serves_stale_copy_when_db_down(client, make_client, settings, engine, rdb):
    await insert_posts(engine, [make_row(new_user_id(), title="남아있는 글")])
    await client.get("/api/board/posts")  # cache + stale 채우기
    await rdb.delete("cache:posts:first")  # 짧은 캐시가 만료된 상황
    async with make_client(settings.model_copy(update={"database_url": BROKEN_DATABASE_URL})) as c:
        r = await c.get("/api/board/posts")
    assert r.status_code == 200
    assert r.json()["items"][0]["title"] == "남아있는 글"


async def test_list_is_503_when_db_down_and_no_stale_copy(make_client, settings):
    async with make_client(settings.model_copy(update={"database_url": BROKEN_DATABASE_URL})) as c:
        r = await c.get("/api/board/posts")
    assert r.status_code == 503
    assert r.json()["code"] == "UNAVAILABLE"


async def test_detail_published_and_cached(client, engine, rdb):
    row = make_row(new_user_id(), title="상세")
    await insert_posts(engine, [row])
    r = await client.get(f"/api/board/posts/{row['id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "published"
    assert r.json()["post"]["title"] == "상세"
    assert 0 < await rdb.ttl(f"cache:post:{row['id']}") <= 30


async def test_detail_pending_failed_and_missing(client, rdb):
    await rdb.set("pending:0192f5a0-0000-7000-8000-000000000001", "1")
    await rdb.set("failed:0192f5a0-0000-7000-8000-000000000002", "1")
    pending = await client.get("/api/board/posts/0192f5a0-0000-7000-8000-000000000001")
    failed = await client.get("/api/board/posts/0192f5a0-0000-7000-8000-000000000002")
    missing = await client.get("/api/board/posts/0192f5a0-0000-7000-8000-000000000003")
    assert pending.json() == {"status": "pending"}
    assert failed.json() == {"status": "failed"}
    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"


async def test_delete_own_post(client, engine, rdb):
    uid = new_user_id()
    row = make_row(uid)
    await insert_posts(engine, [row])
    await client.get("/api/board/posts")
    await client.get(f"/api/board/posts/{row['id']}")

    r = await client.delete(f"/api/board/posts/{row['id']}", headers=user_headers(uid))
    assert r.status_code == 204
    assert await rdb.exists("cache:posts:first", f"cache:post:{row['id']}") == 0
    assert (await client.get("/api/board/posts")).json()["items"] == []
    assert (await client.get(f"/api/board/posts/{row['id']}")).status_code == 404


async def test_delete_others_post_is_403(client, engine):
    row = make_row(new_user_id())
    await insert_posts(engine, [row])
    r = await client.delete(f"/api/board/posts/{row['id']}", headers=user_headers(new_user_id()))
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"


async def test_delete_missing_is_404_and_anonymous_is_401(client):
    missing = "0192f5a0-0000-7000-8000-000000000009"
    assert (await client.delete(f"/api/board/posts/{missing}", headers=user_headers(new_user_id()))).status_code == 404
    assert (await client.delete(f"/api/board/posts/{missing}")).status_code == 401
