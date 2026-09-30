# simple-web-app 백엔드 구현 계획 (계획 1/3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 로그인 서버(auth-svc), 게시판 API(board-api), 글쓰기 워커(board-worker), DB 마이그레이션, 로컬 docker-compose 스택을 만든다. 모든 기능은 실제 Postgres/Redis를 띄운 pytest로 검증한다.

**Architecture:** FastAPI 서비스 두 개(auth, board)와 board 이미지를 공유하는 워커 하나로 구성한다. 세션은 Redis에, 데이터는 Postgres의 `auth` / `board` 스키마에 둔다. 글쓰기는 Redis Streams 큐를 거쳐 워커가 배치로 INSERT하고, 읽기는 Redis 캐시(스탬피드 방지와 stale 대체 포함)로 흡수한다. 모든 서비스는 앱 팩토리 `create_app(settings)` 패턴을 써서, 테스트에서 설정(장애 URL 포함)을 바꿔 가며 띄울 수 있게 한다.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, SQLAlchemy 2 async + asyncpg, redis-py 5 (`redis.asyncio`), argon2-cffi, pydantic-settings, prometheus-client, uuid6(UUIDv7), pytest + pytest-asyncio + httpx + asgi-lifespan + testcontainers, uv, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-30-simple-web-app-design.md`

**연관 계획:** 계획 2 `2026-09-30-simple-web-app-2-frontend-nginx.md`(프론트, nginx), 계획 3 `2026-09-30-simple-web-app-3-k8s-loadtest.md`(k8s, 부하 테스트). 이 계획이 먼저 끝나야 한다.

## Global Constraints

- 사전 준비: Docker 실행 중, `uv` 설치(`brew install uv`), Python 3.12(`uv`가 자동으로 찾는다). 모든 pytest는 testcontainers로 `postgres:16-alpine`, `redis:7-alpine`을 띄운다.
- 저장소 루트는 `simple-web-app/`이다. 아래 모든 경로는 저장소 루트 기준이다.
- 패키지: `services/auth/src/auth`(패키지명 `auth`), `services/board/src/board`(패키지명 `board`). 공통 패키지는 두지 않는다. 비슷한 모듈(errors, infra, observability)도 서비스마다 따로 둔다.
- 컨테이너 포트는 8000이고, Pod(컨테이너)당 uvicorn 프로세스는 1개다. 워커 메트릭 포트는 9100이다.
- 환경변수: `DATABASE_URL`(`postgresql+asyncpg://...`), `REDIS_URL`, `SESSION_TTL_SECONDS`(86400), `QUEUE_MAX_LEN`(50000), `COOKIE_SECURE`(true, 로컬 false), `LOG_LEVEL`(INFO).
- 헤더: `X-User-Id`, `X-User-Nickname`(UTF-8 percent-encoding), `X-Auth-Degraded: 1`, `X-Request-ID`, `Idempotency-Key`.
- 에러 응답은 항상 `{"code": "...", "message": "..."}` 형식이다. 사용 코드: `UNAUTHORIZED`, `FORBIDDEN`, `NOT_FOUND`, `EMAIL_TAKEN`, `NICKNAME_TAKEN`, `INVALID_CREDENTIALS`, `TOO_MANY_ATTEMPTS`, `VALIDATION_ERROR`(422), `QUEUE_FULL`, `UNAVAILABLE`, `IDEMPOTENCY_KEY_REQUIRED`(400). 503과 429에는 `Retry-After`를 붙인다.
- 입력 제한: email 형식, password 8~128자, nickname 2~20자, title 1~100자, body 1~5000자, 목록 limit 1~50(기본 20).
- Redis 키와 TTL: `session:<sid>` 24h, `user_sessions:<user_id>` 24h, `login_fail:<email>` 15m, `idem:<user_id>:<key>` 10m, `pending:<id>` 1h, `failed:<id>` 1h, `cache:posts:first` 3s, `stale:posts:first` 60s, `cache:post:<id>` 30s, `lock:posts:first` 2s. 스트림: `posts:stream`(group `writers`), `posts:dlq`, `user:events`(group `user-events`, MAXLEN ~10000).
- 워커는 처리한 메시지를 **XACK와 함께 XDEL한다.** 따라서 `XLEN posts:stream`은 곧 처리 대기량(backlog)이고, 백프레셔와 KEDA가 이 값을 기준으로 동작한다.
- DB 커넥션: Pod당 `pool_size=5`, `max_overflow=5`.
- 로그인 계정 리밋: 15분 동안 실패가 10회에 도달하면 이후 요청은 429.
- 탈퇴한 사용자의 글은 `author_id = NULL`, `author_nickname = '탈퇴한 사용자'`로 바꾼다.
- 커밋 메시지 끝에는 `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`을 붙인다(아래 커밋 명령에서는 생략하고 표기).

---

## 파일 구조

```
.gitignore
.dockerignore
Makefile
docker-compose.yml
scripts/smoke_backend.py
db/migrations/001_init.sql
services/auth/
  pyproject.toml
  Dockerfile
  src/auth/
    __init__.py
    config.py          Settings (환경변수)
    errors.py          ApiError, 공통 에러 핸들러 (Redis/DB 장애 → 503)
    infra.py           engine/redis 생성, 헬스 체크 함수
    observability.py   JSON 로그, X-Request-ID, HTTP 메트릭, /metrics
    health.py          /healthz, /readyz
    passwords.py       argon2 해시 (스레드풀)
    sessions.py        SessionStore (session:*, user_sessions:*)
    login_limiter.py   계정 기준 로그인 실패 카운터
    events.py          user:events 발행
    users.py           auth.users 저장소 함수
    routes.py          /api/auth/*, /internal/verify
    app.py             create_app(settings)
    main.py            app = create_app()
  tests/
    helpers.py  conftest.py
    test_health.py  test_accounts.py  test_login_limit_and_verify.py  test_delete_account.py
services/board/
  pyproject.toml
  Dockerfile
  src/board/
    __init__.py
    config.py  errors.py  infra.py  observability.py  health.py
    migrate.py         SQL 마이그레이션 러너 (k8s Job, compose migrate 서비스)
    keys.py            Redis 키/스트림 이름
    identity.py        X-User-* 헤더 → CurrentUser
    queue.py           enqueue_post (멱등, 백프레셔)
    posts_repo.py      board.posts 저장소 함수
    metrics.py         cache_hit_ratio, queue_length, queue_lag_seconds, dlq_size
    cache.py           목록/상세 캐시, 스탬피드 방지, stale 대체
    routes.py          /api/board/*
    app.py  main.py
    worker.py          board-worker 본체
    worker_health.py   heartbeat 기반 liveness 체크
  tests/
    helpers.py  conftest.py
    test_migrate.py  test_health.py  test_create_post.py  test_read_and_delete.py
    test_worker_posts.py  test_worker_events_and_loop.py
```

---

### Task 1: 저장소 골격, DB 스키마, 마이그레이션 러너

**Files:**
- Create: `.gitignore`, `Makefile`, `db/migrations/001_init.sql`
- Create: `services/board/pyproject.toml`, `services/board/src/board/__init__.py`, `services/board/src/board/migrate.py`
- Test: `services/board/tests/helpers.py`, `services/board/tests/conftest.py`, `services/board/tests/test_migrate.py`

**Interfaces:**
- Consumes: 없음
- Produces:
  - `board.migrate.migrate(database_url: str, migrations_dir: str | Path) -> list[str]`: 새로 적용한 파일 이름 목록을 반환한다. `postgresql+asyncpg://`와 `postgresql://` 둘 다 받는다.
  - `python -m board.migrate`: 환경변수 `DATABASE_URL`, `MIGRATIONS_DIR`(기본 `/app/migrations`)를 읽는다.
  - 테스트 fixture `pg_url`(session, `postgresql+asyncpg://...`, 마이그레이션 적용 완료), `redis_url`(session)
  - `tests/helpers.py`: `MIGRATIONS_DIR`, `BROKEN_DATABASE_URL`, `BROKEN_REDIS_URL`, `to_dsn(url)`

- [ ] **Step 1: 저장소 기본 파일 작성**

`.gitignore`:
```gitignore
.venv/
__pycache__/
*.pyc
.pytest_cache/
.coverage
node_modules/
dist/
.env
.DS_Store
uv.lock
```

`db/migrations/001_init.sql`:
```sql
CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE auth.users (
  id            UUID PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE,
  nickname      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at    TIMESTAMPTZ
);

CREATE SCHEMA IF NOT EXISTS board;

CREATE TABLE board.posts (
  id              UUID PRIMARY KEY,
  author_id       UUID,
  author_nickname TEXT NOT NULL,
  title           TEXT NOT NULL,
  body            TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL,
  deleted_at      TIMESTAMPTZ
);

CREATE INDEX posts_author_idx ON board.posts (author_id);
```

`services/board/pyproject.toml`:
```toml
[project]
name = "board"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "sqlalchemy[asyncio]>=2.0.30",
  "asyncpg>=0.29",
  "redis>=5.0.4",
  "pydantic-settings>=2.3",
  "prometheus-client>=0.20",
  "uuid6>=2024.7.10",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.2",
  "pytest-asyncio>=0.24",
  "httpx>=0.27",
  "asgi-lifespan>=2.1",
  "testcontainers[postgres,redis]>=4.7",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/board"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
```

`services/board/src/board/__init__.py`:
```python
```
(빈 파일)

`Makefile`:
```make
.PHONY: test test-backend

test-backend:
	cd services/board && uv run --extra dev pytest -q

test: test-backend
```

- [ ] **Step 2: 테스트 헬퍼, conftest, 실패하는 테스트 작성**

`services/board/tests/helpers.py`:
```python
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

# 아무것도 떠 있지 않은 포트. 장애 상황을 흉내 낼 때 쓴다.
BROKEN_DATABASE_URL = "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/nowhere"
BROKEN_REDIS_URL = "redis://127.0.0.1:1/0"


def to_dsn(url: str) -> str:
    """SQLAlchemy URL을 asyncpg가 받는 DSN으로 바꾼다."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)
```

`services/board/tests/conftest.py`:
```python
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
```

`services/board/tests/test_migrate.py`:
```python
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
```

- [ ] **Step 3: 테스트가 실패하는지 확인**

Run: `cd services/board && uv run --extra dev pytest tests/test_migrate.py -v`
Expected: FAIL, conftest import 단계에서 `ModuleNotFoundError: No module named 'board.migrate'`

- [ ] **Step 4: 마이그레이션 러너 구현**

`services/board/src/board/migrate.py`:
```python
"""db/migrations/*.sql 을 이름순으로 한 번씩 적용한다.

여러 Pod/Job이 동시에 실행해도 advisory lock 으로 직렬화된다.
"""
import asyncio
import logging
import os
from pathlib import Path

import asyncpg

log = logging.getLogger("board.migrate")
ADVISORY_LOCK_ID = 7272001


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def migrate(database_url: str, migrations_dir: str | Path) -> list[str]:
    conn = await asyncpg.connect(_dsn(database_url))
    try:
        applied_now: list[str] = []
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK_ID)
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
                " version TEXT PRIMARY KEY,"
                " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {r["version"] for r in await conn.fetch("SELECT version FROM public.schema_migrations")}
            for path in sorted(Path(migrations_dir).glob("*.sql")):
                if path.name in done:
                    continue
                # 인자 없는 execute 는 simple query protocol 이라 여러 문장을 한 번에 실행할 수 있다.
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO public.schema_migrations (version) VALUES ($1)", path.name)
                applied_now.append(path.name)
        return applied_now
    finally:
        await conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    applied = asyncio.run(
        migrate(os.environ["DATABASE_URL"], os.environ.get("MIGRATIONS_DIR", "/app/migrations"))
    )
    log.info("migrations applied: %s", applied or "none")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `cd services/board && uv run --extra dev pytest tests/test_migrate.py -v`
Expected: 2 passed

- [ ] **Step 6: 커밋**

```bash
git add .gitignore Makefile db services/board
git commit -m "feat: add db schema and migration runner"
```

---

### Task 2: auth-svc 기반 (설정, 에러, 인프라, 관측, 헬스 체크)

**Files:**
- Create: `services/auth/pyproject.toml`, `services/auth/src/auth/{__init__,config,errors,infra,observability,health,app,main}.py`
- Test: `services/auth/tests/{helpers,conftest,test_health}.py`
- Modify: `Makefile`

**Interfaces:**
- Consumes: `db/migrations/*.sql` (Task 1)
- Produces:
  - `auth.config.Settings`(`database_url`, `redis_url`, `session_ttl_seconds=86400`, `cookie_secure=True`, `log_level="INFO"`, `login_fail_limit=10`, `login_fail_window_seconds=900`)
  - `auth.errors.ApiError(status, code, message, headers=None)`, `auth.errors.unavailable(message=...) -> ApiError`, `install_error_handlers(app)`
  - `auth.infra.make_engine(url)`, `make_redis(url)`, `check_db(engine) -> bool`, `check_redis(redis) -> bool`
  - `auth.observability.configure_logging(level)`, `RequestContextMiddleware`, `install_metrics_route(app)`, `request_id_var`
  - `auth.app.create_app(settings: Settings | None = None) -> FastAPI`. `app.state`에 `settings`, `engine`, `redis`를 둔다.
  - 테스트 fixture: `settings`(깨끗한 상태 보장), `make_client(settings)`(async context manager), `client`, `rdb`

- [ ] **Step 1: 패키지 설정과 테스트 인프라 작성**

`services/auth/pyproject.toml`:
```toml
[project]
name = "auth"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "sqlalchemy[asyncio]>=2.0.30",
  "asyncpg>=0.29",
  "redis>=5.0.4",
  "pydantic-settings>=2.3",
  "email-validator>=2.1",
  "argon2-cffi>=23.1",
  "prometheus-client>=0.20",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.2",
  "pytest-asyncio>=0.24",
  "httpx>=0.27",
  "asgi-lifespan>=2.1",
  "testcontainers[postgres,redis]>=4.7",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/auth"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
```

`services/auth/src/auth/__init__.py`: 빈 파일

`services/auth/tests/helpers.py`:
```python
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

BROKEN_DATABASE_URL = "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/nowhere"
BROKEN_REDIS_URL = "redis://127.0.0.1:1/0"

PASSWORD = "correct-horse-1"


def to_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)
```

`services/auth/tests/conftest.py`:
```python
import asyncio
from contextlib import asynccontextmanager

import asyncpg
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from auth.app import create_app
from auth.config import Settings
from helpers import MIGRATIONS_DIR, to_dsn


async def _apply_migrations(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            await conn.execute(path.read_text())
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def pg_url():
    with PostgresContainer("postgres:16-alpine", driver=None) as pg:
        url = pg.get_connection_url()
        asyncio.run(_apply_migrations(url))
        yield url.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(scope="session")
def redis_url():
    with RedisContainer("redis:7-alpine") as rc:
        yield f"redis://{rc.get_container_host_ip()}:{rc.get_exposed_port(6379)}/0"


@pytest.fixture
async def clean_state(pg_url, redis_url):
    conn = await asyncpg.connect(to_dsn(pg_url))
    try:
        await conn.execute("TRUNCATE auth.users")
    finally:
        await conn.close()
    r = Redis.from_url(redis_url)
    try:
        await r.flushall()
    finally:
        await r.aclose()


@pytest.fixture
def settings(pg_url, redis_url, clean_state) -> Settings:
    return Settings(database_url=pg_url, redis_url=redis_url, cookie_secure=False)


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
```

- [ ] **Step 2: 실패하는 테스트 작성**

`services/auth/tests/test_health.py`:
```python
from helpers import BROKEN_DATABASE_URL, BROKEN_REDIS_URL


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_readyz_ok(client):
    r = await client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"redis": True, "db": True}


async def test_readyz_503_when_redis_down(make_client, settings):
    async with make_client(settings.model_copy(update={"redis_url": BROKEN_REDIS_URL})) as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"redis": False, "db": True}


async def test_readyz_503_when_db_down(make_client, settings):
    async with make_client(settings.model_copy(update={"database_url": BROKEN_DATABASE_URL})) as c:
        r = await c.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"redis": True, "db": False}


async def test_unknown_route_uses_error_format(client):
    r = await client.get("/nope")
    assert r.status_code == 404
    assert r.json()["code"] == "NOT_FOUND"


async def test_request_id_is_echoed(client):
    r = await client.get("/healthz", headers={"X-Request-ID": "req-123"})
    assert r.headers["X-Request-ID"] == "req-123"


async def test_request_id_is_generated_when_missing(client):
    r = await client.get("/healthz")
    assert len(r.headers["X-Request-ID"]) == 32


async def test_metrics_exposes_http_counters(client):
    await client.get("/healthz")
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert 'http_requests_total{method="GET",route="/healthz",status="200"}' in r.text
```

- [ ] **Step 3: 테스트가 실패하는지 확인**

Run: `cd services/auth && uv run --extra dev pytest tests/test_health.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'auth.app'`

- [ ] **Step 4: 구현**

`services/auth/src/auth/config.py`:
```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    session_ttl_seconds: int = 86400
    cookie_secure: bool = True
    log_level: str = "INFO"
    login_fail_limit: int = 10
    login_fail_window_seconds: int = 900
```

`services/auth/src/auth/errors.py`:
```python
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)
RETRY_AFTER_SECONDS = "5"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def unavailable(message: str = "일시적으로 서비스를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.") -> ApiError:
    return ApiError(503, "UNAVAILABLE", message, {"Retry-After": RETRY_AFTER_SECONDS})


def error_response(status: int, code: str, message: str, headers: dict[str, str] | None = None, **extra) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, **extra}, status_code=status, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return error_response(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        fields = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return error_response(422, "VALIDATION_ERROR", "입력값이 올바르지 않습니다.", fields=fields)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return error_response(exc.status_code, code, str(exc.detail))

    async def _dependency_down(request: Request, exc: Exception):
        log.warning("dependency unavailable: %r", exc)
        err = unavailable()
        return error_response(err.status, err.code, err.message, err.headers)

    # Redis 장애, DB 장애(드라이버 에러 / 연결 거부 같은 OSError)는 모두 503 으로 바꾼다.
    for exc_type in (RedisError, DBAPIError, OSError):
        app.add_exception_handler(exc_type, _dependency_down)
```

`services/auth/src/auth/infra.py`:
```python
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
```

`services/auth/src/auth/observability.py`:
```python
import contextvars
import json
import logging
import sys
import time
import uuid

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

HTTP_REQUESTS = Counter("http_requests_total", "HTTP 요청 수", ["method", "route", "status"])
HTTP_LATENCY = Histogram("http_request_duration_seconds", "HTTP 요청 처리 시간", ["method", "route"])


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", "unmatched")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            HTTP_REQUESTS.labels(request.method, _route_template(request), "500").inc()
            raise
        finally:
            request_id_var.reset(token)
        route = _route_template(request)
        HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        HTTP_LATENCY.labels(request.method, route).observe(time.perf_counter() - start)
        response.headers["X-Request-ID"] = request_id
        return response


def install_metrics_route(app: FastAPI) -> None:
    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

`services/auth/src/auth/health.py`:
```python
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from auth.infra import check_db, check_redis

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    # 외부 의존성은 보지 않는다. DB 장애가 Pod 재시작으로 번지지 않게 하기 위함이다.
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    redis_ok, db_ok = await asyncio.gather(
        check_redis(request.app.state.redis), check_db(request.app.state.engine)
    )
    status = 200 if (redis_ok and db_ok) else 503
    return JSONResponse({"redis": redis_ok, "db": db_ok}, status_code=status)
```

`services/auth/src/auth/app.py`:
```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from auth import health
from auth.config import Settings
from auth.errors import install_error_handlers
from auth.infra import make_engine, make_redis
from auth.observability import RequestContextMiddleware, configure_logging, install_metrics_route


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = make_engine(settings.database_url)
        app.state.redis = make_redis(settings.redis_url)
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await app.state.engine.dispose()

    app = FastAPI(title="auth-svc", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    install_metrics_route(app)
    app.include_router(health.router)
    return app
```

`services/auth/src/auth/main.py`:
```python
from auth.app import create_app

app = create_app()
```

`Makefile`의 `test-backend` 타깃을 아래로 교체:
```make
test-backend:
	cd services/auth && uv run --extra dev pytest -q
	cd services/board && uv run --extra dev pytest -q
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `cd services/auth && uv run --extra dev pytest tests/test_health.py -v`
Expected: 8 passed

- [ ] **Step 6: 커밋**

```bash
git add services/auth Makefile
git commit -m "feat(auth): add service skeleton with health, errors, metrics"
```

---

### Task 3: 회원가입, 로그인, 로그아웃, 내 정보

**Files:**
- Create: `services/auth/src/auth/{passwords,sessions,users,routes}.py`
- Modify: `services/auth/src/auth/app.py` (router 등록), `services/auth/tests/helpers.py` (헬퍼 추가)
- Test: `services/auth/tests/test_accounts.py`

**Interfaces:**
- Consumes: Task 2의 `ApiError`, `create_app`, fixture
- Produces:
  - `auth.passwords.hash_password(password) -> str`(async), `verify_password(password_hash, password) -> bool`(async), `DUMMY_HASH`
  - `auth.sessions.SessionStore(redis, ttl_seconds)`: `create(*, user_id, email, nickname) -> str`, `get(sid) -> dict | None`(키: `user_id`, `email`, `nickname`), `delete(sid, user_id)`, `delete_all(user_id) -> int`
  - `auth.users.create_user(engine, *, email, nickname, password_hash) -> dict`, `get_by_email(engine, email) -> dict | None`(삭제된 사용자 제외), `get_by_id(engine, user_id) -> dict | None`(삭제된 사용자 포함, `deleted_at` 필드 포함). dict 키: `id`(str), `email`, `nickname`, `password_hash`
  - `auth.routes.router`, `COOKIE_NAME = "sid"`, `_store(request)`, `_current_session(request) -> dict`(`sid` 포함), `_set_cookie(response, sid, settings)`, `_clear_cookie(response, settings)`
  - 테스트 헬퍼 `signup(client, email=..., nickname=..., password=PASSWORD) -> dict`, `login(client, email=..., password=PASSWORD) -> httpx.Response`

- [ ] **Step 1: 테스트 헬퍼 추가**

`services/auth/tests/helpers.py` 끝에 추가:
```python
async def signup(client, email: str = "alice@example.com", nickname: str = "alice", password: str = PASSWORD) -> dict:
    r = await client.post("/api/auth/signup", json={"email": email, "nickname": nickname, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


async def login(client, email: str = "alice@example.com", password: str = PASSWORD):
    return await client.post("/api/auth/login", json={"email": email, "password": password})
```

- [ ] **Step 2: 실패하는 테스트 작성**

`services/auth/tests/test_accounts.py`:
```python
from helpers import PASSWORD, login, signup


async def test_signup_returns_user(client):
    user = await signup(client)
    assert user["email"] == "alice@example.com"
    assert user["nickname"] == "alice"
    assert len(user["id"]) == 36


async def test_signup_normalizes_email_case(client):
    user = await signup(client, email="Alice@Example.COM")
    assert user["email"] == "alice@example.com"


async def test_duplicate_email_is_409(client):
    await signup(client)
    r = await client.post("/api/auth/signup", json={"email": "alice@example.com", "nickname": "other", "password": PASSWORD})
    assert r.status_code == 409
    assert r.json()["code"] == "EMAIL_TAKEN"


async def test_duplicate_nickname_is_409(client):
    await signup(client)
    r = await client.post("/api/auth/signup", json={"email": "bob@example.com", "nickname": "alice", "password": PASSWORD})
    assert r.status_code == 409
    assert r.json()["code"] == "NICKNAME_TAKEN"


async def test_short_password_is_422(client):
    r = await client.post("/api/auth/signup", json={"email": "a@example.com", "nickname": "al", "password": "short"})
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"


async def test_login_sets_httponly_cookie_and_session(client, rdb):
    user = await signup(client)
    r = await login(client)
    assert r.status_code == 200
    assert r.json() == user
    set_cookie = r.headers["set-cookie"]
    assert set_cookie.startswith("sid=")
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    sid = client.cookies.get("sid")
    assert 86000 < await rdb.ttl(f"session:{sid}") <= 86400
    assert await rdb.sismember(f"user_sessions:{user['id']}", sid)


async def test_me_returns_current_user(client):
    user = await signup(client)
    await login(client)
    r = await client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json() == user


async def test_me_without_session_is_401(client):
    r = await client.get("/api/auth/me")
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"


async def test_wrong_password_is_401(client):
    await signup(client)
    r = await login(client, password="wrong-password")
    assert r.status_code == 401
    assert r.json()["code"] == "INVALID_CREDENTIALS"


async def test_unknown_email_is_401(client):
    r = await login(client, email="ghost@example.com")
    assert r.status_code == 401
    assert r.json()["code"] == "INVALID_CREDENTIALS"


async def test_logout_removes_session(client, rdb):
    user = await signup(client)
    await login(client)
    sid = client.cookies.get("sid")
    r = await client.post("/api/auth/logout")
    assert r.status_code == 204
    assert await rdb.exists(f"session:{sid}") == 0
    assert not await rdb.sismember(f"user_sessions:{user['id']}", sid)
    assert (await client.get("/api/auth/me")).status_code == 401
```

- [ ] **Step 3: 테스트가 실패하는지 확인**

Run: `cd services/auth && uv run --extra dev pytest tests/test_accounts.py -v`
Expected: FAIL, signup 요청이 404 (`assert 404 == 201`)

- [ ] **Step 4: 구현**

`services/auth/src/auth/passwords.py`:
```python
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from starlette.concurrency import run_in_threadpool

_hasher = PasswordHasher()

# 존재하지 않는 이메일로 로그인할 때도 같은 시간만큼 해시 검증을 해서,
# 응답 시간으로 가입 여부를 추측하지 못하게 한다.
DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


async def hash_password(password: str) -> str:
    # argon2 는 CPU 를 많이 쓰므로 이벤트 루프를 막지 않게 스레드풀에서 실행한다.
    return await run_in_threadpool(_hasher.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    def _verify() -> bool:
        try:
            return _hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    return await run_in_threadpool(_verify)
```

`services/auth/src/auth/sessions.py`:
```python
import json
import secrets

from redis.asyncio import Redis


def _session_key(sid: str) -> str:
    return f"session:{sid}"


def _user_sessions_key(user_id: str) -> str:
    return f"user_sessions:{user_id}"


class SessionStore:
    def __init__(self, redis: Redis, ttl_seconds: int):
        self.redis = redis
        self.ttl = ttl_seconds

    async def create(self, *, user_id: str, email: str, nickname: str) -> str:
        sid = secrets.token_urlsafe(32)
        value = json.dumps({"user_id": user_id, "email": email, "nickname": nickname}, ensure_ascii=False)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set(_session_key(sid), value, ex=self.ttl)
            pipe.sadd(_user_sessions_key(user_id), sid)
            pipe.expire(_user_sessions_key(user_id), self.ttl)
            await pipe.execute()
        return sid

    async def get(self, sid: str) -> dict | None:
        raw = await self.redis.get(_session_key(sid))
        return json.loads(raw) if raw else None

    async def delete(self, sid: str, user_id: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(_session_key(sid))
            pipe.srem(_user_sessions_key(user_id), sid)
            await pipe.execute()

    async def delete_all(self, user_id: str) -> int:
        sids = await self.redis.smembers(_user_sessions_key(user_id))
        async with self.redis.pipeline(transaction=True) as pipe:
            for sid in sids:
                pipe.delete(_session_key(sid))
            pipe.delete(_user_sessions_key(user_id))
            await pipe.execute()
        return len(sids)
```

`services/auth/src/auth/users.py`:
```python
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from auth.errors import ApiError


def _user(row) -> dict:
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "nickname": row["nickname"],
        "password_hash": row["password_hash"],
        "deleted_at": row["deleted_at"],
    }


async def create_user(engine: AsyncEngine, *, email: str, nickname: str, password_hash: str) -> dict:
    user_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO auth.users (id, email, nickname, password_hash) "
                    "VALUES (:id, :email, :nickname, :password_hash)"
                ),
                {"id": user_id, "email": email, "nickname": nickname, "password_hash": password_hash},
            )
    except IntegrityError as exc:
        detail = str(exc.orig)
        if "users_email_key" in detail:
            raise ApiError(409, "EMAIL_TAKEN", "이미 가입된 이메일입니다.") from exc
        if "users_nickname_key" in detail:
            raise ApiError(409, "NICKNAME_TAKEN", "이미 사용 중인 닉네임입니다.") from exc
        raise
    return {"id": str(user_id), "email": email, "nickname": nickname}


async def get_by_email(engine: AsyncEngine, email: str) -> dict | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT id, email, nickname, password_hash, deleted_at FROM auth.users "
                    "WHERE email = :email AND deleted_at IS NULL"
                ),
                {"email": email},
            )
        ).mappings().first()
    return _user(row) if row else None


async def get_by_id(engine: AsyncEngine, user_id: str) -> dict | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT id, email, nickname, password_hash, deleted_at FROM auth.users WHERE id = :id"),
                {"id": uuid.UUID(user_id)},
            )
        ).mappings().first()
    return _user(row) if row else None
```

`services/auth/src/auth/routes.py`:
```python
import logging

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, EmailStr, Field

from auth import users
from auth.config import Settings
from auth.errors import ApiError
from auth.passwords import DUMMY_HASH, hash_password, verify_password
from auth.sessions import SessionStore

log = logging.getLogger(__name__)
router = APIRouter()
COOKIE_NAME = "sid"


class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    nickname: str = Field(min_length=2, max_length=20)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    id: str
    email: str
    nickname: str


def _store(request: Request) -> SessionStore:
    return SessionStore(request.app.state.redis, request.app.state.settings.session_ttl_seconds)


def _set_cookie(response: Response, sid: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        sid,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=settings.cookie_secure, samesite="lax")


async def _current_session(request: Request) -> dict:
    sid = request.cookies.get(COOKIE_NAME)
    session = await _store(request).get(sid) if sid else None
    if session is None:
        raise ApiError(401, "UNAUTHORIZED", "로그인이 필요합니다.")
    return {**session, "sid": sid}


@router.post("/api/auth/signup", status_code=201)
async def signup(body: SignupIn, request: Request) -> UserOut:
    password_hash = await hash_password(body.password)
    user = await users.create_user(
        request.app.state.engine,
        email=body.email.lower(),
        nickname=body.nickname,
        password_hash=password_hash,
    )
    return UserOut(**user)


@router.post("/api/auth/login")
async def login(body: LoginIn, request: Request, response: Response) -> UserOut:
    settings: Settings = request.app.state.settings
    email = body.email.lower()
    user = await users.get_by_email(request.app.state.engine, email)
    ok = await verify_password(user["password_hash"] if user else DUMMY_HASH, body.password)
    if user is None or not ok:
        raise ApiError(401, "INVALID_CREDENTIALS", "이메일 또는 비밀번호가 올바르지 않습니다.")
    sid = await _store(request).create(user_id=user["id"], email=user["email"], nickname=user["nickname"])
    _set_cookie(response, sid, settings)
    return UserOut(id=user["id"], email=user["email"], nickname=user["nickname"])


@router.post("/api/auth/logout", status_code=204)
async def logout(request: Request) -> Response:
    response = Response(status_code=204)
    sid = request.cookies.get(COOKIE_NAME)
    if sid:
        store = _store(request)
        session = await store.get(sid)
        if session:
            await store.delete(sid, session["user_id"])
    _clear_cookie(response, request.app.state.settings)
    return response


@router.get("/api/auth/me")
async def me(request: Request) -> UserOut:
    session = await _current_session(request)
    return UserOut(id=session["user_id"], email=session["email"], nickname=session["nickname"])
```

`services/auth/src/auth/app.py`: import 줄을 `from auth import health, routes`로 바꾸고, `app.include_router(health.router)` 다음 줄에 추가:
```python
    app.include_router(routes.router)
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `cd services/auth && uv run --extra dev pytest -v`
Expected: 모든 테스트 통과 (test_health 8 + test_accounts 11)

- [ ] **Step 6: 커밋**

```bash
git add services/auth
git commit -m "feat(auth): signup, login, logout, me with redis sessions"
```

---

### Task 4: 계정 기준 로그인 리밋, nginx 세션 확인(verify) 엔드포인트

**Files:**
- Create: `services/auth/src/auth/login_limiter.py`
- Modify: `services/auth/src/auth/routes.py` (login 교체, verify 추가)
- Test: `services/auth/tests/test_login_limit_and_verify.py`

**Interfaces:**
- Consumes: Task 3의 `SessionStore`, `_store`, `COOKIE_NAME`
- Produces:
  - `auth.login_limiter.LoginLimiter(redis, limit, window_seconds)`: `blocked_for(email) -> int`(0이면 허용, 아니면 남은 초), `record_failure(email)`, `reset(email)`
  - `GET /internal/verify`: 항상 200. 세션이 있으면 `X-User-Id`, `X-User-Nickname`(percent-encoding) 헤더, Redis 장애 시 `X-Auth-Degraded: 1` 헤더

- [ ] **Step 1: 실패하는 테스트 작성**

`services/auth/tests/test_login_limit_and_verify.py`:
```python
from urllib.parse import unquote

from helpers import BROKEN_REDIS_URL, login, signup


async def test_tenth_failure_blocks_following_attempts(client):
    await signup(client)
    for _ in range(10):
        assert (await login(client, password="wrong-password")).status_code == 401
    r = await login(client)  # 올바른 비밀번호여도 차단
    assert r.status_code == 429
    assert r.json()["code"] == "TOO_MANY_ATTEMPTS"
    assert 1 <= int(r.headers["Retry-After"]) <= 900


async def test_success_resets_failure_counter(client, rdb):
    await signup(client)
    for _ in range(9):
        await login(client, password="wrong-password")
    assert (await login(client)).status_code == 200
    assert await rdb.exists("login_fail:alice@example.com") == 0


async def test_limit_is_per_account(client):
    await signup(client)
    await signup(client, email="bob@example.com", nickname="bob")
    for _ in range(10):
        await login(client, password="wrong-password")
    assert (await login(client, email="bob@example.com")).status_code == 200


async def test_login_is_503_when_redis_down(make_client, settings, client):
    await signup(client)
    async with make_client(settings.model_copy(update={"redis_url": BROKEN_REDIS_URL})) as c:
        r = await login(c)
    assert r.status_code == 503
    assert r.json()["code"] == "UNAVAILABLE"
    assert r.headers["Retry-After"] == "5"


async def test_verify_without_cookie_is_anonymous(client):
    r = await client.get("/internal/verify")
    assert r.status_code == 200
    assert "X-User-Id" not in r.headers
    assert "X-Auth-Degraded" not in r.headers


async def test_verify_with_unknown_sid_is_anonymous(client):
    client.cookies.set("sid", "does-not-exist")
    r = await client.get("/internal/verify")
    assert r.status_code == 200
    assert "X-User-Id" not in r.headers


async def test_verify_returns_user_headers(client):
    user = await signup(client, nickname="한글닉네임")
    await login(client)
    r = await client.get("/internal/verify")
    assert r.status_code == 200
    assert r.headers["X-User-Id"] == user["id"]
    assert r.headers["X-User-Nickname"] != "한글닉네임"  # ASCII 로 인코딩되어 있어야 한다
    assert unquote(r.headers["X-User-Nickname"]) == "한글닉네임"


async def test_verify_reports_degraded_when_redis_down(make_client, settings):
    async with make_client(settings.model_copy(update={"redis_url": BROKEN_REDIS_URL})) as c:
        c.cookies.set("sid", "any")
        r = await c.get("/internal/verify")
    assert r.status_code == 200
    assert r.headers["X-Auth-Degraded"] == "1"
    assert "X-User-Id" not in r.headers
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `cd services/auth && uv run --extra dev pytest tests/test_login_limit_and_verify.py -v`
Expected: FAIL. `test_tenth_failure_blocks_following_attempts`가 `assert 200 == 429`로 실패하고, verify 테스트들은 404로 실패

- [ ] **Step 3: 구현**

`services/auth/src/auth/login_limiter.py`:
```python
from redis.asyncio import Redis


class LoginLimiter:
    """계정(이메일) 기준 로그인 실패 카운터. IP 기준 리밋은 nginx 가 담당한다."""

    def __init__(self, redis: Redis, limit: int, window_seconds: int):
        self.redis = redis
        self.limit = limit
        self.window = window_seconds

    @staticmethod
    def _key(email: str) -> str:
        return f"login_fail:{email}"

    async def blocked_for(self, email: str) -> int:
        count = await self.redis.get(self._key(email))
        if count is None or int(count) < self.limit:
            return 0
        ttl = await self.redis.ttl(self._key(email))
        return max(ttl, 1)

    async def record_failure(self, email: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(self._key(email))
            pipe.expire(self._key(email), self.window, nx=True)
            await pipe.execute()

    async def reset(self, email: str) -> None:
        await self.redis.delete(self._key(email))
```

`services/auth/src/auth/routes.py` 변경:

import 영역에 추가:
```python
from urllib.parse import quote

from redis.exceptions import RedisError

from auth.login_limiter import LoginLimiter
```

기존 `login` 함수 전체를 아래로 교체:
```python
@router.post("/api/auth/login")
async def login(body: LoginIn, request: Request, response: Response) -> UserOut:
    settings: Settings = request.app.state.settings
    email = body.email.lower()
    limiter = LoginLimiter(request.app.state.redis, settings.login_fail_limit, settings.login_fail_window_seconds)
    retry_after = await limiter.blocked_for(email)
    if retry_after:
        raise ApiError(
            429,
            "TOO_MANY_ATTEMPTS",
            "로그인 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요.",
            {"Retry-After": str(retry_after)},
        )
    user = await users.get_by_email(request.app.state.engine, email)
    ok = await verify_password(user["password_hash"] if user else DUMMY_HASH, body.password)
    if user is None or not ok:
        await limiter.record_failure(email)
        raise ApiError(401, "INVALID_CREDENTIALS", "이메일 또는 비밀번호가 올바르지 않습니다.")
    await limiter.reset(email)
    sid = await _store(request).create(user_id=user["id"], email=user["email"], nickname=user["nickname"])
    _set_cookie(response, sid, settings)
    return UserOut(id=user["id"], email=user["email"], nickname=user["nickname"])
```

파일 끝에 추가:
```python
@router.get("/internal/verify", include_in_schema=False)
async def verify(request: Request) -> Response:
    """nginx auth_request 전용. 항상 200 을 반환한다.

    nginx 는 auth_request 가 5xx 를 받으면 원래 요청 전체를 500 으로 끝내므로,
    Redis 장애 시에도 200 + X-Auth-Degraded 로 응답하고 판단은 board-api 에 맡긴다.
    """
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        return Response(status_code=200)
    try:
        session = await _store(request).get(sid)
    except RedisError:
        log.warning("session store unavailable during verify")
        return Response(status_code=200, headers={"X-Auth-Degraded": "1"})
    if session is None:
        return Response(status_code=200)
    return Response(
        status_code=200,
        headers={"X-User-Id": session["user_id"], "X-User-Nickname": quote(session["nickname"], safe="")},
    )
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd services/auth && uv run --extra dev pytest -v`
Expected: 모든 테스트 통과

- [ ] **Step 5: 커밋**

```bash
git add services/auth
git commit -m "feat(auth): per-account login limit and nginx verify endpoint"
```

---

### Task 5: 회원 탈퇴

**Files:**
- Create: `services/auth/src/auth/events.py`
- Modify: `services/auth/src/auth/users.py` (`soft_delete` 추가), `services/auth/src/auth/routes.py` (DELETE 추가)
- Test: `services/auth/tests/test_delete_account.py`

**Interfaces:**
- Consumes: Task 3의 `SessionStore.delete_all`, `users.get_by_id`, `_current_session`, `_clear_cookie`
- Produces:
  - `auth.events.USER_EVENTS = "user:events"`, `publish_user_deleted(redis, user_id)`: `XADD user:events MAXLEN ~ 10000 {type: "user_deleted", user_id}`
  - `auth.users.soft_delete(engine, user_id) -> bool`
  - `DELETE /api/auth/me` `{password}` → 204 / 401 / 403
  - **board-worker(Task 10)가 받는 이벤트 계약:** 스트림 `user:events`, 필드 `type="user_deleted"`, `user_id=<uuid 문자열>`

- [ ] **Step 1: 실패하는 테스트 작성**

`services/auth/tests/test_delete_account.py`:
```python
from helpers import PASSWORD, login, signup


async def _delete(client, password: str = PASSWORD):
    return await client.request("DELETE", "/api/auth/me", json={"password": password})


async def test_delete_requires_session(client):
    r = await _delete(client)
    assert r.status_code == 401


async def test_delete_with_wrong_password_is_403(client):
    await signup(client)
    await login(client)
    r = await _delete(client, password="wrong-password")
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"
    assert (await client.get("/api/auth/me")).status_code == 200


async def test_delete_logs_out_every_device_and_publishes_event(make_client, settings, client, rdb):
    user = await signup(client)
    await login(client)
    async with make_client(settings) as other_device:
        await login(other_device)
        other_sid = other_device.cookies.get("sid")

        r = await _delete(client)
        assert r.status_code == 204
        assert "sid=" in r.headers["set-cookie"]

        assert (await other_device.get("/api/auth/me")).status_code == 401
    assert await rdb.exists(f"session:{other_sid}") == 0
    assert await rdb.exists(f"user_sessions:{user['id']}") == 0

    events = await rdb.xrange("user:events")
    assert len(events) == 1
    assert events[0][1] == {"type": "user_deleted", "user_id": user["id"]}


async def test_deleted_user_cannot_login_and_email_can_be_reused(client):
    await signup(client)
    await login(client)
    assert (await _delete(client)).status_code == 204
    assert (await login(client)).status_code == 401
    again = await signup(client)
    assert again["email"] == "alice@example.com"
    assert again["nickname"] == "alice"


async def test_retry_after_partial_failure_completes(client, settings, rdb):
    """DB 삭제 후 Redis 단계가 실패했다고 가정: 같은 세션으로 다시 요청하면 나머지 단계를 마친다."""
    from auth import users
    from auth.infra import make_engine

    user = await signup(client)
    await login(client)
    engine = make_engine(settings.database_url)
    assert await users.soft_delete(engine, user["id"])  # DB 단계만 끝난 상태
    await engine.dispose()

    r = await _delete(client)
    assert r.status_code == 204
    assert len(await rdb.xrange("user:events")) == 1
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `cd services/auth && uv run --extra dev pytest tests/test_delete_account.py -v`
Expected: FAIL, DELETE가 405 (`METHOD_NOT_ALLOWED`)

- [ ] **Step 3: 구현**

`services/auth/src/auth/events.py`:
```python
from redis.asyncio import Redis

USER_EVENTS = "user:events"
USER_EVENTS_MAXLEN = 10000


async def publish_user_deleted(redis: Redis, user_id: str) -> None:
    await redis.xadd(
        USER_EVENTS,
        {"type": "user_deleted", "user_id": user_id},
        maxlen=USER_EVENTS_MAXLEN,
        approximate=True,
    )
```

`services/auth/src/auth/users.py` 끝에 추가:
```python
async def soft_delete(engine: AsyncEngine, user_id: str) -> bool:
    """탈퇴 처리. 이메일/닉네임을 익명화해 같은 이메일로 재가입할 수 있게 한다. 이미 탈퇴했으면 False."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE auth.users SET deleted_at = now(), "
                "email = 'deleted-' || id::text || '@deleted.invalid', "
                "nickname = 'deleted-' || id::text "
                "WHERE id = :id AND deleted_at IS NULL"
            ),
            {"id": uuid.UUID(user_id)},
        )
    return result.rowcount == 1
```

`services/auth/src/auth/routes.py` 변경:

import 영역에 추가:
```python
from auth.events import publish_user_deleted
```

모델 정의 영역(`UserOut` 아래)에 추가:
```python
class DeleteAccountIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)
```

`me` 함수 아래에 추가:
```python
@router.delete("/api/auth/me", status_code=204)
async def delete_account(body: DeleteAccountIn, request: Request) -> Response:
    """탈퇴. 각 단계가 멱등이라 중간에 실패해도 같은 요청을 다시 보내면 끝까지 진행된다."""
    session = await _current_session(request)
    engine = request.app.state.engine
    user = await users.get_by_id(engine, session["user_id"])
    if user is None or not await verify_password(user["password_hash"], body.password):
        raise ApiError(403, "FORBIDDEN", "비밀번호가 올바르지 않습니다.")
    await users.soft_delete(engine, user["id"])
    await _store(request).delete_all(user["id"])
    await publish_user_deleted(request.app.state.redis, user["id"])
    response = Response(status_code=204)
    _clear_cookie(response, request.app.state.settings)
    return response
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd services/auth && uv run --extra dev pytest -v`
Expected: 모든 테스트 통과

- [ ] **Step 5: 커밋**

```bash
git add services/auth
git commit -m "feat(auth): account deletion with session purge and user_deleted event"
```

---

### Task 6: board-api 기반 (설정, 에러, 인프라, 관측, 헬스 체크, 사용자 헤더)

**Files:**
- Create: `services/board/src/board/{config,errors,infra,observability,health,identity,app,main}.py`
- Modify: `services/board/tests/conftest.py` (앱 fixture 추가)
- Test: `services/board/tests/test_health.py`

**Interfaces:**
- Consumes: Task 1의 `pg_url`, `redis_url`, `helpers`
- Produces:
  - `board.config.Settings`(`database_url`, `redis_url`, `queue_max_len=50000`, `log_level="INFO"`, `page_size=20`, `list_cache_ttl_seconds=3`, `stale_cache_ttl_seconds=60`, `detail_cache_ttl_seconds=30`, `idempotency_ttl_seconds=600`, `status_ttl_seconds=3600`, `worker_batch_size=100`, `worker_block_ms=1000`, `claim_idle_ms=30000`, `max_deliveries=5`, `heartbeat_path="/tmp/worker-heartbeat"`, `worker_metrics_port=9100`)
  - `board.errors.ApiError`, `unavailable()`, `install_error_handlers(app)`, `RETRY_AFTER_SECONDS`
  - `board.infra.make_engine`, `make_redis`, `check_db`, `check_redis`
  - `board.observability.configure_logging`, `RequestContextMiddleware`, `install_metrics_route`
  - `board.identity.CurrentUser(id: str, nickname: str)`, `optional_user(request) -> CurrentUser | None`, `require_user(request) -> CurrentUser`
  - `board.app.create_app(settings=None) -> FastAPI` (`app.state.settings/engine/redis`)
  - fixture: `clean_state`, `settings`, `make_client`, `client`, `rdb`, `engine`

- [ ] **Step 1: conftest에 fixture 추가**

`services/board/tests/conftest.py`를 아래 전체 내용으로 교체:
```python
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
from board.infra import make_engine
from board.migrate import migrate
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
```

- [ ] **Step 2: 실패하는 테스트 작성**

`services/board/tests/test_health.py`:
```python
import uuid
from urllib.parse import quote

import pytest
from starlette.requests import Request

from board.errors import ApiError
from board.identity import optional_user, require_user
from helpers import BROKEN_DATABASE_URL, BROKEN_REDIS_URL


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw})


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200


async def test_readyz_ok(client):
    r = await client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"redis": True, "db": True}


@pytest.mark.parametrize(
    "update, expected",
    [
        ({"redis_url": BROKEN_REDIS_URL}, {"redis": False, "db": True}),
        ({"database_url": BROKEN_DATABASE_URL}, {"redis": True, "db": False}),
    ],
)
async def test_readyz_stays_ready_with_one_dependency(make_client, settings, update, expected):
    async with make_client(settings.model_copy(update=update)) as c:
        r = await c.get("/readyz")
    assert r.status_code == 200
    assert r.json() == expected


async def test_readyz_503_when_both_down(make_client, settings):
    broken = settings.model_copy(update={"redis_url": BROKEN_REDIS_URL, "database_url": BROKEN_DATABASE_URL})
    async with make_client(broken) as c:
        r = await c.get("/readyz")
    assert r.status_code == 503


async def test_metrics_and_request_id(client):
    r = await client.get("/healthz", headers={"X-Request-ID": "abc"})
    assert r.headers["X-Request-ID"] == "abc"
    assert 'route="/healthz"' in (await client.get("/metrics")).text


def test_optional_user_decodes_headers():
    uid = str(uuid.uuid4())
    user = optional_user(_request({"X-User-Id": uid, "X-User-Nickname": quote("재난알림", safe="")}))
    assert user.id == uid
    assert user.nickname == "재난알림"


def test_optional_user_is_none_without_or_with_invalid_header():
    assert optional_user(_request({})) is None
    assert optional_user(_request({"X-User-Id": "not-a-uuid"})) is None


def test_require_user_raises_401_for_anonymous():
    with pytest.raises(ApiError) as e:
        require_user(_request({}))
    assert e.value.status == 401


def test_require_user_raises_503_when_auth_degraded():
    with pytest.raises(ApiError) as e:
        require_user(_request({"X-Auth-Degraded": "1", "X-User-Id": str(uuid.uuid4())}))
    assert e.value.status == 503
    assert e.value.headers["Retry-After"] == "5"
```

- [ ] **Step 3: 테스트가 실패하는지 확인**

Run: `cd services/board && uv run --extra dev pytest tests/test_health.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'board.app'`

- [ ] **Step 4: 구현**

`services/board/src/board/config.py`:
```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    queue_max_len: int = 50000
    log_level: str = "INFO"

    page_size: int = 20
    list_cache_ttl_seconds: int = 3
    stale_cache_ttl_seconds: int = 60
    detail_cache_ttl_seconds: int = 30
    idempotency_ttl_seconds: int = 600
    status_ttl_seconds: int = 3600

    worker_batch_size: int = 100
    worker_block_ms: int = 1000
    claim_idle_ms: int = 30000
    max_deliveries: int = 5
    heartbeat_path: str = "/tmp/worker-heartbeat"
    worker_metrics_port: int = 9100
```

`services/board/src/board/errors.py`:
```python
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)
RETRY_AFTER_SECONDS = "5"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def unavailable(message: str = "일시적으로 서비스를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.") -> ApiError:
    return ApiError(503, "UNAVAILABLE", message, {"Retry-After": RETRY_AFTER_SECONDS})


def error_response(status: int, code: str, message: str, headers: dict[str, str] | None = None, **extra) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, **extra}, status_code=status, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return error_response(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        fields = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return error_response(422, "VALIDATION_ERROR", "입력값이 올바르지 않습니다.", fields=fields)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return error_response(exc.status_code, code, str(exc.detail))

    async def _dependency_down(request: Request, exc: Exception):
        log.warning("dependency unavailable: %r", exc)
        err = unavailable()
        return error_response(err.status, err.code, err.message, err.headers)

    for exc_type in (RedisError, DBAPIError, OSError):
        app.add_exception_handler(exc_type, _dependency_down)
```

`services/board/src/board/infra.py`:
```python
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
```

`services/board/src/board/observability.py`:
```python
import contextvars
import json
import logging
import sys
import time
import uuid

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

HTTP_REQUESTS = Counter("http_requests_total", "HTTP 요청 수", ["method", "route", "status"])
HTTP_LATENCY = Histogram("http_request_duration_seconds", "HTTP 요청 처리 시간", ["method", "route"])


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", "unmatched")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            HTTP_REQUESTS.labels(request.method, _route_template(request), "500").inc()
            raise
        finally:
            request_id_var.reset(token)
        route = _route_template(request)
        HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        HTTP_LATENCY.labels(request.method, route).observe(time.perf_counter() - start)
        response.headers["X-Request-ID"] = request_id
        return response


def install_metrics_route(app: FastAPI) -> None:
    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

`services/board/src/board/health.py`:
```python
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from board.infra import check_db, check_redis

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    # Redis 만 살아 있으면 글쓰기(큐)를, DB 만 살아 있으면 읽기를 처리할 수 있다.
    # 둘 다 죽었을 때만 트래픽에서 빠진다.
    redis_ok, db_ok = await asyncio.gather(
        check_redis(request.app.state.redis), check_db(request.app.state.engine)
    )
    status = 200 if (redis_ok or db_ok) else 503
    return JSONResponse({"redis": redis_ok, "db": db_ok}, status_code=status)
```

`services/board/src/board/identity.py`:
```python
import uuid
from dataclasses import dataclass
from urllib.parse import unquote

from fastapi import Request

from board.errors import ApiError, unavailable


@dataclass(frozen=True)
class CurrentUser:
    id: str
    nickname: str


def optional_user(request: Request) -> CurrentUser | None:
    """nginx 가 auth_request 결과로 채운 헤더를 읽는다. 클라이언트가 보낸 값은 nginx 가 지운다."""
    raw_id = request.headers.get("x-user-id")
    if not raw_id:
        return None
    try:
        user_id = str(uuid.UUID(raw_id))
    except ValueError:
        return None
    nickname = unquote(request.headers.get("x-user-nickname", "")) or "알 수 없음"
    return CurrentUser(id=user_id, nickname=nickname)


def require_user(request: Request) -> CurrentUser:
    if request.headers.get("x-auth-degraded") == "1":
        raise unavailable("로그인 상태를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.")
    user = optional_user(request)
    if user is None:
        raise ApiError(401, "UNAUTHORIZED", "로그인이 필요합니다.")
    return user
```

`services/board/src/board/app.py`:
```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from board import health
from board.config import Settings
from board.errors import install_error_handlers
from board.infra import make_engine, make_redis
from board.observability import RequestContextMiddleware, configure_logging, install_metrics_route


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = make_engine(settings.database_url)
        app.state.redis = make_redis(settings.redis_url)
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await app.state.engine.dispose()

    app = FastAPI(title="board-api", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    install_metrics_route(app)
    app.include_router(health.router)
    return app
```

`services/board/src/board/main.py`:
```python
from board.app import create_app

app = create_app()
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `cd services/board && uv run --extra dev pytest -v`
Expected: 모든 테스트 통과 (test_migrate 2 + test_health 10)

- [ ] **Step 6: 커밋**

```bash
git add services/board
git commit -m "feat(board): add api skeleton with health, identity headers, metrics"
```

---

### Task 7: 비동기 글쓰기 API (큐에 넣기, 멱등 키, 백프레셔)

**Files:**
- Create: `services/board/src/board/{keys,queue,routes}.py`
- Modify: `services/board/src/board/app.py` (router 등록), `services/board/tests/helpers.py`
- Test: `services/board/tests/test_create_post.py`

**Interfaces:**
- Consumes: Task 6의 `require_user`, `CurrentUser`, `ApiError`, `unavailable`, fixture
- Produces:
  - `board.keys`: `POSTS_STREAM`, `POSTS_GROUP`, `POSTS_DLQ`, `USER_EVENTS`, `USER_EVENTS_GROUP`, `FIRST_PAGE`, `FIRST_PAGE_STALE`, `FIRST_PAGE_LOCK`, `idempotency(user_id, key)`, `pending(post_id)`, `failed(post_id)`, `post_cache(post_id)`
  - `board.queue.enqueue_post(redis, settings, user, title, body, idempotency_key) -> str`(post_id)
  - 스트림 메시지 필드(워커와의 계약): `id`, `author_id`, `author_nickname`, `title`, `body`, `created_at`(ISO 8601, UTC)
  - `board.routes.router`: `POST /api/board/posts` → 202 `{id, status: "pending"}`
  - 테스트 헬퍼 `user_headers(user_id, nickname="tester")`, `post_headers(user_id, nickname="tester", key=None)`, `new_user_id()`

- [ ] **Step 1: 테스트 헬퍼 추가**

`services/board/tests/helpers.py` 끝에 추가:
```python
import uuid
from urllib.parse import quote


def new_user_id() -> str:
    return str(uuid.uuid4())


def user_headers(user_id: str, nickname: str = "tester") -> dict[str, str]:
    """nginx 가 auth_request 뒤에 붙여 주는 헤더를 흉내 낸다."""
    return {"X-User-Id": user_id, "X-User-Nickname": quote(nickname, safe="")}


def post_headers(user_id: str, nickname: str = "tester", key: str | None = None) -> dict[str, str]:
    return {**user_headers(user_id, nickname), "Idempotency-Key": key or str(uuid.uuid4())}
```

- [ ] **Step 2: 실패하는 테스트 작성**

`services/board/tests/test_create_post.py`:
```python
import uuid

from helpers import BROKEN_DATABASE_URL, BROKEN_REDIS_URL, new_user_id, post_headers, user_headers

POST = {"title": "대피소 위치", "body": "OO초등학교 체육관이 열려 있습니다."}


async def test_anonymous_write_is_401(client):
    r = await client.post("/api/board/posts", json=POST, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHORIZED"


async def test_degraded_auth_is_503(client):
    headers = {**post_headers(new_user_id()), "X-Auth-Degraded": "1"}
    r = await client.post("/api/board/posts", json=POST, headers=headers)
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "5"


async def test_missing_idempotency_key_is_400(client):
    r = await client.post("/api/board/posts", json=POST, headers=user_headers(new_user_id()))
    assert r.status_code == 400
    assert r.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_invalid_idempotency_key_is_400(client):
    headers = {**user_headers(new_user_id()), "Idempotency-Key": "not-a-uuid"}
    r = await client.post("/api/board/posts", json=POST, headers=headers)
    assert r.status_code == 400
    assert r.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_title_too_long_is_422(client):
    r = await client.post("/api/board/posts", json={"title": "x" * 101, "body": "b"}, headers=post_headers(new_user_id()))
    assert r.status_code == 422


async def test_accepted_post_is_queued_with_pending_marker(client, rdb):
    uid = new_user_id()
    r = await client.post("/api/board/posts", json=POST, headers=post_headers(uid, "재난봇"))
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending"
    post_id = body["id"]
    assert uuid.UUID(post_id).version == 7

    entries = await rdb.xrange("posts:stream")
    assert len(entries) == 1
    fields = entries[0][1]
    assert fields["id"] == post_id
    assert fields["author_id"] == uid
    assert fields["author_nickname"] == "재난봇"
    assert fields["title"] == POST["title"]
    assert fields["created_at"].endswith("+00:00")
    assert 3500 < await rdb.ttl(f"pending:{post_id}") <= 3600


async def test_same_idempotency_key_returns_same_post(client, rdb):
    headers = post_headers(new_user_id(), key=str(uuid.uuid4()))
    first = await client.post("/api/board/posts", json=POST, headers=headers)
    second = await client.post("/api/board/posts", json=POST, headers=headers)
    assert first.json()["id"] == second.json()["id"]
    assert await rdb.xlen("posts:stream") == 1


async def test_full_queue_is_503_and_does_not_burn_idempotency_key(make_client, settings, rdb):
    uid = new_user_id()
    key = str(uuid.uuid4())
    async with make_client(settings.model_copy(update={"queue_max_len": 1})) as c:
        assert (await c.post("/api/board/posts", json=POST, headers=post_headers(uid))).status_code == 202
        full = await c.post("/api/board/posts", json=POST, headers=post_headers(uid, key=key))
        assert full.status_code == 503
        assert full.json()["code"] == "QUEUE_FULL"
        assert full.headers["Retry-After"] == "5"

        # 워커가 큐를 비운 뒤 같은 키로 재시도하면 새로 큐에 들어가야 한다.
        for entry_id, _ in await rdb.xrange("posts:stream"):
            await rdb.xdel("posts:stream", entry_id)
        retry = await c.post("/api/board/posts", json=POST, headers=post_headers(uid, key=key))
    assert retry.status_code == 202
    assert await rdb.xlen("posts:stream") == 1


async def test_writes_are_accepted_while_db_is_down(make_client, settings):
    async with make_client(settings.model_copy(update={"database_url": BROKEN_DATABASE_URL})) as c:
        r = await c.post("/api/board/posts", json=POST, headers=post_headers(new_user_id()))
    assert r.status_code == 202


async def test_writes_are_503_while_redis_is_down(make_client, settings):
    async with make_client(settings.model_copy(update={"redis_url": BROKEN_REDIS_URL})) as c:
        r = await c.post("/api/board/posts", json=POST, headers=post_headers(new_user_id()))
    assert r.status_code == 503
    assert r.json()["code"] == "UNAVAILABLE"
```

- [ ] **Step 3: 테스트가 실패하는지 확인**

Run: `cd services/board && uv run --extra dev pytest tests/test_create_post.py -v`
Expected: FAIL, POST가 404

- [ ] **Step 4: 구현**

`services/board/src/board/keys.py`:
```python
"""Redis 키와 스트림 이름. board-api 와 board-worker 가 함께 쓴다."""

POSTS_STREAM = "posts:stream"
POSTS_GROUP = "writers"
POSTS_DLQ = "posts:dlq"

# auth-svc 가 발행한다 (services/auth/src/auth/events.py 와 같은 이름이어야 한다).
USER_EVENTS = "user:events"
USER_EVENTS_GROUP = "user-events"

FIRST_PAGE = "cache:posts:first"
FIRST_PAGE_STALE = "stale:posts:first"
FIRST_PAGE_LOCK = "lock:posts:first"


def idempotency(user_id: str, key: str) -> str:
    return f"idem:{user_id}:{key}"


def pending(post_id: str) -> str:
    return f"pending:{post_id}"


def failed(post_id: str) -> str:
    return f"failed:{post_id}"


def post_cache(post_id: str) -> str:
    return f"cache:post:{post_id}"
```

`services/board/src/board/queue.py`:
```python
from contextlib import suppress
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError
from uuid6 import uuid7

from board import keys
from board.config import Settings
from board.errors import RETRY_AFTER_SECONDS, ApiError, unavailable
from board.identity import CurrentUser


async def enqueue_post(
    redis: Redis, settings: Settings, user: CurrentUser, title: str, body: str, idempotency_key: str
) -> str:
    """글을 posts:stream 에 넣고 post_id 를 돌려준다. DB 는 건드리지 않는다."""
    idem_key = keys.idempotency(user.id, idempotency_key)
    existing = await redis.get(idem_key)
    if existing:
        return existing

    # 큐 상한 확인을 멱등 키 저장보다 먼저 한다. 503 을 받은 재시도가 "이미 처리됨"으로 처리되면 안 된다.
    if await redis.xlen(keys.POSTS_STREAM) >= settings.queue_max_len:
        raise ApiError(
            503,
            "QUEUE_FULL",
            "요청이 많아 글을 받을 수 없습니다. 잠시 후 다시 시도해 주세요.",
            {"Retry-After": RETRY_AFTER_SECONDS},
        )

    post_id = str(uuid7())
    if not await redis.set(idem_key, post_id, nx=True, ex=settings.idempotency_ttl_seconds):
        # 같은 키로 동시에 들어온 재시도. 먼저 들어온 요청의 post_id 를 돌려준다.
        return await redis.get(idem_key) or post_id

    fields = {
        "id": post_id,
        "author_id": user.id,
        "author_nickname": user.nickname,
        "title": title,
        "body": body,
        "created_at": datetime.now(UTC).isoformat(),
    }
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.xadd(keys.POSTS_STREAM, fields)
            pipe.set(keys.pending(post_id), "1", ex=settings.status_ttl_seconds)
            await pipe.execute()
    except RedisError as exc:
        with suppress(RedisError):
            await redis.delete(idem_key)
        raise unavailable() from exc
    return post_id
```

`services/board/src/board/routes.py`:
```python
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
```

`services/board/src/board/app.py`: import를 `from board import health, routes`로 바꾸고 `app.include_router(health.router)` 다음 줄에 추가:
```python
    app.include_router(routes.router)
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `cd services/board && uv run --extra dev pytest -v`
Expected: 모든 테스트 통과

- [ ] **Step 6: 커밋**

```bash
git add services/board
git commit -m "feat(board): async post creation via redis stream with idempotency and backpressure"
```

---

### Task 8: 읽기 API (캐시, 스탬피드 방지, stale 대체)와 삭제

**Files:**
- Create: `services/board/src/board/{posts_repo,metrics,cache}.py`
- Modify: `services/board/src/board/routes.py` (조회·삭제 추가), `services/board/tests/helpers.py`
- Test: `services/board/tests/test_read_and_delete.py`

**Interfaces:**
- Consumes: Task 7의 `keys`, `routes.router`, `require_user`
- Produces:
  - `board.posts_repo`: `ANONYMIZED_NICKNAME = "탈퇴한 사용자"`, `serialize(row) -> dict`, `list_posts(engine, cursor: str | None, limit: int) -> {"items", "next_cursor"}`, `get_post(engine, post_id) -> dict | None`, `insert_many(conn, rows)`(row: `id: UUID, author_id: UUID | None, author_nickname, title, body, created_at: datetime`, `ON CONFLICT DO NOTHING`), `soft_delete(engine, post_id, author_id) -> "deleted" | "forbidden" | "not_found"`, `anonymize_author(engine, user_id) -> list[str]`
  - `board.metrics.record_cache(hit: bool)`, gauge `cache_hit_ratio`
  - `board.cache`: `first_page(redis, loader, settings) -> dict`, `get_post(redis, post_id)`, `set_post(redis, post_id, post, ttl)`, `post_status(redis, post_id) -> "pending" | "failed" | None`, `invalidate(redis, *post_ids)`(목록 첫 페이지와 지정한 글 캐시 삭제, Redis 에러 무시)
  - 게시글 JSON: `{id, author_id, author_nickname, title, body, created_at}`
  - 테스트 헬퍼 `make_row(author_id, title=..., body=..., nickname=...)`, `insert_posts(engine, rows)`

- [ ] **Step 1: 테스트 헬퍼 추가**

`services/board/tests/helpers.py` 끝에 추가:
```python
from datetime import UTC, datetime

from uuid6 import uuid7


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
```

- [ ] **Step 2: 실패하는 테스트 작성**

`services/board/tests/test_read_and_delete.py`:
```python
import asyncio

from board import cache
from helpers import (
    BROKEN_DATABASE_URL,
    BROKEN_REDIS_URL,
    insert_posts,
    make_row,
    new_user_id,
    user_headers,
)


async def test_empty_list(client):
    r = await client.get("/api/board/posts")
    assert r.status_code == 200
    assert r.json() == {"items": [], "next_cursor": None}


async def test_cursor_pagination_newest_first(client, engine):
    uid = new_user_id()
    rows = [make_row(uid, title=f"글 {i}") for i in range(25)]
    await insert_posts(engine, rows)

    first = (await client.get("/api/board/posts")).json()
    assert [p["title"] for p in first["items"]] == [f"글 {i}" for i in range(24, 4, -1)]
    assert first["next_cursor"] == first["items"][-1]["id"]

    second = (await client.get("/api/board/posts", params={"cursor": first["next_cursor"]})).json()
    assert [p["title"] for p in second["items"]] == [f"글 {i}" for i in range(4, -1, -1)]
    assert second["next_cursor"] is None


async def test_post_json_shape(client, engine):
    uid = new_user_id()
    row = make_row(uid, title="t", body="b", nickname="닉")
    await insert_posts(engine, [row])
    item = (await client.get("/api/board/posts")).json()["items"][0]
    assert item == {
        "id": str(row["id"]),
        "author_id": uid,
        "author_nickname": "닉",
        "title": "t",
        "body": "b",
        "created_at": row["created_at"].isoformat(),
    }


async def test_limit_validation(client):
    assert (await client.get("/api/board/posts", params={"limit": 51})).status_code == 422
    assert (await client.get("/api/board/posts", params={"cursor": "nope"})).status_code == 422


async def test_first_page_is_cached_briefly(client, engine, rdb):
    uid = new_user_id()
    await insert_posts(engine, [make_row(uid, title="처음 글")])
    await client.get("/api/board/posts")
    assert 0 < await rdb.ttl("cache:posts:first") <= 3
    assert 0 < await rdb.ttl("stale:posts:first") <= 60

    await insert_posts(engine, [make_row(uid, title="새 글")])
    cached = (await client.get("/api/board/posts")).json()
    assert [p["title"] for p in cached["items"]] == ["처음 글"]

    await rdb.delete("cache:posts:first")
    fresh = (await client.get("/api/board/posts")).json()
    assert [p["title"] for p in fresh["items"]] == ["새 글", "처음 글"]


async def test_concurrent_cache_misses_hit_db_once(rdb, settings):
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        return {"items": [], "next_cursor": None}

    results = await asyncio.gather(*(cache.first_page(rdb, loader, settings) for _ in range(10)))
    assert calls == 1
    assert all(r == {"items": [], "next_cursor": None} for r in results)


async def test_list_reads_db_directly_when_redis_down(make_client, settings, engine):
    await insert_posts(engine, [make_row(new_user_id(), title="DB 글")])
    async with make_client(settings.model_copy(update={"redis_url": BROKEN_REDIS_URL})) as c:
        r = await c.get("/api/board/posts")
        detail_id = r.json()["items"][0]["id"]
        detail = await c.get(f"/api/board/posts/{detail_id}")
    assert r.status_code == 200
    assert r.json()["items"][0]["title"] == "DB 글"
    assert detail.json()["status"] == "published"


async def test_list_serves_stale_copy_when_db_down(client, make_client, settings, engine, rdb):
    await insert_posts(engine, [make_row(new_user_id(), title="남아있는 글")])
    await client.get("/api/board/posts")  # cache + stale 채우기
    await rdb.delete("cache:posts:first")  # 짧은 캐시가 만료된 상황
    async with make_client(settings.model_copy(update={"database_url": BROKEN_DATABASE_URL})) as c:
        r = await c.get("/api/board/posts")
    assert r.status_code == 200
    assert r.json()["items"][0]["title"] == "남아있는 글"


async def test_list_is_503_when_db_down_and_no_stale_copy(make_client, settings):
    async with make_client(settings.model_copy(update={"database_url": BROKEN_DATABASE_URL})) as c:
        r = await c.get("/api/board/posts")
    assert r.status_code == 503
    assert r.json()["code"] == "UNAVAILABLE"


async def test_detail_published_and_cached(client, engine, rdb):
    row = make_row(new_user_id(), title="상세")
    await insert_posts(engine, [row])
    r = await client.get(f"/api/board/posts/{row['id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "published"
    assert r.json()["post"]["title"] == "상세"
    assert 0 < await rdb.ttl(f"cache:post:{row['id']}") <= 30


async def test_detail_pending_failed_and_missing(client, rdb):
    await rdb.set("pending:0192f5a0-0000-7000-8000-000000000001", "1")
    await rdb.set("failed:0192f5a0-0000-7000-8000-000000000002", "1")
    pending = await client.get("/api/board/posts/0192f5a0-0000-7000-8000-000000000001")
    failed = await client.get("/api/board/posts/0192f5a0-0000-7000-8000-000000000002")
    missing = await client.get("/api/board/posts/0192f5a0-0000-7000-8000-000000000003")
    assert pending.json() == {"status": "pending"}
    assert failed.json() == {"status": "failed"}
    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"


async def test_delete_own_post(client, engine, rdb):
    uid = new_user_id()
    row = make_row(uid)
    await insert_posts(engine, [row])
    await client.get("/api/board/posts")
    await client.get(f"/api/board/posts/{row['id']}")

    r = await client.delete(f"/api/board/posts/{row['id']}", headers=user_headers(uid))
    assert r.status_code == 204
    assert await rdb.exists("cache:posts:first", f"cache:post:{row['id']}") == 0
    assert (await client.get("/api/board/posts")).json()["items"] == []
    assert (await client.get(f"/api/board/posts/{row['id']}")).status_code == 404


async def test_delete_others_post_is_403(client, engine):
    row = make_row(new_user_id())
    await insert_posts(engine, [row])
    r = await client.delete(f"/api/board/posts/{row['id']}", headers=user_headers(new_user_id()))
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"


async def test_delete_missing_is_404_and_anonymous_is_401(client):
    missing = "0192f5a0-0000-7000-8000-000000000009"
    assert (await client.delete(f"/api/board/posts/{missing}", headers=user_headers(new_user_id()))).status_code == 404
    assert (await client.delete(f"/api/board/posts/{missing}")).status_code == 401
```

- [ ] **Step 3: 테스트가 실패하는지 확인**

Run: `cd services/board && uv run --extra dev pytest tests/test_read_and_delete.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'board.posts_repo'`(헬퍼 import) 또는 `board.cache`

- [ ] **Step 4: 구현**

`services/board/src/board/posts_repo.py`:
```python
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

ANONYMIZED_NICKNAME = "탈퇴한 사용자"
_COLUMNS = "id, author_id, author_nickname, title, body, created_at"


def serialize(row) -> dict:
    return {
        "id": str(row["id"]),
        "author_id": str(row["author_id"]) if row["author_id"] else None,
        "author_nickname": row["author_nickname"],
        "title": row["title"],
        "body": row["body"],
        "created_at": row["created_at"].isoformat(),
    }


async def list_posts(engine: AsyncEngine, cursor: str | None, limit: int) -> dict:
    sql = f"SELECT {_COLUMNS} FROM board.posts WHERE deleted_at IS NULL"
    params: dict = {"n": limit + 1}
    if cursor:
        sql += " AND id < :cursor"
        params["cursor"] = uuid.UUID(cursor)
    sql += " ORDER BY id DESC LIMIT :n"
    async with engine.connect() as conn:
        rows = (await conn.execute(text(sql), params)).mappings().all()
    items = [serialize(r) for r in rows[:limit]]
    next_cursor = items[-1]["id"] if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor}


async def get_post(engine: AsyncEngine, post_id: str) -> dict | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(f"SELECT {_COLUMNS} FROM board.posts WHERE id = :id AND deleted_at IS NULL"),
                {"id": uuid.UUID(post_id)},
            )
        ).mappings().first()
    return serialize(row) if row else None


async def insert_many(conn: AsyncConnection, rows: list[dict]) -> None:
    await conn.execute(
        text(
            "INSERT INTO board.posts (id, author_id, author_nickname, title, body, created_at) "
            "VALUES (:id, :author_id, :author_nickname, :title, :body, :created_at) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        rows,
    )


async def soft_delete(engine: AsyncEngine, post_id: str, author_id: str) -> str:
    async with engine.begin() as conn:
        deleted = (
            await conn.execute(
                text(
                    "UPDATE board.posts SET deleted_at = now() "
                    "WHERE id = :id AND author_id = :author AND deleted_at IS NULL RETURNING id"
                ),
                {"id": uuid.UUID(post_id), "author": uuid.UUID(author_id)},
            )
        ).first()
        if deleted:
            return "deleted"
        exists = (
            await conn.execute(
                text("SELECT 1 FROM board.posts WHERE id = :id AND deleted_at IS NULL"),
                {"id": uuid.UUID(post_id)},
            )
        ).first()
    return "forbidden" if exists else "not_found"


async def anonymize_author(engine: AsyncEngine, user_id: str) -> list[str]:
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "UPDATE board.posts SET author_id = NULL, author_nickname = :nickname "
                    "WHERE author_id = :author RETURNING id"
                ),
                {"nickname": ANONYMIZED_NICKNAME, "author": uuid.UUID(user_id)},
            )
        ).all()
    return [str(r[0]) for r in rows]
```

`services/board/src/board/metrics.py`:
```python
from prometheus_client import Gauge

_cache_counts = {"hit": 0, "miss": 0}


def record_cache(hit: bool) -> None:
    _cache_counts["hit" if hit else "miss"] += 1


def _cache_hit_ratio() -> float:
    total = _cache_counts["hit"] + _cache_counts["miss"]
    return _cache_counts["hit"] / total if total else 0.0


CACHE_HIT_RATIO = Gauge("cache_hit_ratio", "Redis 읽기 캐시 적중률 (프로세스 시작 이후 누적)")
CACHE_HIT_RATIO.set_function(_cache_hit_ratio)
```

`services/board/src/board/cache.py`:
```python
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError

from board import keys
from board.config import Settings
from board.metrics import record_cache

log = logging.getLogger(__name__)

DB_ERRORS = (DBAPIError, OSError)
LOCK_TTL_SECONDS = 2
LOCK_WAIT_SECONDS = 0.05
LOCK_WAIT_ATTEMPTS = 5


async def _try(coro: Awaitable):
    """Redis 호출이 실패하면 None 을 돌려준다. 캐시는 없어도 서비스가 동작해야 한다."""
    try:
        return await coro
    except RedisError:
        return None


async def first_page(redis: Redis, loader: Callable[[], Awaitable[dict]], settings: Settings) -> dict:
    try:
        cached = await redis.get(keys.FIRST_PAGE)
    except RedisError:
        log.warning("redis unavailable, reading first page from db")
        return await loader()
    if cached is not None:
        record_cache(hit=True)
        return json.loads(cached)
    record_cache(hit=False)

    # 캐시 스탬피드 방지: 락을 잡은 요청 하나만 DB 를 조회한다.
    got_lock = await _try(redis.set(keys.FIRST_PAGE_LOCK, "1", nx=True, ex=LOCK_TTL_SECONDS))
    if not got_lock:
        for _ in range(LOCK_WAIT_ATTEMPTS):
            await asyncio.sleep(LOCK_WAIT_SECONDS)
            cached = await _try(redis.get(keys.FIRST_PAGE))
            if cached is not None:
                return json.loads(cached)

    try:
        page = await loader()
    except DB_ERRORS:
        stale = await _try(redis.get(keys.FIRST_PAGE_STALE))
        if stale is not None:
            log.warning("db unavailable, serving stale first page")
            return json.loads(stale)
        raise

    payload = json.dumps(page, ensure_ascii=False)
    with suppress(RedisError):
        async with redis.pipeline(transaction=False) as pipe:
            pipe.set(keys.FIRST_PAGE, payload, ex=settings.list_cache_ttl_seconds)
            pipe.set(keys.FIRST_PAGE_STALE, payload, ex=settings.stale_cache_ttl_seconds)
            if got_lock:
                pipe.delete(keys.FIRST_PAGE_LOCK)
            await pipe.execute()
    return page


async def get_post(redis: Redis, post_id: str) -> dict | None:
    raw = await _try(redis.get(keys.post_cache(post_id)))
    record_cache(hit=raw is not None)
    return json.loads(raw) if raw is not None else None


async def set_post(redis: Redis, post_id: str, post: dict, ttl: int) -> None:
    with suppress(RedisError):
        await redis.set(keys.post_cache(post_id), json.dumps(post, ensure_ascii=False), ex=ttl)


async def post_status(redis: Redis, post_id: str) -> str | None:
    try:
        async with redis.pipeline(transaction=False) as pipe:
            pipe.exists(keys.failed(post_id))
            pipe.exists(keys.pending(post_id))
            failed, pending = await pipe.execute()
    except RedisError:
        return None
    if failed:
        return "failed"
    if pending:
        return "pending"
    return None


async def invalidate(redis: Redis, *post_ids: str) -> None:
    with suppress(RedisError):
        await redis.delete(keys.FIRST_PAGE, *(keys.post_cache(p) for p in post_ids))
```

`services/board/src/board/routes.py`를 아래 전체 내용으로 교체:
```python
import uuid

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field

from board import cache, posts_repo
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


@router.get("/api/board/posts")
async def list_posts(
    request: Request,
    cursor: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
) -> dict:
    engine = request.app.state.engine
    settings = request.app.state.settings

    async def load() -> dict:
        return await posts_repo.list_posts(engine, str(cursor) if cursor else None, limit)

    # 트래픽이 가장 몰리는 첫 페이지(기본 크기)만 캐시한다.
    if cursor is None and limit == settings.page_size:
        return await cache.first_page(request.app.state.redis, load, settings)
    return await load()


@router.get("/api/board/posts/{post_id}")
async def get_post(post_id: uuid.UUID, request: Request) -> dict:
    pid = str(post_id)
    redis = request.app.state.redis
    cached = await cache.get_post(redis, pid)
    if cached is not None:
        return {"status": "published", "post": cached}
    post = await posts_repo.get_post(request.app.state.engine, pid)
    if post is not None:
        await cache.set_post(redis, pid, post, request.app.state.settings.detail_cache_ttl_seconds)
        return {"status": "published", "post": post}
    status = await cache.post_status(redis, pid)
    if status is not None:
        return {"status": status}
    raise ApiError(404, "NOT_FOUND", "글을 찾을 수 없습니다.")


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


@router.delete("/api/board/posts/{post_id}", status_code=204)
async def delete_post(post_id: uuid.UUID, request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    pid = str(post_id)
    result = await posts_repo.soft_delete(request.app.state.engine, pid, user.id)
    if result == "forbidden":
        raise ApiError(403, "FORBIDDEN", "본인이 작성한 글만 삭제할 수 있습니다.")
    if result == "not_found":
        raise ApiError(404, "NOT_FOUND", "글을 찾을 수 없습니다.")
    await cache.invalidate(request.app.state.redis, pid)
    return Response(status_code=204)
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `cd services/board && uv run --extra dev pytest -v`
Expected: 모든 테스트 통과

- [ ] **Step 6: 커밋**

```bash
git add services/board
git commit -m "feat(board): cached reads with stampede guard, stale fallback, and delete"
```

---

### Task 9: 워커, 글 저장 (배치 INSERT, ACK, DLQ, 일시 장애 보존, reclaim)

**Files:**
- Create: `services/board/src/board/worker.py`
- Modify: `services/board/tests/conftest.py` (`make_worker`, `worker` fixture), `services/board/tests/helpers.py` (`enqueue`, `fetch_posts`)
- Test: `services/board/tests/test_worker_posts.py`

**Interfaces:**
- Consumes: Task 7의 스트림 메시지 필드와 `keys`, Task 8의 `posts_repo.insert_many`
- Produces:
  - `board.worker.TransientDbError`, `is_transient(exc) -> bool`, `decode_post(fields) -> dict`
  - `board.worker.Worker(engine, redis, settings, name)`: `ensure_groups()`, `process_posts_once(block_ms: int | None = None) -> int`, `reclaim_posts_once() -> int`, `dead_letter(message_id, fields, reason)`. 속성 `engine`은 테스트에서 교체할 수 있다.
  - 동작 보장: 저장에 성공한 메시지는 XACK + XDEL하고, `pending:<id>`와 `cache:posts:first`를 삭제한다. 데이터 오류가 난 메시지는 DLQ로 옮기고 `failed:<id>`를 남긴다. DB 연결 오류(`TransientDbError`)가 나면 메시지를 ACK하지 않고 메모리에 보관했다가 다음 호출 때 다시 시도한다.
  - fixture `make_worker(settings, name="test-worker")`(async context manager), `worker`

- [ ] **Step 1: fixture와 헬퍼 추가**

`services/board/tests/conftest.py` 끝에 추가 (import 영역에 `from board.infra import make_engine, make_redis`, `from board.worker import Worker` 추가. 기존 `from board.infra import make_engine` 줄은 교체):
```python
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
```

`services/board/tests/helpers.py` 끝에 추가:
```python
async def enqueue(redis, settings, user_id: str, title: str = "제목", body: str = "본문") -> str:
    from board.identity import CurrentUser
    from board.queue import enqueue_post

    return await enqueue_post(redis, settings, CurrentUser(user_id, "tester"), title, body, str(uuid.uuid4()))


async def fetch_posts(engine) -> list[dict]:
    from sqlalchemy import text

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, author_id, author_nickname, title, body, deleted_at FROM board.posts ORDER BY id")
            )
        ).mappings().all()
    return [dict(r) for r in rows]
```

- [ ] **Step 2: 실패하는 테스트 작성**

`services/board/tests/test_worker_posts.py`:
```python
import pytest

from board.worker import TransientDbError
from helpers import BROKEN_DATABASE_URL, enqueue, fetch_posts, new_user_id, post_headers


async def test_worker_persists_queued_post(client, worker, engine, rdb):
    r = await client.post("/api/board/posts", json={"title": "긴급", "body": "물 배급"}, headers=post_headers(new_user_id(), "봉사자"))
    post_id = r.json()["id"]
    await client.get("/api/board/posts")  # 첫 페이지 캐시 생성

    assert await worker.process_posts_once() == 1

    rows = await fetch_posts(engine)
    assert [str(r["id"]) for r in rows] == [post_id]
    assert rows[0]["author_nickname"] == "봉사자"
    assert await rdb.xlen("posts:stream") == 0  # ACK 후 XDEL
    assert await rdb.exists(f"pending:{post_id}", "cache:posts:first") == 0
    detail = await client.get(f"/api/board/posts/{post_id}")
    assert detail.json()["status"] == "published"


async def test_worker_batches_up_to_batch_size(worker, settings, rdb, engine):
    uid = new_user_id()
    for i in range(150):
        await enqueue(rdb, settings, uid, title=f"글 {i}")
    assert await worker.process_posts_once() == 100
    assert await worker.process_posts_once() == 50
    assert await worker.process_posts_once() == 0
    assert len(await fetch_posts(engine)) == 150


async def test_duplicate_message_is_inserted_once(worker, settings, rdb, engine):
    await enqueue(rdb, settings, new_user_id())
    (_, fields), = await rdb.xrange("posts:stream")
    await rdb.xadd("posts:stream", fields)  # 같은 글이 두 번 들어온 상황
    assert await worker.process_posts_once() == 2
    assert len(await fetch_posts(engine)) == 1


async def test_undecodable_message_goes_to_dlq(worker, settings, rdb, engine):
    good = await enqueue(rdb, settings, new_user_id(), title="정상")
    await rdb.xadd("posts:stream", {"id": "0192f5a0-0000-7000-8000-00000000000a", "author_id": "not-a-uuid"})

    assert await worker.process_posts_once() == 2

    assert [str(r["id"]) for r in await fetch_posts(engine)] == [good]
    dlq = await rdb.xrange("posts:dlq")
    assert len(dlq) == 1
    assert dlq[0][1]["reason"].startswith("decode:")
    assert await rdb.exists("failed:0192f5a0-0000-7000-8000-00000000000a") == 1
    assert await rdb.xlen("posts:stream") == 0


async def test_row_rejected_by_db_goes_to_dlq_and_others_survive(worker, settings, rdb, engine):
    good = await enqueue(rdb, settings, new_user_id(), title="정상")
    bad = await enqueue(rdb, settings, new_user_id(), title="NUL \x00 문자")  # Postgres text 는 NUL 을 거부한다

    assert await worker.process_posts_once() == 2

    assert [str(r["id"]) for r in await fetch_posts(engine)] == [good]
    assert await rdb.exists(f"failed:{bad}") == 1
    assert await rdb.exists(f"pending:{bad}") == 0
    assert (await rdb.xrange("posts:dlq"))[0][1]["reason"].startswith("insert:")


async def test_db_outage_keeps_messages_and_retries(make_worker, settings, rdb, engine):
    post_id = await enqueue(rdb, settings, new_user_id())
    async with make_worker(settings.model_copy(update={"database_url": BROKEN_DATABASE_URL})) as w:
        with pytest.raises(TransientDbError):
            await w.process_posts_once()
        assert await rdb.xlen("posts:stream") == 1
        assert await rdb.xlen("posts:dlq") == 0

        w.engine = engine  # DB 복구
        assert await w.process_posts_once() == 1
    assert [str(r["id"]) for r in await fetch_posts(engine)] == [post_id]
    assert await rdb.xlen("posts:stream") == 0


async def test_reclaims_messages_of_dead_consumer(make_worker, settings, rdb, engine):
    post_id = await enqueue(rdb, settings, new_user_id())
    async with make_worker(settings.model_copy(update={"claim_idle_ms": 0})) as w:
        # 다른 워커가 읽고 죽은 상황
        await rdb.xreadgroup("writers", "dead-worker", {"posts:stream": ">"}, count=10)
        assert await w.reclaim_posts_once() == 1
    assert [str(r["id"]) for r in await fetch_posts(engine)] == [post_id]
    assert await rdb.xlen("posts:stream") == 0


async def test_repeatedly_failing_message_goes_to_dlq(client, make_worker, settings, rdb, engine):
    post_id = await enqueue(rdb, settings, new_user_id())
    async with make_worker(settings.model_copy(update={"claim_idle_ms": 0, "max_deliveries": 1})) as w:
        await rdb.xreadgroup("writers", "dead-worker", {"posts:stream": ">"}, count=10)  # 1회 전달
        assert await w.reclaim_posts_once() == 1  # reclaim 으로 2회째 → 한도 초과
    assert await fetch_posts(engine) == []
    assert await rdb.xlen("posts:dlq") == 1
    assert (await client.get(f"/api/board/posts/{post_id}")).json() == {"status": "failed"}
```

- [ ] **Step 3: 테스트가 실패하는지 확인**

Run: `cd services/board && uv run --extra dev pytest tests/test_worker_posts.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'board.worker'`

- [ ] **Step 4: 구현**

`services/board/src/board/worker.py`:
```python
"""board-worker: posts:stream 을 소비해 board.posts 에 저장한다."""
import logging
import uuid
from datetime import datetime

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from board import keys, posts_repo
from board.config import Settings

log = logging.getLogger("board.worker")

Entry = tuple[str, dict[str, str]]


class TransientDbError(Exception):
    """DB 연결 문제. 메시지를 버리지 않고 나중에 다시 시도한다."""


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (OSError, ConnectionError)):
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
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `cd services/board && uv run --extra dev pytest -v`
Expected: 모든 테스트 통과

- [ ] **Step 6: 커밋**

```bash
git add services/board
git commit -m "feat(board): worker persists queued posts with dlq, reclaim, and outage retention"
```

---

### Task 10: 워커, 탈퇴 이벤트 처리, 실행 루프, heartbeat, 큐 메트릭

**Files:**
- Create: `services/board/src/board/worker_health.py`
- Modify: `services/board/src/board/worker.py` (이벤트 처리, `run`, `main`), `services/board/src/board/metrics.py` (큐 gauge)
- Test: `services/board/tests/test_worker_events_and_loop.py`

**Interfaces:**
- Consumes: Task 5의 이벤트 계약(`user:events`, `type=user_deleted`, `user_id`), Task 8의 `posts_repo.anonymize_author`, `cache.invalidate`, Task 9의 `Worker`
- Produces:
  - `Worker.process_user_events_once() -> int`, `Worker.reclaim_user_events_once() -> int`, `Worker.run(stop: asyncio.Event)`
  - `python -m board.worker`: 메트릭 서버(9100)를 띄우고, SIGTERM/SIGINT를 받으면 처리 중인 배치까지 끝낸 뒤 종료한다.
  - `board.metrics.refresh_queue_metrics(redis)`, gauge `queue_length`, `queue_lag_seconds`, `dlq_size`
  - `board.worker_health.is_healthy(path, max_age=30, now=None) -> bool`, `python -m board.worker_health`(환경변수 `HEARTBEAT_PATH`, 기본 `/tmp/worker-heartbeat`; exit 0/1)

- [ ] **Step 1: 실패하는 테스트 작성**

`services/board/tests/test_worker_events_and_loop.py`:
```python
import asyncio
import os
import time

from prometheus_client import REGISTRY

from board.metrics import refresh_queue_metrics
from board.worker_health import is_healthy
from helpers import enqueue, fetch_posts, insert_posts, make_row, new_user_id, post_headers


async def test_user_deleted_event_anonymizes_posts(client, worker, engine, rdb):
    leaving, staying = new_user_id(), new_user_id()
    mine = make_row(leaving, nickname="떠나는사람")
    other = make_row(staying, nickname="남는사람")
    await insert_posts(engine, [mine, other])
    await client.get("/api/board/posts")
    await client.get(f"/api/board/posts/{mine['id']}")

    await rdb.xadd("user:events", {"type": "user_deleted", "user_id": leaving})
    assert await worker.process_user_events_once() == 1

    rows = {str(r["id"]): r for r in await fetch_posts(engine)}
    assert rows[str(mine["id"])]["author_id"] is None
    assert rows[str(mine["id"])]["author_nickname"] == "탈퇴한 사용자"
    assert rows[str(other["id"])]["author_nickname"] == "남는사람"
    assert await rdb.exists("cache:posts:first", f"cache:post:{mine['id']}") == 0
    assert await rdb.xlen("user:events") == 0


async def test_invalid_user_event_is_dropped(worker, rdb):
    await rdb.xadd("user:events", {"type": "user_deleted", "user_id": "garbage"})
    await rdb.xadd("user:events", {"type": "something_else"})
    assert await worker.process_user_events_once() == 2
    assert await rdb.xlen("user:events") == 0


async def test_unacked_user_event_is_reclaimed(make_worker, settings, engine, rdb):
    uid = new_user_id()
    await insert_posts(engine, [make_row(uid)])
    await rdb.xadd("user:events", {"type": "user_deleted", "user_id": uid})
    async with make_worker(settings.model_copy(update={"claim_idle_ms": 0})) as w:
        await rdb.xreadgroup("user-events", "dead-worker", {"user:events": ">"}, count=10)
        assert await w.reclaim_user_events_once() == 1
    assert (await fetch_posts(engine))[0]["author_id"] is None


async def test_run_loop_processes_until_stopped(client, worker, engine, settings):
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    r = await client.post("/api/board/posts", json={"title": "루프", "body": "테스트"}, headers=post_headers(new_user_id()))
    post_id = r.json()["id"]

    for _ in range(50):
        if [str(p["id"]) for p in await fetch_posts(engine)] == [post_id]:
            break
        await asyncio.sleep(0.1)
    assert [str(p["id"]) for p in await fetch_posts(engine)] == [post_id]
    assert is_healthy(settings.heartbeat_path)

    stop.set()
    await asyncio.wait_for(task, timeout=3)


async def test_run_loop_recreates_groups_after_redis_data_loss(client, worker, engine, rdb):
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0.3)
    await rdb.flushall()  # Redis 재시작으로 스트림과 consumer group 이 사라진 상황

    r = await client.post("/api/board/posts", json={"title": "복구", "body": "후"}, headers=post_headers(new_user_id()))
    post_id = r.json()["id"]
    for _ in range(80):
        if [str(p["id"]) for p in await fetch_posts(engine)] == [post_id]:
            break
        await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=3)
    assert [str(p["id"]) for p in await fetch_posts(engine)] == [post_id]


async def test_queue_metrics(rdb, settings):
    uid = new_user_id()
    for _ in range(3):
        await enqueue(rdb, settings, uid)
    await rdb.xadd("posts:dlq", {"id": "x"})
    await refresh_queue_metrics(rdb)
    assert REGISTRY.get_sample_value("queue_length") == 3
    assert REGISTRY.get_sample_value("queue_lag_seconds") >= 0
    assert REGISTRY.get_sample_value("dlq_size") == 1


def test_worker_health(tmp_path):
    path = tmp_path / "hb"
    assert not is_healthy(path)
    path.touch()
    assert is_healthy(path)
    old = time.time() - 60
    os.utime(path, (old, old))
    assert not is_healthy(path)
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `cd services/board && uv run --extra dev pytest tests/test_worker_events_and_loop.py -v`
Expected: FAIL, `ImportError: cannot import name 'refresh_queue_metrics'`

- [ ] **Step 3: 구현**

`services/board/src/board/metrics.py` 끝에 추가 (import 영역에 `import time`과 `from board import keys` 추가):
```python
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
```

`services/board/src/board/worker_health.py`:
```python
"""k8s exec liveness probe: 워커 루프가 30초 안에 heartbeat 파일을 갱신했는지 확인한다."""
import os
import sys
import time
from pathlib import Path

MAX_AGE_SECONDS = 30


def is_healthy(path: str | Path, max_age: float = MAX_AGE_SECONDS, now: float | None = None) -> bool:
    try:
        mtime = Path(path).stat().st_mtime
    except FileNotFoundError:
        return False
    return ((now if now is not None else time.time()) - mtime) < max_age


def main() -> None:
    sys.exit(0 if is_healthy(os.environ.get("HEARTBEAT_PATH", "/tmp/worker-heartbeat")) else 1)


if __name__ == "__main__":
    main()
```

`services/board/src/board/worker.py` 변경:

모듈 docstring을 `"""board-worker: posts:stream 과 user:events 를 소비해 DB 에 반영한다."""`로 바꾸고, import 영역을 아래로 교체:
```python
import asyncio
import logging
import os
import signal
import socket
import time
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from prometheus_client import start_http_server
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from board import cache, keys, posts_repo
from board.config import Settings
from board.infra import make_engine, make_redis
from board.metrics import refresh_queue_metrics
from board.observability import configure_logging

log = logging.getLogger("board.worker")

Entry = tuple[str, dict[str, str]]
RECLAIM_INTERVAL_SECONDS = 10
METRICS_INTERVAL_SECONDS = 5
INITIAL_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 5.0
```
(기존의 `log = ...`, `Entry = ...` 정의는 위 블록으로 대체된다.)

`Worker` 클래스 안, `dead_letter` 메서드 아래에 추가:
```python
    async def process_user_events_once(self) -> int:
        resp = await self.redis.xreadgroup(
            keys.USER_EVENTS_GROUP, self.name, {keys.USER_EVENTS: ">"}, count=self.settings.worker_batch_size
        )
        entries: list[Entry] = resp[0][1] if resp else []
        for mid, fields in entries:
            await self._handle_user_event(mid, fields)
        return len(entries)

    async def reclaim_user_events_once(self) -> int:
        result = await self.redis.xautoclaim(
            keys.USER_EVENTS,
            keys.USER_EVENTS_GROUP,
            self.name,
            min_idle_time=self.settings.claim_idle_ms,
            start_id="0-0",
            count=self.settings.worker_batch_size,
        )
        claimed: list[Entry] = [(mid, fields) for mid, fields in result[1] if fields]
        for mid, fields in claimed:
            await self._handle_user_event(mid, fields)
        return len(claimed)

    async def _handle_user_event(self, message_id: str, fields: dict[str, str]) -> None:
        if fields.get("type") == "user_deleted":
            try:
                user_id = str(uuid.UUID(fields["user_id"]))
            except (KeyError, ValueError):
                log.error("dropping invalid user event", extra={"extra_fields": {"message_id": message_id}})
            else:
                # DB 오류는 그대로 올려서 ACK 하지 않는다. 익명화는 멱등이라 reclaim 으로 다시 처리하면 된다.
                post_ids = await posts_repo.anonymize_author(self.engine, user_id)
                await cache.invalidate(self.redis, *post_ids)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xack(keys.USER_EVENTS, keys.USER_EVENTS_GROUP, message_id)
            pipe.xdel(keys.USER_EVENTS, message_id)
            await pipe.execute()

    async def run(self, stop: asyncio.Event) -> None:
        heartbeat = Path(self.settings.heartbeat_path)
        groups_ready = False
        backoff = INITIAL_BACKOFF_SECONDS
        last_reclaim = last_metrics = 0.0
        log.info("worker started", extra={"extra_fields": {"consumer": self.name}})
        while not stop.is_set():
            # DB/Redis 장애 중에도 heartbeat 를 갱신한다. 의존성 장애로 Pod 가 재시작되면 안 된다.
            heartbeat.touch()
            try:
                if not groups_ready:
                    await self.ensure_groups()
                    groups_ready = True
                await self.process_posts_once(block_ms=self.settings.worker_block_ms)
                await self.process_user_events_once()
                now = time.monotonic()
                if now - last_reclaim >= RECLAIM_INTERVAL_SECONDS:
                    await self.reclaim_posts_once()
                    await self.reclaim_user_events_once()
                    last_reclaim = now
                if now - last_metrics >= METRICS_INTERVAL_SECONDS:
                    await refresh_queue_metrics(self.redis)
                    last_metrics = now
                backoff = INITIAL_BACKOFF_SECONDS
            except Exception as exc:
                if isinstance(exc, ResponseError) and "NOGROUP" in str(exc):
                    # 영속성 없는 Redis 가 재시작되어 스트림/그룹이 사라졌다. 다음 반복에서 다시 만든다.
                    groups_ready = False
                log.warning(
                    "worker iteration failed, backing off",
                    extra={"extra_fields": {"error": repr(exc), "backoff_seconds": backoff}},
                )
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        log.info("worker stopped")
```

파일 끝(클래스 밖)에 추가:
```python
async def _run(settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    redis = make_redis(settings.redis_url)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    name = os.environ.get("HOSTNAME") or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
    try:
        await Worker(engine, redis, settings, name).run(stop)
    finally:
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    start_http_server(settings.worker_metrics_port)
    asyncio.run(_run(settings))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd services/board && uv run --extra dev pytest -v`
Expected: 모든 테스트 통과

- [ ] **Step 5: 커밋**

```bash
git add services/board
git commit -m "feat(board): worker handles user_deleted events, run loop, heartbeat, queue metrics"
```

---

### Task 11: Docker 이미지, docker-compose 스택, 백엔드 스모크 테스트

**Files:**
- Create: `.dockerignore`, `services/auth/Dockerfile`, `services/board/Dockerfile`, `docker-compose.yml`, `scripts/smoke_backend.py`
- Modify: `Makefile`

**Interfaces:**
- Consumes: Task 1~10의 모든 모듈
- Produces (계획 2, 3이 사용):
  - 이미지 `simple-web-app/auth:dev`, `simple-web-app/board:dev`. 저장소 루트를 빌드 컨텍스트로 쓴다(`docker build -f services/<svc>/Dockerfile .`). uid 10001 non-root, 포트 8000. board 이미지는 `/app/migrations`를 포함한다.
  - compose 서비스 `postgres`, `redis`, `migrate`, `auth`, `board-api`, `board-worker`(호스트 포트 없음). auth와 board-api는 소스를 마운트해 hot reload 한다.
  - Makefile 타깃 `dev`, `down`, `smoke-backend`, `test-backend`, `test`

- [ ] **Step 1: 스모크 스크립트 작성 (실패 확인용)**

`scripts/smoke_backend.py`:
```python
"""compose 네트워크 안(auth 컨테이너)에서 실행한다: 회원가입 → 로그인 → 글쓰기 → 워커 반영 확인.

nginx 가 붙여 주는 X-User-* 헤더는 직접 넣어서 흉내 낸다. (nginx 경유 테스트는 계획 2)
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.cookiejar import CookieJar
from urllib.parse import quote

AUTH = "http://auth:8000"
BOARD = "http://board-api:8000"
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def call(method: str, url: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict | None]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with opener.open(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


def wait_ready(url: str) -> None:
    for _ in range(60):
        try:
            if call("GET", f"{url}/readyz")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(1)
    sys.exit(f"not ready: {url}")


def main() -> None:
    wait_ready(AUTH)
    wait_ready(BOARD)
    suffix = uuid.uuid4().hex[:8]
    email, password, nickname = f"smoke-{suffix}@example.com", "smoke-password-1", f"sm{suffix}"

    status, user = call("POST", f"{AUTH}/api/auth/signup", {"email": email, "password": password, "nickname": nickname})
    assert status == 201, (status, user)
    status, _ = call("POST", f"{AUTH}/api/auth/login", {"email": email, "password": password})
    assert status == 200, status

    headers = {"X-User-Id": user["id"], "X-User-Nickname": quote(nickname, safe=""), "Idempotency-Key": str(uuid.uuid4())}
    status, created = call("POST", f"{BOARD}/api/board/posts", {"title": "smoke", "body": "hello"}, headers)
    assert status == 202, (status, created)

    for _ in range(20):
        status, detail = call("GET", f"{BOARD}/api/board/posts/{created['id']}")
        if status == 200 and detail["status"] == "published":
            print("SMOKE OK", created["id"])
            return
        time.sleep(0.5)
    sys.exit(f"post was not published: {detail}")


if __name__ == "__main__":
    main()
```

`Makefile`을 아래 전체 내용으로 교체:
```make
.PHONY: dev down smoke-backend test test-backend

dev:
	docker compose up --build

down:
	docker compose down

smoke-backend:
	docker compose up -d --build --wait postgres redis auth board-api board-worker
	docker compose exec -T auth python - < scripts/smoke_backend.py

test-backend:
	cd services/auth && uv run --extra dev pytest -q
	cd services/board && uv run --extra dev pytest -q

test: test-backend
```

- [ ] **Step 2: 실패 확인**

Run: `make smoke-backend`
Expected: FAIL, `no configuration file provided: not found` (docker-compose.yml 없음)

- [ ] **Step 3: Dockerfile과 compose 작성**

`.dockerignore`:
```
.git
**/.venv
**/__pycache__
**/.pytest_cache
**/node_modules
frontend/dist
docs
```

`services/auth/Dockerfile`:
```dockerfile
# 빌드 컨텍스트: 저장소 루트 (docker build -f services/auth/Dockerfile .)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY services/auth/pyproject.toml ./
COPY services/auth/src ./src
RUN pip install . \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app

USER 10001
EXPOSE 8000
CMD ["uvicorn", "auth.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
```

`services/board/Dockerfile`:
```dockerfile
# 빌드 컨텍스트: 저장소 루트 (docker build -f services/board/Dockerfile .)
# 같은 이미지를 board-api, board-worker(python -m board.worker), db-migrate(python -m board.migrate)가 함께 쓴다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MIGRATIONS_DIR=/app/migrations

WORKDIR /app
COPY services/board/pyproject.toml ./
COPY services/board/src ./src
RUN pip install . \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY db/migrations ./migrations

USER 10001
EXPOSE 8000 9100
CMD ["uvicorn", "board.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
```

`docker-compose.yml`:
```yaml
name: simple-web-app

x-backend-env: &backend-env
  DATABASE_URL: postgresql+asyncpg://app:app@postgres:5432/app
  REDIS_URL: redis://redis:6379/0
  COOKIE_SECURE: "false"
  LOG_LEVEL: INFO

x-board-build: &board-build
  context: .
  dockerfile: services/board/Dockerfile

services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
      POSTGRES_DB: app
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app -d app"]
      interval: 2s
      timeout: 3s
      retries: 30

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      timeout: 3s
      retries: 30

  migrate:
    build: *board-build
    image: simple-web-app/board:dev
    command: ["python", "-m", "board.migrate"]
    environment: *backend-env
    depends_on:
      postgres:
        condition: service_healthy

  auth:
    build:
      context: .
      dockerfile: services/auth/Dockerfile
    image: simple-web-app/auth:dev
    # 개발용: 소스를 마운트하고 PYTHONPATH 로 설치본보다 먼저 읽게 해서 hot reload 한다.
    command: ["uvicorn", "auth.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/src"]
    environment:
      <<: *backend-env
      PYTHONPATH: /app/src
    volumes:
      - ./services/auth/src:/app/src:ro
    depends_on:
      migrate:
        condition: service_completed_successfully
      redis:
        condition: service_healthy

  board-api:
    build: *board-build
    image: simple-web-app/board:dev
    command: ["uvicorn", "board.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/src"]
    environment:
      <<: *backend-env
      PYTHONPATH: /app/src
    volumes:
      - ./services/board/src:/app/src:ro
    depends_on:
      migrate:
        condition: service_completed_successfully
      redis:
        condition: service_healthy

  board-worker:
    build: *board-build
    image: simple-web-app/board:dev
    command: ["python", "-m", "board.worker"]
    environment:
      <<: *backend-env
      PYTHONPATH: /app/src
    volumes:
      - ./services/board/src:/app/src:ro
    depends_on:
      migrate:
        condition: service_completed_successfully
      redis:
        condition: service_healthy

volumes:
  pgdata: {}
```

- [ ] **Step 4: 스모크 테스트 통과 확인**

Run: `make smoke-backend`
Expected: 마지막 줄에 `SMOKE OK <uuid>` 출력

추가 확인:
Run: `docker compose exec -T board-worker python -m board.worker_health; echo $?`
Expected: `0`

Run: `docker compose exec -T board-worker python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:9100/metrics').read().decode())" | grep -E '^(queue_length|dlq_size)'`
Expected: `queue_length 0.0`, `dlq_size 0.0`

- [ ] **Step 5: 전체 테스트와 정리**

Run: `make test && docker compose down`
Expected: auth, board 모든 테스트 통과, compose 종료

- [ ] **Step 6: 커밋**

```bash
git add .dockerignore services/auth/Dockerfile services/board/Dockerfile docker-compose.yml scripts Makefile
git commit -m "feat: docker images, compose stack, and backend smoke test"
```

---

## 스펙 대응표 (계획 1 범위)

| 스펙 | 태스크 |
|---|---|
| §3 인증 API, 쿠키, 세션 | 3 |
| §3 verify(익명 / degraded), 헤더 인코딩 | 4 |
| §3 회원 탈퇴, 모든 세션 삭제, 이벤트 | 5 |
| §3 탈퇴 후 글 익명화 | 10 |
| §4 비동기 글쓰기, 멱등, 백프레셔, pending | 7 |
| §4 워커 배치 INSERT, XAUTOCLAIM, DLQ, failed | 9 |
| §4 캐시(3s / 30s / stale 60s), 스탬피드 방지, 커서 페이지네이션, 삭제 | 8 |
| §4 데이터 모델, 스키마 분리 | 1 |
| §6 에러 형식, 의존성 장애 시 동작 | 2, 4, 6, 7, 8 |
| §7 프로브(healthz / readyz 규칙, 워커 heartbeat), 정상 종료, 커넥션 풀 | 2, 6, 10 |
| §9 JSON 로그, X-Request-ID, /metrics, 큐·캐시 지표 | 2, 6, 8, 10 |
| §10 백엔드 테스트 (testcontainers, 장애 시나리오) | 전체 |
| §11 make dev / make test | 11 |
| §4 로그인 계정 리밋 | 4 |

범위 밖(다른 계획): nginx 라우팅·리밋·헤더 제거(계획 2), 프론트엔드(계획 2), k8s·HPA·KEDA·NetworkPolicy·부하 테스트(계획 3).

**알려진 한계:** 탈퇴 과정에서 DB 처리 후 Redis 단계가 실패하면 503을 반환한다. 사용자가 다시 요청하면 멱등하게 나머지 단계가 끝난다(Task 5 테스트로 확인). 재시도하지 않으면 해당 사용자의 기존 세션은 TTL(24시간)까지 남는다.
