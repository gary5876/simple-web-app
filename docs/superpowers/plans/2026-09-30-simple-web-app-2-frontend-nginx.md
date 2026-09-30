# simple-web-app 구현 계획 2/3: 프론트엔드 + nginx 게이트웨이

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** React SPA와 이를 서빙하는 nginx 게이트웨이(라우팅, `auth_request` 인증, 요청 리밋, 장애 시 JSON 응답)를 만들고, docker-compose 스택에서 통합 테스트와 E2E 스모크 테스트로 검증한다.

**Architecture:** 프론트엔드는 Vite로 빌드한 정적 파일이고, `nginxinc/nginx-unprivileged` 이미지 하나에 nginx 설정과 함께 담긴다. nginx는 `/api/auth/*`를 auth-svc로, `/api/board/*`를 `auth_request`로 세션을 확인한 뒤 board-api로 넘긴다. 프론트의 서버 상태는 TanStack Query가 관리하고, 429/503 재시도·멱등 키·낙관적 "게시 중" 표시는 작은 순수 함수 모듈로 분리해 단위 테스트한다.

**Tech Stack:** Node 22, React 18, TypeScript 5, Vite 5, react-router-dom 6, TanStack Query 5, Vitest 2 + Testing Library + jsdom, Playwright, nginx 1.27 (unprivileged), pytest + httpx (nginx 통합 테스트)

**Spec:** `docs/superpowers/specs/2026-09-30-simple-web-app-design.md` (§5 프론트엔드, §6 에러 처리, §8 nginx, §10 테스트)

**선행 조건:** 계획 1(백엔드)이 끝나 있어야 한다. 계획 1이 만든 `docker-compose.yml`(서비스 `postgres`, `redis`, `migrate`, `auth`, `board-api`, `board-worker`)과 `Makefile`(`dev`, `test-backend`, `test`)을 수정한다.

## Global Constraints

- 저장소 루트는 `simple-web-app/`. 프론트엔드 코드는 `frontend/`, nginx 설정은 `nginx/`, nginx 통합 테스트는 `tests/nginx/`.
- 프론트 이미지 빌드 컨텍스트는 **저장소 루트**, Dockerfile은 `frontend/Dockerfile`. 로컬 이미지 태그는 `simple-web-app/frontend:dev`.
- nginx는 `nginxinc/nginx-unprivileged:1.27-alpine`, 컨테이너 포트 **8080**, compose 호스트 포트 8080.
- `REAL_IP_FROM`은 **공백으로 구분한 CIDR 목록**이다(예: GCP `"130.211.0.0/22 35.191.0.0/16"`). nginx 템플릿에 직접 넣지 않고, 엔트리포인트 스크립트 `/docker-entrypoint.d/15-real-ip.sh`가 CIDR마다 `set_real_ip_from <cidr>;` 한 줄씩 `/etc/nginx/conf.d/real-ip.inc`에 써 두면 템플릿이 이 파일을 `include`한다.
- nginx 템플릿 환경변수와 기본값: `AUTH_UPSTREAM=auth:8000`, `BOARD_UPSTREAM=board-api:8000`, `REAL_IP_FROM=10.0.0.0/8`, `RATE_READ=100r/s`, `BURST_READ=200`, `RATE_WRITE=2r/s`, `BURST_WRITE=5`, `RATE_LOGIN=30r/m`, `BURST_LOGIN=10`.
- nginx는 `resolver`를 쓰지 않고 `upstream` 블록으로 시작 시점에 호스트 이름을 해석한다(upstream keepalive를 쓰기 위해서). k8s에서는 Service ClusterIP가 고정이라 문제가 없고, compose에서는 백엔드 컨테이너를 다시 만들면 nginx도 재시작해야 한다.
- 모든 API 에러 본문은 `{"code": string, "message": string}`. nginx가 직접 만드는 에러 코드는 `RATE_LIMITED`(429, `Retry-After: 2`), `UNAVAILABLE`(503, `Retry-After: 5`), `NOT_FOUND`(404).
- 클라이언트가 보낸 `X-User-Id`, `X-User-Nickname`, `X-Auth-Degraded` 헤더는 백엔드에 절대 그대로 전달하지 않는다.
- 글쓰기 리밋 키는 세션 쿠키 `sid`다(아래 "스펙과 다른 점" 참고).
- 프론트 폴링 간격 5초(탭이 숨겨지면 중지), 상태 확인 시작 30초, 재시도 최대 3회(429/503만), 백오프 `min(500·2^n, 8000)ms`와 `Retry-After` 중 큰 값 + 0~249ms 지터.
- UI 문구는 한국어, 코드 식별자는 영어. CSS는 `frontend/src/styles.css` 하나만 사용한다.
- 비밀번호 8~128자, 닉네임 2~20자, 제목 1~100자, 본문 1~5000자.

### 스펙과 다른 점 (구현 전에 알아둘 것)

1. **글쓰기 리밋 키를 `X-User-Id`가 아니라 `$cookie_sid`로 한다.** nginx의 `limit_req`는 PREACCESS 단계에서 실행되고 `auth_request`는 그 다음인 ACCESS 단계에서 실행된다. 따라서 `limit_req`가 실행되는 시점에는 `auth_request_set`으로 받은 `$auth_user_id`가 아직 비어 있다. 세션 쿠키도 로그인한 사용자당 하나라서 효과는 거의 같고, 위조한 쿠키는 인증에 실패해 board-api가 401로 거절한다.
2. **(계획 1과 합의됨)** board-api는 `X-Auth-Degraded`를 `X-User-Id`보다 먼저 확인해 503을 반환하고, Redis 장애 시 `GET /api/auth/me`는 503, 인증 실패 코드는 `UNAUTHORIZED`, 워커는 `NOGROUP` 시 consumer group을 다시 만든다. `test_degraded.py`는 이 동작을 전제로 한다.
3. **auth-svc 자체가 죽으면 `/api/board/*`는 읽기도 503을 반환한다.** `auth_request`가 5xx를 받으면 nginx는 요청 전체를 500으로 끝내기 때문이다. 그래서 이 500을 JSON 503 `UNAVAILABLE`로 바꿔 준다. Redis 장애는 auth-svc가 `X-Auth-Degraded`로 처리하므로 읽기가 계속 동작한다.

---

## 파일 구조

```
frontend/
  package.json, package-lock.json, tsconfig.json, vite.config.ts, index.html
  Dockerfile, Dockerfile.dockerignore
  playwright.config.ts
  e2e/smoke.spec.ts
  src/
    main.tsx                 진입점: QueryClient, Router
    App.tsx                  Provider와 라우트 조립
    App.test.tsx
    queryClient.ts           401 UNAUTHORIZED 전역 처리
    queryClient.test.ts
    styles.css
    api/
      types.ts               User, Post, PostPage, PostStatus
      client.ts              fetch 래퍼, ApiError, 재시도/백오프
      client.test.ts
      messages.ts            에러 코드 → 한국어 메시지
      messages.test.ts
      auth.ts                auth API 함수
      board.ts               board API 함수
    auth/
      useMe.ts               ME_KEY, useMe 훅
      redirect.ts            loginPath, safeNext
      redirect.test.ts
      RequireAuth.tsx
      RequireAuth.test.tsx
      LoginPage.tsx
      LoginPage.test.tsx
      SignupPage.tsx
      MePage.tsx
    board/
      keys.ts                POSTS_KEY, postKey
      pending.ts             "게시 중" 목록 순수 로직
      pending.test.ts
      PendingPostsContext.tsx 게시 대기 목록 상태 + 30초 상태 확인
      ListPage.tsx
      ListPage.test.tsx
      PostPage.tsx
      WritePage.tsx
      WritePage.test.tsx
    lib/id.ts                newId()
    ui/
      Layout.tsx             헤더 네비게이션
      Toast.tsx              재시도 안내 토스트
    test/
      setup.ts               jest-dom, cleanup
      http.ts                jsonResponse, stubFetch
      utils.tsx              createTestQueryClient, TEST_USER, LocationDisplay
  docker/15-real-ip.sh       REAL_IP_FROM 목록 → real-ip.inc 생성 (엔트리포인트 스크립트)
nginx/
  default.conf.template      http 컨텍스트 설정 (envsubst 대상)
  snippets/proxy_common.conf 공통 프록시 헤더
  snippets/auth_proxy.conf   auth-svc 프록시 공통 설정
tests/nginx/
  requirements.txt
  conftest.py
  test_config.py
  test_routing.py
  test_degraded.py
  test_ratelimit.py
scripts/wait-http.sh
docker-compose.yml           (수정) nginx 서비스 추가
Makefile                     (수정) fe-dev, test-frontend, test-nginx, test-e2e, test
.gitignore                   (수정)
```

---

### Task 1: 프론트엔드 프로젝트 골격 + API 클라이언트

**Files:**
- Create: `frontend/package.json`, `frontend/tsconfig.json`, `frontend/vite.config.ts`, `frontend/index.html`
- Create: `frontend/src/main.tsx`, `frontend/src/App.tsx`, `frontend/src/styles.css`
- Create: `frontend/src/api/types.ts`, `frontend/src/api/client.ts`, `frontend/src/api/messages.ts`
- Create: `frontend/src/test/setup.ts`, `frontend/src/test/http.ts`
- Test: `frontend/src/api/client.test.ts`, `frontend/src/api/messages.test.ts`
- Modify: `Makefile`, `.gitignore`

**Interfaces:**
- Consumes: 없음
- Produces:
  - `types.ts`: `User {id, email, nickname}`, `Post {id, author_id: string|null, author_nickname, title, body, created_at}`, `PostPage {items: Post[], next_cursor: string|null}`, `PostStatus` (`{status:'published', post}` | `{status:'pending'}` | `{status:'failed'}`)
  - `client.ts`: `class ApiError extends Error {status: number; code: string; retryAfter: number|null; fields: unknown}`, `computeBackoff(attempt: number, retryAfterSec: number|null, random: () => number): number`, `createClient(deps: ClientDeps): {request<T>(path: string, options?: RequestOptions): Promise<T>; onRetry(listener: RetryListener|null): void}`, `api` (기본 인스턴스), `MAX_RETRIES = 3`, 타입 `RequestOptions {method?, body?, headers?}`, `RetryInfo {attempt, delayMs, status}`
  - `messages.ts`: `errorMessage(err: unknown): string`
  - `test/http.ts`: `jsonResponse(status, body?, headers?): Response`, `stubFetch(handler: (url: string, init: RequestInit) => Response | Promise<Response>)`

- [ ] **Step 1: 프로젝트 설정 파일 작성**

`frontend/package.json`:
```json
{
  "name": "simple-web-app-frontend",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "typecheck": "tsc --noEmit",
    "test": "vitest run",
    "test:watch": "vitest",
    "e2e": "playwright test"
  },
  "dependencies": {
    "@tanstack/react-query": "^5.59.0",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-router-dom": "^6.28.0"
  },
  "devDependencies": {
    "@playwright/test": "^1.48.2",
    "@testing-library/dom": "^10.4.0",
    "@testing-library/jest-dom": "^6.6.2",
    "@testing-library/react": "^16.0.1",
    "@testing-library/user-event": "^14.5.2",
    "@types/node": "^22.9.0",
    "@types/react": "^18.3.12",
    "@types/react-dom": "^18.3.1",
    "@vitejs/plugin-react": "^4.3.3",
    "jsdom": "^25.0.1",
    "typescript": "^5.6.3",
    "vite": "^5.4.10",
    "vitest": "^2.1.4"
  }
}
```

`frontend/tsconfig.json`:
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noEmit": true,
    "skipLibCheck": true,
    "isolatedModules": true,
    "resolveJsonModule": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "types": ["vite/client", "node"]
  },
  "include": ["src", "e2e", "vite.config.ts", "playwright.config.ts"]
}
```

`frontend/vite.config.ts`:
```ts
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 개발 중에는 compose의 nginx(8080)를 거쳐 백엔드를 호출한다. 인증 흐름이 운영과 같아진다.
    proxy: { '/api': { target: 'http://localhost:8080' } },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
```

`frontend/index.html`:
```html
<!doctype html>
<html lang="ko">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>재난 커뮤니티</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

`frontend/src/main.tsx` (Task 4에서 Provider를 붙여 교체한다):
```tsx
import React from 'react'
import ReactDOM from 'react-dom/client'
import { App } from './App'
import './styles.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
```

`frontend/src/App.tsx` (Task 4에서 라우트를 붙여 교체한다):
```tsx
export function App() {
  return <h1>재난 커뮤니티</h1>
}
```

`frontend/src/styles.css`:
```css
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, -apple-system, 'Apple SD Gothic Neo', sans-serif; color: #1f2328; background: #f6f8fa; }
a { color: #0969da; text-decoration: none; }
.container { max-width: 720px; margin: 0 auto; padding: 0 16px 48px; }
.header { display: flex; justify-content: space-between; align-items: center; padding: 16px 0; }
.header nav { display: flex; gap: 12px; }
.brand { font-weight: 700; color: #1f2328; }
form { display: flex; flex-direction: column; gap: 12px; max-width: 480px; }
label { display: flex; flex-direction: column; gap: 4px; font-size: 14px; }
input, textarea { padding: 8px; border: 1px solid #d0d7de; border-radius: 6px; font: inherit; }
button { padding: 8px 14px; border: 1px solid #d0d7de; border-radius: 6px; background: #fff; cursor: pointer; font: inherit; }
button:disabled { opacity: 0.6; cursor: default; }
button.danger { border-color: #cf222e; color: #cf222e; }
.error { color: #cf222e; }
.posts { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 8px; }
.post { background: #fff; border: 1px solid #d0d7de; border-radius: 6px; padding: 12px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.post .title { font-weight: 600; flex: 1 1 auto; }
.post .meta, .meta { color: #656d76; font-size: 13px; }
.post.pending { border-style: dashed; }
.badge { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #fff8c5; }
.body { white-space: pre-wrap; background: #fff; border: 1px solid #d0d7de; border-radius: 6px; padding: 16px; }
.toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); background: #1f2328; color: #fff; padding: 10px 16px; border-radius: 6px; }
```

`frontend/src/api/types.ts`:
```ts
export interface User {
  id: string
  email: string
  nickname: string
}

export interface Post {
  id: string
  author_id: string | null
  author_nickname: string
  title: string
  body: string
  created_at: string
}

export interface PostPage {
  items: Post[]
  next_cursor: string | null
}

export type PostStatus =
  | { status: 'published'; post: Post }
  | { status: 'pending' }
  | { status: 'failed' }
```

`frontend/src/test/setup.ts`:
```ts
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})
```

`frontend/src/test/http.ts`:
```ts
import { vi } from 'vitest'

export function jsonResponse(status: number, body?: unknown, headers: Record<string, string> = {}): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

export function stubFetch(handler: (url: string, init: RequestInit) => Response | Promise<Response>) {
  const fn = vi.fn(async (url: string, init: RequestInit) => handler(url, init))
  vi.stubGlobal('fetch', fn)
  return fn
}
```

- [ ] **Step 2: 의존성 설치**

Run: `cd frontend && npm install`
Expected: `package-lock.json`이 생성되고 에러 없이 끝난다.

- [ ] **Step 3: API 클라이언트 실패 테스트 작성**

`frontend/src/api/client.test.ts`:
```ts
import { describe, expect, it, vi } from 'vitest'
import { ApiError, MAX_RETRIES, computeBackoff, createClient } from './client'
import { jsonResponse } from '../test/http'

function setup(responses: Response[]) {
  const fetch = vi.fn(async (_input: string, _init: RequestInit) => {
    const next = responses.shift()
    if (!next) throw new Error('no more responses')
    return next
  })
  const sleep = vi.fn(async (_ms: number) => {})
  const client = createClient({ fetch, sleep, random: () => 0 })
  return { client, fetch, sleep }
}

describe('computeBackoff', () => {
  it('grows exponentially from 500ms', () => {
    expect(computeBackoff(0, null, () => 0)).toBe(500)
    expect(computeBackoff(2, null, () => 0)).toBe(2000)
  })
  it('caps at 8000ms plus jitter', () => {
    expect(computeBackoff(10, null, () => 0.999)).toBe(8000 + 249)
  })
  it('honors Retry-After when it is longer', () => {
    expect(computeBackoff(0, 5, () => 0)).toBe(5000)
    expect(computeBackoff(3, 1, () => 0)).toBe(4000)
  })
})

describe('request', () => {
  it('returns parsed JSON on success', async () => {
    const { client, fetch } = setup([jsonResponse(200, { ok: true })])
    await expect(client.request('/api/x')).resolves.toEqual({ ok: true })
    expect(fetch).toHaveBeenCalledWith('/api/x', expect.objectContaining({ method: 'GET', credentials: 'same-origin' }))
  })

  it('returns undefined on 204', async () => {
    const { client } = setup([new Response(null, { status: 204 })])
    await expect(client.request('/api/x', { method: 'POST' })).resolves.toBeUndefined()
  })

  it('sends JSON bodies with a content type', async () => {
    const { client, fetch } = setup([jsonResponse(201, {})])
    await client.request('/api/x', { method: 'POST', body: { a: 1 } })
    const init = fetch.mock.calls[0][1]
    expect(init.body).toBe('{"a":1}')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
  })

  it('throws ApiError with the server error code', async () => {
    const { client } = setup([jsonResponse(409, { code: 'EMAIL_TAKEN', message: 'taken' })])
    const err = await client.request('/api/x').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err).toMatchObject({ status: 409, code: 'EMAIL_TAKEN', message: 'taken' })
  })

  it('falls back to HTTP_<status> for non-JSON errors', async () => {
    const { client } = setup([new Response('<html>bad gateway</html>', { status: 502 })])
    await expect(client.request('/api/x')).rejects.toMatchObject({ status: 502, code: 'HTTP_502' })
  })

  it('does not retry 500', async () => {
    const { client, fetch } = setup([jsonResponse(500, { code: 'INTERNAL', message: 'boom' })])
    await expect(client.request('/api/x')).rejects.toMatchObject({ status: 500 })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('retries 429 using Retry-After and notifies the listener', async () => {
    const { client, fetch, sleep } = setup([
      jsonResponse(429, { code: 'RATE_LIMITED', message: 'slow down' }, { 'Retry-After': '2' }),
      jsonResponse(200, { ok: true }),
    ])
    const listener = vi.fn()
    client.onRetry(listener)
    await expect(client.request('/api/x')).resolves.toEqual({ ok: true })
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(sleep).toHaveBeenCalledWith(2000)
    expect(listener).toHaveBeenCalledWith({ attempt: 1, delayMs: 2000, status: 429 })
  })

  it(`gives up after ${MAX_RETRIES} retries on 503`, async () => {
    const busy = () => jsonResponse(503, { code: 'QUEUE_FULL', message: 'busy' })
    const { client, fetch } = setup([busy(), busy(), busy(), busy()])
    await expect(client.request('/api/x')).rejects.toMatchObject({ status: 503, code: 'QUEUE_FULL' })
    expect(fetch).toHaveBeenCalledTimes(MAX_RETRIES + 1)
  })

  it('sends identical headers on every retry', async () => {
    const { client, fetch } = setup([
      jsonResponse(503, { code: 'QUEUE_FULL', message: 'busy' }),
      jsonResponse(202, { id: 'p1', status: 'pending' }),
    ])
    await client.request('/api/board/posts', { method: 'POST', body: {}, headers: { 'Idempotency-Key': 'k-1' } })
    const keys = fetch.mock.calls.map(([, init]) => (init.headers as Record<string, string>)['Idempotency-Key'])
    expect(keys).toEqual(['k-1', 'k-1'])
  })
})
```

`frontend/src/api/messages.test.ts`:
```ts
import { describe, expect, it } from 'vitest'
import { ApiError } from './client'
import { errorMessage } from './messages'

describe('errorMessage', () => {
  it('maps known codes to Korean messages', () => {
    expect(errorMessage(new ApiError(409, 'EMAIL_TAKEN', 'x'))).toBe('이미 가입된 이메일입니다.')
  })
  it('falls back to the status for unknown codes', () => {
    expect(errorMessage(new ApiError(418, 'TEAPOT', 'x'))).toBe('오류가 발생했습니다 (418).')
  })
  it('treats non-API errors as network errors', () => {
    expect(errorMessage(new TypeError('Failed to fetch'))).toBe('네트워크 오류가 발생했습니다.')
  })
})
```

- [ ] **Step 4: 테스트가 실패하는지 확인**

Run: `cd frontend && npx vitest run src/api`
Expected: FAIL — `Failed to resolve import "./client"` / `"./messages"`

- [ ] **Step 5: 클라이언트와 메시지 구현**

`frontend/src/api/client.ts`:
```ts
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly retryAfter: number | null
  readonly fields: unknown

  constructor(status: number, code: string, message: string, retryAfter: number | null = null, fields?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.retryAfter = retryAfter
    this.fields = fields
  }
}

export type FetchLike = (input: string, init: RequestInit) => Promise<Response>

export interface ClientDeps {
  fetch: FetchLike
  sleep: (ms: number) => Promise<void>
  random: () => number
}

export interface RequestOptions {
  method?: string
  body?: unknown
  headers?: Record<string, string>
}

export interface RetryInfo {
  attempt: number
  delayMs: number
  status: number
}

export type RetryListener = (info: RetryInfo) => void

export const MAX_RETRIES = 3
const RETRYABLE_STATUS = new Set([429, 503])
const BASE_DELAY_MS = 500
const MAX_DELAY_MS = 8000
const JITTER_MS = 250

export function computeBackoff(attempt: number, retryAfterSec: number | null, random: () => number): number {
  const exponential = Math.min(BASE_DELAY_MS * 2 ** attempt, MAX_DELAY_MS)
  const base = retryAfterSec === null ? exponential : Math.max(retryAfterSec * 1000, exponential)
  return base + Math.floor(random() * JITTER_MS)
}

export function createClient(deps: ClientDeps) {
  let retryListener: RetryListener | null = null

  async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json', ...options.headers }
    let body: string | undefined
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json'
      body = JSON.stringify(options.body)
    }

    for (let attempt = 0; ; attempt++) {
      const res = await deps.fetch(path, { method: options.method ?? 'GET', headers, body, credentials: 'same-origin' })
      if (res.ok) {
        if (res.status === 204) return undefined as T
        return (await res.json()) as T
      }
      const error = await toApiError(res)
      if (!RETRYABLE_STATUS.has(res.status) || attempt >= MAX_RETRIES) throw error
      const delayMs = computeBackoff(attempt, error.retryAfter, deps.random)
      retryListener?.({ attempt: attempt + 1, delayMs, status: res.status })
      await deps.sleep(delayMs)
    }
  }

  function onRetry(listener: RetryListener | null): void {
    retryListener = listener
  }

  return { request, onRetry }
}

async function toApiError(res: Response): Promise<ApiError> {
  const header = res.headers.get('Retry-After')
  const retryAfter = header !== null && /^\d+$/.test(header) ? Number(header) : null
  let code = `HTTP_${res.status}`
  let message = res.statusText || 'Request failed'
  let fields: unknown
  try {
    const data: unknown = await res.json()
    if (isErrorBody(data)) {
      code = data.code
      message = data.message ?? message
      fields = data.fields
    }
  } catch {
    // 본문이 JSON이 아니면(예: LB가 만든 HTML 502) 기본값을 쓴다.
  }
  return new ApiError(res.status, code, message, retryAfter, fields)
}

function isErrorBody(value: unknown): value is { code: string; message?: string; fields?: unknown } {
  return typeof value === 'object' && value !== null && typeof (value as { code?: unknown }).code === 'string'
}

export const api = createClient({
  fetch: (input, init) => fetch(input, init),
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  random: Math.random,
})
```

`frontend/src/api/messages.ts`:
```ts
import { ApiError } from './client'

const MESSAGES: Record<string, string> = {
  INVALID_CREDENTIALS: '이메일 또는 비밀번호가 올바르지 않습니다.',
  TOO_MANY_ATTEMPTS: '로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.',
  RATE_LIMITED: '요청이 너무 많습니다. 잠시 후 다시 시도하세요.',
  QUEUE_FULL: '지금 글이 너무 많이 몰리고 있습니다. 잠시 후 다시 시도하세요.',
  UNAVAILABLE: '서비스가 일시적으로 불안정합니다. 잠시 후 다시 시도하세요.',
  EMAIL_TAKEN: '이미 가입된 이메일입니다.',
  NICKNAME_TAKEN: '이미 사용 중인 닉네임입니다.',
  FORBIDDEN: '권한이 없거나 비밀번호가 올바르지 않습니다.',
  UNAUTHORIZED: '로그인이 필요합니다.',
  NOT_FOUND: '찾을 수 없습니다.',
  VALIDATION_ERROR: '입력값을 확인해 주세요.',
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return MESSAGES[err.code] ?? `오류가 발생했습니다 (${err.status}).`
  return '네트워크 오류가 발생했습니다.'
}
```

- [ ] **Step 6: 테스트 통과와 빌드 확인**

Run: `cd frontend && npx vitest run src/api && npm run build`
Expected: 테스트 전부 PASS, `dist/index.html`과 `dist/assets/*.js` 생성

- [ ] **Step 7: Makefile과 .gitignore 수정**

`Makefile`에 아래 타깃을 추가하고 `.PHONY` 줄에 `fe-dev test-frontend`를 추가한다. 레시피 줄은 반드시 **탭**으로 시작해야 한다.
```make
fe-dev:
	cd frontend && npm run dev

test-frontend:
	cd frontend && npm ci && npm run typecheck && npm test
```

`.gitignore`에 추가한다(파일이 없으면 만든다):
```
frontend/node_modules/
frontend/dist/
frontend/test-results/
frontend/playwright-report/
tests/nginx/.venv/
```

Run: `make test-frontend`
Expected: typecheck 통과, 테스트 PASS

- [ ] **Step 8: 커밋**

```bash
git add frontend Makefile .gitignore
git commit -m "feat(frontend): scaffold Vite app with retrying API client"
```

---

### Task 2: 인증 화면과 로그인 상태 처리

**Files:**
- Create: `frontend/src/api/auth.ts`, `frontend/src/auth/useMe.ts`, `frontend/src/auth/redirect.ts`, `frontend/src/auth/RequireAuth.tsx`, `frontend/src/auth/LoginPage.tsx`, `frontend/src/auth/SignupPage.tsx`, `frontend/src/auth/MePage.tsx`
- Create: `frontend/src/queryClient.ts`, `frontend/src/ui/Layout.tsx`, `frontend/src/ui/Toast.tsx`, `frontend/src/test/utils.tsx`
- Test: `frontend/src/auth/redirect.test.ts`, `frontend/src/auth/RequireAuth.test.tsx`, `frontend/src/auth/LoginPage.test.tsx`, `frontend/src/queryClient.test.ts`

**Interfaces:**
- Consumes: Task 1의 `api`, `ApiError`, `MAX_RETRIES`, `errorMessage`, `User`, `jsonResponse`, `stubFetch`
- Produces:
  - `authApi.signup(input: {email, password, nickname}): Promise<User>`, `authApi.login(input: {email, password}): Promise<User>`, `authApi.logout(): Promise<void>`, `authApi.me(): Promise<User | null>` (401이면 null), `authApi.deleteAccount(password: string): Promise<void>`
  - `ME_KEY = ['me']`, `useMe()` → `UseQueryResult<User | null>`
  - `loginPath(next: string): string`, `safeNext(raw: string | null): string`
  - `<RequireAuth>{children}</RequireAuth>`
  - `LoginPage`, `SignupPage`, `MePage` 컴포넌트
  - `createQueryClient(onUnauthorized: () => void): QueryClient`, `isUnauthorized(err: unknown): boolean`
  - `<Layout>`, `<ToastProvider>`
  - 테스트 유틸: `createTestQueryClient(me?: User | null)`, `TEST_USER`, `<LocationDisplay />` (`data-testid="location"`)

- [ ] **Step 1: 테스트 유틸 작성**

`frontend/src/test/utils.tsx`:
```tsx
import { QueryClient } from '@tanstack/react-query'
import { useLocation } from 'react-router-dom'
import type { User } from '../api/types'
import { ME_KEY } from '../auth/useMe'

export const TEST_USER: User = {
  id: '01920000-0000-7000-8000-000000000001',
  email: 'user@example.com',
  nickname: '테스터',
}

export function createTestQueryClient(me?: User | null): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  if (me !== undefined) client.setQueryData(ME_KEY, me)
  return client
}

export function LocationDisplay() {
  const location = useLocation()
  return <div data-testid="location">{location.pathname + location.search}</div>
}
```

- [ ] **Step 2: 실패 테스트 작성**

`frontend/src/auth/redirect.test.ts`:
```ts
import { describe, expect, it } from 'vitest'
import { loginPath, safeNext } from './redirect'

describe('loginPath', () => {
  it('encodes the return path', () => {
    expect(loginPath('/write?draft=1')).toBe('/login?next=%2Fwrite%3Fdraft%3D1')
  })
})

describe('safeNext', () => {
  it('accepts same-origin paths', () => {
    expect(safeNext('/write')).toBe('/write')
  })
  it.each([null, '', '//evil.example.com', 'https://evil.example.com'])('rejects %s', (raw) => {
    expect(safeNext(raw)).toBe('/')
  })
})
```

`frontend/src/auth/RequireAuth.test.tsx`:
```tsx
import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { RequireAuth } from './RequireAuth'
import { LocationDisplay, TEST_USER, createTestQueryClient } from '../test/utils'

function renderAt(path: string, me: typeof TEST_USER | null) {
  render(
    <QueryClientProvider client={createTestQueryClient(me)}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/write" element={<RequireAuth><p>비밀 화면</p></RequireAuth>} />
          <Route path="/login" element={<LocationDisplay />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('RequireAuth', () => {
  it('redirects guests to login with the return path', async () => {
    renderAt('/write?draft=1', null)
    expect(await screen.findByTestId('location')).toHaveTextContent('/login?next=%2Fwrite%3Fdraft%3D1')
    expect(screen.queryByText('비밀 화면')).not.toBeInTheDocument()
  })

  it('renders children for logged-in users', () => {
    renderAt('/write', TEST_USER)
    expect(screen.getByText('비밀 화면')).toBeInTheDocument()
  })
})
```

`frontend/src/auth/LoginPage.test.tsx`:
```tsx
import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { LoginPage } from './LoginPage'
import { ME_KEY } from './useMe'
import { jsonResponse, stubFetch } from '../test/http'
import { LocationDisplay, TEST_USER, createTestQueryClient } from '../test/utils'

function renderLogin(client = createTestQueryClient(null)) {
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/login?next=%2Fwrite']}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/write" element={<LocationDisplay />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return client
}

async function submit() {
  await userEvent.type(screen.getByLabelText('이메일'), 'user@example.com')
  await userEvent.type(screen.getByLabelText('비밀번호'), 'password123')
  await userEvent.click(screen.getByRole('button', { name: '로그인' }))
}

describe('LoginPage', () => {
  it('stores the user and returns to the next path', async () => {
    const fetch = stubFetch(() => jsonResponse(200, TEST_USER))
    const client = renderLogin()
    await submit()
    expect(await screen.findByTestId('location')).toHaveTextContent('/write')
    expect(client.getQueryData(ME_KEY)).toEqual(TEST_USER)
    expect(fetch).toHaveBeenCalledWith('/api/auth/login', expect.objectContaining({ method: 'POST' }))
  })

  it('shows a friendly message for wrong credentials', async () => {
    stubFetch(() => jsonResponse(401, { code: 'INVALID_CREDENTIALS', message: 'bad' }))
    renderLogin()
    await submit()
    expect(await screen.findByRole('alert')).toHaveTextContent('이메일 또는 비밀번호가 올바르지 않습니다.')
  })
})
```

`frontend/src/queryClient.test.ts`:
```ts
import { MutationObserver } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from './api/client'
import { ME_KEY } from './auth/useMe'
import { createQueryClient } from './queryClient'
import { TEST_USER } from './test/utils'

async function failMutation(error: ApiError, onUnauthorized: () => void) {
  const client = createQueryClient(onUnauthorized)
  client.setQueryData(ME_KEY, TEST_USER)
  const observer = new MutationObserver(client, { mutationFn: () => Promise.reject(error) })
  await observer.mutate().catch(() => undefined)
  return client
}

describe('createQueryClient', () => {
  it('clears the user and calls onUnauthorized for UNAUTHORIZED', async () => {
    const onUnauthorized = vi.fn()
    const client = await failMutation(new ApiError(401, 'UNAUTHORIZED', 'x'), onUnauthorized)
    expect(onUnauthorized).toHaveBeenCalledOnce()
    expect(client.getQueryData(ME_KEY)).toBeNull()
  })

  it('ignores other 401 codes such as INVALID_CREDENTIALS', async () => {
    const onUnauthorized = vi.fn()
    const client = await failMutation(new ApiError(401, 'INVALID_CREDENTIALS', 'x'), onUnauthorized)
    expect(onUnauthorized).not.toHaveBeenCalled()
    expect(client.getQueryData(ME_KEY)).toEqual(TEST_USER)
  })
})
```

- [ ] **Step 3: 테스트가 실패하는지 확인**

Run: `cd frontend && npx vitest run src/auth src/queryClient.test.ts`
Expected: FAIL — `./redirect`, `./RequireAuth`, `./LoginPage`, `./useMe`, `./queryClient`를 찾을 수 없음

- [ ] **Step 4: 인증 모듈 구현**

`frontend/src/api/auth.ts`:
```ts
import { ApiError, api } from './client'
import type { User } from './types'

export const authApi = {
  signup: (input: { email: string; password: string; nickname: string }) =>
    api.request<User>('/api/auth/signup', { method: 'POST', body: input }),
  login: (input: { email: string; password: string }) =>
    api.request<User>('/api/auth/login', { method: 'POST', body: input }),
  logout: () => api.request<void>('/api/auth/logout', { method: 'POST' }),
  me: async (): Promise<User | null> => {
    try {
      return await api.request<User>('/api/auth/me')
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return null
      throw err
    }
  },
  deleteAccount: (password: string) => api.request<void>('/api/auth/me', { method: 'DELETE', body: { password } }),
}
```

`frontend/src/auth/useMe.ts`:
```ts
import { useQuery } from '@tanstack/react-query'
import { authApi } from '../api/auth'

export const ME_KEY = ['me'] as const

export function useMe() {
  return useQuery({ queryKey: ME_KEY, queryFn: authApi.me, staleTime: 60_000, retry: false })
}
```

`frontend/src/auth/redirect.ts`:
```ts
export function loginPath(next: string): string {
  return `/login?next=${encodeURIComponent(next)}`
}

// 다른 사이트로 튕기는 오픈 리다이렉트를 막기 위해 같은 출처의 경로만 허용한다.
export function safeNext(raw: string | null): string {
  if (!raw || !raw.startsWith('/') || raw.startsWith('//')) return '/'
  return raw
}
```

`frontend/src/auth/RequireAuth.tsx`:
```tsx
import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { loginPath } from './redirect'
import { useMe } from './useMe'

export function RequireAuth({ children }: { children: ReactNode }) {
  const { data: me, isPending } = useMe()
  const location = useLocation()
  if (isPending) return <p>불러오는 중…</p>
  if (!me) return <Navigate to={loginPath(location.pathname + location.search)} replace />
  return <>{children}</>
}
```

`frontend/src/auth/LoginPage.tsx`:
```tsx
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { authApi } from '../api/auth'
import { errorMessage } from '../api/messages'
import { safeNext } from './redirect'
import { ME_KEY } from './useMe'

export function LoginPage() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const login = useMutation({
    mutationFn: authApi.login,
    onSuccess: (user) => {
      queryClient.setQueryData(ME_KEY, user)
      navigate(safeNext(params.get('next')), { replace: true })
    },
  })

  return (
    <section>
      <h1>로그인</h1>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          login.mutate({ email, password })
        }}
      >
        <label>
          이메일
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email" />
        </label>
        <label>
          비밀번호
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="current-password" />
        </label>
        {login.isError && <p className="error" role="alert">{errorMessage(login.error)}</p>}
        <button type="submit" disabled={login.isPending}>로그인</button>
      </form>
      <p>
        계정이 없나요? <Link to="/signup">회원가입</Link>
      </p>
    </section>
  )
}
```

`frontend/src/auth/SignupPage.tsx`:
```tsx
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authApi } from '../api/auth'
import { errorMessage } from '../api/messages'
import { ME_KEY } from './useMe'

export function SignupPage() {
  const [email, setEmail] = useState('')
  const [nickname, setNickname] = useState('')
  const [password, setPassword] = useState('')
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const signup = useMutation({
    mutationFn: async () => {
      await authApi.signup({ email, password, nickname })
      return authApi.login({ email, password })
    },
    onSuccess: (user) => {
      queryClient.setQueryData(ME_KEY, user)
      navigate('/', { replace: true })
    },
  })

  return (
    <section>
      <h1>회원가입</h1>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          signup.mutate()
        }}
      >
        <label>
          이메일
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email" />
        </label>
        <label>
          닉네임
          <input value={nickname} onChange={(e) => setNickname(e.target.value)} required minLength={2} maxLength={20} />
        </label>
        <label>
          비밀번호
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={8} maxLength={128} autoComplete="new-password" />
        </label>
        {signup.isError && <p className="error" role="alert">{errorMessage(signup.error)}</p>}
        <button type="submit" disabled={signup.isPending}>가입하기</button>
      </form>
    </section>
  )
}
```

`frontend/src/auth/MePage.tsx`:
```tsx
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authApi } from '../api/auth'
import { errorMessage } from '../api/messages'
import { ME_KEY, useMe } from './useMe'

export function MePage() {
  const { data: me } = useMe()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [password, setPassword] = useState('')

  // 먼저 이동한 뒤 사용자 정보를 지운다. 순서가 반대면 RequireAuth가 /login으로 보내 버린다.
  const signOut = () => {
    navigate('/', { replace: true })
    queryClient.setQueryData(ME_KEY, null)
  }
  const logout = useMutation({ mutationFn: () => authApi.logout(), onSuccess: signOut })
  const remove = useMutation({ mutationFn: (pw: string) => authApi.deleteAccount(pw), onSuccess: signOut })

  if (!me) return null

  return (
    <section>
      <h1>내 정보</h1>
      <dl>
        <dt>닉네임</dt>
        <dd>{me.nickname}</dd>
        <dt>이메일</dt>
        <dd>{me.email}</dd>
      </dl>
      <button onClick={() => logout.mutate()} disabled={logout.isPending}>로그아웃</button>

      <h2>회원 탈퇴</h2>
      <p>탈퇴해도 작성한 글은 '탈퇴한 사용자' 이름으로 남습니다.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          remove.mutate(password)
        }}
      >
        <label>
          비밀번호 확인
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="current-password" />
        </label>
        {remove.isError && <p className="error" role="alert">{errorMessage(remove.error)}</p>}
        <button type="submit" className="danger" disabled={remove.isPending}>회원 탈퇴</button>
      </form>
    </section>
  )
}
```

`frontend/src/queryClient.ts`:
```ts
import { MutationCache, QueryClient } from '@tanstack/react-query'
import { ApiError } from './api/client'
import { ME_KEY } from './auth/useMe'

export function isUnauthorized(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401 && err.code === 'UNAUTHORIZED'
}

export function createQueryClient(onUnauthorized: () => void): QueryClient {
  const client: QueryClient = new QueryClient({
    // 429/503 재시도는 api 클라이언트가 이미 하므로 Query 차원의 재시도는 끈다.
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    mutationCache: new MutationCache({
      onError: (err) => {
        if (!isUnauthorized(err)) return
        client.setQueryData(ME_KEY, null)
        onUnauthorized()
      },
    }),
  })
  return client
}
```

`frontend/src/ui/Layout.tsx`:
```tsx
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useMe } from '../auth/useMe'

export function Layout({ children }: { children: ReactNode }) {
  const { data: me } = useMe()
  return (
    <div className="container">
      <header className="header">
        <Link to="/" className="brand">재난 커뮤니티</Link>
        <nav>
          {me ? (
            <>
              <Link to="/write">글쓰기</Link>
              <Link to="/me">내 정보</Link>
            </>
          ) : (
            <>
              <Link to="/login">로그인</Link>
              <Link to="/signup">회원가입</Link>
            </>
          )}
        </nav>
      </header>
      <main>{children}</main>
    </div>
  )
}
```

`frontend/src/ui/Toast.tsx`:
```tsx
import { useEffect, useState, type ReactNode } from 'react'
import { MAX_RETRIES, api } from '../api/client'

export function ToastProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState<string | null>(null)

  useEffect(() => {
    api.onRetry(({ attempt }) => setMessage(`요청이 많아 잠시 후 자동으로 다시 시도합니다 (${attempt}/${MAX_RETRIES})`))
    return () => api.onRetry(null)
  }, [])

  useEffect(() => {
    if (!message) return
    const timer = setTimeout(() => setMessage(null), 3000)
    return () => clearTimeout(timer)
  }, [message])

  return (
    <>
      {children}
      {message && <div className="toast" role="status">{message}</div>}
    </>
  )
}
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `cd frontend && npx vitest run && npm run typecheck`
Expected: 전부 PASS, 타입 에러 없음

- [ ] **Step 6: 커밋**

```bash
git add frontend/src
git commit -m "feat(frontend): add auth pages, login guard and 401 handling"
```

---

### Task 3: 게시글 목록·상세와 "게시 중" 상태 관리

**Files:**
- Create: `frontend/src/api/board.ts`, `frontend/src/board/keys.ts`, `frontend/src/board/pending.ts`, `frontend/src/board/PendingPostsContext.tsx`, `frontend/src/board/ListPage.tsx`, `frontend/src/board/PostPage.tsx`, `frontend/src/lib/id.ts`
- Test: `frontend/src/board/pending.test.ts`, `frontend/src/board/ListPage.test.tsx`

**Interfaces:**
- Consumes: Task 1의 `api`, `ApiError`, `errorMessage`, 타입들 / Task 2의 `useMe`, `createTestQueryClient`, `stubFetch`, `jsonResponse`
- Produces:
  - `boardApi.list(cursor?: string | null): Promise<PostPage>`, `boardApi.get(id: string): Promise<PostStatus>`, `boardApi.create(input: {title, body}, idempotencyKey: string): Promise<{id: string; status: 'pending'}>`, `boardApi.remove(id: string): Promise<void>`
  - `POSTS_KEY = ['posts']`, `postKey(id) = ['post', id]`
  - `pending.ts`: `type PendingState = 'pending' | 'delayed' | 'failed'`, `interface PendingPost {id, title, body, author_nickname, submittedAt: number, state: PendingState}`, `STATUS_CHECK_AFTER_MS = 30_000`, `PENDING_LABEL`, `upsertPending`, `visiblePending`, `dueForStatusCheck`, `applyStatus`
  - `<PendingPostsProvider initialPendings? checkIntervalMs?>`, `usePendingPosts(): {pendings, add(p), replace(oldId, p), prune(serverIds: Set<string>)}`
  - `ListPage`, `PostPage`, `POLL_INTERVAL_MS = 5000`
  - `newId(): string`

- [ ] **Step 1: 순수 로직 실패 테스트 작성**

`frontend/src/board/pending.test.ts`:
```ts
import { describe, expect, it } from 'vitest'
import { STATUS_CHECK_AFTER_MS, applyStatus, dueForStatusCheck, upsertPending, visiblePending, type PendingPost } from './pending'

const NOW = 1_000_000

function pending(id: string, overrides: Partial<PendingPost> = {}): PendingPost {
  return { id, title: `title ${id}`, body: 'body', author_nickname: '테스터', submittedAt: NOW, state: 'pending', ...overrides }
}

describe('upsertPending', () => {
  it('adds new posts to the top without duplicates', () => {
    const list = [pending('a'), pending('b')]
    expect(upsertPending(list, pending('b', { title: 'new' })).map((p) => [p.id, p.title])).toEqual([
      ['b', 'new'],
      ['a', 'title a'],
    ])
  })
})

describe('visiblePending', () => {
  it('hides posts that the server already returned', () => {
    expect(visiblePending([pending('a'), pending('b')], new Set(['a'])).map((p) => p.id)).toEqual(['b'])
  })
})

describe('dueForStatusCheck', () => {
  it('returns non-failed posts older than 30 seconds', () => {
    const list = [
      pending('fresh', { submittedAt: NOW - STATUS_CHECK_AFTER_MS + 1 }),
      pending('old', { submittedAt: NOW - STATUS_CHECK_AFTER_MS }),
      pending('delayed', { submittedAt: NOW - 60_000, state: 'delayed' }),
      pending('failed', { submittedAt: NOW - 60_000, state: 'failed' }),
    ]
    expect(dueForStatusCheck(list, NOW).map((p) => p.id)).toEqual(['old', 'delayed'])
  })
})

describe('applyStatus', () => {
  const list = [pending('a'), pending('b')]
  it('removes published posts', () => {
    expect(applyStatus(list, 'a', 'published').map((p) => p.id)).toEqual(['b'])
  })
  it('marks still-pending posts as delayed', () => {
    expect(applyStatus(list, 'a', 'pending')[0].state).toBe('delayed')
  })
  it('marks failed posts as failed', () => {
    expect(applyStatus(list, 'b', 'failed')[1].state).toBe('failed')
  })
})
```

- [ ] **Step 2: 실패 확인**

Run: `cd frontend && npx vitest run src/board/pending.test.ts`
Expected: FAIL — `./pending`을 찾을 수 없음

- [ ] **Step 3: 순수 로직 구현**

`frontend/src/board/pending.ts`:
```ts
import type { PostStatus } from '../api/types'

export type PendingState = 'pending' | 'delayed' | 'failed'

export interface PendingPost {
  id: string
  title: string
  body: string
  author_nickname: string
  submittedAt: number
  state: PendingState
}

export const STATUS_CHECK_AFTER_MS = 30_000

export const PENDING_LABEL: Record<PendingState, string> = {
  pending: '게시 중…',
  delayed: '지연 중',
  failed: '게시 실패',
}

export function upsertPending(list: PendingPost[], post: PendingPost): PendingPost[] {
  return [post, ...list.filter((p) => p.id !== post.id)]
}

export function visiblePending(list: PendingPost[], serverIds: Set<string>): PendingPost[] {
  return list.filter((p) => !serverIds.has(p.id))
}

export function dueForStatusCheck(list: PendingPost[], now: number): PendingPost[] {
  return list.filter((p) => p.state !== 'failed' && now - p.submittedAt >= STATUS_CHECK_AFTER_MS)
}

export function applyStatus(list: PendingPost[], id: string, status: PostStatus['status']): PendingPost[] {
  if (status === 'published') return list.filter((p) => p.id !== id)
  const state: PendingState = status === 'failed' ? 'failed' : 'delayed'
  return list.map((p) => (p.id === id ? { ...p, state } : p))
}
```

Run: `cd frontend && npx vitest run src/board/pending.test.ts`
Expected: PASS

- [ ] **Step 4: 목록 화면 실패 테스트 작성**

`frontend/src/board/ListPage.test.tsx`:
```tsx
import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import type { Post } from '../api/types'
import { jsonResponse, stubFetch } from '../test/http'
import { createTestQueryClient } from '../test/utils'
import { ListPage } from './ListPage'
import { PendingPostsProvider } from './PendingPostsContext'
import type { PendingPost } from './pending'

function post(id: string, title: string): Post {
  return { id, author_id: 'u1', author_nickname: '작성자', title, body: 'b', created_at: '2026-09-30T00:00:00Z' }
}

function pending(id: string, title: string, state: PendingPost['state'] = 'pending'): PendingPost {
  return { id, title, body: 'b', author_nickname: '테스터', submittedAt: Date.now(), state }
}

function renderList(initialPendings: PendingPost[]) {
  render(
    <QueryClientProvider client={createTestQueryClient(null)}>
      <MemoryRouter>
        <PendingPostsProvider initialPendings={initialPendings}>
          <ListPage />
        </PendingPostsProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ListPage', () => {
  it('replaces a pending post once the server returns it', async () => {
    stubFetch(() => jsonResponse(200, { items: [post('p1', '서버에 반영된 글')], next_cursor: null }))
    renderList([pending('p1', '서버에 반영된 글'), pending('p2', '아직 대기 중인 글')])

    expect(await screen.findByRole('link', { name: '서버에 반영된 글' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText('게시 중…')).toHaveLength(1))
    expect(screen.getByText('아직 대기 중인 글')).toBeInTheDocument()
  })

  it('offers a retry button for failed posts', async () => {
    stubFetch(() => jsonResponse(200, { items: [], next_cursor: null }))
    renderList([pending('p3', '실패한 글', 'failed')])
    expect(await screen.findByText('게시 실패')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '다시 시도' })).toBeInTheDocument()
  })

  it('shows a load-more button when there is a next page', async () => {
    stubFetch(() => jsonResponse(200, { items: [post('p4', '첫 페이지 글')], next_cursor: 'p4' }))
    renderList([])
    expect(await screen.findByRole('button', { name: '더 보기' })).toBeInTheDocument()
  })
})
```

- [ ] **Step 5: 실패 확인**

Run: `cd frontend && npx vitest run src/board/ListPage.test.tsx`
Expected: FAIL — `./ListPage`, `./PendingPostsContext`를 찾을 수 없음

- [ ] **Step 6: board API, 컨텍스트, 화면 구현**

`frontend/src/lib/id.ts`:
```ts
export function newId(): string {
  return crypto.randomUUID()
}
```

`frontend/src/api/board.ts`:
```ts
import { api } from './client'
import type { PostPage, PostStatus } from './types'

export const boardApi = {
  list: (cursor?: string | null) =>
    api.request<PostPage>(cursor ? `/api/board/posts?cursor=${encodeURIComponent(cursor)}` : '/api/board/posts'),
  get: (id: string) => api.request<PostStatus>(`/api/board/posts/${encodeURIComponent(id)}`),
  create: (input: { title: string; body: string }, idempotencyKey: string) =>
    api.request<{ id: string; status: 'pending' }>('/api/board/posts', {
      method: 'POST',
      body: input,
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
  remove: (id: string) => api.request<void>(`/api/board/posts/${encodeURIComponent(id)}`, { method: 'DELETE' }),
}
```

`frontend/src/board/keys.ts`:
```ts
export const POSTS_KEY = ['posts'] as const

export function postKey(id: string) {
  return ['post', id] as const
}
```

`frontend/src/board/PendingPostsContext.tsx`:
```tsx
import { useQueryClient } from '@tanstack/react-query'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { boardApi } from '../api/board'
import { ApiError } from '../api/client'
import type { PostStatus } from '../api/types'
import { POSTS_KEY } from './keys'
import { applyStatus, dueForStatusCheck, upsertPending, visiblePending, type PendingPost } from './pending'

interface PendingPostsApi {
  pendings: PendingPost[]
  add: (post: PendingPost) => void
  replace: (oldId: string, post: PendingPost) => void
  prune: (serverIds: Set<string>) => void
}

const PendingPostsContext = createContext<PendingPostsApi | null>(null)

async function fetchStatus(id: string): Promise<PostStatus['status']> {
  try {
    return (await boardApi.get(id)).status
  } catch (err) {
    // pending 키(TTL 1시간)도 DB 행도 없으면 404가 온다. 게시되지 못한 것으로 본다.
    if (err instanceof ApiError && err.status === 404) return 'failed'
    return 'pending'
  }
}

export function PendingPostsProvider({
  children,
  initialPendings = [],
  checkIntervalMs = 5000,
}: {
  children: ReactNode
  initialPendings?: PendingPost[]
  checkIntervalMs?: number
}) {
  const queryClient = useQueryClient()
  const [pendings, setPendings] = useState<PendingPost[]>(initialPendings)
  const latest = useRef(pendings)
  latest.current = pendings

  const add = useCallback((post: PendingPost) => setPendings((list) => upsertPending(list, post)), [])
  const replace = useCallback(
    (oldId: string, post: PendingPost) => setPendings((list) => upsertPending(list.filter((p) => p.id !== oldId), post)),
    [],
  )
  const prune = useCallback(
    (serverIds: Set<string>) =>
      setPendings((list) => {
        const next = visiblePending(list, serverIds)
        return next.length === list.length ? list : next
      }),
    [],
  )

  // 30초가 지나도 목록에 나타나지 않은 글은 서버에 상태를 물어본다.
  useEffect(() => {
    const timer = setInterval(async () => {
      for (const post of dueForStatusCheck(latest.current, Date.now())) {
        const status = await fetchStatus(post.id)
        setPendings((list) => applyStatus(list, post.id, status))
        if (status === 'published') void queryClient.invalidateQueries({ queryKey: POSTS_KEY })
      }
    }, checkIntervalMs)
    return () => clearInterval(timer)
  }, [checkIntervalMs, queryClient])

  const value = useMemo(() => ({ pendings, add, replace, prune }), [pendings, add, replace, prune])
  return <PendingPostsContext.Provider value={value}>{children}</PendingPostsContext.Provider>
}

export function usePendingPosts(): PendingPostsApi {
  const value = useContext(PendingPostsContext)
  if (!value) throw new Error('usePendingPosts must be used inside PendingPostsProvider')
  return value
}
```

`frontend/src/board/ListPage.tsx`:
```tsx
import { useInfiniteQuery, useMutation } from '@tanstack/react-query'
import { useEffect, useMemo } from 'react'
import { Link } from 'react-router-dom'
import { boardApi } from '../api/board'
import { errorMessage } from '../api/messages'
import { newId } from '../lib/id'
import { POSTS_KEY } from './keys'
import { PENDING_LABEL, visiblePending, type PendingPost } from './pending'
import { usePendingPosts } from './PendingPostsContext'

export const POLL_INTERVAL_MS = 5000

export function ListPage() {
  const { pendings, prune, replace } = usePendingPosts()
  const posts = useInfiniteQuery({
    queryKey: POSTS_KEY,
    queryFn: ({ pageParam }) => boardApi.list(pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    // 여러 페이지를 펼친 상태에서 폴링하면 모든 페이지를 다시 불러오므로 첫 페이지만 볼 때만 폴링한다.
    refetchInterval: (query) => ((query.state.data?.pages.length ?? 1) > 1 ? false : POLL_INTERVAL_MS),
    refetchIntervalInBackground: false,
  })

  const firstPageIds = useMemo(
    () => new Set(posts.data?.pages[0]?.items.map((p) => p.id) ?? []),
    [posts.data],
  )
  useEffect(() => prune(firstPageIds), [firstPageIds, prune])

  const retry = useMutation({
    mutationFn: async (post: PendingPost) => {
      const res = await boardApi.create({ title: post.title, body: post.body }, newId())
      return { old: post, id: res.id }
    },
    onSuccess: ({ old, id }) => replace(old.id, { ...old, id, submittedAt: Date.now(), state: 'pending' }),
  })

  const items = posts.data?.pages.flatMap((page) => page.items) ?? []

  return (
    <section>
      <h1>게시판</h1>
      {posts.isPending && <p>불러오는 중…</p>}
      {posts.isError && <p className="error" role="alert">{errorMessage(posts.error)}</p>}
      {retry.isError && <p className="error" role="alert">{errorMessage(retry.error)}</p>}
      <ul className="posts">
        {visiblePending(pendings, firstPageIds).map((p) => (
          <li key={p.id} className="post pending">
            <span className="title">{p.title}</span>
            <span className="badge">{PENDING_LABEL[p.state]}</span>
            {p.state === 'failed' && (
              <button onClick={() => retry.mutate(p)} disabled={retry.isPending}>다시 시도</button>
            )}
            <span className="meta">{p.author_nickname}</span>
          </li>
        ))}
        {items.map((post) => (
          <li key={post.id} className="post">
            <Link to={`/posts/${post.id}`} className="title">{post.title}</Link>
            <span className="meta">
              {post.author_nickname} · {new Date(post.created_at).toLocaleString('ko-KR')}
            </span>
          </li>
        ))}
      </ul>
      {posts.hasNextPage && (
        <button onClick={() => posts.fetchNextPage()} disabled={posts.isFetchingNextPage}>더 보기</button>
      )}
    </section>
  )
}
```

`frontend/src/board/PostPage.tsx`:
```tsx
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { boardApi } from '../api/board'
import { errorMessage } from '../api/messages'
import { useMe } from '../auth/useMe'
import { POSTS_KEY, postKey } from './keys'

export function PostPage() {
  const { id = '' } = useParams()
  const { data: me } = useMe()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const status = useQuery({ queryKey: postKey(id), queryFn: () => boardApi.get(id) })
  const remove = useMutation({
    mutationFn: () => boardApi.remove(id),
    onSuccess: () => {
      queryClient.removeQueries({ queryKey: postKey(id) })
      void queryClient.invalidateQueries({ queryKey: POSTS_KEY })
      navigate('/', { replace: true })
    },
  })

  if (status.isPending) return <p>불러오는 중…</p>
  if (status.isError) return <p className="error" role="alert">{errorMessage(status.error)}</p>
  if (status.data.status === 'pending') return <p>게시 대기 중인 글입니다. 잠시 후 다시 확인해 주세요.</p>
  if (status.data.status === 'failed') return <p className="error">게시에 실패한 글입니다.</p>

  const post = status.data.post
  return (
    <article>
      <h1>{post.title}</h1>
      <p className="meta">
        {post.author_nickname} · {new Date(post.created_at).toLocaleString('ko-KR')}
      </p>
      <div className="body">{post.body}</div>
      {me && me.id === post.author_id && (
        <>
          {remove.isError && <p className="error" role="alert">{errorMessage(remove.error)}</p>}
          <button className="danger" onClick={() => remove.mutate()} disabled={remove.isPending}>삭제</button>
        </>
      )}
    </article>
  )
}
```

- [ ] **Step 7: 테스트 통과 확인**

Run: `cd frontend && npx vitest run && npm run typecheck`
Expected: 전부 PASS

- [ ] **Step 8: 커밋**

```bash
git add frontend/src
git commit -m "feat(frontend): add post list/detail with pending post tracking"
```

---

### Task 4: 글쓰기 화면(멱등 키)과 앱 조립

**Files:**
- Create: `frontend/src/board/WritePage.tsx`
- Modify: `frontend/src/App.tsx`, `frontend/src/main.tsx` (전체 교체)
- Test: `frontend/src/board/WritePage.test.tsx`, `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: Task 2의 `RequireAuth`, `LoginPage`, `SignupPage`, `MePage`, `Layout`, `ToastProvider`, `createQueryClient`, `loginPath`, `useMe` / Task 3의 `boardApi`, `PendingPostsProvider`, `usePendingPosts`, `ListPage`, `PostPage`, `newId`
- Produces: `WritePage`, `App`, 라우트 `/`, `/posts/:id`, `/write`, `/login`, `/signup`, `/me`, `*`. E2E(Task 7)가 쓰는 화면 문구: 링크 `글쓰기`·`내 정보`·`로그인`, 라벨 `이메일`·`닉네임`·`비밀번호`·`제목`·`본문`·`비밀번호 확인`, 버튼 `가입하기`·`등록`·`회원 탈퇴`, 배지 `게시 중…`

- [ ] **Step 1: 실패 테스트 작성**

`frontend/src/board/WritePage.test.tsx`:
```tsx
import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { jsonResponse, stubFetch } from '../test/http'
import { TEST_USER, createTestQueryClient } from '../test/utils'
import { PendingPostsProvider } from './PendingPostsContext'
import { WritePage } from './WritePage'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

describe('WritePage', () => {
  it('reuses the same Idempotency-Key when the user resubmits after a failure', async () => {
    const fetch = stubFetch((_url, init) =>
      init.method === 'POST' && fetch.mock.calls.length === 1
        ? jsonResponse(500, { code: 'INTERNAL', message: 'boom' })
        : jsonResponse(202, { id: 'p1', status: 'pending' }),
    )
    render(
      <QueryClientProvider client={createTestQueryClient(TEST_USER)}>
        <MemoryRouter initialEntries={['/write']}>
          <PendingPostsProvider>
            <Routes>
              <Route path="/write" element={<WritePage />} />
              <Route path="/" element={<p>목록 화면</p>} />
            </Routes>
          </PendingPostsProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    await userEvent.type(screen.getByLabelText('제목'), '대피소 정보')
    await userEvent.type(screen.getByLabelText('본문'), '체육관 개방')
    await userEvent.click(screen.getByRole('button', { name: '등록' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('오류가 발생했습니다 (500).')

    await userEvent.click(screen.getByRole('button', { name: '등록' }))
    expect(await screen.findByText('목록 화면')).toBeInTheDocument()

    const keys = fetch.mock.calls.map(([, init]) => (init.headers as Record<string, string>)['Idempotency-Key'])
    expect(keys).toHaveLength(2)
    expect(keys[0]).toMatch(UUID)
    expect(keys[1]).toBe(keys[0])
  })
})
```

`frontend/src/App.test.tsx`:
```tsx
import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { App } from './App'
import { jsonResponse, stubFetch } from './test/http'
import { createTestQueryClient } from './test/utils'

describe('App', () => {
  it('shows the board and guest navigation', async () => {
    stubFetch((url) =>
      url === '/api/auth/me'
        ? jsonResponse(401, { code: 'UNAUTHORIZED', message: 'login required' })
        : jsonResponse(200, { items: [], next_cursor: null }),
    )
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('heading', { name: '게시판' })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: '로그인' })).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: 실패 확인**

Run: `cd frontend && npx vitest run src/board/WritePage.test.tsx src/App.test.tsx`
Expected: FAIL — `./WritePage`를 찾을 수 없음, App 테스트는 '게시판' 제목을 찾지 못함

- [ ] **Step 3: 구현**

`frontend/src/board/WritePage.tsx`:
```tsx
import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { boardApi } from '../api/board'
import { errorMessage } from '../api/messages'
import { useMe } from '../auth/useMe'
import { newId } from '../lib/id'
import { usePendingPosts } from './PendingPostsContext'

export function WritePage() {
  const { data: me } = useMe()
  const { add } = usePendingPosts()
  const navigate = useNavigate()
  // 폼을 열 때 한 번만 만든다. 사용자가 다시 눌러도, 클라이언트가 자동 재시도해도 같은 키를 쓴다.
  const [idempotencyKey] = useState(newId)
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')

  const create = useMutation({
    mutationFn: () => boardApi.create({ title, body }, idempotencyKey),
    onSuccess: (res) => {
      add({ id: res.id, title, body, author_nickname: me?.nickname ?? '', submittedAt: Date.now(), state: 'pending' })
      navigate('/')
    },
  })

  return (
    <section>
      <h1>글쓰기</h1>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          create.mutate()
        }}
      >
        <label>
          제목
          <input value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={100} />
        </label>
        <label>
          본문
          <textarea value={body} onChange={(e) => setBody(e.target.value)} required maxLength={5000} rows={10} />
        </label>
        {create.isError && <p className="error" role="alert">{errorMessage(create.error)}</p>}
        <button type="submit" disabled={create.isPending}>등록</button>
      </form>
    </section>
  )
}
```

`frontend/src/App.tsx` (전체 교체):
```tsx
import { Route, Routes } from 'react-router-dom'
import { LoginPage } from './auth/LoginPage'
import { MePage } from './auth/MePage'
import { RequireAuth } from './auth/RequireAuth'
import { SignupPage } from './auth/SignupPage'
import { ListPage } from './board/ListPage'
import { PendingPostsProvider } from './board/PendingPostsContext'
import { PostPage } from './board/PostPage'
import { WritePage } from './board/WritePage'
import { Layout } from './ui/Layout'
import { ToastProvider } from './ui/Toast'

export function App() {
  return (
    <ToastProvider>
      <PendingPostsProvider>
        <Layout>
          <Routes>
            <Route path="/" element={<ListPage />} />
            <Route path="/posts/:id" element={<PostPage />} />
            <Route path="/write" element={<RequireAuth><WritePage /></RequireAuth>} />
            <Route path="/login" element={<LoginPage />} />
            <Route path="/signup" element={<SignupPage />} />
            <Route path="/me" element={<RequireAuth><MePage /></RequireAuth>} />
            <Route path="*" element={<p>페이지를 찾을 수 없습니다.</p>} />
          </Routes>
        </Layout>
      </PendingPostsProvider>
    </ToastProvider>
  )
}
```

`frontend/src/main.tsx` (전체 교체):
```tsx
import { QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { App } from './App'
import { loginPath } from './auth/redirect'
import { createQueryClient } from './queryClient'
import './styles.css'

const queryClient = createQueryClient(() => {
  window.location.assign(loginPath(window.location.pathname + window.location.search))
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
)
```

- [ ] **Step 4: 테스트와 빌드 확인**

Run: `cd frontend && npx vitest run && npm run build`
Expected: 전부 PASS, 빌드 성공

- [ ] **Step 5: 개발 서버로 수동 확인**

Run: `make dev`(다른 터미널) 후 `make fe-dev`, 브라우저에서 `http://localhost:5173` 접속
Expected: 이 시점에는 compose에 nginx가 없어서 API 호출이 실패하고 목록 화면에 "네트워크 오류" 또는 `HTTP_502` 메시지가 뜬다. 화면 구조(헤더, 게시판 제목, 로그인 링크)만 확인한다. 실제 동작 확인은 Task 5 이후에 한다.

- [ ] **Step 6: 커밋**

```bash
git add frontend/src
git commit -m "feat(frontend): add write page with idempotency key and wire routes"
```

---

### Task 5: nginx 게이트웨이 이미지, compose 연결, 라우팅 통합 테스트

**Files:**
- Create: `nginx/default.conf.template`, `nginx/snippets/proxy_common.conf`, `nginx/snippets/auth_proxy.conf`
- Create: `frontend/Dockerfile`, `frontend/Dockerfile.dockerignore`
- Create: `tests/nginx/requirements.txt`, `tests/nginx/conftest.py`, `tests/nginx/test_routing.py`
- Modify: `docker-compose.yml` (nginx 서비스 추가), `Makefile`

**Interfaces:**
- Consumes: Task 4의 빌드 결과(`frontend/dist`), 계획 1의 compose 서비스 `auth`, `board-api`와 API 계약(`/internal/verify` 응답 헤더 `X-User-Id`, `X-User-Nickname`, `X-Auth-Degraded`)
- Produces:
  - 이미지 `simple-web-app/frontend:dev` (포트 8080, `/nginx-health`)
  - 테스트 픽스처(`conftest.py`): `BASE_URL`, `compose(*args)`, `wait_until(check, timeout)`, `stack_ready()`, `new_credentials()`, `login_client(creds) -> httpx.Client`, 픽스처 `credentials`(session), `session_client`(session), `anon`
  - Makefile 타깃 `test-nginx`

- [ ] **Step 1: 통합 테스트 작성 (실패 예상)**

`tests/nginx/requirements.txt`:
```
pytest==8.3.3
httpx==0.27.2
```

`tests/nginx/conftest.py`:
```python
import os
import pathlib
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest

BASE_URL = os.environ.get("NGINX_BASE_URL", "http://localhost:8080")
ROOT = pathlib.Path(__file__).resolve().parents[2]
PASSWORD = "nginx-test-password"


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True)


def wait_until(check: Callable[[], bool], timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(1)
    raise TimeoutError(f"condition not met within {timeout}s (last error: {last_error})")


def stack_ready() -> bool:
    return httpx.get(f"{BASE_URL}/api/board/posts", timeout=5).status_code == 200


@pytest.fixture(scope="session", autouse=True)
def stack() -> None:
    # 이미 떠 있는 스택에 붙이고 싶으면 NGINX_TEST_SKIP_COMPOSE=1
    if os.environ.get("NGINX_TEST_SKIP_COMPOSE") != "1":
        compose("up", "-d", "--build")
    wait_until(stack_ready)


def new_credentials() -> dict[str, str]:
    suffix = uuid.uuid4().hex[:10]
    return {"email": f"nginx-{suffix}@example.com", "password": PASSWORD, "nickname": f"ng{suffix}"}


def login_client(creds: dict[str, str]) -> httpx.Client:
    res = httpx.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": creds["email"], "password": creds["password"]},
        timeout=10,
    )
    assert res.status_code == 200, res.text
    # 쿠키 jar의 localhost 도메인 처리 차이를 피하려고 Cookie 헤더를 직접 넣는다.
    return httpx.Client(base_url=BASE_URL, timeout=10, headers={"Cookie": f"sid={res.cookies['sid']}"})


@pytest.fixture(scope="session")
def credentials() -> dict[str, str]:
    creds = new_credentials()
    res = httpx.post(f"{BASE_URL}/api/auth/signup", json=creds, timeout=10)
    assert res.status_code == 201, res.text
    return creds


@pytest.fixture(scope="session")
def session_client(credentials: dict[str, str]) -> Iterator[httpx.Client]:
    client = login_client(credentials)
    yield client
    client.close()


@pytest.fixture
def anon() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        yield client
```

`tests/nginx/test_routing.py`:
```python
import re
import uuid

import httpx


def new_post_headers() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def test_nginx_health(anon: httpx.Client) -> None:
    res = anon.get("/nginx-health")
    assert res.status_code == 200


def test_spa_fallback_serves_index_without_cache(anon: httpx.Client) -> None:
    res = anon.get("/posts/some-client-side-route")
    assert res.status_code == 200
    assert '<div id="root">' in res.text
    assert res.headers["cache-control"] == "no-cache"


def test_hashed_assets_are_immutable(anon: httpx.Client) -> None:
    index = anon.get("/").text
    match = re.search(r'src="(/assets/[^"]+\.js)"', index)
    assert match, index
    res = anon.get(match.group(1))
    assert res.status_code == 200
    assert "immutable" in res.headers["cache-control"]
    assert anon.get("/assets/does-not-exist.js").status_code == 404


def test_every_response_carries_a_request_id(anon: httpx.Client) -> None:
    res = anon.get("/api/board/posts")
    assert re.fullmatch(r"[0-9a-f]{32}", res.headers["x-request-id"])


def test_internal_endpoints_are_not_exposed(anon: httpx.Client) -> None:
    for path in ["/_verify", "/internal/verify", "/healthz", "/readyz", "/metrics"]:
        assert anon.get(path).status_code == 404, path


def test_unknown_api_path_returns_json_404(anon: httpx.Client) -> None:
    res = anon.get("/api/nope")
    assert res.status_code == 404
    assert res.json()["code"] == "NOT_FOUND"


def test_auth_routes_are_proxied(anon: httpx.Client) -> None:
    res = anon.get("/api/auth/me")
    assert res.status_code == 401
    assert res.json()["code"] == "UNAUTHORIZED"


def test_anonymous_can_read_posts(anon: httpx.Client) -> None:
    res = anon.get("/api/board/posts")
    assert res.status_code == 200
    assert "items" in res.json()


def test_spoofed_user_header_is_stripped(anon: httpx.Client) -> None:
    # nginx가 헤더를 그대로 넘기면 board-api는 이 요청을 로그인 사용자로 보고 202를 반환한다.
    res = anon.post(
        "/api/board/posts",
        json={"title": "spoof", "body": "spoof"},
        headers={**new_post_headers(), "X-User-Id": str(uuid.uuid4()), "X-User-Nickname": "evil"},
    )
    assert res.status_code == 401


def test_logged_in_user_can_post(session_client: httpx.Client) -> None:
    res = session_client.post("/api/board/posts", json={"title": "nginx", "body": "ok"}, headers=new_post_headers())
    assert res.status_code == 202, res.text
    assert res.json()["status"] == "pending"


def test_spoofed_degraded_header_is_stripped(session_client: httpx.Client) -> None:
    # 넘어가면 board-api가 세션 저장소 장애로 보고 503을 반환한다.
    res = session_client.post(
        "/api/board/posts",
        json={"title": "spoof", "body": "degraded"},
        headers={**new_post_headers(), "X-Auth-Degraded": "1"},
    )
    assert res.status_code == 202, res.text
```

- [ ] **Step 2: Makefile에 test-nginx 추가 후 실패 확인**

`Makefile`에 추가하고 `.PHONY`에 `test-nginx`를 넣는다(레시피 줄은 탭):
```make
NGINX_TEST_VENV := tests/nginx/.venv

$(NGINX_TEST_VENV)/bin/pytest: tests/nginx/requirements.txt
	python3 -m venv $(NGINX_TEST_VENV)
	$(NGINX_TEST_VENV)/bin/pip install -q -r tests/nginx/requirements.txt
	touch $@

test-nginx: $(NGINX_TEST_VENV)/bin/pytest
	$(NGINX_TEST_VENV)/bin/pytest tests/nginx/test_routing.py -v
```

Run: `make test-nginx`
Expected: FAIL — compose에 nginx 서비스가 없어서 `wait_until`이 `TimeoutError`(localhost:8080 연결 거부)로 끝난다.

- [ ] **Step 3: nginx 설정 작성**

`nginx/snippets/proxy_common.conf`:
```nginx
proxy_http_version 1.1;
proxy_set_header Connection "";
proxy_set_header Host $host;
proxy_set_header X-Request-ID $request_id;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_connect_timeout 2s;
proxy_read_timeout 15s;
```

`nginx/snippets/auth_proxy.conf`:
```nginx
include /etc/nginx/snippets/proxy_common.conf;
# 값이 빈 문자열이면 nginx는 헤더를 아예 전달하지 않는다. 클라이언트가 보낸 값도 함께 사라진다.
proxy_set_header X-User-Id "";
proxy_set_header X-User-Nickname "";
proxy_set_header X-Auth-Degraded "";
proxy_pass http://auth_upstream;
```

`nginx/default.conf.template`:
```nginx
# nginx 공식 이미지가 시작할 때 envsubst로 처리해 /etc/nginx/conf.d/default.conf로 만든다.
# ${VAR}는 정의된 환경변수만 치환되고, $var 형태의 nginx 변수는 그대로 남는다.

upstream auth_upstream {
    server ${AUTH_UPSTREAM};
    keepalive 32;
}

upstream board_upstream {
    server ${BOARD_UPSTREAM};
    keepalive 32;
}

server {
    listen 8080;
    server_name _;
    root /usr/share/nginx/html;
    server_tokens off;
    client_max_body_size 64k;

    gzip on;
    gzip_min_length 1024;
    gzip_types application/json application/javascript text/css image/svg+xml;

    # location에서 add_header를 쓰면 여기 값은 상속되지 않으므로 그런 location에는 다시 적는다.
    add_header X-Request-ID $request_id always;

    location = /nginx-health {
        access_log off;
        default_type text/plain;
        return 200 "ok\n";
    }

    location ~ ^/(healthz|readyz|metrics)$ {
        return 404;
    }

    location /internal/ {
        return 404;
    }

    location = /_verify {
        internal;
        include /etc/nginx/snippets/proxy_common.conf;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_pass http://auth_upstream/internal/verify;
    }

    location /api/auth/ {
        include /etc/nginx/snippets/auth_proxy.conf;
    }

    location /api/board/ {
        auth_request /_verify;
        auth_request_set $auth_user_id $upstream_http_x_user_id;
        auth_request_set $auth_user_nickname $upstream_http_x_user_nickname;
        auth_request_set $auth_degraded $upstream_http_x_auth_degraded;

        include /etc/nginx/snippets/proxy_common.conf;
        # proxy_set_header는 클라이언트가 보낸 같은 이름의 헤더를 덮어쓴다. 비어 있으면 전달하지 않는다.
        proxy_set_header X-User-Id $auth_user_id;
        proxy_set_header X-User-Nickname $auth_user_nickname;
        proxy_set_header X-Auth-Degraded $auth_degraded;
        proxy_pass http://board_upstream;
    }

    location /api/ {
        default_type application/json;
        return 404 '{"code":"NOT_FOUND","message":"Not found"}';
    }

    location /assets/ {
        add_header Cache-Control "public, max-age=31536000, immutable" always;
        add_header X-Request-ID $request_id always;
        try_files $uri =404;
    }

    location = /index.html {
        add_header Cache-Control "no-cache" always;
        add_header X-Request-ID $request_id always;
    }

    location / {
        try_files $uri /index.html;
    }
}
```

- [ ] **Step 4: Dockerfile 작성**

`frontend/Dockerfile` (빌드 컨텍스트는 저장소 루트):
```dockerfile
# syntax=docker/dockerfile:1
FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
ENV AUTH_UPSTREAM=auth:8000 \
    BOARD_UPSTREAM=board-api:8000 \
    REAL_IP_FROM=10.0.0.0/8 \
    RATE_READ=100r/s \
    BURST_READ=200 \
    RATE_WRITE=2r/s \
    BURST_WRITE=5 \
    RATE_LOGIN=30r/m \
    BURST_LOGIN=10
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY nginx/snippets/ /etc/nginx/snippets/
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
```

`frontend/Dockerfile.dockerignore` (BuildKit은 `<Dockerfile>.dockerignore`를 루트 `.dockerignore`보다 우선 적용한다):
```
**
!frontend/package.json
!frontend/package-lock.json
!frontend/index.html
!frontend/tsconfig.json
!frontend/vite.config.ts
!frontend/src/**
!frontend/docker/**
!nginx/**
```

- [ ] **Step 5: compose에 nginx 서비스 추가**

`docker-compose.yml`의 `services:` 아래에 추가한다:
```yaml
  nginx:
    build:
      context: .
      dockerfile: frontend/Dockerfile
    image: simple-web-app/frontend:dev
    environment:
      # 로컬에는 앞단 LB가 없으므로 X-Forwarded-For를 믿지 않는다.
      REAL_IP_FROM: 127.0.0.1/32
    ports:
      - "8080:8080"
    depends_on:
      - auth
      - board-api
```

- [ ] **Step 6: 통합 테스트 통과 확인**

Run: `make test-nginx`
Expected: `test_routing.py`의 11개 테스트 전부 PASS

실패하면 `docker compose logs nginx`로 envsubst 결과나 `host not found in upstream` 에러를 확인한다.

- [ ] **Step 7: 브라우저로 전체 흐름 확인**

Run: `make dev` 후 `http://localhost:8080` 접속
Expected: 회원가입 → 글쓰기 → 목록 맨 위에 "게시 중…"으로 떴다가 5초 안에 링크로 바뀐다 → 상세 → 삭제가 동작한다.

- [ ] **Step 8: 커밋**

```bash
git add nginx frontend/Dockerfile frontend/Dockerfile.dockerignore tests/nginx docker-compose.yml Makefile
git commit -m "feat(nginx): add auth_request gateway image and routing tests"
```

---

### Task 6: 요청 리밋, 실제 클라이언트 IP, 장애 시 JSON 응답

**Files:**
- Create: `frontend/docker/15-real-ip.sh`
- Modify: `nginx/default.conf.template` (전체 교체), `nginx/snippets/auth_proxy.conf` (전체 교체), `frontend/Dockerfile` (COPY 한 줄 추가), `Makefile` (`test-nginx` 레시피)
- Test: `tests/nginx/test_config.py`, `tests/nginx/test_degraded.py`, `tests/nginx/test_ratelimit.py`

**Interfaces:**
- Consumes: Task 5의 `conftest.py` 헬퍼(`compose`, `wait_until`, `stack_ready`, `login_client`, `BASE_URL`, `credentials`, `session_client`, `anon`)
- Produces: nginx 응답 계약 — 429 `{"code":"RATE_LIMITED"}` + `Retry-After: 2`, 503 `{"code":"UNAVAILABLE"}` + `Retry-After: 5`

**테스트 실행 순서가 중요하다.** `test_degraded.py`는 Redis를 재시작해서 세션을 지우고, `test_ratelimit.py`는 로그인 리밋을 소진시킨다. 그래서 `test_config.py` → `test_routing.py` → `test_degraded.py` → `test_ratelimit.py` 순서로 파일을 명시해 실행한다(pytest는 명령줄에 적힌 순서대로 파일을 수집한다).

- [ ] **Step 1: 실패 테스트 작성**

`tests/nginx/test_config.py` (CIDR 목록 처리 확인. 스택이 떠 있어야 upstream 호스트 이름이 해석되므로 `--no-deps`로 같은 compose 네트워크에 일회성 컨테이너를 띄운다):
```python
import subprocess

from conftest import ROOT

MARKER = "--- real-ip.inc ---"


def test_real_ip_from_accepts_a_space_separated_cidr_list() -> None:
    # docker-entrypoint.sh는 명령이 nginx일 때만 /docker-entrypoint.d/ 스크립트를 실행한다.
    script = f"/docker-entrypoint.sh nginx -t && echo '{MARKER}' && cat /etc/nginx/conf.d/real-ip.inc"
    res = subprocess.run(
        [
            "docker", "compose", "run", "--rm", "--no-deps",
            "-e", "REAL_IP_FROM=130.211.0.0/22 35.191.0.0/16",
            "--entrypoint", "sh", "nginx", "-c", script,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    output = res.stdout + res.stderr
    assert res.returncode == 0, output
    assert "test is successful" in output
    inc = res.stdout.split(MARKER, 1)[1]
    assert [line.strip() for line in inc.strip().splitlines()] == [
        "set_real_ip_from 130.211.0.0/22;",
        "set_real_ip_from 35.191.0.0/16;",
    ]
```

`tests/nginx/test_degraded.py`:
```python
import uuid
from collections.abc import Iterator

import httpx
import pytest

from conftest import BASE_URL, compose, stack_ready, wait_until


def auth_redis_ready() -> bool:
    # Redis가 살아 있으면 잘못된 세션에 401, 죽어 있으면 503을 돌려준다.
    res = httpx.get(f"{BASE_URL}/api/auth/me", headers={"Cookie": "sid=probe"}, timeout=5)
    return res.status_code == 401


@pytest.fixture
def redis_down() -> Iterator[None]:
    compose("stop", "redis")
    try:
        yield
    finally:
        compose("start", "redis")
        wait_until(auth_redis_ready)


@pytest.fixture
def auth_down() -> Iterator[None]:
    compose("stop", "auth")
    try:
        yield
    finally:
        compose("start", "auth")
        # 컨테이너 IP가 바뀌었을 수 있으므로 upstream을 다시 해석하게 nginx를 재시작한다.
        compose("restart", "nginx")
        wait_until(stack_ready)


def test_reads_still_work_when_redis_is_down(anon: httpx.Client, redis_down: None) -> None:
    res = anon.get("/api/board/posts")
    assert res.status_code == 200, res.text
    assert "items" in res.json()


def test_writes_get_503_when_redis_is_down(session_client: httpx.Client, redis_down: None) -> None:
    res = session_client.post(
        "/api/board/posts",
        json={"title": "degraded", "body": "redis down"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert res.status_code == 503, res.text
    assert res.json()["code"] == "UNAVAILABLE"
    assert "retry-after" in res.headers


def test_board_returns_json_503_when_auth_is_down(anon: httpx.Client, auth_down: None) -> None:
    res = anon.get("/api/board/posts")
    assert res.status_code == 503
    assert res.json()["code"] == "UNAVAILABLE"
    assert res.headers["retry-after"] == "5"
```

`tests/nginx/test_ratelimit.py`:
```python
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

from conftest import BASE_URL, login_client


def post_once(client: httpx.Client) -> httpx.Response:
    return client.post(
        "/api/board/posts",
        json={"title": "rate", "body": "limit"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


def test_write_limit_is_per_session(credentials: dict[str, str]) -> None:
    flooding = login_client(credentials)
    responses = [post_once(flooding) for _ in range(15)]
    statuses = [r.status_code for r in responses]
    assert 202 in statuses
    limited = next(r for r in responses if r.status_code == 429)
    assert limited.json()["code"] == "RATE_LIMITED"
    assert limited.headers["retry-after"] == "2"

    # 같은 사용자라도 다른 세션은 영향을 받지 않는다.
    other = login_client(credentials)
    assert post_once(other).status_code == 202


def test_read_limit_blocks_a_flooding_ip() -> None:
    with httpx.Client(base_url=BASE_URL, timeout=10, limits=httpx.Limits(max_connections=32)) as client:
        with ThreadPoolExecutor(max_workers=32) as pool:
            statuses = list(pool.map(lambda _: client.get(f"/api/board/posts/{uuid.uuid4()}").status_code, range(800)))
    assert 404 in statuses
    assert 429 in statuses


def test_login_limit_ignores_spoofed_forwarded_for() -> None:
    # 로그인 리밋을 소진시키므로 이 파일의 마지막 테스트여야 한다.
    limited = None
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        for i in range(25):
            res = client.post(
                "/api/auth/login",
                json={"email": f"nobody-{uuid.uuid4().hex[:8]}@example.com", "password": "wrong-password"},
                headers={"X-Forwarded-For": f"203.0.113.{i}"},
            )
            if res.status_code == 429 and res.json().get("code") == "RATE_LIMITED":
                limited = res
                break
    assert limited is not None, "IP 기준 로그인 리밋이 걸리지 않았다 (X-Forwarded-For 위조로 우회됐을 수 있음)"
    assert limited.headers["retry-after"] == "2"
```

`Makefile`의 `test-nginx` 레시피를 교체한다(탭):
```make
test-nginx: $(NGINX_TEST_VENV)/bin/pytest
	$(NGINX_TEST_VENV)/bin/pytest tests/nginx/test_config.py tests/nginx/test_routing.py tests/nginx/test_degraded.py tests/nginx/test_ratelimit.py -v
```

- [ ] **Step 2: 실패 확인**

Run: `make test-nginx`
Expected: `test_config.py` FAIL(`real-ip.inc`가 없어 `cat` 실패), `test_board_returns_json_503_when_auth_is_down` FAIL(nginx 기본 HTML 500), `test_ratelimit.py` 3개 FAIL(429가 나오지 않음). `test_reads_still_work_when_redis_is_down`, `test_writes_get_503_when_redis_is_down`은 계획 1 백엔드가 올바르면 이미 PASS.

- [ ] **Step 3: nginx 설정에 리밋과 에러 처리 추가**

`frontend/docker/15-real-ip.sh` (nginx 공식 이미지는 `/docker-entrypoint.d/`의 스크립트를 이름 순으로 실행하므로 `20-envsubst-on-templates.sh`보다 먼저 돈다):
```sh
#!/bin/sh
# REAL_IP_FROM(공백으로 구분한 CIDR 목록)을 CIDR마다 set_real_ip_from 한 줄로 바꾼다.
# 템플릿의 ${REAL_IP_FROM}에 그대로 넣으면 CIDR가 두 개 이상일 때 nginx 문법 오류가 난다.
# /etc/nginx/conf.d는 nginx-unprivileged 이미지에서 nginx 사용자(uid 101)가 쓸 수 있다.
# 확장자가 .conf가 아니므로 conf.d/*.conf로 자동 포함되지 않고, 템플릿의 include로만 읽힌다.
set -eu
out=/etc/nginx/conf.d/real-ip.inc
: > "$out"
for cidr in ${REAL_IP_FROM:-}; do
  printf 'set_real_ip_from %s;\n' "$cidr" >> "$out"
done
echo "$0: wrote $(wc -l < "$out") set_real_ip_from line(s) to $out"
```

`frontend/Dockerfile`의 `COPY nginx/snippets/ /etc/nginx/snippets/` 줄 바로 아래에 추가한다:
```dockerfile
COPY --chmod=755 frontend/docker/15-real-ip.sh /docker-entrypoint.d/15-real-ip.sh
```

`nginx/snippets/auth_proxy.conf` (전체 교체):
```nginx
include /etc/nginx/snippets/proxy_common.conf;
# 값이 빈 문자열이면 nginx는 헤더를 아예 전달하지 않는다. 클라이언트가 보낸 값도 함께 사라진다.
proxy_set_header X-User-Id "";
proxy_set_header X-User-Nickname "";
proxy_set_header X-Auth-Degraded "";
# nginx가 직접 만든 에러만 가로챈다. auth-svc가 보낸 429(TOO_MANY_ATTEMPTS)나 503은 그대로 전달된다.
error_page 429 = @rate_limited;
error_page 502 504 = @unavailable;
proxy_pass http://auth_upstream;
```

`nginx/default.conf.template` (전체 교체):
```nginx
# nginx 공식 이미지가 시작할 때 envsubst로 처리해 /etc/nginx/conf.d/default.conf로 만든다.
# ${VAR}는 정의된 환경변수만 치환되고, $var 형태의 nginx 변수는 그대로 남는다.

# limit_req는 키가 빈 문자열인 요청을 세지 않는다. 이를 이용해 메서드별로 다른 리밋을 건다.
map $request_method $board_read_key {
    GET     $binary_remote_addr;
    HEAD    $binary_remote_addr;
    default "";
}

# limit_req(PREACCESS 단계)는 auth_request(ACCESS 단계)보다 먼저 실행되어 사용자 ID를 알 수 없다.
# 그래서 로그인 사용자마다 하나씩 있는 세션 쿠키로 센다. 쿠키가 없는 쓰기 요청은 board-api가 401로 거절한다.
map $request_method $board_write_key {
    GET     "";
    HEAD    "";
    OPTIONS "";
    default $cookie_sid;
}

limit_req_zone $board_read_key     zone=board_read:10m  rate=${RATE_READ};
limit_req_zone $board_write_key    zone=board_write:10m rate=${RATE_WRITE};
limit_req_zone $binary_remote_addr zone=auth_login:10m  rate=${RATE_LOGIN};
limit_req_status 429;
limit_req_log_level warn;

upstream auth_upstream {
    server ${AUTH_UPSTREAM};
    keepalive 32;
}

upstream board_upstream {
    server ${BOARD_UPSTREAM};
    keepalive 32;
}

server {
    listen 8080;
    server_name _;
    root /usr/share/nginx/html;
    server_tokens off;
    client_max_body_size 64k;

    # LB 뒤에서 실제 클라이언트 IP를 얻는다. 믿을 수 있는 대역(LB)에서 온 X-Forwarded-For만 반영한다.
    # set_real_ip_from 줄들은 15-real-ip.sh가 REAL_IP_FROM 목록으로 만든다.
    include /etc/nginx/conf.d/real-ip.inc;
    real_ip_header X-Forwarded-For;
    real_ip_recursive on;

    gzip on;
    gzip_min_length 1024;
    gzip_types application/json application/javascript text/css image/svg+xml;

    # location에서 add_header를 쓰면 여기 값은 상속되지 않으므로 그런 location에는 다시 적는다.
    add_header X-Request-ID $request_id always;

    location = /nginx-health {
        access_log off;
        default_type text/plain;
        return 200 "ok\n";
    }

    location ~ ^/(healthz|readyz|metrics)$ {
        return 404;
    }

    location /internal/ {
        return 404;
    }

    location = /_verify {
        internal;
        include /etc/nginx/snippets/proxy_common.conf;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_pass http://auth_upstream/internal/verify;
    }

    location ~ ^/api/auth/(login|signup)$ {
        limit_req zone=auth_login burst=${BURST_LOGIN} nodelay;
        include /etc/nginx/snippets/auth_proxy.conf;
    }

    location /api/auth/ {
        include /etc/nginx/snippets/auth_proxy.conf;
    }

    location /api/board/ {
        limit_req zone=board_read burst=${BURST_READ} nodelay;
        limit_req zone=board_write burst=${BURST_WRITE} nodelay;
        error_page 429 = @rate_limited;
        # auth_request 대상이 5xx를 받으면 nginx는 500을 만든다(auth-svc 다운). board-api 다운은 502/504.
        # board-api가 직접 보낸 503(QUEUE_FULL 등)은 가로채지 않는다.
        error_page 500 502 504 = @unavailable;

        auth_request /_verify;
        auth_request_set $auth_user_id $upstream_http_x_user_id;
        auth_request_set $auth_user_nickname $upstream_http_x_user_nickname;
        auth_request_set $auth_degraded $upstream_http_x_auth_degraded;

        include /etc/nginx/snippets/proxy_common.conf;
        # proxy_set_header는 클라이언트가 보낸 같은 이름의 헤더를 덮어쓴다. 비어 있으면 전달하지 않는다.
        proxy_set_header X-User-Id $auth_user_id;
        proxy_set_header X-User-Nickname $auth_user_nickname;
        proxy_set_header X-Auth-Degraded $auth_degraded;
        proxy_pass http://board_upstream;
    }

    location /api/ {
        default_type application/json;
        return 404 '{"code":"NOT_FOUND","message":"Not found"}';
    }

    location @rate_limited {
        default_type application/json;
        add_header Retry-After 2 always;
        add_header X-Request-ID $request_id always;
        return 429 '{"code":"RATE_LIMITED","message":"Too many requests"}';
    }

    location @unavailable {
        default_type application/json;
        add_header Retry-After 5 always;
        add_header X-Request-ID $request_id always;
        return 503 '{"code":"UNAVAILABLE","message":"Service temporarily unavailable"}';
    }

    location /assets/ {
        add_header Cache-Control "public, max-age=31536000, immutable" always;
        add_header X-Request-ID $request_id always;
        try_files $uri =404;
    }

    location = /index.html {
        add_header Cache-Control "no-cache" always;
        add_header X-Request-ID $request_id always;
    }

    location / {
        try_files $uri /index.html;
    }
}
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `make test-nginx`
Expected: `test_config.py` 1개, `test_routing.py` 11개, `test_degraded.py` 3개, `test_ratelimit.py` 3개 전부 PASS

`test_read_limit_blocks_a_flooding_ip`가 가끔 429를 못 받으면 요청 수(800)를 늘리지 말고 먼저 `docker compose logs nginx | grep limiting`으로 리밋이 동작하는지 확인한다.

- [ ] **Step 5: 커밋**

```bash
git add nginx frontend/docker frontend/Dockerfile tests/nginx Makefile
git commit -m "feat(nginx): add rate limits, real client IP and JSON error responses"
```

---

### Task 7: Playwright E2E 스모크 테스트와 전체 테스트 타깃

**Files:**
- Create: `frontend/playwright.config.ts`, `frontend/e2e/smoke.spec.ts`, `scripts/wait-http.sh`
- Modify: `Makefile` (`test-e2e` 추가, `test` 수정)

**Interfaces:**
- Consumes: Task 4의 화면 문구, Task 5의 compose nginx 서비스(8080)
- Produces: `make test-e2e`, `make test` = `test-backend test-frontend test-e2e test-nginx`

- [ ] **Step 1: E2E 테스트 작성**

`frontend/playwright.config.ts`:
```ts
import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  retries: 0,
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:8080',
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
```

`frontend/e2e/smoke.spec.ts`:
```ts
import { expect, test } from '@playwright/test'

test('signup → write a post → see it published → delete account', async ({ page }) => {
  const id = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`
  const email = `e2e-${id}@example.com`
  const password = 'e2e-password-123'
  const nickname = `e2e${id}`.slice(0, 20)
  const title = `대피소 안내 ${id}`

  await page.goto('/signup')
  await page.getByLabel('이메일').fill(email)
  await page.getByLabel('닉네임').fill(nickname)
  await page.getByLabel('비밀번호').fill(password)
  await page.getByRole('button', { name: '가입하기' }).click()
  await expect(page.getByRole('link', { name: '내 정보' })).toBeVisible()

  await page.getByRole('link', { name: '글쓰기' }).click()
  await page.getByLabel('제목').fill(title)
  await page.getByLabel('본문').fill('시청 체육관이 대피소로 개방되었습니다.')
  await page.getByRole('button', { name: '등록' }).click()
  await expect(page).toHaveURL('/')

  // 먼저 "게시 중…"으로 보이고, 워커가 저장한 뒤 폴링(5초)으로 실제 글 링크로 바뀐다.
  const item = page.getByRole('listitem').filter({ hasText: title })
  await expect(item.first()).toBeVisible()
  await expect(item.getByRole('link', { name: title })).toBeVisible({ timeout: 20_000 })
  await expect(item.getByText('게시 중…')).toHaveCount(0)

  await page.getByRole('link', { name: '내 정보' }).click()
  await page.getByLabel('비밀번호 확인').fill(password)
  await page.getByRole('button', { name: '회원 탈퇴' }).click()
  await expect(page.getByRole('link', { name: '로그인' })).toBeVisible()
})
```

`scripts/wait-http.sh`:
```bash
#!/usr/bin/env bash
# 사용법: scripts/wait-http.sh <url> [timeout_seconds]
set -euo pipefail
url="$1"
timeout="${2:-120}"
deadline=$((SECONDS + timeout))
until curl -sf -o /dev/null "$url"; do
  if (( SECONDS >= deadline )); then
    echo "timed out waiting for $url" >&2
    exit 1
  fi
  sleep 1
done
```

Run: `chmod +x scripts/wait-http.sh`

- [ ] **Step 2: Makefile 수정**

`Makefile`에 추가하고 `.PHONY`에 `test-e2e`를 넣는다(탭):
```make
test-e2e:
	docker compose up -d --build
	scripts/wait-http.sh http://localhost:8080/api/board/posts 120
	cd frontend && npx playwright install chromium && npx playwright test
```

계획 1이 만든 `test` 타깃의 의존성 줄을 다음으로 바꾼다. E2E는 로그인 리밋을 소진시키는 `test-nginx`보다 먼저 돌아야 한다.
```make
test: test-backend test-frontend test-e2e test-nginx
```

- [ ] **Step 3: E2E 실행**

Run: `make test-e2e`
Expected: `1 passed`. 실패하면 `frontend/test-results/` 아래 trace를 `npx playwright show-trace <path>`로 연다.

- [ ] **Step 4: 전체 테스트 실행**

Run: `docker compose down -v && make test`
Expected: 백엔드, 프론트 단위, E2E, nginx 통합 테스트가 모두 PASS

- [ ] **Step 5: 커밋**

```bash
git add frontend/playwright.config.ts frontend/e2e scripts/wait-http.sh Makefile
git commit -m "test: add Playwright smoke test and aggregate test target"
```

---

## 스펙 대비 점검 (self-review)

| 스펙 요구사항 | 담당 Task |
|---|---|
| §5 앱 시작 시 `/me` 확인 | Task 2 `useMe`, Task 4 `Layout`·`App` |
| §5 5초 폴링, 숨겨진 탭에서는 중지 | Task 3 `ListPage` (`refetchIntervalInBackground: false`) |
| §5 "게시 중…" 먼저 표시 → 실제 글로 교체 | Task 3 `pending.ts`, `PendingPostsContext`, `ListPage` / Task 4 `WritePage` |
| §5 30초 후 상태 확인, 지연 중/실패 + 다시 시도 | Task 3 `dueForStatusCheck`, `fetchStatus`, 재시도 버튼 |
| §5 폼마다 Idempotency-Key, 재시도에도 같은 키 | Task 1 클라이언트 테스트, Task 4 `WritePage` 테스트 |
| §6 401 → 로그인 화면, 로그인 후 원래 화면 | Task 2 `RequireAuth`, `createQueryClient`, `safeNext` |
| §6 429/503 토스트 + 백오프 + 지터 최대 3회 | Task 1 `computeBackoff`, Task 2 `ToastProvider` |
| §6 409/403/422 메시지 | Task 1 `errorMessage` |
| §8 라우팅, SPA fallback, 캐시 헤더, gzip, keepalive | Task 5 |
| §8 `X-User-*` 제거, `/internal/*` 404 | Task 5 테스트 |
| §8 IP 기준 읽기·로그인 리밋, 사용자 기준 쓰기 리밋, 429 JSON + Retry-After | Task 6 (쓰기는 세션 쿠키 기준, 위 "스펙과 다른 점" 참고) |
| §8 real_ip(여러 CIDR), 수치는 환경변수로 주입 | Task 5 Dockerfile ENV, Task 6 `15-real-ip.sh`·템플릿·`test_config.py` |
| §8 `X-Request-ID` 전달 | Task 5 |
| §6 Redis 장애 시 읽기 정상·쓰기 503 | Task 6 `test_degraded.py` |
| §10 nginx 통합 테스트, Playwright 스모크 1개 | Task 5·6·7 |
| §11 `make fe-dev`, `make test` | Task 1, Task 7 |
