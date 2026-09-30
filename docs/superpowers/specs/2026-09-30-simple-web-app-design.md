# simple-web-app 설계 문서

- 작성일: 2026-09-30
- 상태: 설계 확정, 구현 계획 작성 전

## 1. 목적과 범위

재난 상황에 대응하는 멀티클라우드 인프라(Terraform, AWS CLI, GCP CLI 기반 자동 배포, 별도 프로젝트 `one-click-deploy-k8s`)를 시연하기 위한 **대상 워크로드**다. 누구나 아무 글이나 쓰는 커뮤니티 게시판 하나로 구성한다.

재난 상황에서는 **읽기와 쓰기가 동시에, 짧은 시간에 폭증하고 이런 폭증이 반복된다**는 것을 전제로 설계한다.

### 기능 범위 (최소)
- 회원가입, 로그인, 로그아웃, 회원 탈퇴
- 게시글 목록(커서 페이지네이션), 상세, 작성, 본인 글 삭제
- 익명 사용자도 글을 읽을 수 있음. 작성과 삭제는 로그인 필요

### 범위 밖
- 댓글, 수정, 좋아요, 검색, 파일 업로드
- 클러스터·노드·매니지드 DB·LB·DNS·Prometheus 설치 등 인프라 프로비저닝 (`one-click-deploy-k8s` 담당)

### 설계 원칙
1. **앱 Pod는 상태를 갖지 않는다.** 세션은 Redis, 데이터는 PostgreSQL에 두고, Pod는 언제든 죽거나 늘어날 수 있다.
2. **클라우드에 종속되지 않는다.** 모든 접속 정보는 환경변수로 받고, 클라우드별 차이는 Kustomize overlay에만 둔다.
3. **장애가 번지지 않게 한다.** Redis와 DB 중 하나가 죽어도 서비스의 일부는 계속 동작한다.

## 2. 아키텍처

```
                  ┌──────────────── k8s cluster (EKS / GKE / kind) ────────────────┐
브라우저 ─► Ingress/LB ─► nginx (Deployment, HPA)
                          │  ├─ /              → React 빌드 결과(정적 파일, nginx 이미지에 포함)
                          │  ├─ /api/auth/*    → auth-svc
                          │  └─ /api/board/*   → auth_request(/_verify → auth-svc) → board-api
                          │
                          ├─ auth-svc     (FastAPI, HPA)  ── Redis(세션, 이벤트 발행), Postgres(auth.users)
                          ├─ board-api    (FastAPI, HPA)  ── Redis(캐시, 큐), Postgres(board.posts) 읽기
                          └─ board-worker (Python, 큐 길이 기반 확장) ── Redis 큐 소비 → Postgres(board.posts) 쓰기
                  └──────────────────────────────────────────────────────────────┘
Postgres / Redis: local overlay는 클러스터 내부 StatefulSet, aws/gcp overlay는 매니지드 서비스
                  (RDS / Cloud SQL, ElastiCache / Memorystore)를 환경변수 URL로 연결
```

### 컴포넌트
| 컴포넌트 | 기술 | 책임 |
|---|---|---|
| frontend + nginx | React + Vite + TypeScript, nginx | 멀티스테이지 빌드(Node로 빌드 → nginx 이미지에 정적 파일 포함). 라우팅, 인증 게이트웨이(`auth_request`), 요청 리밋, 압축, 캐시 헤더 |
| auth-svc | FastAPI, SQLAlchemy async(asyncpg), redis.asyncio, argon2 | 회원가입, 로그인, 로그아웃, 탈퇴, 세션 확인 |
| board-api | FastAPI, SQLAlchemy async, redis.asyncio | 게시글 조회(캐시), 작성(큐에 넣기), 삭제 |
| board-worker | Python asyncio (board-api와 같은 이미지, 실행 명령만 다름) | 글쓰기 큐와 사용자 이벤트 소비 → DB 반영 |
| db-migrate | board 이미지 기반 k8s Job | `db/migrations`의 SQL을 순서대로 실행(`auth`, `board` 스키마 모두). board 이미지는 저장소 루트를 빌드 컨텍스트로 써서 마이그레이션 파일을 포함한다 |
| postgres, redis | 공식 이미지 StatefulSet | local overlay 전용 |

### 디렉터리 구조
```
simple-web-app/
  frontend/            React + Vite + TS, Dockerfile(멀티스테이지 → nginx)
  nginx/               nginx.conf 템플릿 (리밋 수치, real_ip 대역은 환경변수/ConfigMap으로 주입)
  services/
    auth/              FastAPI 앱, Dockerfile, tests/
    board/             FastAPI 앱 + worker, Dockerfile, tests/
  db/migrations/       SQL 마이그레이션 파일
  k8s/
    base/              Deployment, Service, HPA, PDB, NetworkPolicy, ConfigMap, Job
    overlays/local/    postgres·redis StatefulSet, Secret 직접 생성, NodePort/Ingress
    overlays/aws/      ECR 이미지, ALB Ingress, Terraform이 만든 Secret 참조, LB 대역
    overlays/gcp/      Artifact Registry 이미지, GCE Ingress, Terraform이 만든 Secret 참조, LB 대역
  loadtest/            k6 재난 시나리오
  docker-compose.yml   로컬 개발용 전체 스택
  Makefile
```

## 3. 인증 (auth-svc)

nginx `auth_request` 게이트웨이 방식을 쓴다. board-api는 인증 로직을 모르고, nginx가 넘겨준 헤더만 믿는다.

### API
| 메서드 · 경로 | 설명 |
|---|---|
| `POST /api/auth/signup` `{email, password, nickname}` | argon2 해시(스레드풀에서 실행, 이벤트 루프가 막히지 않게) → `auth.users` INSERT → 201. 이메일·닉네임 중복 시 409 |
| `POST /api/auth/login` `{email, password}` | 해시 검증 → `sid = secrets.token_urlsafe(32)` → Redis `SET session:<sid> {user_id, nickname} EX 86400`, `SADD user_sessions:<user_id> <sid>` → `Set-Cookie: sid=<sid>; HttpOnly; Secure; SameSite=Lax; Path=/` |
| `POST /api/auth/logout` | `DEL session:<sid>`, `SREM user_sessions:<user_id> <sid>` → 쿠키 만료 → 204 |
| `GET /api/auth/me` | 세션이 있으면 `{user_id, email, nickname}`, 없으면 401 |
| `DELETE /api/auth/me` `{password}` | 아래 "회원 탈퇴" 참고 |
| `GET /internal/verify` | nginx `auth_request` 전용. 세션이 있으면 `200` + `X-User-Id`, `X-User-Nickname` 헤더. **세션이 없어도 헤더 없이 200.** 로그인 필수 여부는 board-api가 판단. **Redis 장애로 세션을 확인할 수 없으면 `200` + `X-Auth-Degraded: 1`** (nginx는 auth_request가 5xx를 받으면 요청 전체를 500으로 끝내므로, 읽기까지 막히지 않도록 200으로 응답하고 판단은 board-api에 넘긴다) |

로컬(HTTP)에서는 환경변수 `COOKIE_SECURE=false`로 `Secure` 속성을 끈다.

### 회원 탈퇴
1. 비밀번호를 다시 확인한다(쿠키만 탈취당했을 때 계정이 삭제되는 것을 막기 위해). 틀리면 403.
2. `auth.users`에 `deleted_at`을 기록하고, 이메일과 닉네임은 `deleted-<user_id>` 형태로 익명화해서 같은 이메일로 재가입할 수 있게 한다.
3. `SMEMBERS user_sessions:<user_id>`로 모든 세션을 찾아 삭제한다(모든 기기에서 로그아웃).
4. `XADD user:events {type: "user_deleted", user_id}`를 발행한다.
5. 쿠키를 만료시키고 204를 반환한다.

board-worker가 `user:events`를 받으면 해당 사용자의 posts를 `author_id = NULL`, `author_nickname = '탈퇴한 사용자'`로 **익명화해서 유지**하고, 목록·상세 캐시를 무효화한다. 재난 상황에서 올라온 정보성 글이 사라지지 않게 하기 위함이다.

### 헤더 위조 방지
- nginx는 클라이언트가 보낸 `X-User-Id`, `X-User-Nickname` 헤더를 항상 지우고, `auth_request` 결과로만 다시 채운다.
- `/internal/*`는 nginx에서 `internal` location으로만 접근할 수 있고, 외부에서 요청하면 404가 난다.
- NetworkPolicy로 auth-svc와 board-api는 nginx Pod에서 오는 요청만 받는다.

## 4. 게시판 (board-api, board-worker)

### API
| 메서드 · 경로 | 설명 |
|---|---|
| `GET /api/board/posts?cursor=<post_id>&limit=20` | 커서 페이지네이션(`id < cursor ORDER BY id DESC`). 응답 `{items, next_cursor}` |
| `GET /api/board/posts/{id}` | DB에 있으면 `{status: "published", post}`. 없고 Redis `pending:<id>`가 있으면 `{status: "pending"}`. Redis `failed:<id>`가 있으면 `{status: "failed"}`. 모두 없으면 404 |
| `POST /api/board/posts` `{title, body}` + `Idempotency-Key` 헤더 | 로그인 필수. 아래 "비동기 글쓰기" 참고 |
| `DELETE /api/board/posts/{id}` | 로그인 필수, 본인 글만 가능(아니면 403). 동기 soft delete → 캐시 무효화 → 204 |

입력 제한: 제목 1~100자, 본문 1~5000자.

### 비동기 글쓰기
```
POST /api/board/posts
 board-api:
   1. X-User-Id가 없으면 401
   2. X-Auth-Degraded 헤더가 있으면(세션 저장소 장애) 503
   3. GET idem:<user_id>:<key> → 이미 있으면 저장된 post_id로 202 반환
   4. XLEN posts:stream ≥ QUEUE_MAX_LEN(기본 50000)이면 503 + Retry-After: 5
   5. post_id = UUIDv7 생성
      SET idem:<user_id>:<key> <post_id> NX EX 600 → 실패하면(동시 재시도) 저장된 post_id로 202 반환
   6. XADD posts:stream {id, author_id, author_nickname, title, body, created_at}
      SET pending:<id> 1 EX 3600
      XADD가 실패하면 idem 키를 삭제하고 503 (재시도 시 새로 큐에 넣을 수 있게)
   7. 202 {id, status: "pending"}
 board-worker (consumer group "writers"):
   XREADGROUP COUNT 100 BLOCK 1000
   → bulk INSERT ... ON CONFLICT (id) DO NOTHING
   → XACK → 처리한 각 id의 pending:<id> 삭제 → cache:posts:first 삭제
```
- 워커가 죽어서 ACK하지 못한 메시지는 다른 워커가 `XAUTOCLAIM`(idle 30초 이상)으로 넘겨받는다. INSERT가 멱등이라 두 번 처리해도 안전하다.
- 5번 넘게 전달돼도 처리되지 않은 메시지는 `posts:dlq`로 옮기고, `failed:<id>`(TTL 1시간)를 기록하고, `pending:<id>`를 삭제하고, 에러 로그를 남긴 뒤 원래 스트림에서 ACK한다.
- 쓰기가 몰릴수록 배치 하나에 묶이는 건수가 커져 DB 부담이 줄어든다.
- 같은 워커가 `user:events` 스트림(consumer group "user-events")도 처리한다.

### 캐시 읽기
- 목록 첫 페이지(cursor 없음)는 Redis `cache:posts:first`에 **TTL 3초**로 저장한다. 두 번째 페이지부터는 DB를 직접 조회한다(인덱스를 타는 PK 범위 조회라 부담이 적다).
- 상세는 `cache:post:<id>`에 TTL 30초로 저장한다. 삭제·익명화 시 삭제한다.
- **캐시 스탬피드 방지**: 캐시가 비었으면 `SET lock:posts:first 1 NX EX 2`를 잡은 요청 하나만 DB를 조회해서 캐시를 채운다. 락을 못 잡은 요청은 50ms 간격으로 최대 5번 캐시를 다시 확인하고, 그래도 없으면 DB를 직접 조회한다.
- **만료된 캐시 활용**: 캐시 값을 저장할 때 TTL 60초짜리 사본 `stale:posts:first`도 함께 저장한다. DB 조회가 실패하면 이 사본을 반환한다.

### 데이터 모델
```sql
CREATE SCHEMA auth;
CREATE TABLE auth.users (
  id            UUID PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE,
  nickname      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at    TIMESTAMPTZ
);

CREATE SCHEMA board;
CREATE TABLE board.posts (
  id              UUID PRIMARY KEY,          -- UUIDv7, 시간순 정렬 가능
  author_id       UUID,                      -- 탈퇴 시 NULL
  author_nickname TEXT NOT NULL,             -- 비정규화, 서비스 간 JOIN 없음
  title           TEXT NOT NULL,
  body            TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL,
  deleted_at      TIMESTAMPTZ
);
CREATE INDEX posts_author_idx ON board.posts (author_id);
```
- auth-svc는 `auth` 스키마만, board-api와 board-worker는 `board` 스키마만 접근한다. DB 계정도 스키마별로 분리한다(local overlay에서는 편의상 하나의 계정 사용 가능).
- 서비스 간 외래 키는 두지 않는다. 나중에 DB를 물리적으로 분리해도 코드 변경이 필요 없다.
- 목록 조회는 `WHERE deleted_at IS NULL`, PK 역순으로 한다.

### Redis 키 정리
| 키 | 용도 | TTL |
|---|---|---|
| `session:<sid>` | 세션 | 24h |
| `user_sessions:<user_id>` | 사용자별 세션 목록 (Set) | 24h, 로그인할 때마다 갱신 |
| `login_fail:<email>` | 계정 기준 로그인 실패 카운터 | 15m |
| `posts:stream`, `posts:dlq` | 글쓰기 큐, 실패 큐 | - |
| `user:events` | 사용자 이벤트 스트림 | - (MAXLEN ~10000) |
| `idem:<user_id>:<key>` | 멱등 키 | 10m |
| `pending:<id>` | 게시 대기 상태 | 1h |
| `failed:<id>` | 게시 실패(DLQ 이동) 상태 | 1h |
| `cache:posts:first`, `stale:posts:first`, `cache:post:<id>` | 읽기 캐시 | 3s / 60s / 30s |
| `lock:posts:first` | 스탬피드 방지 락 | 2s |

## 5. 프론트엔드

- React + Vite + TypeScript, react-router, TanStack Query(서버 상태 관리, 폴링, 낙관적 업데이트).
- 화면: 목록, 상세, 글쓰기, 로그인, 회원가입, 내 정보(로그아웃, 탈퇴).
- 앱을 처음 띄울 때 `GET /api/auth/me`로 로그인 상태를 확인한다.
- 목록은 화면이 보이는 동안 5초 간격으로 폴링하고, 탭이 숨겨지면 멈춘다. WebSocket은 연결 상태를 유지해야 해서 확장이 어려우므로 쓰지 않는다.
- 글을 작성하면 목록 맨 위에 "게시 중…" 표시와 함께 먼저 보여준다(낙관적 업데이트). 이후 목록을 새로 불러왔을 때 실제 글로 바뀐다. 30초가 지나도 목록에 없으면 `GET /api/board/posts/{id}`로 상태를 확인해 `pending`(지연 중) 또는 `failed`(다시 시도 버튼)를 표시한다.
- 글 작성 폼을 열 때 `Idempotency-Key`(UUID)를 만들고, 재시도할 때도 같은 키를 쓴다.

## 6. 에러 처리

모든 API 에러는 `{"code": "<ERROR_CODE>", "message": "<사람이 읽는 설명>"}` 형식으로 반환한다.

| 상황 | 서버 | 프론트 |
|---|---|---|
| 401 로그인 필요 / 세션 만료 | `UNAUTHORIZED` | 로그인 화면으로 이동, 로그인 후 원래 화면으로 복귀 |
| 403 권한 없음 / 비밀번호 불일치 | `FORBIDDEN` | 메시지 표시 |
| 409 중복 가입 | `EMAIL_TAKEN`, `NICKNAME_TAKEN` | 폼 필드 에러 표시 |
| 422 입력 검증 실패 | `VALIDATION_ERROR` | 폼 필드 에러 표시 |
| 429 리밋 초과 | nginx가 JSON 에러 + `Retry-After` 반환 | 토스트 안내, 지수 백오프 + 지터로 최대 3회 자동 재시도 |
| 503 큐 가득 참 / 의존성 장애 | `QUEUE_FULL`, `UNAVAILABLE` + `Retry-After` | 429와 동일 |

### 의존성 장애 시 동작
| 장애 | auth-svc | board-api 읽기 | board-api 쓰기 |
|---|---|---|---|
| Redis 장애 | 로그인·로그아웃·탈퇴 503. verify는 `X-Auth-Degraded: 1`로 응답 | 익명으로 처리, 캐시를 건너뛰고 DB 직접 조회 | 503 (`X-Auth-Degraded` 또는 큐 쓰기 실패) |
| DB 장애 | 가입·로그인·탈퇴 503, 기존 세션 verify는 정상 | 만료된 캐시(stale) 반환, 없으면 503 | 정상 (큐에 쌓였다가 DB 복구 후 처리) |

## 7. k8s 구성

### 워크로드
| 워크로드 | 이미지 | 확장 기준 | min / max |
|---|---|---|---|
| nginx | `frontend` | HPA, CPU 60% | 2 / 10 |
| auth-svc | `auth` | HPA, CPU 60% | 2 / 20 |
| board-api | `board` | HPA, CPU 60% | 2 / 20 |
| board-worker | `board` | KEDA Redis Streams scaler(대기 메시지 수). KEDA가 없는 클러스터용으로 CPU HPA 대체 매니페스트 제공 | 1 / 10 |
| postgres, redis | 공식 이미지 | StatefulSet, local overlay 전용 | 1 |
| db-migrate | `board` | Job | - |

- uvicorn은 Pod당 프로세스 1개로 실행하고, 확장은 Pod 수로 한다.
- HPA behavior: scale-up은 `stabilizationWindowSeconds: 0`, 15초마다 최대 100% 증가. scale-down은 `stabilizationWindowSeconds: 300`.
- 모든 컨테이너에 resources requests/limits를 명시한다.
- 앱 워크로드마다 PDB `minAvailable: 1`.
- 노드 확장(cluster autoscaler)은 인프라 프로젝트가 담당한다.

### 프로브
- `livenessProbe: /healthz`: 프로세스 상태만 확인하고 외부 의존성은 보지 않는다(DB 장애가 Pod 연쇄 재시작으로 번지는 것 방지).
- `readinessProbe: /readyz`
  - auth-svc: Redis와 DB 모두 연결 가능해야 ready.
  - board-api: **Redis와 DB 중 하나라도 연결 가능하면 ready.** 둘 다 불가할 때만 트래픽에서 제외한다.
  - board-worker: HTTP 서버가 없으므로 heartbeat 파일 기반 exec liveness만 둔다.

### 정상 종료
- `preStop: sleep 5`, `terminationGracePeriodSeconds: 30`.
- board-worker는 SIGTERM을 받으면 새 메시지를 읽지 않고, 처리 중인 배치를 끝내 ACK한 뒤 종료한다.

### DB 커넥션
- Pod당 `pool_size=5`, `max_overflow=5`.
- 최대 커넥션 수 = (auth 20 + board-api 20 + worker 10) × 10 = 500. Postgres `max_connections`는 이 값보다 크게 설정하거나, 인프라 쪽에서 PgBouncer / RDS Proxy를 둔다. 이 계산은 README에 명시한다.

### 설정과 Secret
- 앱이 받는 환경변수: `DATABASE_URL`, `REDIS_URL`, `SESSION_TTL_SECONDS`, `QUEUE_MAX_LEN`, `COOKIE_SECURE`, `LOG_LEVEL`.
- local overlay는 Secret을 직접 생성한다. aws/gcp overlay는 **Terraform이 생성하는 Secret 이름(`app-db`, `app-redis`)을 참조만** 하고, 값 주입은 인프라 프로젝트가 담당한다.

### NetworkPolicy
- auth-svc, board-api: nginx Pod에서 오는 요청만 허용
- postgres, redis (local): 앱 Pod에서 오는 요청만 허용

## 8. nginx

### 라우팅
- `/`: 정적 파일, SPA fallback(`try_files $uri /index.html`)
- `/api/auth/`: auth-svc로 프록시
- `/api/board/`: `auth_request /_verify` → `auth_request_set`으로 받은 사용자 헤더를 붙여 board-api로 프록시
- `/_verify`: `internal`, auth-svc `/internal/verify`로 프록시(요청 body 제외)

### 요청 리밋
리밋의 목적은 **소수 클라이언트의 비정상적인 요청량이 전체 서비스를 망가뜨리는 것을 막는 것**이다(정상적인 트래픽 증가는 HPA, 쓰기 총량은 큐 백프레셔가 담당한다). 재난 상황에서는 통신사 CGNAT나 대피소 와이파이 때문에 많은 정상 사용자가 IP 하나를 공유하므로, **IP 기준 리밋은 느슨하게, 사용자 기준 리밋은 엄격하게** 건다.

| 대상 | 키 | 기본값 |
|---|---|---|
| 읽기 (`GET /api/board/*`) | 클라이언트 IP | 100r/s, burst 200 |
| 글쓰기·삭제 | `X-User-Id` (auth_request 결과). 키가 비어 있는 익명 요청은 리밋에 집계되지 않고 board-api가 401로 거절 | 2r/s, burst 5 |
| 로그인·회원가입 (IP 기준) | 클라이언트 IP | 30r/m, burst 10 |
| 로그인 (계정 기준) | 이메일, auth-svc가 Redis 카운터로 처리 | 15분에 실패 10회 초과 시 429 |

- 한도를 넘으면 JSON 에러 본문과 `Retry-After`를 포함한 429를 반환한다.
- LB 뒤의 실제 클라이언트 IP는 `real_ip_header X-Forwarded-For` + `set_real_ip_from <LB 대역>`으로 가져온다. LB 대역은 overlay별로 주입한다.
- 모든 수치는 ConfigMap으로 주입해서 부하 테스트 결과에 따라 조정한다.

### 기타
- upstream keepalive, gzip
- 해시가 붙은 정적 파일은 `Cache-Control: public, max-age=31536000, immutable`, `index.html`은 `no-cache`
- 클라이언트가 보낸 `X-User-*` 헤더는 항상 제거
- `$request_id`를 `X-Request-ID`로 upstream에 전달

## 9. 관측
- 모든 서비스는 `X-Request-ID`를 포함한 JSON 구조화 로그를 stdout으로 남긴다.
- 각 서비스는 `/metrics`(Prometheus 형식)를 노출한다. 기본 HTTP 지표에 더해 `queue_length`, `queue_lag_seconds`, `dlq_size`, `cache_hit_ratio`를 추가한다. `/metrics`는 nginx를 통해 외부로 노출하지 않는다.
- Prometheus·Grafana 설치는 인프라 프로젝트 담당이다.

## 10. 테스트
- **백엔드**: pytest + httpx AsyncClient. Postgres와 Redis는 testcontainers로 실제 인스턴스를 띄워 테스트한다(mock 사용 안 함).
  - auth: 회원가입 → 로그인 → verify → 로그아웃, 중복 가입, 잘못된 비밀번호, 계정 기준 로그인 리밋, 탈퇴(모든 세션 삭제, `user_deleted` 이벤트 발행, 같은 이메일로 재가입 가능)
  - board: 글쓰기 202 → 워커가 한 번 처리 → 목록에 나타남, 멱등 키, 큐 상한 도달 시 503, DLQ 이동, 본인 글만 삭제, 탈퇴 사용자 익명화, 커서 페이지네이션, 캐시 무효화
  - 장애 시나리오: Redis를 끊었을 때 읽기가 DB로 가는지, DB를 끊었을 때 쓰기가 큐로 가고 읽기가 stale 캐시를 반환하는지, readiness 판정
- **nginx 통합 테스트** (docker-compose 환경): 클라이언트가 보낸 `X-User-Id` 제거, `/_verify`와 `/internal/*` 외부 접근 시 404, 리밋 초과 시 429, SPA fallback
- **프론트**: vitest로 API 클라이언트(재시도·백오프·멱등 키)와 낙관적 업데이트 로직을 테스트한다. Playwright 스모크 테스트 1개: 회원가입 → 글쓰기 → 목록 확인 → 탈퇴.
- **k8s**: `kustomize build k8s/overlays/{local,aws,gcp} | kubeconform -strict`.

## 11. 실행 방법
| 명령 | 동작 |
|---|---|
| `make dev` | docker-compose로 전체 스택 실행, 백엔드 hot reload |
| `make fe-dev` | Vite dev server (API 요청은 compose의 nginx로 프록시) |
| `make test` | 백엔드, 프론트, nginx 통합 테스트 |
| `make k8s-local` | kind 클러스터 생성 → 이미지 로드 → `kubectl apply -k k8s/overlays/local` |
| `make loadtest` | k6 재난 시나리오 실행 |

### k6 재난 시나리오
- 평상시 트래픽 → **30초 만에 20배 폭증** → 5분 유지 → 감소 → 두 번째 폭증
- 요청 비율: 읽기 80%, 쓰기 15%, 로그인 5%
- 통과 기준: 읽기 p95 < 300ms, 쓰기 202 응답 p95 < 100ms, 5xx 비율 < 1% (백프레셔 503은 따로 집계)
- 테스트 중 `kubectl get hpa -w`로 Pod 확장을 관찰한다.

## 12. 인프라 프로젝트와의 계약
`one-click-deploy-k8s`가 이 앱을 배포하려면 다음을 제공해야 한다.
- 이미지 레지스트리(ECR / Artifact Registry)와 이미지 태그
- Secret `app-db`(`DATABASE_URL`), `app-redis`(`REDIS_URL`)
- Ingress 컨트롤러(ALB / GCE)와 LB 대역(nginx `set_real_ip_from`용)
- (선택) KEDA, metrics-server(HPA 필수), Prometheus

배포 순서: 이미지 빌드·푸시 → Secret 준비 → `db-migrate` Job 완료 대기 → `kubectl apply -k k8s/overlays/<cloud>`.
