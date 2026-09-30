import pytest
import sqlalchemy.exc

from board.worker import TransientDbError, is_transient
from helpers import BROKEN_DATABASE_URL, enqueue, fetch_posts, new_user_id, post_headers


def test_pool_timeout_is_transient():
    assert is_transient(sqlalchemy.exc.TimeoutError())


async def test_worker_persists_queued_post(client, worker, engine, rdb):
    r = await client.post("/api/board/posts", json={"title": "긴급", "body": "물 배급"}, headers=post_headers(new_user_id(), "봉사자"))
    post_id = r.json()["id"]
    await client.get("/api/board/posts")  # 첫 페이지 캐시 생성

    assert await worker.process_posts_once() == 1

    rows = await fetch_posts(engine)
    assert [str(r["id"]) for r in rows] == [post_id]
    assert rows[0]["author_nickname"] == "봉사자"
    assert await rdb.xlen("posts:stream") == 0  # ACK 후 XDEL
    assert await rdb.exists(f"pending:{post_id}", "cache:posts:first") == 0
    detail = await client.get(f"/api/board/posts/{post_id}")
    assert detail.json()["status"] == "published"


async def test_worker_batches_up_to_batch_size(worker, settings, rdb, engine):
    uid = new_user_id()
    for i in range(150):
        await enqueue(rdb, settings, uid, title=f"글 {i}")
    assert await worker.process_posts_once() == 100
    assert await worker.process_posts_once() == 50
    assert await worker.process_posts_once() == 0
    assert len(await fetch_posts(engine)) == 150


async def test_duplicate_message_is_inserted_once(worker, settings, rdb, engine):
    await enqueue(rdb, settings, new_user_id())
    (_, fields), = await rdb.xrange("posts:stream")
    await rdb.xadd("posts:stream", fields)  # 같은 글이 두 번 들어온 상황
    assert await worker.process_posts_once() == 2
    assert len(await fetch_posts(engine)) == 1


async def test_undecodable_message_goes_to_dlq(worker, settings, rdb, engine):
    good = await enqueue(rdb, settings, new_user_id(), title="정상")
    await rdb.xadd("posts:stream", {"id": "0192f5a0-0000-7000-8000-00000000000a", "author_id": "not-a-uuid"})

    assert await worker.process_posts_once() == 2

    assert [str(r["id"]) for r in await fetch_posts(engine)] == [good]
    dlq = await rdb.xrange("posts:dlq")
    assert len(dlq) == 1
    assert dlq[0][1]["reason"].startswith("decode:")
    assert await rdb.exists("failed:0192f5a0-0000-7000-8000-00000000000a") == 1
    assert await rdb.xlen("posts:stream") == 0


async def test_row_rejected_by_db_goes_to_dlq_and_others_survive(worker, settings, rdb, engine):
    good = await enqueue(rdb, settings, new_user_id(), title="정상")
    bad = await enqueue(rdb, settings, new_user_id(), title="NUL \x00 문자")  # Postgres text 는 NUL 을 거부한다

    assert await worker.process_posts_once() == 2

    assert [str(r["id"]) for r in await fetch_posts(engine)] == [good]
    assert await rdb.exists(f"failed:{bad}") == 1
    assert await rdb.exists(f"pending:{bad}") == 0
    assert (await rdb.xrange("posts:dlq"))[0][1]["reason"].startswith("insert:")


async def test_db_outage_keeps_messages_and_retries(make_worker, settings, rdb, engine):
    post_id = await enqueue(rdb, settings, new_user_id())
    async with make_worker(settings.model_copy(update={"database_url": BROKEN_DATABASE_URL})) as w:
        with pytest.raises(TransientDbError):
            await w.process_posts_once()
        assert await rdb.xlen("posts:stream") == 1
        assert await rdb.xlen("posts:dlq") == 0

        w.engine = engine  # DB 복구
        assert await w.process_posts_once() == 1
    assert [str(r["id"]) for r in await fetch_posts(engine)] == [post_id]
    assert await rdb.xlen("posts:stream") == 0


async def test_reclaims_messages_of_dead_consumer(make_worker, settings, rdb, engine):
    post_id = await enqueue(rdb, settings, new_user_id())
    async with make_worker(settings.model_copy(update={"claim_idle_ms": 0})) as w:
        # 다른 워커가 읽고 죽은 상황
        await rdb.xreadgroup("writers", "dead-worker", {"posts:stream": ">"}, count=10)
        assert await w.reclaim_posts_once() == 1
    assert [str(r["id"]) for r in await fetch_posts(engine)] == [post_id]
    assert await rdb.xlen("posts:stream") == 0


async def test_repeatedly_failing_message_goes_to_dlq(client, make_worker, settings, rdb, engine):
    post_id = await enqueue(rdb, settings, new_user_id())
    async with make_worker(settings.model_copy(update={"claim_idle_ms": 0, "max_deliveries": 1})) as w:
        await rdb.xreadgroup("writers", "dead-worker", {"posts:stream": ">"}, count=10)  # 1회 전달
        assert await w.reclaim_posts_once() == 1  # reclaim 으로 2회째 → 한도 초과
    assert await fetch_posts(engine) == []
    assert await rdb.xlen("posts:dlq") == 1
    assert (await client.get(f"/api/board/posts/{post_id}")).json() == {"status": "failed"}
