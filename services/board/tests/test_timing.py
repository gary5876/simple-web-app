"""Server-Timing 계측 검증.

계측 코드를 믿지 않고 확인하는 방법 두 가지를 쓴다.
- 호출 횟수: 코드상 정해진 Redis/DB 왕복 수와 계측이 센 수가 같아야 한다.
- 교정(calibration): Redis/Postgres 쪽에서 일부러 지연을 만들고, 계측이 그 지연을 해당 구간에서만 잡는지 본다.
"""

import asyncio
import re

import asyncpg

from helpers import insert_posts, make_row, new_user_id, post_headers, to_dsn

POST = {"title": "대피소 위치", "body": "OO초등학교 체육관이 열려 있습니다."}
INJECTED_MS = 200


def parse_server_timing(value: str) -> dict[str, dict]:
    """'redis;dur=1.2;desc="3", app;dur=4.5' -> {"redis": {"dur": 1.2, "n": 3}, "app": {"dur": 4.5, "n": None}}"""
    spans = {}
    for part in value.split(","):
        fields = [f.strip() for f in part.split(";")]
        name, attrs = fields[0], dict(f.split("=", 1) for f in fields[1:])
        n = attrs.get("desc")
        spans[name] = {"dur": float(attrs["dur"]), "n": int(n.strip('"')) if n else None}
    return spans


def spans_of(response) -> dict[str, dict]:
    return parse_server_timing(response.headers["Server-Timing"])


async def test_write_counts_four_redis_round_trips_and_no_db(client):
    # enqueue_post: GET(멱등 키) + XLEN + SET NX + 파이프라인(XADD, SET) 1회 = 4번 왕복
    r = await client.post("/api/board/posts", json=POST, headers=post_headers(new_user_id()))
    assert r.status_code == 202
    spans = spans_of(r)
    assert spans["redis"]["n"] == 4
    assert "db" not in spans


async def test_cached_first_page_is_one_redis_get(client, engine):
    await insert_posts(engine, [make_row(new_user_id())])
    await client.get("/api/board/posts")  # 캐시를 채운다
    spans = spans_of(await client.get("/api/board/posts"))
    assert spans["redis"]["n"] == 1
    assert "db" not in spans


async def test_cursor_page_goes_to_db_with_one_query(client, engine):
    rows = [make_row(new_user_id()) for _ in range(3)]
    await insert_posts(engine, rows)
    cursor = str(max(r["id"] for r in rows))
    spans = spans_of(await client.get(f"/api/board/posts?cursor={cursor}"))
    assert spans["db"]["n"] == 1
    assert spans["db_query"]["n"] == 1
    assert "redis" not in spans


async def test_spans_fit_inside_app_total(client, engine):
    await insert_posts(engine, [make_row(new_user_id())])
    spans = spans_of(await client.get("/api/board/posts"))  # 캐시 미스: redis + db
    assert spans["db_query"]["dur"] <= spans["db"]["dur"]
    assert spans["redis"]["dur"] + spans["db"]["dur"] <= spans["app"]["dur"]


async def test_redis_pause_shows_up_only_in_redis_span(client, rdb):
    # CLIENT PAUSE: Redis 서버가 모든 클라이언트 명령을 INJECTED_MS 동안 붙잡는다(앱 코드 밖에서 만든 지연).
    await rdb.execute_command("CLIENT", "PAUSE", INJECTED_MS, "ALL")
    r = await client.post("/api/board/posts", json=POST, headers=post_headers(new_user_id()))
    assert r.status_code == 202
    spans = spans_of(r)
    assert spans["redis"]["dur"] >= INJECTED_MS * 0.8
    assert spans["app"]["dur"] - spans["redis"]["dur"] < INJECTED_MS * 0.5


async def test_table_lock_shows_up_in_db_query_span(client, engine, pg_url):
    rows = [make_row(new_user_id()) for _ in range(3)]
    await insert_posts(engine, rows)
    cursor = str(max(r["id"] for r in rows))
    # 커넥션을 미리 열어 둔다. 안 그러면 연결 수립 시간만큼 잠금 대기가 줄어 db_query가 INJECTED_MS보다 짧게 나온다.
    await client.get(f"/api/board/posts?cursor={cursor}")

    # 다른 연결이 테이블을 INJECTED_MS 동안 잠가서 SELECT를 기다리게 한다.
    locker = await asyncpg.connect(to_dsn(pg_url))
    locked = asyncio.Event()

    async def hold_lock():
        async with locker.transaction():
            await locker.execute("LOCK TABLE board.posts IN ACCESS EXCLUSIVE MODE")
            locked.set()
            await asyncio.sleep(INJECTED_MS / 1000)

    task = asyncio.create_task(hold_lock())
    try:
        await locked.wait()
        r = await client.get(f"/api/board/posts?cursor={cursor}")
        await task
    finally:
        await locker.close()

    assert r.status_code == 200
    spans = spans_of(r)
    assert spans["db_query"]["dur"] >= INJECTED_MS * 0.8
    assert spans["db"]["dur"] - spans["db_query"]["dur"] < INJECTED_MS * 0.5


async def test_app_span_is_always_present(client):
    r = await client.get("/healthz")
    assert re.fullmatch(r"app;dur=\d+\.\d+", r.headers["Server-Timing"])
