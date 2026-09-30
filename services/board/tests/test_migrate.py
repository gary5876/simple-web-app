import asyncpg

from board.migrate import migrate
from helpers import MIGRATIONS_DIR, to_dsn


async def test_rerun_applies_nothing(pg_url):
    assert await migrate(pg_url, MIGRATIONS_DIR) == []


async def test_fresh_database_gets_both_schemas(pg_url):
    admin_dsn = to_dsn(pg_url)
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute("DROP DATABASE IF EXISTS fresh")
        await conn.execute("CREATE DATABASE fresh")
    finally:
        await conn.close()

    fresh_dsn = admin_dsn.rsplit("/", 1)[0] + "/fresh"
    assert await migrate(fresh_dsn, MIGRATIONS_DIR) == ["001_init.sql"]

    conn = await asyncpg.connect(fresh_dsn)
    try:
        rows = await conn.fetch(
            "SELECT table_schema || '.' || table_name AS t FROM information_schema.tables "
            "WHERE table_schema IN ('auth', 'board')"
        )
    finally:
        await conn.close()
    assert {r["t"] for r in rows} == {"auth.users", "board.posts"}
