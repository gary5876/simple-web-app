from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

BROKEN_DATABASE_URL = "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/nowhere"
BROKEN_REDIS_URL = "redis://127.0.0.1:1/0"

PASSWORD = "correct-horse-1"


def to_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)
