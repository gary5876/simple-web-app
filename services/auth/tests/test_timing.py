"""Server-Timing 계측 검증. 방식은 board/tests/test_timing.py와 같다(호출 횟수 대조 + 일부러 만든 지연 교정)."""

import asyncio

from auth import passwords
from helpers import login, signup

INJECTED_MS = 200


def spans_of(response) -> dict[str, dict]:
    spans = {}
    for part in response.headers["Server-Timing"].split(","):
        fields = [f.strip() for f in part.split(";")]
        attrs = dict(f.split("=", 1) for f in fields[1:])
        n = attrs.get("desc")
        spans[fields[0]] = {"dur": float(attrs["dur"]), "n": int(n.strip('"')) if n else None}
    return spans


async def test_login_counts(client):
    await signup(client)
    r = await login(client)
    assert r.status_code == 200
    spans = spans_of(r)
    # 차단 확인 GET + 실패 카운터 DELETE + 세션 생성 파이프라인 1회 = 3번 왕복
    assert spans["redis"]["n"] == 3
    assert spans["db"]["n"] == 1
    assert spans["db_query"]["n"] == 1
    assert spans["hash"]["n"] == 1
    assert spans["hash_wait"]["n"] == 1


async def test_verify_with_session_is_one_redis_get(client):
    await signup(client)
    await login(client)
    spans = spans_of(await client.get("/internal/verify"))
    assert spans["redis"]["n"] == 1
    assert "db" not in spans


async def test_verify_without_cookie_touches_nothing(client):
    spans = spans_of(await client.get("/internal/verify"))
    assert set(spans) == {"app"}


async def test_held_hash_slots_show_up_only_in_hash_wait(make_client, settings):
    # argon2 슬롯을 모두 테스트가 쥐고 있다가 INJECTED_MS 뒤에 놓는다. 로그인은 그만큼 hash_wait에서 기다려야 한다.
    async with make_client(settings.model_copy(update={"hash_concurrency": 1})) as c:
        await signup(c)
        await passwords._semaphore.acquire()

        async def release_later():
            await asyncio.sleep(INJECTED_MS / 1000)
            passwords._semaphore.release()

        task = asyncio.create_task(release_later())
        r = await login(c)
        await task

    assert r.status_code == 200
    spans = spans_of(r)
    assert spans["hash_wait"]["dur"] >= INJECTED_MS * 0.8
    # 기다린 시간이 argon2 계산 시간(hash)으로 새어 들어가면 안 된다.
    assert spans["hash"]["dur"] < INJECTED_MS
