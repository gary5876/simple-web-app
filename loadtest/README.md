# 부하 테스트 (k6 disaster)

## 로컬 실행 결과

로컬 kind 클러스터(Docker 6 CPU / 3.8GiB)에서는 임계치를 **충족하지 못했다.** 가장 가능성이 높은 원인은
노드 포화(control plane + ~20개 Pod + k6가 6 vCPU를 나눠 씀)다. 다만 이것만 따로 떼어 검증하지는 못했다
(앱 쪽 원인을 배제하는 단독 실험 없음).

측정값 (1차 수정 라운드):

| 실행 | read p95 (목표 <300ms) | write p95 (목표 <100ms) | server_errors (목표 <1%) |
|---|---|---|---|
| `make loadtest-quick` | 568ms | 1.22s | 90.29% |
| `make loadtest-local` (USERS=60, PEAK=80, 14분) | 2.03s | 2.25s | 90.81% |

- 재시작: 실행 중 누적 Pod 재시작 9 -> 90 (전부 exit 137 = liveness probe 강제 종료).
  control plane(controller-manager, scheduler)도 CrashLoop, metrics-server 재시작으로 HPA 메트릭이 `<unknown>`이 됨.
- 종료 후 노드 load average 11/34/33 (6 CPU), swap 사용.

대응으로 바꾼 것 (효과 미검증 — 수정 후 부하 재실행 없음):
- verify 분리: `auth-verify` Deployment (argon2 트래픽과 분리)
- 로컬 오버레이(`local-loadtest`)에서 HPA 상한 제한 (auth 4, auth-verify 4, board-api 6, nginx 4, worker 4)
- probe 강화: liveness timeout 5s x 6회, readiness timeout 3s (CPU 기아 때 재시작 폭주 방지)
- argon2 `parallelism=1` (해시당 스레드 1개; 기존 p=4 해시도 검증됨)
- `PROFILE=local` 축소 프로필, `PEAK` 덮어쓰기

이 수정 이후에는 부하를 다시 돌려 보지 않았다(노트북 자원 문제). **임계치는 클라우드(EKS/GKE) 목표값이며,
그쪽에서 다시 측정해야 한다.** 임계치는 완화하지 않았다.

## 로컬 오버레이의 nginx 리밋 완화 (`k8s/overlays/local-loadtest`)

로컬 결과는 **클라우드 리밋 설정과 비교할 수 없다.** 부하 테스트용 오버레이는 nginx 리밋을 다음처럼 바꾼다.

| 리밋 | base (클라우드) | local-loadtest |
|---|---|---|
| 읽기 (`RATE_READ` / `BURST_READ`, IP별) | 100r/s / 200 | **10000r/s / 20000** |
| 로그인·가입 (`RATE_LOGIN` / `BURST_LOGIN`, IP별) | 30r/m / 10 | **6000r/m / 1000** |
| 글쓰기 (`RATE_WRITE` / `BURST_WRITE`, 세션 쿠키 sid별) | 2r/s / 5 | 그대로 (2r/s / 5) |

- 로그인 값은 `auth_sensitive` 존(`DELETE /api/auth/me`)에도 그대로 적용된다.
- 이유: k6는 PC 한 대(소스 IP 하나)에서 돌기 때문에, IP 기준 리밋을 그대로 두면 처리량이 리밋에서 막혀
  HPA가 늘어날 만큼의 부하가 서비스까지 가지 못한다(로그인 30r/m이면 setup 가입부터 429).
- 글쓰기 리밋은 sid별이라 사용자를 돌려 쓰면 걸리지 않으므로 유지한다. 걸린 429는 `rate_limited_429`로 따로 센다.

## 사용법

환경 변수 (`k6 run -e 이름=값`, Make 타깃은 `USERS`/`PEAK`를 그대로 넘긴다):

| 변수 | 기본값 | 설명 |
|---|---|---|
| `BASE_URL` | `http://localhost:8080` | 대상 주소 |
| `USERS` | 500 (QUICK 20, local 60) | setup에서 가입·로그인할 사용자 수 |
| `BASELINE` | 20 (QUICK 5, local 4) | 평상시 초당 요청 수 |
| `PEAK` | `BASELINE` x 20 | 폭증 구간 초당 요청 수 |
| `QUICK` | 없음 | `1`이면 각 단계를 10초로 줄임(약 1분 30초) |
| `PROFILE` | 없음 | `local`이면 노트북용 축소 기본값 사용 |

임계치 (완화하지 않음, 실패 시 k6 종료 코드 99):
- `read_duration` p(95) < 300ms
- `write_accepted_duration` p(95) < 100ms
- `server_errors` rate < 1%

커스텀 메트릭:
- `read_duration` (Trend): 200 응답 읽기(`GET /api/board/posts`, 다음 페이지 포함) 시간
- `write_accepted_duration` (Trend): 202로 접수된 글쓰기 시간
- `server_errors` (Rate): 5xx 비율. 백프레셔 503(`QUEUE_FULL`)은 제외
- `backpressure_503` (Counter): 백프레셔 503(`QUEUE_FULL`) 수
- `rate_limited_429` (Counter): nginx 리밋 429(`RATE_LIMITED`) 수

## 실행 방법

```
make loadtest                 # 전체 시나리오 (클라우드/큰 머신용)
make loadtest-quick           # 약 1분 30초, 동작 확인용
make loadtest-local PEAK=40   # 노트북용 축소판, PEAK/USERS 조절 (세 타깃 모두 지원)
```

로컬에서는 먼저 `make k8s-local-loadtest`로 클러스터를 띄운다. Docker에 **최소 8GiB 메모리**(가능하면 CPU도 더)가 필요하다.

## 응답시간 분해 (층별 분석)

요청 하나의 응답시간을 층별로 나눠 보고, 어디서 시간이 드는지 역추정하기 위한 도구다.

### 무엇을 재는가

```
k6가 본 시간
 └ nginx rt                               (nginx perf 로그)
    ├ verify(vt) = verify 대기 + verify 앱  (auth_request /_verify)
    ├ upstream(urt) = 앱 앞 대기 + 앱 시간     (board-api 또는 auth)
    │    앱 시간 = redis + db(풀·연결 + 쿼리) + argon2 대기 + argon2 계산 + 나머지(파이썬 CPU)
    └ nginx 자체
```

- 앱은 구간별 시간을 `Server-Timing` 헤더로 보낸다(`services/*/src/*/timing.py`). nginx가 이를 perf 로그에 남기고 클라이언트에는 숨긴다.
- "앱 앞 대기"(urt - 앱 시간)는 요청이 uvicorn/이벤트 루프에서 줄 서 있던 시간이다. 앱 안에서는 보이지 않아 뺄셈으로만 구한다.
  같은 현상을 앱 쪽에서 본 지표가 `/metrics`의 `event_loop_lag_seconds`다.
- 평균은 층끼리 더해지지만 p95는 더해지지 않는다. 그래서 분해표는 평균, p95는 구간별로 따로 낸다.

### 계측을 믿어도 되는지 (교정)

계측 코드를 읽지 않고도 확인할 수 있게, 정답을 아는 상황을 만들어 맞히는지 보는 테스트가 있다(`services/*/tests/test_timing.py`).

| 테스트 | 만드는 상황 (앱 코드 밖) | 기대 |
|---|---|---|
| Redis 지연 | `CLIENT PAUSE 200` | redis 구간만 약 200ms 늘어남 |
| DB 지연 | 다른 연결이 `LOCK TABLE`을 200ms 잡음 | db 쿼리 구간만 약 200ms 늘어남 |
| argon2 대기 | 테스트가 argon2 슬롯을 200ms 쥐고 있음 | argon2 대기만 늘고 계산 시간은 그대로 |
| 호출 횟수 | 글쓰기 1회 | Redis 왕복 정확히 4회, DB 0회 |

`make test-backend`로 돌린다. 분석 표의 "층 순서 위반"(안쪽 층이 바깥 층보다 긴 요청 수)이 0이 아니면 계측이나 파싱을 의심한다.

### 실행

로컬 확인 (docker compose, 낮은 부하만):

```
make dev   # 다른 터미널
make loadtest-steps RATES=3,10 STEP=20 USERS=4
docker compose logs nginx --no-log-prefix > loadtest/results/nginx.log
python3 loadtest/analyze.py --nginx loadtest/results/nginx.log --k6 loadtest/results/steps-summary.json
```

GCP 실험실 (kind VM + k6 VM, `scripts/gcp-lab.sh`):

```
export GCP_PROJECT=<프로젝트 ID> GCP_ACCOUNT=<계정>   # 계정은 선택
scripts/gcp-lab.sh up && scripts/gcp-lab.sh push   # push는 도구 설치가 끝난 뒤(약 3분)

# lab VM (scripts/gcp-lab.sh ssh)
cd ~/simple-web-app && make k8s-local-loadtest
make loadtest-collect                       # 부하 동안 켜 둔다(Ctrl+C로 종료)

# k6 VM (scripts/gcp-lab.sh ssh-k6). BASE_URL은 status의 lab 내부 IP
cd ~/simple-web-app && make loadtest-steps BASE_URL=http://<lab 내부 IP>:8080 RATES=5,20,50,100,200
# 끝나면 요약 JSON을 lab VM으로 옮긴다(아래 status로 IP 확인)

# lab VM
make loadtest-analyze SINCE=30m             # loadtest/results/report.md

scripts/gcp-lab.sh down                     # 다 쓰면 삭제
```

HPA가 Pod 수를 바꾸면 단계끼리 비교가 어려워진다. 처리시간 자체를 볼 때는 Pod 수를 고정한다:
`kubectl -n simple-web-app patch hpa board-api -p '{"spec":{"minReplicas":2,"maxReplicas":2}}'`

### 표 읽기

- **2. 평균 분해**: 부하를 올릴 때 어느 칸이 먼저 커지는지 본다. 처리시간(redis, db 쿼리, 앱 나머지)은 거의 그대로이고
  대기 칸(앱 앞 대기, db 풀·연결, argon2 대기)이 커지면 그 자원이 포화된 것이다.
- **4. 활용 법칙**: `요청당 CPU = 사용 코어 / 처리량`. 낮은 부하에서 구한 값이 곧 요청 하나의 CPU 비용이고,
  `필요 코어 = 목표 rps x 요청당 CPU`로 목표 부하에 필요한 Pod 수를 추정할 수 있다.
- **5. 노드 메모리**: PSI(메모리 압박)와 swap-in, major fault가 0보다 커지는 단계에서 모든 칸이 함께 느려지면 메모리 부족이다.
  재현: `docker update --memory 4g --memory-swap 12g simple-web-app-control-plane`
