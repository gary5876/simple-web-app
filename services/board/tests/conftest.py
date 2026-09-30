import asyncio

import pytest
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from board.migrate import migrate
from helpers import MIGRATIONS_DIR


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
