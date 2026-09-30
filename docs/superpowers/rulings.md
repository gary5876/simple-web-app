# 구현 중 내린 결정 목록 (subagent-driven development, 2026-09-30~10-01)

각 줄은 '결정 — 이유 — 틀렸을 때의 비용' 형식이다. 계획별 순서대로 정리했다.

## 계획 1-backend

- Ruling: T1 must keep a `.superpowers/` line in .gitignore (merge with plan content) — SDD workspace must stay untracked — cost if wrong: none.
- Ruling: duplicated errors/infra/observability modules in auth and board stand — spec §2 says no shared package; separate build contexts/images — cost if wrong: later refactor into shared lib.
- Ruling: implementers use sonnet (not cheapest) — tasks run testcontainers/Docker and may need debugging beyond transcription — cost: higher token spend.
- Task 5: Ruling: reorder delete_account to soft_delete → publish_user_deleted → delete_all (plan had publish last) — spec §3 requires retry-completion; purging sessions before publish makes a failed XADD unrecoverable — cost if wrong: possible duplicate user_deleted events on retry (worker anonymize is idempotent)
- Task 8: Ruling: treat sqlalchemy.exc.TimeoutError (pool exhaustion) as a DB outage — add to cache.DB_ERRORS and to the 503 handler in BOTH services' errors.py; Task 9 is_transient must include it too — spec §4/§6 stale-or-503 under DB pressure — cost if wrong: none material
- Task 9: Ruling: is_transient classifies by SQLSTATE (walk orig/__cause__ for .sqlstate): class 08, class 53, 57P01/57P02/57P03, 25006, 40001, 40P01 are transient (in addition to existing rules) — spec §4 forbids DLQ during DB outage; plan's type-based check misses asyncpg server errors — cost if wrong: a truly bad row with those codes retries forever (not realistic)
- Task 9: Ruling: reclaim_posts_once probes DB (SELECT 1) before any delivery-count dead-lettering; if unreachable/transient → raise TransientDbError, no DLQ; _ack_posts also deletes failed:<id> so a late success wins — outage length must not decide DLQ — cost if wrong: one extra SELECT per reclaim pass (every 10s)
- Ruling: user_deleted race — worker sets deleted_user:<id> (EX 7200) on the event and _persist_posts anonymizes rows whose author is marked — spec §3 anonymization must hold under backlog — cost if wrong: one MGET per batch
- Ruling: remove delivery-count dead-lettering in reclaim; reclaimed entries always go through _persist_posts (non-transient failures still DLQ via poison path); _probe_db removed if unused — outage length must never decide DLQ — cost if wrong: a message that crashes the worker process (not an exception) would be retried forever
- Ruling: asyncpg command_timeout=5 in both services' make_engine — spec §7 no pod restarts from DB failure; blackholed DB must fail fast — cost: long queries >5s fail (none exist)
- Ruling: reproducible builds via committed uv.lock + exported requirements.lock per service; Dockerfiles install pinned deps then `pip install --no-deps .` — plan 3 pushes images to registries — cost: lockfile maintenance
- Ruling: also fix in this wave: verify broad except → degraded 200; metrics refresh in its own try; queue gauges only registered in worker process; catch-all 500 {code:"INTERNAL"} both services; cache.invalidate also deletes stale:posts:first; nickname strip; detail endpoint returns pending/failed from markers when DB fails
- Final: parked — two-worker race between deleted_user MGET and marker SET can still store one attributed post — Ruling: accepted, narrow window; anonymize is re-runnable manually — cost: rare attributed post
- Final: parked — deleted_user marker TTL 7200s; backlog older than 2h stores attributed posts — Ruling: accepted, outage >2h out of scope
- Final: parked — catch-all 500 lacks X-Request-ID/request_id and is double-logged (Starlette ServerErrorMiddleware order) — Ruling: accepted, cosmetic
- Final: parked — corrupt session JSON reported as degraded (writes 503 until TTL) — Ruling: accepted, corrupt sessions not realistically produced
- Final: parked — hatchling build backend unpinned in image build — Ruling: accepted, runtime deps pinned

## 계획 2-frontend-nginx

- Ruling: R1 proxy_hide_header X-Request-ID — duplicate header breaks request-id test — cost: none
- Ruling: R2 write limit keyed on $cookie_sid (spec says X-User-Id) — nginx phase order makes user id unavailable at limit time — cost: a user with many sessions gets several quotas
- Ruling: R3 fix Redis-restart comment only — cost: none
- Ruling: R4 tests/nginx venv via uv python 3.12 — cost: none
- Ruling: R5 keep `id` in /me (spec text said user_id) — code+plan agree — cost: spec wording mismatch to amend
- Ruling: R6 lockfile generated clean; regenerate in node:22-alpine if needed; never npm install in Dockerfile — reproducibility — cost: extra step
- Ruling: R7 test-e2e runs npm ci — cost: slower target
- Ruling: R8 SignupPage shows per-field 422/409 errors — spec §6 — cost: small UI code
- Ruling: R9 failed-post retry uses new idempotency key — cost: none
- Ruling: R10 UUID fallback for non-secure origins — cost: tiny helper
- Ruling: R11 lower test-stack read limits if flaky — cost: test config differs from prod defaults
- Task 2: Ruling: RequireAuth/Layout must treat /me query error (503) as "unavailable", not logged-out (plan code redirected) — spec: 503 ≠ logged out — cost: small UI state
- Ruling: carry Task 5 minors into Task 6 (same files) — cheap and in the touched area — cost: slightly larger Task 6 diff
- Ruling: client never auto-retries when Retry-After > 30s (and never for TOO_MANY_ATTEMPTS) — account lockout would freeze login UI up to 45 min — cost: none
- Ruling: rate-limit DELETE /api/auth/me in nginx via auth_login zone keyed per IP through a method map — protects password re-check against stolen-cookie brute force (spec gap) — cost: shares login budget per IP
- Ruling: also fix safeNext backslash/origin check and Layout pending state (cheap)
- Final: parked — no RED run shown for DELETE /me nginx limit test — Ruling: accepted; config diff + passing test adequate

## 계획 3-k8s-loadtest

- Ruling: R1 pipefail for kustomize|kubeconform — masked render errors — cost: none
- Ruling: R2 prune worker HPA/ScaledObject when switching overlays; wait KEDA webhooks — cost: none
- Ruling: R3 SIGNUP_BATCH=4 + NEW Task 0: argon2 semaphore in auth-svc (plan-1 code change) — disaster signup spike could OOM auth pods — cost: signup/login latency queues under burst
- Ruling: R4 relax read limits in local-loadtest — single-IP k6 — cost: loadtest config ≠ prod
- Ruling: R5 document per-replica limits — cost: none
- Ruling: R6 nginx worker autotune — cost: none
- Ruling: R7 halve local resource requests (Docker 3.8GiB) instead of asking user to resize Docker — cost: local HPA math differs from cloud
- Ruling: R8 pin kind node image + compatible KEDA version — cost: version bump from plan text
- Ruling: R9 kind load fallback via image-archive — cost: none
- Ruling: R10 accept spec deviations (netpol monitoring/keda, worker CPU HPA default, DB math in deploy.md) — cost: spec text mismatch
- Ruling: R11 wording fixes — cost: none
- Ruling: R12 migrate wait 600s — cost: none
- Task 1: Ruling: R1 implemented by rendering to a variable (macOS make 3.81 ignores .SHELLFLAGS) — accepted, fails correctly
- Task 5: Ruling: KEDA v2.19.0 (supports k8s 1.32–1.34) instead of plan's 2.15.1; real deployment names keda-metrics-apiserver/keda-admission — cost: none
- Ruling: NEW Task 6b — split verify into its own Deployment `auth-verify` (same auth image, no argon2 traffic) with its own HPA/PDB/NetworkPolicy; nginx /_verify → VERIFY_UPSTREAM (default auth:8000 for compose, auth-verify:8000 in k8s); auth readiness gets READY_REQUIRES_DB (false for auth-verify, which needs only Redis) — spec §6 intent: login storms must not take down reads — cost: one more Deployment, small backend+nginx change
- Ruling: Task 6 fix round after 6b — local-loadtest overlay caps HPA max (auth 4, auth-verify 4, board-api 6, nginx 4, worker 4) and restores full auth requests there; k6 gets env-scalable USERS/PEAK with documented LOCAL profile; thresholds unchanged (cloud targets) — laptop cannot host 20 argon2 pods — cost: local run is a scaled-down rehearsal
- Ruling: base liveness probes tolerate CPU starvation (timeoutSeconds 5, periodSeconds 10, failureThreshold 6; readiness timeoutSeconds 3) for all app Deployments incl. auth-verify/worker exec — spec §7 no restart cascades under load — cost: dead pods detected ≤60s later
- Ruling: no more load runs on this laptop; Task 6 closes with probe hardening + argon2 parallelism=1 (fewer threads per hash under CPU limits) committed; load-test thresholds recorded as "not met locally — node saturation; re-measure on cloud" — cost: k6 thresholds unverified
- Task 6: parked — Step 5 (<1% server errors) and Step 6 (HPA under full spike) unmet/unobserved — Ruling: accepted per user stopping load on this laptop; re-measure on cloud — cost: disaster thresholds unverified
- Task 6: complete (commits ff4712b..f0fd234 excl. 6b, 1 parked)
- Task 9: Ruling: Makefile K8S_VERSION 1.31.0→1.34.0 to match pinned kind node — cost: none
- Ruling: GKE FrontendConfig HTTP→HTTPS redirect — COOKIE_SECURE requires HTTPS — cost: none
- Ruling: GCP LB IP /32 as explicit placeholder in REAL_IP_FROM + placeholder table row — cost: placeholder IP trusted until replaced (documentation range)
- Ruling: nginx preStop sleep 15 (spec said 5) + ALB deregistration_delay 30s + GKE connectionDraining 30s + doc ALB readiness-gate namespace label — LB target removal lag causes 5xx on scale-down — cost: slower pod termination; spec deviation listed
- Ruling: argon2 semaphore acquire timeout 5s → 503 UNAVAILABLE Retry-After — avoid hashing for departed clients during storms — cost: some logins get 503 under extreme load
- Ruling: also fix hash_concurrency ≥1 validation, test_config nginx -t result, zone topologySpread (ScheduleAnyway), migrate wait also detects failed, deploy.md namespace ordering + k8s-validate mention + AWS trust-range note
- Ruling: exceed the one-fix-wave rule for a single scoped test fix (docker run --add-host for auth/board-api/auth-verify in both test_config tests) — leaving `make test` broken is worse than a small extra dispatch — cost: one extra small commit outside the standard process
