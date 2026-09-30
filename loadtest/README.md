# 부하 테스트 (k6 disaster)

## 로컬 실행 결과

로컬 kind 클러스터(Docker 6 CPU / 3.8GiB)에서는 임계치를 **충족하지 못했다.** 원인은 앱 로직이 아니라
노드 포화(control plane + ~20개 Pod + k6가 6 vCPU를 나눠 씀)다.

측정값 (1차 수정 라운드):

| 실행 | read p95 (목표 <300ms) | write p95 (목표 <100ms) | server_errors (목표 <1%) |
|---|---|---|---|
| `make loadtest-quick` | 568ms | 1.22s | 90.29% |
| `make loadtest-local` (USERS=60, PEAK=80, 14분) | 2.03s | 2.25s | 90.81% |

- 재시작: 실행 중 누적 Pod 재시작 9 -> 90 (전부 exit 137 = liveness probe 강제 종료).
  control plane(controller-manager, scheduler)도 CrashLoop, metrics-server 재시작으로 HPA 메트릭이 `<unknown>`이 됨.
- 종료 후 노드 load average 11/34/33 (6 CPU), swap 사용.

대응으로 바꾼 것:
- verify 분리: `auth-verify` Deployment (argon2 트래픽과 분리)
- 로컬 오버레이(`local-loadtest`)에서 HPA 상한 제한 (auth 4, auth-verify 4, board-api 6, nginx 4, worker 4)
- probe 강화: liveness timeout 5s x 6회, readiness timeout 3s (CPU 기아 때 재시작 폭주 방지)
- argon2 `parallelism=1` (해시당 스레드 1개; 기존 p=4 해시도 검증됨)
- `PROFILE=local` 축소 프로필, `PEAK` 덮어쓰기

이 수정 이후에는 부하를 다시 돌려 보지 않았다(노트북 자원 문제). **임계치는 클라우드(EKS/GKE) 목표값이며,
그쪽에서 다시 측정해야 한다.** 임계치는 완화하지 않았다.

## 실행 방법

```
make loadtest                 # 전체 시나리오 (클라우드/큰 머신용)
make loadtest-quick           # 약 1분 30초, 동작 확인용
make loadtest-local PEAK=40   # 노트북용 축소판, PEAK 조절
```

로컬에서는 먼저 `make k8s-local-loadtest`로 클러스터를 띄운다. Docker에 **최소 8GiB 메모리**(가능하면 CPU도 더)가 필요하다.
