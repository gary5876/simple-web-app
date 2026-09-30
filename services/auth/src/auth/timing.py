"""요청 하나의 처리 시간을 구간(redis, db 등)별로 모아 Server-Timing 헤더로 내보낸다.

부하 테스트에서 응답시간을 층별로 나눠 보기 위한 계측이다. nginx가 이 헤더를 로그에 남기고 클라이언트에는 숨긴다.
요청 밖(워커 루프 등)에서 부른 track()은 아무것도 기록하지 않는다.
"""

import contextvars
import functools
import time
from contextlib import contextmanager

from redis.asyncio import Redis
from redis.asyncio.client import Pipeline
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

# 구간 이름 -> [누적 초, 호출 수]
_spans: contextvars.ContextVar[dict[str, list] | None] = contextvars.ContextVar("timing_spans", default=None)


def begin() -> tuple[dict[str, list], contextvars.Token]:
    spans: dict[str, list] = {}
    return spans, _spans.set(spans)


def end(token: contextvars.Token) -> None:
    _spans.reset(token)


def add(name: str, seconds: float) -> None:
    spans = _spans.get()
    if spans is None:
        return
    entry = spans.setdefault(name, [0.0, 0])
    entry[0] += seconds
    entry[1] += 1


@contextmanager
def track(name: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        add(name, time.perf_counter() - start)


def timed(name: str):
    """async 함수 전체를 한 구간으로 잰다."""

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            with track(name):
                return await fn(*args, **kwargs)

        return wrapper

    return decorator


def server_timing(spans: dict[str, list], total_seconds: float) -> str:
    parts = [f'{name};dur={sec * 1000:.3f};desc="{n}"' for name, (sec, n) in spans.items()]
    parts.append(f"app;dur={total_seconds * 1000:.3f}")
    return ", ".join(parts)


class TimedPipeline(Pipeline):
    # 파이프라인은 명령을 모았다가 execute()에서 한 번에 왕복하므로 execute만 잰다.
    async def execute(self, raise_on_error: bool = True):
        with track("redis"):
            return await super().execute(raise_on_error)


class TimedRedis(Redis):
    """명령 한 번(= 왕복 한 번)마다 redis 구간에 더한다. 연결 풀 대기 시간도 포함된다."""

    async def execute_command(self, *args, **options):
        with track("redis"):
            return await super().execute_command(*args, **options)

    def pipeline(self, transaction: bool = True, shard_hint: str | None = None) -> Pipeline:
        return TimedPipeline(self.connection_pool, self.response_callbacks, transaction, shard_hint)


def instrument_engine(engine: AsyncEngine) -> None:
    """SQL 실행 자체(db_query)를 잰다. 커넥션 풀 대기·pre-ping·커밋은 db - db_query로 드러난다."""

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        conn.info["timing_start"] = time.perf_counter()

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        start = conn.info.pop("timing_start", None)
        if start is not None:
            add("db_query", time.perf_counter() - start)
