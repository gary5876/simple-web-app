import uuid

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from board.errors import ApiError
from board.identity import CurrentUser, require_user
from board.queue import enqueue_post

router = APIRouter()


class PostIn(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=5000)


def _idempotency_key(value: str | None) -> str:
    try:
        return str(uuid.UUID(value or ""))
    except ValueError:
        raise ApiError(
            400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key 헤더(UUID)가 필요합니다."
        ) from None


@router.post("/api/board/posts", status_code=202)
async def create_post(
    body: PostIn,
    request: Request,
    user: CurrentUser = Depends(require_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    key = _idempotency_key(idempotency_key)
    post_id = await enqueue_post(
        request.app.state.redis, request.app.state.settings, user, body.title, body.body, key
    )
    return {"id": post_id, "status": "pending"}
