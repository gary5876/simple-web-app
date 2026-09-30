import asyncio
import time

import asyncpg
import asyncpg.exceptions as pgexc
import pytest
import sqlalchemy.exc
from sqlalchemy import text
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_connection, AsyncAdapt_asyncpg_dbapi

from board.worker import TransientDbError, is_transient
from helpers import BROKEN_DATABASE_URL, enqueue, fetch_posts, new_user_id, post_headers


def test_pool_timeout_is_transient():
    assert is_transient(sqlalchemy.exc.TimeoutError())


def test_statement_timeout_error_is_transient():
    # asyncpg 의 command_timeout 은 asyncio.TimeoutError(= 내장 TimeoutError, OSError 하위)를 던진다.
    assert is_transient(asyncio.TimeoutError())


async def test_engine_enforces_statement_timeout(engine):
    started = time.monotonic()
    with pytest.raises(Exception) as info:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT pg_sleep(10)"))
    assert time.monotonic() - started < 8
    assert is_transient(info.value)


def _wrapped_like_sqlalchemy(pg_error: Exception) -> sqlalchemy.exc.DBAPIError:
    dbapi = AsyncAdapt_asyncpg_dbapi(asyncpg)
    try:
        AsyncAdapt_asyncpg_connection._handle_exception_no_connection(dbapi, pg_error)
    except Exception as adapted:
        return sqlalchemy.exc.DBAPIError.instance("SELECT 1", None, adapted, dbapi.Error)
    raise AssertionError("unreachable")


@pytest.mark.parametrize(
    "pg_error",
    [pgexc.CannotConnectNowError("starting up"), pgexc.TooManyConnectionsError("too many"), pgexc.ReadOnlySQLTransactionError("ro")],
)
def test_postgres_outage_sqlstates_are_transient(pg_error):
    wrapped = _wrapped_like_sqlalchemy(pg_error)
    assert not wrapped.connection_invalidated
    assert is_transient(wrapped)


def test_data_error_is_not_transient():
    assert not is_transient(_wrapped_like_sqlalchemy(pgexc.CharacterNotInRepertoireError("bad NUL")))


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


async def test_over_delivered_valid_post_is_inserted_on_reclaim(client, make_worker, settings, rdb, engine):
    """긴 DB 장애 뒤에는 전달 횟수가 많이 쌓인다. 정상 글은 횟수와 상관없이 저장되어야 한다."""
    post_id = await enqueue(rdb, settings, new_user_id())
    async with make_worker(settings.model_copy(update={"claim_idle_ms": 0})) as w:
        (_, [(mid, _)]), = await rdb.xreadgroup("writers", "dead-worker", {"posts:stream": ">"}, count=10)
        for _ in range(10):  # 장애 중 reclaim 이 반복된 상황
            await rdb.xclaim("posts:stream", "writers", "dead-worker", 0, [mid])
        assert await w.reclaim_posts_once() == 1
    assert [str(r["id"]) for r in await fetch_posts(engine)] == [post_id]
    assert await rdb.xlen("posts:dlq") == 0
    assert await rdb.xlen("posts:stream") == 0
    assert (await client.get(f"/api/board/posts/{post_id}")).json()["status"] == "published"


async def test_reclaim_does_not_dead_letter_during_db_outage(make_worker, settings, rdb):
    await enqueue(rdb, settings, new_user_id())
    broken = settings.model_copy(update={"database_url": BROKEN_DATABASE_URL, "claim_idle_ms": 0})
    async with make_worker(broken) as w:
        await rdb.xreadgroup("writers", "dead-worker", {"posts:stream": ">"}, count=10)
        with pytest.raises(TransientDbError):
            await w.reclaim_posts_once()
    assert await rdb.xlen("posts:dlq") == 0
    assert await rdb.xlen("posts:stream") == 1


async def test_successful_save_clears_stale_failed_marker(worker, settings, rdb):
    post_id = await enqueue(rdb, settings, new_user_id())
    await rdb.set(f"failed:{post_id}", "1")
    assert await worker.process_posts_once() == 1
    assert await rdb.exists(f"failed:{post_id}") == 0


async def test_post_from_deleted_user_is_stored_anonymized(worker, settings, rdb, engine):
    """탈퇴 이벤트가 큐에 남은 글보다 먼저 처리되어도 그 글은 익명으로 저장되어야 한다."""
    leaving, staying = new_user_id(), new_user_id()
    await enqueue(rdb, settings, leaving, title="탈퇴 전 글")
    await enqueue(rdb, settings, staying, title="남는 글")
    await rdb.xadd("user:events", {"type": "user_deleted", "user_id": leaving})

    assert await worker.process_user_events_once() == 1
    assert 0 < await rdb.ttl(f"deleted_user:{leaving}") <= 7200
    assert await worker.process_posts_once() == 2

    rows = {r["title"]: r for r in await fetch_posts(engine)}
    assert rows["탈퇴 전 글"]["author_id"] is None
    assert rows["탈퇴 전 글"]["author_nickname"] == "탈퇴한 사용자"
    assert str(rows["남는 글"]["author_id"]) == staying
    assert rows["남는 글"]["author_nickname"] == "tester"
