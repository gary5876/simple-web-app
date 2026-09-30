import asyncio
import os
import subprocess
import sys
import time

from prometheus_client import REGISTRY

from board.worker_metrics import QUEUE_LENGTH, refresh_queue_metrics
from board.worker_health import is_healthy
from helpers import BROKEN_DATABASE_URL, enqueue, fetch_posts, insert_posts, make_row, new_user_id, post_headers


async def test_user_deleted_event_anonymizes_posts(client, worker, engine, rdb):
    leaving, staying = new_user_id(), new_user_id()
    mine = make_row(leaving, nickname="떠나는사람")
    other = make_row(staying, nickname="남는사람")
    await insert_posts(engine, [mine, other])
    await client.get("/api/board/posts")
    await client.get(f"/api/board/posts/{mine['id']}")

    await rdb.xadd("user:events", {"type": "user_deleted", "user_id": leaving})
    assert await worker.process_user_events_once() == 1

    rows = {str(r["id"]): r for r in await fetch_posts(engine)}
    assert rows[str(mine["id"])]["author_id"] is None
    assert rows[str(mine["id"])]["author_nickname"] == "탈퇴한 사용자"
    assert rows[str(other["id"])]["author_nickname"] == "남는사람"
    assert await rdb.exists("cache:posts:first", f"cache:post:{mine['id']}") == 0
    assert await rdb.xlen("user:events") == 0


async def test_invalid_user_event_is_dropped(worker, rdb):
    await rdb.xadd("user:events", {"type": "user_deleted", "user_id": "garbage"})
    await rdb.xadd("user:events", {"type": "something_else"})
    assert await worker.process_user_events_once() == 2
    assert await rdb.xlen("user:events") == 0


async def test_unacked_user_event_is_reclaimed(make_worker, settings, engine, rdb):
    uid = new_user_id()
    await insert_posts(engine, [make_row(uid)])
    await rdb.xadd("user:events", {"type": "user_deleted", "user_id": uid})
    async with make_worker(settings.model_copy(update={"claim_idle_ms": 0})) as w:
        await rdb.xreadgroup("user-events", "dead-worker", {"user:events": ">"}, count=10)
        assert await w.reclaim_user_events_once() == 1
    assert (await fetch_posts(engine))[0]["author_id"] is None


async def test_run_loop_processes_until_stopped(client, worker, engine, settings):
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    r = await client.post("/api/board/posts", json={"title": "루프", "body": "테스트"}, headers=post_headers(new_user_id()))
    post_id = r.json()["id"]

    for _ in range(50):
        if [str(p["id"]) for p in await fetch_posts(engine)] == [post_id]:
            break
        await asyncio.sleep(0.1)
    assert [str(p["id"]) for p in await fetch_posts(engine)] == [post_id]
    assert is_healthy(settings.heartbeat_path)

    stop.set()
    await asyncio.wait_for(task, timeout=3)


async def test_run_loop_recreates_groups_after_redis_data_loss(client, worker, engine, rdb):
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0.3)
    await rdb.flushall()  # Redis 재시작으로 스트림과 consumer group 이 사라진 상황

    r = await client.post("/api/board/posts", json={"title": "복구", "body": "후"}, headers=post_headers(new_user_id()))
    post_id = r.json()["id"]
    for _ in range(80):
        if [str(p["id"]) for p in await fetch_posts(engine)] == [post_id]:
            break
        await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=3)
    assert [str(p["id"]) for p in await fetch_posts(engine)] == [post_id]


async def test_queue_metrics(rdb, settings):
    uid = new_user_id()
    for _ in range(3):
        await enqueue(rdb, settings, uid)
    await rdb.xadd("posts:dlq", {"id": "x"})
    await refresh_queue_metrics(rdb)
    assert REGISTRY.get_sample_value("queue_length") == 3
    assert REGISTRY.get_sample_value("queue_lag_seconds") >= 0
    assert REGISTRY.get_sample_value("dlq_size") == 1


async def test_run_loop_refreshes_queue_metrics_during_db_outage(make_worker, settings, rdb):
    uid = new_user_id()
    for _ in range(4):
        await enqueue(rdb, settings, uid)
    QUEUE_LENGTH.set(0)
    async with make_worker(settings.model_copy(update={"database_url": BROKEN_DATABASE_URL})) as w:
        stop = asyncio.Event()
        task = asyncio.create_task(w.run(stop))
        for _ in range(30):
            if REGISTRY.get_sample_value("queue_length") == 4:
                break
            await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=6)
    assert REGISTRY.get_sample_value("queue_length") == 4


def test_queue_gauges_are_not_registered_by_api_process():
    code = (
        "import board.app, board.routes, board.cache, board.metrics\n"
        "from prometheus_client import REGISTRY, generate_latest\n"
        "text = generate_latest(REGISTRY).decode()\n"
        "assert 'cache_hit_ratio' in text, text\n"
        "for name in ('queue_length', 'queue_lag_seconds', 'dlq_size'):\n"
        "    assert name not in text, name\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_worker_health(tmp_path):
    path = tmp_path / "hb"
    assert not is_healthy(path)
    path.touch()
    assert is_healthy(path)
    old = time.time() - 60
    os.utime(path, (old, old))
    assert not is_healthy(path)
