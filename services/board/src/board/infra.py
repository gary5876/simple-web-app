import asyncio

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        pool_timeout=5,
        connect_args={"timeout": 2},
    )


def make_redis(url: str) -> Redis:
    # socket_timeout 은 워커의 XREADGROUP BLOCK(1초)보다 길어야 한다.
    return Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=3,
        health_check_interval=30,
    )


async def check_redis(redis: Redis) -> bool:
    try:
        return bool(await asyncio.wait_for(redis.ping(), timeout=1.0))
    except Exception:
        return False


async def check_db(engine: AsyncEngine) -> bool:
    async def _select_one() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(_select_one(), timeout=2.0)
        return True
    except Exception:
        return False
