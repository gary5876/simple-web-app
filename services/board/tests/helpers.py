import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from uuid6 import uuid7

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

# 아무것도 떠 있지 않은 포트. 장애 상황을 흉내 낼 때 쓴다.
BROKEN_DATABASE_URL = "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/nowhere"
BROKEN_REDIS_URL = "redis://127.0.0.1:1/0"


def to_dsn(url: str) -> str:
    """SQLAlchemy URL을 asyncpg가 받는 DSN으로 바꾼다."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def new_user_id() -> str:
    return str(uuid.uuid4())


def user_headers(user_id: str, nickname: str = "tester") -> dict[str, str]:
    """nginx 가 auth_request 뒤에 붙여 주는 헤더를 흉내 낸다."""
    return {"X-User-Id": user_id, "X-User-Nickname": quote(nickname, safe="")}


def post_headers(user_id: str, nickname: str = "tester", key: str | None = None) -> dict[str, str]:
    return {**user_headers(user_id, nickname), "Idempotency-Key": key or str(uuid.uuid4())}


def make_row(author_id: str | None, title: str = "제목", body: str = "본문", nickname: str = "tester") -> dict:
    return {
        "id": uuid.UUID(str(uuid7())),
        "author_id": uuid.UUID(author_id) if author_id else None,
        "author_nickname": nickname,
        "title": title,
        "body": body,
        "created_at": datetime.now(UTC),
    }


async def insert_posts(engine, rows: list[dict]) -> None:
    from board.posts_repo import insert_many

    async with engine.begin() as conn:
        await insert_many(conn, rows)
