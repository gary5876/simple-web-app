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
