import asyncio
from contextlib import asynccontextmanager

import asyncpg
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from board.app import create_app
from board.config import Settings
from board.infra import make_engine, make_redis
from board.migrate import migrate
from board.worker import Worker
from helpers import MIGRATIONS_DIR, to_dsn


@pytest.fixture(scope="session")
def pg_url():
    with PostgresContainer("postgres:16-alpine", driver=None) as pg:
        url = pg.get_connection_url()
        asyncio.run(migrate(url, MIGRATIONS_DIR))
        yield url.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(scope="session")
def redis_url():
    with RedisContainer("redis:7-alpine") as rc:
        yield f"redis://{rc.get_container_host_ip()}:{rc.get_exposed_port(6379)}/0"


@pytest.fixture
async def clean_state(pg_url, redis_url):
    conn = await asyncpg.connect(to_dsn(pg_url))
    try:
        await conn.execute("TRUNCATE board.posts, auth.users")
    finally:
        await conn.close()
    r = Redis.from_url(redis_url)
    try:
        await r.flushall()
    finally:
        await r.aclose()


@pytest.fixture
def settings(pg_url, redis_url, clean_state, tmp_path) -> Settings:
    return Settings(database_url=pg_url, redis_url=redis_url, heartbeat_path=str(tmp_path / "heartbeat"))


@pytest.fixture
def make_client():
    @asynccontextmanager
    async def _make(settings: Settings):
        app = create_app(settings)
        async with LifespanManager(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
                yield c

    return _make


@pytest.fixture
async def client(make_client, settings):
    async with make_client(settings) as c:
        yield c


@pytest.fixture
async def rdb(settings):
    r = Redis.from_url(settings.redis_url, decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
async def engine(settings):
    e = make_engine(settings.database_url)
    yield e
    await e.dispose()


@pytest.fixture
def make_worker():
    @asynccontextmanager
    async def _make(settings: Settings, name: str = "test-worker"):
        engine = make_engine(settings.database_url)
        redis = make_redis(settings.redis_url)
        worker = Worker(engine, redis, settings, name)
        try:
            await worker.ensure_groups()
            yield worker
        finally:
            await redis.aclose()
            await worker.engine.dispose()
            if worker.engine is not engine:
                await engine.dispose()

    return _make


@pytest.fixture
async def worker(make_worker, settings):
    async with make_worker(settings) as w:
        yield w
