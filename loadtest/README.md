# 부하 테스트 (k6 disaster)

## 시나리오 버전

| 버전 | 모양 | 위치 |
|---|---|---|
| `disaster-v1` | 평상시 → 30초 만에 20배 → 5분 유지 → 감소 → 두 번째 폭증 (약 14분) | git 태그 `loadtest-disaster-v1` |
| `disaster-v2` (현재) | 토스증권 미국 정규장 개장 모양. 아래 참고 (약 19분) | `loadtest/disaster.js` |

v1을 다시 돌리려면 `git show loadtest-disaster-v1:loadtest/disaster.js > /tmp/disaster-v1.js`로 꺼내서
`loadtest/` 안에 두고 실행한다(`lib.js`를 상대 경로로 가져오기 때문).

### v2: 개장 모양

[토스증권 해외주식 안정화 글](https://toss.tech/article/28738)에 나온 개장 상황을 본떴다.

| 원문 | v2에서 |
|---|---|
| 22:30 정규장 개장 직후 주문 호출이 급격히 증가, 평소의 20배 이상 | 장전 2분(BASELINE) 뒤 **10초 만에 PEAK(BASELINE x 20)** |
| 운영 이슈 대부분이 개장 초반에 발생 | 개장 초반 5분(`open`)을 따로 집계 |
| 급증이 최소 2시간 지속 | 개장 지속 10분(`hold`). 2시간을 재현하지 않고 안정 상태에 들어가는지만 본다(아래 참고) |
| 개장 직후 예약 주문 100만 건 이상을 배치로 전송 | 개장 순간 사용자마다 예약 쓰기 4건(`reserved`, 기본 500명 x 4 = 2,000건)을 최대한 빨리 넣음 |

- 예약 쓰기 4건은 nginx의 세션당 순간 허용치(5건)보다 하나 적은 값이다. 같은 순간 실시간 쓰기가 같은 사용자로 들어와도 429가 나지 않게 한다.
- 실시간 트래픽과 예약 쓰기가 같은 순간에 겹친다. 큐에 쌓인 쓰기를 worker가 소화하는 동안 실시간 요청이 계속 들어오는 상황이다.
- 원문의 브로커 TPS 제한, 장애 감지, 브로커 전환은 본뜨지 않았다(서버 코드는 그대로).

### 2시간을 그대로 돌리지 않는 이유

같은 부하를 유지하면 보통 10분 안팎에 안정 상태에 들어간다(HPA가 Pod 수를 정하고, 캐시가 데워지고, 큐 길이가 일정해짐).
그 뒤로 같은 부하를 더 유지해서 새로 드러나는 것은 시간에 따라 쌓이는 값뿐이다. 이런 값은 일정한 속도로 늘거나
TTL에서 멈추므로, 2시간을 기다리지 않고 기울기와 "속도 x 시간"으로 판단한다.

| 쌓이는 것 | 설정 | PEAK 400 rps에서 2시간 뒤 | 판단 방법 |
|---|---|---|---|
| 글쓰기 큐 적체 | 상한 50,000건 | worker 처리가 유입보다 느리면 계속 증가 | `hold` 구간 측정(아래) |
| 멱등 키 (Redis) | TTL 10분 | 10분 뒤 약 36,000개에서 멈춤 | 계산 |
| 글 상태 키 (Redis) | TTL 1시간 | 1시간 뒤 약 216,000개에서 멈춤 | 계산 |
| 세션 (Redis) | TTL 1일 | 멈추지 않고 약 144,000개 | 계산 |
| 게시글 (DB) | 지우지 않음 | 약 432,000건 | 계산 |
| Redis 메모리 | 로컬 256MiB, noeviction | 세션 증가만큼 늘어남 | `hold` 구간 측정(아래) |
| Pod 메모리 | | 누수가 있으면 계속 증가 | `hold` 구간 측정(아래) |

쓰기 15%(초당 60건), 로그인 5%(초당 20건) 기준이다. TTL은 `services/board/src/board/config.py`, `services/auth/src/auth/config.py` 값이다.
실제 개장 후 2시간은 최고치가 그대로 유지되지 않고 서서히 줄어들므로, 2시간 내내 PEAK를 유지하는 건 현실보다 가혹한 가정이다.
그래도 장시간 테스트가 필요하면 `HOLD=2h`로 가끔 돌린다.

계산으로 알 수 없는 값(큐 적체, Redis 실제 메모리, Pod 메모리)은 `collect.py`로 잰다(k8s에서만 동작).
부하를 거는 동안 다른 터미널에서 `make loadtest-collect`를 켜 두고, 끝나면 `loadtest/results/resources.jsonl`을 그 실행의 결과 폴더로 옮긴다.
`store` 줄이 큐와 Redis 값이고, `top` 줄이 Pod 메모리다.

```sh
# 시각, 큐 길이, 가장 오래된 메시지 나이(초), DLQ, Redis 사용 MiB, Redis 상한 MiB(0이면 상한 없음)
jq -r 'select(.kind=="store") | [(.ts|todate), .queue_length, .queue_lag_seconds, .dlq_size, (.redis_used_mi|floor), (.redis_max_mi|floor)] | @tsv' resources.jsonl
# 시각, Pod, 메모리 MiB
jq -r 'select(.kind=="top") | [(.ts|todate), .pod, .mem_mi] | @tsv' resources.jsonl
```

`hold` 구간은 `meta.json`의 `finished_at`에서 `cool` 길이만큼 앞이 끝이고, 거기서 `hold` 길이만큼 앞이 시작이다. 이 구간의 시작과 끝을 비교한다.

- **큐 길이, 나이**: 일정한 범위를 오르내리면 worker가 따라가고 있는 것이다. 계속 늘면 상한까지 남은 시간 = (50,000 - 끝값) / 분당 증가량.
- **DLQ**: 0이 아니면 저장에 실패한 글이 있다는 뜻이다.
- **Redis 메모리**: 2시간 뒤 예상 = 시작값 + (끝값 - 시작값) / hold 길이 x 2시간. 이 값을 상한과 비교한다. 세션 키 때문에 조금씩 느는 것은 정상이다.
- **Pod 메모리**: hold 동안 Pod마다 계속 늘면 누수를 의심한다. HPA로 Pod 수가 바뀌므로 합계가 아니라 Pod별로 본다.

### 결과가 버전끼리 섞이지 않게 하는 방법

- 버전 이름은 `disaster.js`의 `SCENARIO_ID` 한 곳에만 있다. 시나리오 모양을 바꾸면 이 값을 올리고, 바꾸기 전 커밋에 `loadtest-disaster-<옛 버전>` 태그를 붙인다.
- 모든 메트릭에 `test:<버전>` 태그가 붙는다.
- Make 타깃은 결과를 `loadtest/results/<버전>/<날짜-시각>-<모드>/`에 남긴다. 모드는 `full`, `quick`, `local`이다.
  - `summary.json`: k6 요약 전체
  - `meta.json`: 버전, 모드, 사용자 수, 구간 길이, 요청 비율 등 실행 조건
- `k6 run`을 직접 부르면 `OUT`을 주지 않는 한 파일을 남기지 않는다.

## v1 로컬 실행 결과

아래는 **v1 시나리오**로 측정한 값이다. v2와 직접 비교할 수 없다.

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
- argon2 `parallelism=1` (해시당 스레드 1개, 기존 p=4로 만든 해시도 그대로 검증됨)
- `PROFILE=local` 축소 프로필, `PEAK` 덮어쓰기

이 수정 이후에는 부하를 다시 돌려 보지 않았다(노트북 자원 문제). **임계치는 클라우드(EKS/GKE) 목표값이며,
그쪽에서 다시 측정해야 한다.** 임계치는 완화하지 않았다.

## 로컬 오버레이의 nginx 리밋 완화 (`k8s/overlays/local-loadtest`)

로컬 결과는 **클라우드 리밋 설정과 비교할 수 없다.** 부하 테스트용 오버레이는 nginx 리밋을 다음처럼 바꾼다.

| 리밋 | base (클라우드) | local-loadtest |
|---|---|---|
| 읽기 (`RATE_READ` / `BURST_READ`, IP별) | 100r/s / 200 | **10000r/s / 20000** |
| 로그인, 가입 (`RATE_LOGIN` / `BURST_LOGIN`, IP별) | 30r/m / 10 | **6000r/m / 1000** |
| 글쓰기 (`RATE_WRITE` / `BURST_WRITE`, 세션 쿠키 sid별) | 2r/s / 5 | 그대로 (2r/s / 5) |

- 로그인 값은 `auth_sensitive` 존(`DELETE /api/auth/me`)에도 그대로 적용된다.
- 이유: k6는 PC 한 대(소스 IP 하나)에서 돌기 때문에, IP 기준 리밋을 그대로 두면 처리량이 리밋에서 막혀
  HPA가 늘어날 만큼의 부하가 서비스까지 가지 못한다(로그인 30r/m이면 setup 가입부터 429).
- 글쓰기 리밋은 sid별이라 사용자를 돌려 쓰면 걸리지 않으므로 유지한다. 걸린 429는 `rate_limited_429`로 따로 센다.

## 사용법

환경 변수 (`k6 run -e 이름=값`. Make 타깃은 아래 변수를 `make loadtest HOLD=1h`처럼 그대로 넘긴다):

| 변수 | 기본값 | 설명 |
|---|---|---|
| `BASE_URL` | `http://localhost:8080` | 대상 주소 |
| `USERS` | 500 (QUICK 20, local 60) | setup에서 가입하고 로그인할 사용자 수 |
| `BASELINE` | 20 (QUICK 5, local 4) | 장전 초당 요청 수 |
| `PEAK` | `BASELINE` x 20 | 개장 후 초당 요청 수 |
| `MIX` | `80,15,5` | 읽기, 쓰기, 로그인 비율(%). 쓰기 비중을 올리려면 `60,35,5` 등 |
| `PRE`, `RAMP`, `OPEN`, `HOLD`, `COOL` | `2m`, `10s`, `5m`, `10m`, `2m` | 장전, 개장 급증, 개장 초반, 개장 지속, 진정 구간 길이 |
| `RESERVED` | 4 | 개장 순간 사용자마다 넣는 예약 쓰기 수. 5를 넘기면 429가 난다 |
| `QUICK` | 없음 | `1`이면 모든 구간을 10초로 줄임(구간 길이 변수는 무시, 약 1분) |
| `PROFILE` | 없음 | `local`이면 노트북용 축소 기본값 사용 |
| `OUT` | 없음 | 결과 폴더. Make 타깃이 버전별 폴더를 만들어 넘긴다 |

임계치 (완화하지 않음, 실패 시 k6 종료 코드 99):
- `read_duration` p(95) < 300ms
- `write_accepted_duration` p(95) < 100ms
- `server_errors` rate < 1%

구간별 수치: 요약에 `{ phase:pre }`, `surge`, `open`, `hold`, `cool`, `reserved`로 나뉘어 나온다.
판정은 전체 기준으로만 하고, 구간별 값은 "개장 초반이 먼저 무너지는지" 보는 용도다.

커스텀 메트릭:
- `read_duration` (Trend): 200 응답 읽기(`GET /api/board/posts`, 다음 페이지 포함) 시간
- `write_accepted_duration` (Trend): 202로 접수된 글쓰기 시간
- `server_errors` (Rate): 5xx 비율. 백프레셔 503(`QUEUE_FULL`)은 제외
- `backpressure_503` (Counter): 백프레셔 503(`QUEUE_FULL`) 수
- `rate_limited_429` (Counter): nginx 리밋 429(`RATE_LIMITED`) 수

## 실행 방법

```
make loadtest                 # 전체 시나리오, 약 19분 (클라우드/큰 머신용)
make loadtest HOLD=2h         # 장시간 테스트, 가끔만 ("2시간을 그대로 돌리지 않는 이유" 참고)
make loadtest-quick           # 구간마다 10초, 약 1분, 동작 확인용
make loadtest-local PEAK=40   # 노트북용 축소판, 환경 변수 조절은 세 타깃 모두 지원
```

docker compose(`make dev`)에서 동작만 확인할 때는 `make loadtest-quick USERS=4 PEAK=10`처럼 작게 돌린다.
compose의 nginx는 로그인과 가입 리밋(30r/m, burst 10)이 그대로라 사용자를 많이 만들면 setup에서 429가 난다.

로컬에서는 먼저 `make k8s-local-loadtest`로 클러스터를 띄운다. Docker에 **최소 8GiB 메모리**(가능하면 CPU도 더)가 필요하다.

## 응답시간 분해 (층별 분석)

요청 하나의 응답시간을 층별로 나눠 보고, 어디서 시간이 드는지 역추정하기 위한 도구다.

### 무엇을 재는가

```
k6가 본 시간
 └ nginx rt                               (nginx perf 로그)
    ├ verify(vt) = verify 대기 + verify 앱  (auth_request /_verify)
    ├ upstream(urt) = 앱 앞 대기 + 앱 시간     (board-api 또는 auth)
    │    앱 시간 = redis + db(풀, 연결 + 쿼리) + argon2 대기 + argon2 계산 + 나머지(파이썬 CPU)
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
  대기 칸(앱 앞 대기, db 풀, 연결, argon2 대기)이 커지면 그 자원이 포화된 것이다.
- **4. 활용 법칙**: `요청당 CPU = 사용 코어 / 처리량`. 낮은 부하에서 구한 값이 곧 요청 하나의 CPU 비용이고,
  `필요 코어 = 목표 rps x 요청당 CPU`로 목표 부하에 필요한 Pod 수를 추정할 수 있다.
- **5. 노드 메모리**: PSI(메모리 압박)와 swap-in, major fault가 0보다 커지는 단계에서 모든 칸이 함께 느려지면 메모리 부족이다.
  재현: `docker update --memory 4g --memory-swap 12g simple-web-app-control-plane`
