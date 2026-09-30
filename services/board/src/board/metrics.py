import time

from prometheus_client import Gauge

from board import keys

_cache_counts = {"hit": 0, "miss": 0}


def record_cache(hit: bool) -> None:
    _cache_counts["hit" if hit else "miss"] += 1


def _cache_hit_ratio() -> float:
    total = _cache_counts["hit"] + _cache_counts["miss"]
    return _cache_counts["hit"] / total if total else 0.0


CACHE_HIT_RATIO = Gauge("cache_hit_ratio", "Redis 읽기 캐시 적중률 (프로세스 시작 이후 누적)")
CACHE_HIT_RATIO.set_function(_cache_hit_ratio)


QUEUE_LENGTH = Gauge("queue_length", "처리 대기 중인 글쓰기 메시지 수 (XLEN posts:stream)")
QUEUE_LAG_SECONDS = Gauge("queue_lag_seconds", "가장 오래된 대기 메시지의 나이(초)")
DLQ_SIZE = Gauge("dlq_size", "실패 큐(posts:dlq)에 쌓인 메시지 수")


async def refresh_queue_metrics(redis) -> None:
    async with redis.pipeline(transaction=False) as pipe:
        pipe.xlen(keys.POSTS_STREAM)
        pipe.xrange(keys.POSTS_STREAM, count=1)
        pipe.xlen(keys.POSTS_DLQ)
        length, oldest, dlq = await pipe.execute()
    QUEUE_LENGTH.set(length)
    if oldest:
        oldest_ms = int(oldest[0][0].split("-")[0])
        QUEUE_LAG_SECONDS.set(max(0.0, time.time() - oldest_ms / 1000))
    else:
        QUEUE_LAG_SECONDS.set(0.0)
    DLQ_SIZE.set(dlq)
