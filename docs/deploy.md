# 배포 가이드

`one-click-deploy-k8s` 인프라 프로젝트가 따라야 하는 배포 절차와 계약, 그리고 DB 커넥션 계산을 담는다.

## 사전 요구사항
- Kubernetes 1.32 이상. KEDA v2.19.0을 쓴다면 1.32~1.34 범위여야 한다.
- **metrics-server** (HPA에 필수. GKE는 기본 포함, EKS는 설치 필요, kind는 `make k8s-local`이 설치).
- **Redis 7 이상, `maxmemory-policy noeviction`.** 메모리가 부족할 때 글쓰기 큐(`posts:stream`)나 세션이 임의로 삭제되면 안 된다.
- **HTTPS 필수.** 클라우드 overlay는 `COOKIE_SECURE=true`라서 HTTP로 접속하면 로그인이 되지 않는다.
- 로컬 kind와 부하 테스트: Docker에 **8GiB 이상**의 메모리를 권장한다. 3.8GiB에서는 노드가 포화되어 Pod 재시작이 폭주했다([loadtest/README.md](../loadtest/README.md)). local overlay는 리소스 requests를 절반으로 낮춰 두었지만 limits는 그대로다.
- 로컬 도구: kind, kubectl, kustomize(클라우드 이미지 지정용), kubeconform, k6, uv, node.
- 배포 전에 교체할 자리표시자(overlay에 예시 값이 들어 있다):

| 자리표시자 | 위치 | 예시 값 |
|---|---|---|
| ECR 계정 ID / 리전 | `k8s/overlays/aws/kustomization.yaml` `images` | `123456789012`, `ap-northeast-2` |
| Artifact Registry 프로젝트 / 리전 | `k8s/overlays/gcp/kustomization.yaml` `images` | `my-gcp-project`, `asia-northeast3` |
| 도메인 | 각 overlay `ingress.yaml`의 host, GCP `managedcertificate.yaml` | `board.example.com` |
| ACM 인증서 ARN | `k8s/overlays/aws/ingress.yaml` `certificate-arn` | `arn:aws:acm:...:certificate/0000...` |
| VPC CIDR | `k8s/overlays/aws/kustomization.yaml` `REAL_IP_FROM` | `10.0.0.0/16` |

## overlay 목록
| overlay | 용도 | Postgres/Redis | worker 확장 |
|---|---|---|---|
| `k8s/overlays/local` | kind 로컬 실행 | 클러스터 내부 StatefulSet | CPU HPA |
| `k8s/overlays/local-loadtest` | 로컬 부하 테스트 (IP 리밋 완화, HPA 상한 축소) | 클러스터 내부 StatefulSet | KEDA (스트림 길이) |
| `k8s/overlays/aws` | EKS | RDS, ElastiCache (Terraform) | CPU HPA (KEDA 선택) |
| `k8s/overlays/gcp` | GKE | Cloud SQL, Memorystore (Terraform) | CPU HPA (KEDA 선택) |

워크로드: `nginx`, `auth`, `auth-verify`, `board-api`, `board-worker`, Job `db-migrate`.

### auth-verify를 따로 둔 이유
nginx는 보호된 요청마다 `auth_request`로 세션을 검증한다(`VERIFY_UPSTREAM=auth-verify:8000`). 이 검증이 로그인·가입(argon2 해시, CPU 집약)과 같은 Pod를 쓰면, 로그인 폭주 때 해시가 CPU를 다 써서 세션 검증이 밀리고 모든 요청이 지연된다. 그래서 `auth-verify` Deployment를 분리했다. 같은 auth 이미지이지만 검증 경로만 받으며, HPA(2~10, CPU 60%)와 PDB(`minAvailable: 1`)를 따로 가진다. `/internal/verify`는 NetworkPolicy로 nginx Pod에서만 닿는다.

## 로컬
```bash
docker compose down            # compose도 8080을 쓰므로 먼저 내린다
make k8s-local                 # http://localhost:8080
make k8s-smoke
make k8s-down
```
부하 테스트:
```bash
make k8s-local-loadtest        # KEDA 설치 + local-loadtest overlay
kubectl -n simple-web-app get hpa -w   # 다른 터미널
make loadtest-local            # 노트북용 축소판, 약 14분. 동작 확인만은 make loadtest-quick
```
`make loadtest`는 클라우드 규모의 전체 시나리오이다. 조절: `make loadtest K6_ARGS="-e BASELINE=50 -e USERS=1000"` (PEAK 기본값은 BASELINE x 20). 결과와 변수 설명은 [loadtest/README.md](../loadtest/README.md)를 본다. **로컬에서는 임계치를 충족하지 못했으며, 임계치는 클라우드 목표값이므로 EKS/GKE에서 다시 측정해야 한다.**

## 클라우드 배포 절차
```bash
# 1. 이미지 빌드·푸시 (TAG는 git sha 권장)
docker build -t $REGISTRY/simple-web-app/auth:$TAG -f services/auth/Dockerfile .
docker build -t $REGISTRY/simple-web-app/board:$TAG -f services/board/Dockerfile .
docker build -t $REGISTRY/simple-web-app/frontend:$TAG -f frontend/Dockerfile .
docker push $REGISTRY/simple-web-app/auth:$TAG
docker push $REGISTRY/simple-web-app/board:$TAG
docker push $REGISTRY/simple-web-app/frontend:$TAG

# 2. 이미지 지정 (overlay 디렉터리에서, kustomize CLI 필요)
cd k8s/overlays/aws   # 또는 gcp
kustomize edit set image \
  simple-web-app/auth=$REGISTRY/simple-web-app/auth:$TAG \
  simple-web-app/board=$REGISTRY/simple-web-app/board:$TAG \
  simple-web-app/frontend=$REGISTRY/simple-web-app/frontend:$TAG
cd -

# 3. Secret 준비 (Terraform이 생성): app-db(DATABASE_URL), app-redis(REDIS_URL)
kubectl -n simple-web-app get secret app-db app-redis

# 4. 적용 -> 마이그레이션 대기 -> 롤아웃 대기
kubectl -n simple-web-app delete job db-migrate --ignore-not-found --wait=true
kubectl apply -k k8s/overlays/aws
kubectl -n simple-web-app wait --for=condition=complete job/db-migrate --timeout=600s
for d in nginx auth auth-verify board-api board-worker; do
  kubectl -n simple-web-app rollout status deployment/$d --timeout=300s
done
```
Job의 Pod 템플릿은 변경할 수 없으므로, 기존 Job을 지운 뒤 적용한다.

**주의:** 마이그레이션 Job과 앱 Pod가 동시에 시작된다. 스키마 변경은 이전 버전 코드와 호환되게(컬럼 추가 -> 코드 배포 -> 이전 컬럼 제거 순서) 작성해야 한다. 첫 배포에서는 테이블이 만들어지기 전 몇 초 동안 API가 5xx를 낼 수 있다.

## 인프라 프로젝트가 제공해야 하는 것
| 항목 | AWS | GCP | 앱 쪽 참조 위치 |
|---|---|---|---|
| 이미지 레지스트리 | ECR `simple-web-app/{frontend,auth,board}` | Artifact Registry `simple-web-app` 저장소 | overlay `images` |
| Secret `app-db` | `DATABASE_URL=postgresql+asyncpg://...` (RDS) | 동일 (Cloud SQL) | base `secretKeyRef` |
| Secret `app-redis` | `REDIS_URL=redis://...` 또는 `rediss://...` (ElastiCache) | 동일 (Memorystore) | base `secretKeyRef` |
| Ingress 컨트롤러 | AWS Load Balancer Controller (`ingressClassName: alb`) | GKE 기본 GCE Ingress | overlay `ingress.yaml` |
| 도메인 + 인증서 | ACM 인증서 ARN, 도메인 | 도메인 (ManagedCertificate가 발급), 전역 고정 IP `simple-web-app-ip` | `ingress.yaml`, `managedcertificate.yaml` |
| LB 대역 (`REAL_IP_FROM`) | VPC CIDR | GFE 대역(기본값 포함) + **LB 고정 IP/32 추가** | overlay `nginx-config` |
| metrics-server | 필수 (HPA) | GKE 기본 포함 | - |
| KEDA | 선택 (v2.19.0) | 선택 (v2.19.0) | `components/keda-worker` |
| Prometheus | 선택. 네임스페이스 이름이 `monitoring`이어야 NetworkPolicy가 수집을 허용 | 동일 | base `networkpolicy.yaml` |
| NetworkPolicy 적용 CNI | VPC CNI의 network policy 기능 활성화 | Dataplane V2 | - |

### REAL_IP_FROM
- **AWS:** ALB는 VPC 서브넷의 IP로 접속하므로 `REAL_IP_FROM` = VPC CIDR(Terraform 값과 일치).
- **GCP:** GFE 대역(`130.211.0.0/22 35.191.0.0/16`)이 기본값이다. GCE LB는 `X-Forwarded-For` 끝에 LB 자신의 IP를 붙인다. 이 IP를 신뢰 대역에 넣지 않으면 모든 사용자의 IP가 LB IP로 인식되어 IP 리밋을 전원이 공유한다. **배포할 때** overlay의 `REAL_IP_FROM` 값 끝에 전역 고정 IP `simple-web-app-ip`를 ` <IP>/32`로 추가한다. 값은 공백으로 구분한 CIDR 목록이다.

### nginx 리밋
- `limit_req` 존은 **nginx Pod(replica)마다 따로** 존재한다. nginx HPA가 2~10개로 늘면 실효 한도는 대략 `rate x replicas`이고 burst도 마찬가지다.
- `RATE_LOGIN`/`BURST_LOGIN`은 로그인·가입(`auth_login`)뿐 아니라 `auth_sensitive` 존(`DELETE /api/auth/me`)에도 적용된다.
- 글쓰기 리밋(`RATE_WRITE`/`BURST_WRITE`)의 키는 사용자가 아니라 **세션 쿠키(sid)**이다. 같은 사용자가 세션을 여러 개 가지면 각각 따로 센다.
- 기본값: 읽기 100r/s(burst 200), 글쓰기 2r/s(burst 5), 로그인 30r/m(burst 10).

### KEDA를 쓰는 경우
KEDA v2.19.0은 k8s 1.32~1.34를 지원한다. 로컬은 `make k8s-keda-install`로 설치한다. 클라우드 overlay에서 `components` 주석을 풀면 CPU HPA(`board-worker`)가 삭제되고 `ScaledObject`가 대신 HPA를 만든다. 트리거는 `redis-streams`의 `streamLength: 500`(worker가 XACK + XDEL 하므로 `XLEN posts:stream` = 밀린 작업량)이고 min 1 / max 10이다. overlay의 `patches`에 Redis 주소를 지정한다.
```yaml
patches:
  - target:
      kind: ScaledObject
      name: board-worker
    patch: |-
      - op: replace
        path: /spec/triggers/0/metadata/address
        value: <redis-host>:6379
```
- **네트워크 허용:** KEDA operator가 매니지드 Redis에 접속할 수 있어야 한다. 클라우드 overlay에는 이 허용이 없으므로, 보안 그룹(ElastiCache) / 방화벽·NetworkPolicy(Memorystore)에서 KEDA Pod 대역의 6379 접근을 열어야 한다. (로컬 overlay의 `networkpolicy-data.yaml`은 `keda` 네임스페이스를 허용한다.)
- Redis에 비밀번호나 TLS가 있으면 KEDA `TriggerAuthentication`을 추가한다(`enableTLS`, `password`).

## DB 커넥션 계산
각 서비스의 SQLAlchemy 엔진은 Pod당 pool 5 + overflow 5 = 최대 10 커넥션이다(`services/*/src/*/infra.py`). `auth-verify`는 주로 Redis만 쓰지만 같은 auth 코드라 엔진 설정이 같고, DB에 닿을 수 있으면 풀 상한이 그대로 계산에 들어간다. 아래는 모든 HPA가 최대값에 도달한 최악의 경우이다.

| 워크로드 | 최대 Pod | Pod당 커넥션 | 합계 |
|---|---|---|---|
| auth | 20 | 10 | 200 |
| auth-verify | 10 | 10 | 100 |
| board-api | 20 | 10 | 200 |
| board-worker | 10 | 10 | 100 |
| db-migrate Job | 1 | 1 (`asyncpg.connect()` 단일 연결) | 1 |
| **합계** | | | **601** |

- Postgres `max_connections`는 **650 이상**으로 설정한다(601 + 관리·모니터링 여유분). 풀은 필요할 때 커넥션을 만들므로 평상시에는 훨씬 적게 쓴다. 최악 상황 대비 값이다.
- 일반 `local` overlay는 base의 `maxReplicas`를 그대로 유지하고, `local-loadtest`만 상한을 낮춘다(auth 4, auth-verify 4, board-api 6, nginx 4, worker 4). `local`의 `max_connections=600`은 노트북 자원으로는 HPA 최대값(601)까지 갈 수 없다는 전제이며 클라우드에는 쓰지 않는다.
- RDS/Cloud SQL 인스턴스 크기에 따라 기본 `max_connections`가 이보다 작을 수 있다. 이때는 파라미터를 올리거나, RDS Proxy / PgBouncer를 두고 `DATABASE_URL`을 프록시 주소로 바꾼다.
- HPA `maxReplicas`(`k8s/base/hpa.yaml`)나 풀 크기를 바꾸면 이 표도 다시 계산한다.
- 로그인 부하: argon2는 `parallelism=1`이고 Pod당 동시 해시 수는 `HASH_CONCURRENCY=4`(기본값)로 제한한다. 해시가 몰려도 Pod 하나가 CPU와 메모리(limit 512Mi)를 무한정 쓰지 않는다. 기존 parallelism=4로 만든 해시도 계속 검증된다.

## 스펙과 다른 점 (의도적)
1. **worker PDB `maxUnavailable: 1`:** 스펙은 모든 앱 워크로드에 `minAvailable: 1`이지만, 최소 1개로 운영되는 worker에 적용하면 노드 drain이 막힌다.
2. **배포 순서:** 스펙은 "마이그레이션 완료 대기 후 apply"이지만, 여기서는 전체 apply 후 Job 완료를 기다린다. 대신 스키마 변경을 하위 호환으로 작성해야 한다(위 주의 참고).
3. **NetworkPolicy 허용:** 스펙에 없던 `monitoring` 네임스페이스(Prometheus 수집)와 `keda` 네임스페이스(로컬 Redis 접근) 허용을 추가했다. 클라우드의 KEDA->Redis 허용은 인프라 쪽 계약이다.
4. **worker 확장:** 기본은 CPU HPA이고, KEDA(`streamLength`)는 선택 component이다. local-loadtest만 KEDA를 쓴다.
5. **auth-verify 분리:** 스펙의 auth 하나를 auth(로그인·가입 등)와 auth-verify(nginx 세션 검증)로 나눴다. 기본 HPA 2~10, DB 커넥션 계산에 포함된다.
6. **글쓰기 리밋 키:** 사용자가 아니라 세션 쿠키(sid). nginx `limit_req`가 `auth_request`보다 먼저 실행되어 사용자 ID를 알 수 없기 때문이다. 리밋은 replica별이라 실효 한도는 rate x replicas이다.
7. **DB 커넥션 계산:** README가 아니라 이 문서에 둔다.
8. **probe 강화:** liveness timeout 5초 / period 10초 / failureThreshold 6, readiness timeout 3초. CPU 기아 상태에서 probe 한 번 늦었다고 Pod를 죽여 재시작이 폭주하던 문제(로컬 측정에서 관찰)에 대한 대응이다.
9. **argon2:** `parallelism=1`, Pod당 `HASH_CONCURRENCY=4`.
10. **인프라 계약 추가:** 도메인·인증서, GCP 전역 고정 IP, `monitoring` 네임스페이스 이름, Redis `noeviction`과 7 이상, HTTPS 필수, `REAL_IP_FROM` 규칙.
11. **local-loadtest overlay:** 한 PC(IP 하나)에서 부하를 낼 수 있도록 읽기·로그인 IP 리밋을 완화하고 HPA 상한을 낮춘다. 클라우드 리밋과 비교할 수 없다.
