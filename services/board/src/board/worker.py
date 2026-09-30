"""board-worker: posts:stream 을 소비해 board.posts 에 저장한다."""
import logging
import uuid
from datetime import datetime

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine

from board import keys, posts_repo
from board.config import Settings

log = logging.getLogger("board.worker")

Entry = tuple[str, dict[str, str]]


class TransientDbError(Exception):
    """DB 연결 문제. 메시지를 버리지 않고 나중에 다시 시도한다."""


def is_transient(exc: BaseException) -> bool:
    # PoolTimeoutError: 커넥션 풀 고갈. DB 가 바쁜 것이므로 배치를 보존하고 재시도한다.
    if isinstance(exc, (OSError, ConnectionError, PoolTimeoutError)):
        return True
    if isinstance(exc, DBAPIError):
        return bool(exc.connection_invalidated) or isinstance(exc, (OperationalError, InterfaceError))
    return False


def decode_post(fields: dict[str, str]) -> dict:
    return {
        "id": uuid.UUID(fields["id"]),
        "author_id": uuid.UUID(fields["author_id"]),
        "author_nickname": fields["author_nickname"],
        "title": fields["title"],
        "body": fields["body"],
        "created_at": datetime.fromisoformat(fields["created_at"]),
    }


class Worker:
    def __init__(self, engine: AsyncEngine, redis: Redis, settings: Settings, name: str):
        self.engine = engine
        self.redis = redis
        self.settings = settings
        self.name = name
        self._inflight: list[Entry] | None = None

    async def ensure_groups(self) -> None:
        for stream, group in ((keys.POSTS_STREAM, keys.POSTS_GROUP), (keys.USER_EVENTS, keys.USER_EVENTS_GROUP)):
            try:
                await self.redis.xgroup_create(stream, group, id="0", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    async def process_posts_once(self, block_ms: int | None = None) -> int:
        """새 메시지를 최대 batch_size 개 읽어 저장한다. DB 연결 오류 시 배치를 보관하고 TransientDbError."""
        if self._inflight is None:
            resp = await self.redis.xreadgroup(
                keys.POSTS_GROUP,
                self.name,
                {keys.POSTS_STREAM: ">"},
                count=self.settings.worker_batch_size,
                block=block_ms,
            )
            self._inflight = resp[0][1] if resp else []
        entries = self._inflight
        if entries:
            await self._persist_posts(entries)
        self._inflight = None
        return len(entries)

    async def reclaim_posts_once(self) -> int:
        """죽은 워커가 가져가서 오래 ACK 하지 않은 메시지를 넘겨받는다."""
        result = await self.redis.xautoclaim(
            keys.POSTS_STREAM,
            keys.POSTS_GROUP,
            self.name,
            min_idle_time=self.settings.claim_idle_ms,
            start_id="0-0",
            count=self.settings.worker_batch_size,
        )
        claimed: list[Entry] = [(mid, fields) for mid, fields in result[1] if fields]
        if not claimed:
            return 0
        retry: list[Entry] = []
        for mid, fields in claimed:
            info = await self.redis.xpending_range(keys.POSTS_STREAM, keys.POSTS_GROUP, min=mid, max=mid, count=1)
            deliveries = info[0]["times_delivered"] if info else 0
            if deliveries > self.settings.max_deliveries:
                await self.dead_letter(mid, fields, f"delivered {deliveries} times")
            else:
                retry.append((mid, fields))
        if retry:
            await self._persist_posts(retry)
        return len(claimed)

    async def _persist_posts(self, entries: list[Entry]) -> None:
        decoded: list[tuple[str, dict[str, str], dict]] = []
        poison: list[tuple[str, dict[str, str], str]] = []
        for mid, fields in entries:
            try:
                decoded.append((mid, fields, decode_post(fields)))
            except (KeyError, ValueError) as exc:
                poison.append((mid, fields, f"decode: {exc!r}"))

        stored: list[tuple[str, dict[str, str], dict]] = []
        if decoded:
            try:
                await self._insert([row for _, _, row in decoded])
                stored = decoded
            except TransientDbError:
                raise
            except Exception as exc:
                # 배치 안의 어떤 행이 DB 에서 거부됐다. 한 행씩 다시 넣어 범인만 골라낸다.
                log.warning("batch insert failed, retrying row by row: %r", exc)
                for item in decoded:
                    try:
                        await self._insert([item[2]])
                        stored.append(item)
                    except TransientDbError:
                        raise
                    except Exception as row_exc:
                        poison.append((item[0], item[1], f"insert: {row_exc!r}"))

        if stored:
            await self._ack_posts([(mid, str(row["id"])) for mid, _, row in stored])
        for mid, fields, reason in poison:
            await self.dead_letter(mid, fields, reason)

    async def _insert(self, rows: list[dict]) -> None:
        try:
            async with self.engine.begin() as conn:
                await posts_repo.insert_many(conn, rows)
        except Exception as exc:
            if is_transient(exc):
                raise TransientDbError(repr(exc)) from exc
            raise

    async def _ack_posts(self, items: list[tuple[str, str]]) -> None:
        message_ids = [mid for mid, _ in items]
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xack(keys.POSTS_STREAM, keys.POSTS_GROUP, *message_ids)
            pipe.xdel(keys.POSTS_STREAM, *message_ids)
            pipe.delete(*(keys.pending(post_id) for _, post_id in items))
            pipe.delete(keys.FIRST_PAGE)
            await pipe.execute()

    async def dead_letter(self, message_id: str, fields: dict[str, str], reason: str) -> None:
        log.error(
            "moving message to DLQ",
            extra={"extra_fields": {"message_id": message_id, "reason": reason}},
        )
        post_id = fields.get("id")
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xadd(keys.POSTS_DLQ, {**fields, "source_id": message_id, "reason": reason[:500]})
            if post_id:
                pipe.set(keys.failed(post_id), "1", ex=self.settings.status_ttl_seconds)
                pipe.delete(keys.pending(post_id))
            pipe.xack(keys.POSTS_STREAM, keys.POSTS_GROUP, message_id)
            pipe.xdel(keys.POSTS_STREAM, message_id)
            await pipe.execute()
