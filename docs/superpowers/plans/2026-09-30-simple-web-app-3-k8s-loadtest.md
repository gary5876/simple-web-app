# simple-web-app PLAN 3: k8s 매니페스트 · 로컬 클러스터 · 부하 테스트 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PLAN 1(백엔드)과 PLAN 2(프론트+nginx)가 만든 이미지 3개를 k8s에서 실행하는 Kustomize 매니페스트(base, local, local-loadtest, aws, gcp)와 kind 로컬 배포, k6 재난 부하 테스트, 배포 문서를 만든다.

**Architecture:** `k8s/base`에는 클라우드 중립적인 Deployment/Service/HPA/PDB/NetworkPolicy/Job을 둔다. 클라우드별 차이(이미지 레지스트리, Ingress, 실제 IP 대역, Secret 출처)는 overlay에만 둔다. board-worker의 큐 길이 기반 확장(KEDA)은 Kustomize component로 분리해서 overlay가 선택한다. 부하 테스트는 로그인 IP 리밋을 풀어 둔 `local-loadtest` overlay에서 k6로 실행한다.

**Tech Stack:** Kubernetes 1.31 API, Kustomize(`kubectl kustomize`), kubeconform, kind, metrics-server v0.7.2, KEDA v2.15.1, k6, PostgreSQL 16, Redis 7.2

**Spec:** `docs/superpowers/specs/2026-09-30-simple-web-app-design.md` (§7 k8s 구성, §8 nginx, §11 실행 방법, §12 인프라 계약)

**선행 조건:** PLAN 1, PLAN 2가 완료되어 다음 명령으로 이미지 3개가 빌드되어야 한다.
```bash
docker build -t simple-web-app/auth:dev -f services/auth/Dockerfile .
docker build -t simple-web-app/board:dev -f services/board/Dockerfile .
docker build -t simple-web-app/frontend:dev -f frontend/Dockerfile .
```

## Global Constraints

- 네임스페이스: `simple-web-app`
- 공통 라벨: `app.kubernetes.io/name=<component>`, `app.kubernetes.io/part-of=simple-web-app` (part-of는 selector에 넣지 않는다)
- Service 이름·포트: `auth`(8000), `board-api`(8000), `nginx`(80→8080), `postgres`(5432), `redis`(6379)
- 이미지 이름: `simple-web-app/frontend`, `simple-web-app/auth`, `simple-web-app/board`. 로컬 태그 `dev`, `imagePullPolicy: IfNotPresent`
- 백엔드 컨테이너: 포트 8000, `/healthz`(liveness), `/readyz`(readiness), `/metrics`, uid 10001 non-root
- board-worker: `python -m board.worker`, 메트릭 포트 9100, liveness exec `python -m board.worker_health`
- db-migrate: `python -m board.migrate` (멱등)
- Secret: `app-db`(키 `DATABASE_URL`), `app-redis`(키 `REDIS_URL`)
- 앱 환경변수: `SESSION_TTL_SECONDS=86400`, `QUEUE_MAX_LEN=50000`, `COOKIE_SECURE=true`(local은 `false`), `LOG_LEVEL=INFO`
- nginx 환경변수: `AUTH_UPSTREAM=auth:8000`, `BOARD_UPSTREAM=board-api:8000`, `REAL_IP_FROM`(공백으로 구분한 CIDR 목록 허용), `RATE_READ=100r/s`, `BURST_READ=200`, `RATE_WRITE=2r/s`, `BURST_WRITE=5`, `RATE_LOGIN=30r/m`, `BURST_LOGIN=10`. 헬스 체크 `/nginx-health`
- HPA: CPU 60%, scale-up `stabilizationWindowSeconds: 0` + 15초마다 최대 100%, scale-down `stabilizationWindowSeconds: 300`
- min/max: nginx 2/10, auth 2/20, board-api 2/20, board-worker 1/10
- resources: nginx 100m/128Mi 요청, 500m/256Mi 제한 · auth 250m/256Mi, 1000m/512Mi · board-api 200m/256Mi, 1000m/512Mi · worker 100m/128Mi, 500m/256Mi
- 모든 앱 Pod: `preStop: sleep 5`(worker 제외), `terminationGracePeriodSeconds: 30`
- DB 커넥션: Pod당 pool 5 + overflow 5 → 최대 (20+20+10)×10 = 500
- board-worker는 처리한 항목을 XACK + XDEL 하므로 `XLEN posts:stream` = 밀린 작업량. KEDA는 `streamLength` 트리거를 쓴다
- 이 Mac에는 kind, k6, kubeconform이 설치되어 있지 않다. `kubectl`(1.36)은 있다. Kustomize는 `kubectl kustomize`를 쓴다
- Makefile은 PLAN 1이 만들었다. 이 계획은 타깃을 **추가만** 한다. 레시피 줄은 반드시 **탭**으로 들여쓴다

---

## 파일 구조

```
k8s/
  base/
    kustomization.yaml     네임스페이스, 공통 라벨, ConfigMap 생성기(app-config, nginx-config)
    namespace.yaml
    nginx.yaml             Deployment + Service
    auth.yaml              Deployment + Service
    board-api.yaml         Deployment + Service
    board-worker.yaml      Deployment
    db-migrate.yaml        Job
    hpa.yaml               HPA 4개
    pdb.yaml               PDB 4개
    networkpolicy.yaml     auth, board-api, board-worker 인그레스 제한
  components/
    keda-worker/           ScaledObject + worker CPU HPA 삭제 패치
  overlays/
    local/                 postgres·redis StatefulSet, Secret 생성, NodePort 30080, 데이터 NetworkPolicy
    local-loadtest/        local + keda-worker + 로그인 리밋 완화
    aws/                   ECR 이미지, ALB Ingress, VPC 대역
    gcp/                   Artifact Registry 이미지, GCE Ingress, BackendConfig, ManagedCertificate
  kind/cluster.yaml        hostPort 8080 → nodePort 30080
  tests/smoke.sh           kind 배포 후 종단 간 스모크 테스트
loadtest/disaster.js       k6 재난 시나리오
docs/deploy.md             배포 절차, 인프라 계약, DB 커넥션 계산
Makefile                   k8s-validate, k8s-local, k8s-local-loadtest, k8s-smoke, k8s-down, loadtest, loadtest-quick 추가
```

---

### Task 1: 검증 도구와 base 워크로드 (Deployment, Service, Job, ConfigMap)

**Files:**
- Modify: `Makefile` (끝에 추가)
- Create: `k8s/base/kustomization.yaml`, `k8s/base/namespace.yaml`, `k8s/base/nginx.yaml`, `k8s/base/auth.yaml`, `k8s/base/board-api.yaml`, `k8s/base/board-worker.yaml`, `k8s/base/db-migrate.yaml`

**Interfaces:**
- Consumes: 이미지 3개와 그 실행 명령(Global Constraints)
- Produces: Deployment `nginx`, `auth`, `board-api`, `board-worker`, Job `db-migrate`, Service `nginx`/`auth`/`board-api`, ConfigMap `app-config`/`nginx-config`(해시 접미사 붙음). 데이터 저장소에 접근하는 Pod에는 라벨 `simple-web-app/data-access: "true"`. Makefile 변수 `K8S_TARGETS`, `KUBECONFORM`, 타깃 `k8s-validate`

- [ ] **Step 1: 도구 설치**

```bash
brew install kind k6 kubeconform
kubectl version --client
kind version
kubeconform -v
k6 version
```
Expected: 네 명령 모두 버전을 출력한다.

- [ ] **Step 2: Makefile에 검증 타깃 추가**

`Makefile` 끝에 추가한다(레시피 줄은 탭).

```make
# ---------- k8s ----------
NS ?= simple-web-app
K8S_VERSION ?= 1.31.0
CRD_SCHEMA := https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json
KUBECONFORM := kubeconform -strict -summary -kubernetes-version $(K8S_VERSION) -schema-location default -schema-location '$(CRD_SCHEMA)'
K8S_TARGETS := k8s/base

.PHONY: k8s-validate
k8s-validate: ## 모든 kustomize 대상 렌더링 + 스키마 검증
	@set -e; for t in $(K8S_TARGETS); do \
		echo "== $$t"; \
		kubectl kustomize $$t | $(KUBECONFORM); \
	done
```

- [ ] **Step 3: 검증이 실패하는지 확인**

Run: `make k8s-validate`
Expected: FAIL — `error: must build at directory: .../k8s/base: file is not directory` 류의 에러

- [ ] **Step 4: base 작성**

`k8s/base/namespace.yaml`
```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: simple-web-app
```

`k8s/base/kustomization.yaml`
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: simple-web-app
labels:
  - pairs:
      app.kubernetes.io/part-of: simple-web-app
    includeSelectors: false
resources:
  - namespace.yaml
  - nginx.yaml
  - auth.yaml
  - board-api.yaml
  - board-worker.yaml
  - db-migrate.yaml
configMapGenerator:
  - name: app-config
    literals:
      - SESSION_TTL_SECONDS=86400
      - QUEUE_MAX_LEN=50000
      - COOKIE_SECURE=true
      - LOG_LEVEL=INFO
  - name: nginx-config
    literals:
      - AUTH_UPSTREAM=auth:8000
      - BOARD_UPSTREAM=board-api:8000
      - REAL_IP_FROM=127.0.0.1/32
      - RATE_READ=100r/s
      - BURST_READ=200
      - RATE_WRITE=2r/s
      - BURST_WRITE=5
      - RATE_LOGIN=30r/m
      - BURST_LOGIN=10
```
ConfigMap은 해시 접미사를 유지한다. 값이 바뀌면 이름이 바뀌어서 Deployment가 자동으로 롤링 업데이트된다.

`k8s/base/nginx.yaml`
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx
  labels:
    app.kubernetes.io/name: nginx
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: nginx
  template:
    metadata:
      labels:
        app.kubernetes.io/name: nginx
    spec:
      terminationGracePeriodSeconds: 30
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: nginx
          image: simple-web-app/frontend
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8080
          envFrom:
            - configMapRef:
                name: nginx-config
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 256Mi
          readinessProbe:
            httpGet:
              path: /nginx-health
              port: http
            periodSeconds: 5
            timeoutSeconds: 2
            failureThreshold: 3
          livenessProbe:
            httpGet:
              path: /nginx-health
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10
            timeoutSeconds: 2
            failureThreshold: 3
          lifecycle:
            preStop:
              exec:
                command: ["sleep", "5"]
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
---
apiVersion: v1
kind: Service
metadata:
  name: nginx
  labels:
    app.kubernetes.io/name: nginx
spec:
  selector:
    app.kubernetes.io/name: nginx
  ports:
    - name: http
      port: 80
      targetPort: http
```
nginx 이미지는 시작할 때 envsubst로 설정 파일을 쓰므로 `readOnlyRootFilesystem`을 켜지 않는다. 이미지 자체 uid(nginx-unprivileged 계열)를 쓰기 때문에 `runAsUser`도 지정하지 않는다.

`k8s/base/auth.yaml`
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: auth
  labels:
    app.kubernetes.io/name: auth
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: auth
  template:
    metadata:
      labels:
        app.kubernetes.io/name: auth
        simple-web-app/data-access: "true"
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8000"
        prometheus.io/path: /metrics
    spec:
      terminationGracePeriodSeconds: 30
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: auth
          image: simple-web-app/auth
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8000
          envFrom:
            - configMapRef:
                name: app-config
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: app-db
                  key: DATABASE_URL
            - name: REDIS_URL
              valueFrom:
                secretKeyRef:
                  name: app-redis
                  key: REDIS_URL
          resources:
            requests:
              cpu: 250m
              memory: 256Mi
            limits:
              cpu: "1"
              memory: 512Mi
          readinessProbe:
            httpGet:
              path: /readyz
              port: http
            periodSeconds: 5
            timeoutSeconds: 2
            failureThreshold: 3
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 10
            periodSeconds: 10
            timeoutSeconds: 2
            failureThreshold: 3
          lifecycle:
            preStop:
              exec:
                command: ["sleep", "5"]
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: tmp
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: auth
  labels:
    app.kubernetes.io/name: auth
spec:
  selector:
    app.kubernetes.io/name: auth
  ports:
    - name: http
      port: 8000
      targetPort: http
```

`k8s/base/board-api.yaml`
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: board-api
  labels:
    app.kubernetes.io/name: board-api
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: board-api
  template:
    metadata:
      labels:
        app.kubernetes.io/name: board-api
        simple-web-app/data-access: "true"
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8000"
        prometheus.io/path: /metrics
    spec:
      terminationGracePeriodSeconds: 30
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: board-api
          image: simple-web-app/board
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8000
          envFrom:
            - configMapRef:
                name: app-config
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: app-db
                  key: DATABASE_URL
            - name: REDIS_URL
              valueFrom:
                secretKeyRef:
                  name: app-redis
                  key: REDIS_URL
          resources:
            requests:
              cpu: 200m
              memory: 256Mi
            limits:
              cpu: "1"
              memory: 512Mi
          readinessProbe:
            httpGet:
              path: /readyz
              port: http
            periodSeconds: 5
            timeoutSeconds: 2
            failureThreshold: 3
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 10
            periodSeconds: 10
            timeoutSeconds: 2
            failureThreshold: 3
          lifecycle:
            preStop:
              exec:
                command: ["sleep", "5"]
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: tmp
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: board-api
  labels:
    app.kubernetes.io/name: board-api
spec:
  selector:
    app.kubernetes.io/name: board-api
  ports:
    - name: http
      port: 8000
      targetPort: http
```

`k8s/base/board-worker.yaml`
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: board-worker
  labels:
    app.kubernetes.io/name: board-worker
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: board-worker
  template:
    metadata:
      labels:
        app.kubernetes.io/name: board-worker
        simple-web-app/data-access: "true"
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "9100"
        prometheus.io/path: /metrics
    spec:
      terminationGracePeriodSeconds: 30
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: board-worker
          image: simple-web-app/board
          imagePullPolicy: IfNotPresent
          command: ["python", "-m", "board.worker"]
          ports:
            - name: metrics
              containerPort: 9100
          envFrom:
            - configMapRef:
                name: app-config
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: app-db
                  key: DATABASE_URL
            - name: REDIS_URL
              valueFrom:
                secretKeyRef:
                  name: app-redis
                  key: REDIS_URL
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 256Mi
          livenessProbe:
            exec:
              command: ["python", "-m", "board.worker_health"]
            initialDelaySeconds: 20
            periodSeconds: 15
            timeoutSeconds: 5
            failureThreshold: 3
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: tmp
          emptyDir: {}
```
worker는 Service 엔드포인트에 포함되지 않으므로 `preStop` sleep을 두지 않는다. SIGTERM을 받으면 처리 중인 배치를 끝내고 종료한다(PLAN 1 구현). 모든 Deployment는 `replicas`를 생략한다. HPA가 개수를 관리하므로, 여기에 적으면 `kubectl apply`를 할 때마다 개수가 되돌려진다.

`k8s/base/db-migrate.yaml`
```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: db-migrate
  labels:
    app.kubernetes.io/name: db-migrate
spec:
  backoffLimit: 10
  activeDeadlineSeconds: 600
  template:
    metadata:
      labels:
        app.kubernetes.io/name: db-migrate
        simple-web-app/data-access: "true"
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: migrate
          image: simple-web-app/board
          imagePullPolicy: IfNotPresent
          command: ["python", "-m", "board.migrate"]
          envFrom:
            - configMapRef:
                name: app-config
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: app-db
                  key: DATABASE_URL
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 500m
              memory: 256Mi
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: tmp
          emptyDir: {}
```
Job의 Pod 템플릿은 수정할 수 없으므로, 배포 스크립트는 apply 전에 기존 Job을 지운다(Task 4의 `k8s-local`, `docs/deploy.md`). DB가 아직 준비되지 않았으면 Job이 실패하고 `backoffLimit` 안에서 재시도한다.

- [ ] **Step 5: 검증 통과 확인**

Run: `make k8s-validate`
Expected: PASS — `Summary: 11 resources found parsing stdin - Valid: 11, Invalid: 0, Errors: 0, Skipped: 0` (Namespace 1, ConfigMap 2, Deployment 4, Service 3, Job 1)

추가 확인:
```bash
kubectl kustomize k8s/base | grep -E '^\s+name: (app-config|nginx-config)-' | sort -u
```
Expected: `app-config-<hash>`, `nginx-config-<hash>` 참조가 출력된다(Deployment의 configMapRef가 해시 이름으로 바뀌었는지 확인).

- [ ] **Step 6: Commit**

```bash
git add Makefile k8s/base
git commit -m "feat(k8s): add base workloads, services, migrate job"
```

---

### Task 2: 오토스케일링과 가용성 (HPA, PDB)

**Files:**
- Create: `k8s/base/hpa.yaml`, `k8s/base/pdb.yaml`
- Modify: `k8s/base/kustomization.yaml` (resources에 추가)

**Interfaces:**
- Consumes: Task 1의 Deployment 이름 4개
- Produces: HPA `nginx`, `auth`, `board-api`, `board-worker` (Task 5의 KEDA component가 `board-worker` HPA를 이 이름으로 삭제한다), PDB 4개

- [ ] **Step 1: 실패하는 검사 작성·실행**

```bash
test "$(kubectl kustomize k8s/base | grep -c '^kind: HorizontalPodAutoscaler')" = 4 && \
test "$(kubectl kustomize k8s/base | grep -c '^kind: PodDisruptionBudget')" = 4 && echo OK
```
Expected: FAIL — `OK`가 출력되지 않는다(현재 0개).

- [ ] **Step 2: `k8s/base/hpa.yaml` 작성**

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: nginx
  labels:
    app.kubernetes.io/name: nginx
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: nginx
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 0
      policies:
        - type: Percent
          value: 100
          periodSeconds: 15
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
        - type: Percent
          value: 50
          periodSeconds: 60
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: auth
  labels:
    app.kubernetes.io/name: auth
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: auth
  minReplicas: 2
  maxReplicas: 20
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 0
      policies:
        - type: Percent
          value: 100
          periodSeconds: 15
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
        - type: Percent
          value: 50
          periodSeconds: 60
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: board-api
  labels:
    app.kubernetes.io/name: board-api
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: board-api
  minReplicas: 2
  maxReplicas: 20
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 0
      policies:
        - type: Percent
          value: 100
          periodSeconds: 15
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
        - type: Percent
          value: 50
          periodSeconds: 60
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: board-worker
  labels:
    app.kubernetes.io/name: board-worker
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: board-worker
  minReplicas: 1
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 0
      policies:
        - type: Percent
          value: 100
          periodSeconds: 15
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
        - type: Percent
          value: 50
          periodSeconds: 60
```
`board-worker`의 CPU HPA는 KEDA가 없는 클러스터를 위한 기본값이다. KEDA를 쓰는 overlay는 Task 5의 component가 이 HPA를 지우고 큐 길이 기반 ScaledObject로 바꾼다.

- [ ] **Step 3: `k8s/base/pdb.yaml` 작성**

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: nginx
  labels:
    app.kubernetes.io/name: nginx
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: nginx
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: auth
  labels:
    app.kubernetes.io/name: auth
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: auth
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: board-api
  labels:
    app.kubernetes.io/name: board-api
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: board-api
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: board-worker
  labels:
    app.kubernetes.io/name: board-worker
spec:
  maxUnavailable: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: board-worker
```
**스펙과 다른 점:** worker는 최소 1개로 운영되므로 `minAvailable: 1`을 걸면 Pod가 1개일 때 노드 drain이 영원히 막힌다. 그래서 worker만 `maxUnavailable: 1`을 쓴다. worker가 잠깐 0개가 되어도 메시지는 스트림에 남아 있다가 새 Pod가 처리하므로 데이터 손실이 없다.

- [ ] **Step 4: kustomization에 등록**

`k8s/base/kustomization.yaml`의 `resources`를 다음으로 바꾼다.
```yaml
resources:
  - namespace.yaml
  - nginx.yaml
  - auth.yaml
  - board-api.yaml
  - board-worker.yaml
  - db-migrate.yaml
  - hpa.yaml
  - pdb.yaml
```

- [ ] **Step 5: 검사 통과 확인**

Run: Step 1의 명령, 그리고 `make k8s-validate`
Expected: `OK`, 그리고 `Valid: 19, Invalid: 0, Errors: 0`

- [ ] **Step 6: Commit**

```bash
git add k8s/base
git commit -m "feat(k8s): add HPA with fast scale-up and PDBs"
```

---

### Task 3: NetworkPolicy (앱 서비스 인그레스 제한)

**Files:**
- Create: `k8s/base/networkpolicy.yaml`
- Modify: `k8s/base/kustomization.yaml`

**Interfaces:**
- Consumes: Pod 라벨 `app.kubernetes.io/name`
- Produces: NetworkPolicy `auth-ingress`, `board-api-ingress`, `board-worker-ingress`. Prometheus 수집을 위해 네임스페이스 `monitoring`에서 오는 요청을 허용한다(인프라 계약, Task 8)

- [ ] **Step 1: 실패하는 검사 실행**

```bash
test "$(kubectl kustomize k8s/base | grep -c '^kind: NetworkPolicy')" = 3 && echo OK
```
Expected: FAIL — 출력 없음

- [ ] **Step 2: `k8s/base/networkpolicy.yaml` 작성**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: auth-ingress
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: auth
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app.kubernetes.io/name: nginx
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: monitoring
      ports:
        - port: 8000
          protocol: TCP
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: board-api-ingress
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: board-api
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app.kubernetes.io/name: nginx
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: monitoring
      ports:
        - port: 8000
          protocol: TCP
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: board-worker-ingress
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: board-worker
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: monitoring
      ports:
        - port: 9100
          protocol: TCP
```
nginx는 LB에서 직접 들어오는 트래픽을 받아야 하므로 정책을 두지 않는다. `/internal/verify`는 nginx Pod에서만 auth에 닿을 수 있다(스펙 §3 헤더 위조 방지). kubelet의 프로브는 노드에서 오는 트래픽이라 이 정책의 영향을 받지 않는다.

- [ ] **Step 3: kustomization에 등록**

`k8s/base/kustomization.yaml`의 `resources` 끝에 `- networkpolicy.yaml`을 추가한다.

- [ ] **Step 4: 검사 통과 확인**

Run: Step 1의 명령, `make k8s-validate`
Expected: `OK`, `Valid: 22, Invalid: 0, Errors: 0`

- [ ] **Step 5: Commit**

```bash
git add k8s/base
git commit -m "feat(k8s): restrict ingress to auth, board-api, worker"
```

---

### Task 4: local overlay, kind 클러스터, 스모크 테스트

**Files:**
- Create: `k8s/overlays/local/kustomization.yaml`, `k8s/overlays/local/postgres.yaml`, `k8s/overlays/local/redis.yaml`, `k8s/overlays/local/networkpolicy-data.yaml`, `k8s/overlays/local/nginx-service-patch.yaml`
- Create: `k8s/kind/cluster.yaml`, `k8s/tests/smoke.sh`
- Modify: `Makefile`

**Interfaces:**
- Consumes: Task 1~3의 base, 이미지 3개(`:dev`)
- Produces: overlay `k8s/overlays/local`(Task 5가 확장), Service `postgres`/`redis`, 로컬 접속 주소 `http://localhost:8080`, Makefile 타깃 `k8s-local`(변수 `LOCAL_OVERLAY` 기본값 `local`), `k8s-smoke`, `k8s-down`, 변수 `KIND_CLUSTER`

- [ ] **Step 1: 스모크 테스트 작성**

`k8s/tests/smoke.sh`
```bash
#!/usr/bin/env bash
# kind 등 로컬 클러스터에 배포된 스택을 nginx를 통해 종단 간 확인한다.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
NS="${NS:-simple-web-app}"
SUFFIX="$(date +%s)"
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

uuid() {
  if command -v uuidgen >/dev/null 2>&1; then uuidgen | tr 'A-Z' 'a-z'; else cat /proc/sys/kernel/random/uuid; fi
}
json=(-H 'Content-Type: application/json')

echo "1) nginx health"
curl -fsS "$BASE_URL/nginx-health" >/dev/null

echo "2) 익명 목록 조회"
curl -fsS "$BASE_URL/api/board/posts" | grep -q '"items"'

echo "3) 회원가입 + 로그인"
curl -fsS "${json[@]}" \
  -d "{\"email\":\"smoke-$SUFFIX@example.com\",\"password\":\"smoke-Passw0rd!\",\"nickname\":\"smoke$SUFFIX\"}" \
  "$BASE_URL/api/auth/signup" >/dev/null
curl -fsS -c "$JAR" "${json[@]}" \
  -d "{\"email\":\"smoke-$SUFFIX@example.com\",\"password\":\"smoke-Passw0rd!\"}" \
  "$BASE_URL/api/auth/login" >/dev/null

echo "4) 글쓰기 → 202"
status=$(curl -sS -o /dev/null -w '%{http_code}' -b "$JAR" "${json[@]}" \
  -H "Idempotency-Key: $(uuid)" \
  -d "{\"title\":\"smoke $SUFFIX\",\"body\":\"k8s smoke test\"}" \
  "$BASE_URL/api/board/posts")
[ "$status" = "202" ] || { echo "   expected 202, got $status"; exit 1; }

echo "5) 워커가 처리해서 목록에 나타나는지 (최대 15초)"
found=0
for _ in $(seq 1 15); do
  if curl -fsS "$BASE_URL/api/board/posts" | grep -q "smoke $SUFFIX"; then found=1; break; fi
  sleep 1
done
[ "$found" = "1" ] || { echo "   post not visible"; exit 1; }

echo "6) 위조한 X-User-Id 헤더로 익명 글쓰기 → 401"
status=$(curl -sS -o /dev/null -w '%{http_code}' "${json[@]}" \
  -H 'X-User-Id: 00000000-0000-0000-0000-000000000001' \
  -H "Idempotency-Key: $(uuid)" \
  -d '{"title":"spoof","body":"spoof"}' \
  "$BASE_URL/api/board/posts")
[ "$status" = "401" ] || { echo "   expected 401, got $status"; exit 1; }

echo "7) NetworkPolicy: 다른 Pod에서 auth 직접 접근 차단"
if kubectl -n "$NS" run np-probe --rm -i --quiet --restart=Never \
     --image=curlimages/curl:8.10.1 --labels=app.kubernetes.io/name=np-probe \
     -- curl -sS -m 3 http://auth:8000/healthz >/dev/null 2>&1; then
  echo "   WARN: auth에 직접 접근됨 — 이 클러스터의 CNI가 NetworkPolicy를 적용하지 않음"
  [ "${REQUIRE_NETPOL:-0}" = "1" ] && exit 1
else
  echo "   차단됨"
fi

echo "SMOKE OK"
```
```bash
chmod +x k8s/tests/smoke.sh
```

- [ ] **Step 2: kind 설정 작성**

`k8s/kind/cluster.yaml`
```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30080
        hostPort: 8080
        protocol: TCP
```
`make dev`(docker-compose)도 8080을 쓰므로, kind 클러스터를 만들기 전에 compose를 내려야 한다(`docker compose down`).

- [ ] **Step 3: Makefile 타깃 추가**

`Makefile`에서 `K8S_TARGETS` 줄을 다음으로 바꾼다.
```make
K8S_TARGETS := k8s/base k8s/overlays/local
```
그리고 k8s 섹션 끝에 추가한다.
```make
KIND_CLUSTER ?= simple-web-app
LOCAL_OVERLAY ?= local
METRICS_SERVER_URL := https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml
APP_DEPLOYMENTS := nginx auth board-api board-worker

.PHONY: k8s-local k8s-smoke k8s-down
k8s-local: ## kind 클러스터 생성 → 이미지 빌드·로드 → overlay 적용 → 마이그레이션·롤아웃 대기
	@kind get clusters 2>/dev/null | grep -qx $(KIND_CLUSTER) || \
		kind create cluster --name $(KIND_CLUSTER) --config k8s/kind/cluster.yaml
	@kubectl -n kube-system get deploy metrics-server >/dev/null 2>&1 || { \
		kubectl apply -f $(METRICS_SERVER_URL) && \
		kubectl -n kube-system patch deployment metrics-server --type=json \
			-p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'; }
	docker build -t simple-web-app/auth:dev -f services/auth/Dockerfile .
	docker build -t simple-web-app/board:dev -f services/board/Dockerfile .
	docker build -t simple-web-app/frontend:dev -f frontend/Dockerfile .
	kind load docker-image --name $(KIND_CLUSTER) \
		simple-web-app/auth:dev simple-web-app/board:dev simple-web-app/frontend:dev
	-kubectl -n $(NS) delete job db-migrate --ignore-not-found --wait=true
	kubectl apply -k k8s/overlays/$(LOCAL_OVERLAY)
	kubectl -n $(NS) wait --for=condition=complete job/db-migrate --timeout=300s
	kubectl -n $(NS) rollout restart deployment $(APP_DEPLOYMENTS)
	@for d in $(APP_DEPLOYMENTS); do kubectl -n $(NS) rollout status deployment/$$d --timeout=180s; done

k8s-smoke: ## 로컬 클러스터 스모크 테스트
	NS=$(NS) ./k8s/tests/smoke.sh

k8s-down: ## kind 클러스터 삭제
	kind delete cluster --name $(KIND_CLUSTER)
```
`rollout restart`를 하는 이유: 로컬 태그 `dev`는 매번 같아서, 이미지를 다시 빌드해도 Pod가 새 이미지로 바뀌지 않는다.

- [ ] **Step 4: 검증이 실패하는지 확인**

Run: `make k8s-validate`
Expected: FAIL — `k8s/overlays/local`이 없다는 에러

- [ ] **Step 5: local overlay 작성**

`k8s/overlays/local/kustomization.yaml`
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: simple-web-app
labels:
  - pairs:
      app.kubernetes.io/part-of: simple-web-app
    includeSelectors: false
resources:
  - ../../base
  - postgres.yaml
  - redis.yaml
  - networkpolicy-data.yaml
patches:
  - path: nginx-service-patch.yaml
configMapGenerator:
  - name: app-config
    behavior: merge
    literals:
      - COOKIE_SECURE=false
secretGenerator:
  - name: app-db
    literals:
      - DATABASE_URL=postgresql+asyncpg://app:app@postgres:5432/app
    options:
      disableNameSuffixHash: true
  - name: app-redis
    literals:
      - REDIS_URL=redis://redis:6379/0
    options:
      disableNameSuffixHash: true
  - name: postgres-credentials
    literals:
      - POSTGRES_USER=app
      - POSTGRES_PASSWORD=app
      - POSTGRES_DB=app
    options:
      disableNameSuffixHash: true
images:
  - name: simple-web-app/frontend
    newTag: dev
  - name: simple-web-app/auth
    newTag: dev
  - name: simple-web-app/board
    newTag: dev
```
Secret 이름은 클라우드 overlay가 참조하는 이름(`app-db`, `app-redis`)과 같아야 하므로 해시 접미사를 끈다.

`k8s/overlays/local/postgres.yaml`
```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
  labels:
    app.kubernetes.io/name: postgres
spec:
  serviceName: postgres
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: postgres
  template:
    metadata:
      labels:
        app.kubernetes.io/name: postgres
    spec:
      containers:
        - name: postgres
          image: postgres:16-alpine
          args: ["-c", "max_connections=600"]
          ports:
            - name: postgres
              containerPort: 5432
          envFrom:
            - secretRef:
                name: postgres-credentials
          env:
            - name: PGDATA
              value: /var/lib/postgresql/data/pgdata
          resources:
            requests:
              cpu: 250m
              memory: 256Mi
            limits:
              cpu: "1"
              memory: 1Gi
          readinessProbe:
            exec:
              command: ["pg_isready", "-U", "app", "-d", "app"]
            periodSeconds: 5
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: 1Gi
---
apiVersion: v1
kind: Service
metadata:
  name: postgres
  labels:
    app.kubernetes.io/name: postgres
spec:
  selector:
    app.kubernetes.io/name: postgres
  ports:
    - name: postgres
      port: 5432
      targetPort: postgres
```
`max_connections=600`: 스펙 §7의 최대 500개 + 마이그레이션·관리용 여유분.

`k8s/overlays/local/redis.yaml`
```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redis
  labels:
    app.kubernetes.io/name: redis
spec:
  serviceName: redis
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: redis
  template:
    metadata:
      labels:
        app.kubernetes.io/name: redis
    spec:
      containers:
        - name: redis
          image: redis:7.2-alpine
          args:
            - redis-server
            - --appendonly
            - "yes"
            - --maxmemory
            - 256mb
            - --maxmemory-policy
            - noeviction
          ports:
            - name: redis
              containerPort: 6379
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 384Mi
          readinessProbe:
            exec:
              command: ["redis-cli", "ping"]
            periodSeconds: 5
          volumeMounts:
            - name: data
              mountPath: /data
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: 1Gi
---
apiVersion: v1
kind: Service
metadata:
  name: redis
  labels:
    app.kubernetes.io/name: redis
spec:
  selector:
    app.kubernetes.io/name: redis
  ports:
    - name: redis
      port: 6379
      targetPort: redis
```
`noeviction`: 메모리가 가득 찼을 때 Redis가 스트림(글쓰기 큐)이나 세션을 임의로 지우면 데이터가 사라진다. 차라리 쓰기 에러를 내서 앱이 503으로 백프레셔를 걸게 한다. 캐시 키는 모두 TTL이 있어서 스스로 사라진다.

`k8s/overlays/local/networkpolicy-data.yaml`
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: postgres-ingress
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: postgres
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              simple-web-app/data-access: "true"
      ports:
        - port: 5432
          protocol: TCP
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: redis-ingress
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: redis
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              simple-web-app/data-access: "true"
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: keda
      ports:
        - port: 6379
          protocol: TCP
```
`keda` 네임스페이스 허용: KEDA operator가 스트림 길이를 읽으려면 Redis에 접근해야 한다(Task 5).

`k8s/overlays/local/nginx-service-patch.yaml`
```yaml
apiVersion: v1
kind: Service
metadata:
  name: nginx
spec:
  type: NodePort
  ports:
    - name: http
      port: 80
      targetPort: http
      nodePort: 30080
```

- [ ] **Step 6: 검증 통과 확인**

Run: `make k8s-validate`
Expected: PASS — base와 local 모두 `Invalid: 0, Errors: 0`

추가 확인(COOKIE_SECURE 병합, NodePort):
```bash
kubectl kustomize k8s/overlays/local | grep -E 'COOKIE_SECURE|nodePort'
```
Expected: `COOKIE_SECURE: "false"`, `nodePort: 30080`

- [ ] **Step 7: kind에 배포**

Run: `docker compose down; make k8s-local`
Expected: `job.batch/db-migrate condition met`, 이후 Deployment 4개가 모두 `successfully rolled out`

- [ ] **Step 8: 스모크 테스트 실행**

Run: `make k8s-smoke`
Expected: `SMOKE OK`. 7번 항목은 `차단됨`이 정상이다. 사용 중인 kind 버전의 기본 CNI(kindnet)가 NetworkPolicy를 지원하지 않으면 `WARN`이 나오며, 이 경우 실패로 보지 않는다(클라우드 CNI에서 다시 확인).

HPA가 메트릭을 읽는지 확인:
```bash
kubectl -n simple-web-app get hpa
```
Expected: `TARGETS` 열이 `<unknown>/60%`가 아니라 `cpu: 3%/60%`처럼 숫자로 나온다(metrics-server 기동 후 1~2분 걸릴 수 있음).

- [ ] **Step 9: Commit**

```bash
git add Makefile k8s/overlays/local k8s/kind k8s/tests
git commit -m "feat(k8s): add local overlay, kind cluster and smoke test"
```

---

### Task 5: KEDA 큐 길이 기반 worker 확장 + 부하 테스트용 overlay

**Files:**
- Create: `k8s/components/keda-worker/kustomization.yaml`, `k8s/components/keda-worker/scaledobject.yaml`, `k8s/components/keda-worker/delete-worker-hpa.yaml`
- Create: `k8s/overlays/local-loadtest/kustomization.yaml`
- Modify: `Makefile`

**Interfaces:**
- Consumes: Task 2의 HPA 이름 `board-worker`, Task 4의 `k8s/overlays/local`, `k8s-local`의 `LOCAL_OVERLAY` 변수
- Produces: component `k8s/components/keda-worker`(aws/gcp overlay가 선택적으로 포함), overlay `k8s/overlays/local-loadtest`, Makefile 타깃 `k8s-keda-install`, `k8s-local-loadtest`

**설계 결정 (로그인 리밋 문제):** 부하 테스트는 한 대의 PC(IP 하나)에서 실행된다. 로그인·회원가입 IP 리밋(30r/m)이 그대로면 k6 setup의 사용자 500명 가입과 시나리오의 로그인 5%가 전부 429로 막혀서, 서버 성능이 아니라 리밋을 측정하게 된다. k6 쪽에서 요청 속도를 낮추면 "로그인 5%" 비율을 지킬 수 없다. 그래서 **부하 테스트 전용 overlay `local-loadtest`에서 로그인 리밋만 완화**한다. 읽기(100r/s)와 글쓰기(사용자별 2r/s) 리밋은 그대로 둔다. 읽기 IP 리밋에 걸리는 요청은 k6가 `rate_limited_429`로 따로 집계해서, nginx 리밋이 동작하는 모습도 보여준다.

- [ ] **Step 1: 실패하는 검증 준비**

`Makefile`의 `K8S_TARGETS`를 바꾼다.
```make
K8S_TARGETS := k8s/base k8s/overlays/local k8s/overlays/local-loadtest
```
Run: `make k8s-validate`
Expected: FAIL — `k8s/overlays/local-loadtest`가 없다는 에러

- [ ] **Step 2: KEDA component 작성**

`k8s/components/keda-worker/kustomization.yaml`
```yaml
apiVersion: kustomize.config.k8s.io/v1alpha1
kind: Component
resources:
  - scaledobject.yaml
patches:
  - path: delete-worker-hpa.yaml
```

`k8s/components/keda-worker/delete-worker-hpa.yaml`
```yaml
$patch: delete
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: board-worker
```
KEDA는 ScaledObject마다 자체 HPA(`keda-hpa-board-worker`)를 만든다. base의 CPU HPA가 남아 있으면 HPA 두 개가 같은 Deployment를 두고 서로 다른 값으로 조정하게 되므로 삭제한다.

`k8s/components/keda-worker/scaledobject.yaml`
```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: board-worker
  labels:
    app.kubernetes.io/name: board-worker
spec:
  scaleTargetRef:
    name: board-worker
  minReplicaCount: 1
  maxReplicaCount: 10
  pollingInterval: 5
  cooldownPeriod: 300
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleUp:
          stabilizationWindowSeconds: 0
          policies:
            - type: Percent
              value: 100
              periodSeconds: 15
        scaleDown:
          stabilizationWindowSeconds: 300
          policies:
            - type: Percent
              value: 50
              periodSeconds: 60
  triggers:
    - type: redis-streams
      metadata:
        address: redis.simple-web-app.svc.cluster.local:6379
        stream: posts:stream
        streamLength: "500"
```
**`streamLength`를 쓰는 이유:** worker는 처리한 항목을 XACK한 뒤 XDEL까지 하므로 `XLEN posts:stream`이 곧 밀린 작업량이다. `pendingEntriesCount`는 워커가 이미 읽었지만 아직 ACK하지 않은 항목만 세기 때문에, 아직 아무도 읽지 않은 대기 메시지가 쌓여도 늘어나지 않는다. 목표값 500은 "worker 1개당 밀린 메시지 500건"을 뜻한다(worker는 한 번에 100건씩 처리하므로 배치 5개 분량). 클라우드 overlay에서 이 component를 쓸 때는 `address`를 매니지드 Redis 주소로 패치해야 한다(`docs/deploy.md`).

- [ ] **Step 3: local-loadtest overlay 작성**

`k8s/overlays/local-loadtest/kustomization.yaml`
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: simple-web-app
resources:
  - ../local
components:
  - ../../components/keda-worker
configMapGenerator:
  - name: nginx-config
    behavior: merge
    literals:
      - RATE_LOGIN=6000r/m
      - BURST_LOGIN=1000
```

- [ ] **Step 4: 검증 통과 확인**

Run: `make k8s-validate`
Expected: PASS — local-loadtest도 `Invalid: 0, Errors: 0`. ScaledObject 스키마는 CRDs-catalog(`keda.sh/scaledobject_v1alpha1.json`)에서 가져온다.

추가 확인:
```bash
kubectl kustomize k8s/overlays/local-loadtest | grep -cE '^kind: (HorizontalPodAutoscaler|ScaledObject)'
kubectl kustomize k8s/overlays/local-loadtest | grep -E 'RATE_LOGIN'
```
Expected: `4`(HPA 3개 + ScaledObject 1개), `RATE_LOGIN: 6000r/m`

- [ ] **Step 5: Makefile 타깃 추가**

k8s 섹션 끝에 추가한다.
```make
KEDA_URL := https://github.com/kedacore/keda/releases/download/v2.15.1/keda-2.15.1.yaml

.PHONY: k8s-keda-install k8s-local-loadtest
k8s-keda-install: ## 현재 kubectl 컨텍스트에 KEDA 설치
	kubectl apply --server-side -f $(KEDA_URL)
	kubectl -n keda rollout status deployment/keda-operator --timeout=180s
	kubectl -n keda rollout status deployment/keda-operator-metrics-apiserver --timeout=180s

k8s-local-loadtest: ## kind에 부하 테스트용 overlay 배포 (KEDA 포함, 로그인 리밋 완화)
	@kind get clusters 2>/dev/null | grep -qx $(KIND_CLUSTER) || \
		kind create cluster --name $(KIND_CLUSTER) --config k8s/kind/cluster.yaml
	$(MAKE) k8s-keda-install
	$(MAKE) k8s-local LOCAL_OVERLAY=local-loadtest
```
KEDA CRD는 annotation 크기 제한을 넘기 때문에 `--server-side`로 적용해야 한다.

- [ ] **Step 6: 배포와 확장 동작 확인**

Run: `make k8s-local-loadtest && make k8s-smoke`
Expected: `SMOKE OK`

```bash
kubectl -n simple-web-app get scaledobject board-worker
kubectl -n simple-web-app get hpa
```
Expected: ScaledObject `READY=True`, HPA 목록에 `keda-hpa-board-worker`가 있고 `board-worker`(CPU HPA)는 없다.

- [ ] **Step 7: Commit**

```bash
git add Makefile k8s/components k8s/overlays/local-loadtest
git commit -m "feat(k8s): add KEDA stream-length worker scaling and loadtest overlay"
```

---

### Task 6: k6 재난 부하 테스트

**Files:**
- Create: `loadtest/disaster.js`
- Modify: `Makefile`

**Interfaces:**
- Consumes: API(`POST /api/auth/signup` 201, `POST /api/auth/login` 200 + 쿠키 `sid`, `GET /api/board/posts` → `{items, next_cursor}`, `POST /api/board/posts` + `Idempotency-Key` → 202, 503 `{code:"QUEUE_FULL"}`, 429), Task 5의 `local-loadtest` 배포
- Produces: 환경변수 `BASE_URL`(기본 `http://localhost:8080`), `USERS`, `BASELINE`, `PEAK`, `QUICK`. 커스텀 메트릭 `read_duration`, `write_accepted_duration`, `server_errors`, `backpressure_503`, `rate_limited_429`. Makefile 타깃 `loadtest`, `loadtest-quick`

- [ ] **Step 1: Makefile 타깃 추가**

```make
BASE_URL ?= http://localhost:8080
K6_ARGS ?=

.PHONY: loadtest loadtest-quick
loadtest: ## k6 재난 시나리오 (약 14분). 먼저 make k8s-local-loadtest
	k6 run -e BASE_URL=$(BASE_URL) $(K6_ARGS) loadtest/disaster.js

loadtest-quick: ## 단축 시나리오 (약 1분 30초, 동작 확인용)
	k6 run -e BASE_URL=$(BASE_URL) -e QUICK=1 $(K6_ARGS) loadtest/disaster.js
```

- [ ] **Step 2: 실패 확인**

Run: `k6 inspect loadtest/disaster.js`
Expected: FAIL — 파일이 없다는 에러

- [ ] **Step 3: `loadtest/disaster.js` 작성**

```javascript
// 재난 상황 트래픽 시뮬레이션 (spec §11)
// 평상시 → 30초 만에 20배 폭증 → 5분 유지 → 감소 → 두 번째 폭증
// 요청 비율: 읽기 80%, 쓰기 15%, 로그인 5%
import http from 'k6/http';
import { check } from 'k6';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';
import { uuidv4 } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
const QUICK = __ENV.QUICK === '1';
const USERS = parseInt(__ENV.USERS || (QUICK ? '20' : '500'), 10);
const BASELINE = parseInt(__ENV.BASELINE || (QUICK ? '5' : '20'), 10); // 초당 요청 수
const PEAK = parseInt(__ENV.PEAK || String(BASELINE * 20), 10);
const PASSWORD = 'loadtest-Passw0rd!';
const JSON_HEADERS = { 'Content-Type': 'application/json' };
const SIGNUP_BATCH = 25;

const readDuration = new Trend('read_duration', true);
const writeAcceptedDuration = new Trend('write_accepted_duration', true);
const serverErrors = new Rate('server_errors');
const backpressure = new Counter('backpressure_503');
const rateLimited = new Counter('rate_limited_429');

function stages() {
  const d = (full) => (QUICK ? '10s' : full);
  return [
    { target: BASELINE, duration: d('1m') }, // 평상시
    { target: PEAK, duration: d('30s') }, // 1차 폭증
    { target: PEAK, duration: d('5m') },
    { target: BASELINE, duration: d('1m') }, // 감소
    { target: BASELINE, duration: d('2m') },
    { target: PEAK, duration: d('30s') }, // 2차 폭증
    { target: PEAK, duration: d('3m') },
    { target: 0, duration: d('1m') },
  ];
}

export const options = {
  setupTimeout: '10m',
  scenarios: {
    disaster: {
      executor: 'ramping-arrival-rate',
      startRate: BASELINE,
      timeUnit: '1s',
      preAllocatedVUs: Math.max(50, PEAK),
      maxVUs: PEAK * 5,
      stages: stages(),
    },
  },
  thresholds: {
    read_duration: ['p(95)<300'],
    write_accepted_duration: ['p(95)<100'],
    server_errors: ['rate<0.01'],
  },
};

// 요청마다 새 쿠키 저장소를 써서 VU 사이에 세션이 섞이지 않게 한다.
function params(extra = {}) {
  return { headers: JSON_HEADERS, jar: new http.CookieJar(), ...extra };
}

function errorCode(res) {
  try {
    return res.json('code');
  } catch (e) {
    return null;
  }
}

// 백프레셔 503(QUEUE_FULL)은 설계된 동작이므로 서버 에러와 따로 집계한다.
// nginx 리밋 초과는 429 {code:"RATE_LIMITED"} (Retry-After: 2)로 온다.
function record(res) {
  if (res.status === 429 && errorCode(res) === 'RATE_LIMITED') rateLimited.add(1);
  const isBackpressure = res.status === 503 && errorCode(res) === 'QUEUE_FULL';
  if (isBackpressure) backpressure.add(1);
  serverErrors.add(res.status >= 500 && !isBackpressure);
}

export function setup() {
  const runId = Date.now().toString(36);
  const users = [];
  for (let i = 0; i < USERS; i++) {
    users.push({ email: `lt-${runId}-${i}@example.com`, nickname: `lt${runId}${i}` });
  }

  for (let i = 0; i < users.length; i += SIGNUP_BATCH) {
    const chunk = users.slice(i, i + SIGNUP_BATCH);

    const signups = http.batch(
      chunk.map((u) => [
        'POST',
        `${BASE_URL}/api/auth/signup`,
        JSON.stringify({ email: u.email, password: PASSWORD, nickname: u.nickname }),
        params(),
      ]),
    );
    signups.forEach((res, j) => {
      if (res.status !== 201) {
        throw new Error(`signup failed for ${chunk[j].email}: ${res.status} ${res.body}`);
      }
    });

    const logins = http.batch(
      chunk.map((u) => [
        'POST',
        `${BASE_URL}/api/auth/login`,
        JSON.stringify({ email: u.email, password: PASSWORD }),
        params(),
      ]),
    );
    logins.forEach((res, j) => {
      const cookie = res.cookies.sid;
      if (res.status !== 200 || !cookie || cookie.length === 0) {
        throw new Error(`login failed for ${chunk[j].email}: ${res.status} ${res.body}`);
      }
      chunk[j].sid = cookie[0].value;
    });
  }

  return { users: users.map((u) => ({ email: u.email, sid: u.sid })) };
}

function read() {
  const res = http.get(`${BASE_URL}/api/board/posts`, params({ tags: { op: 'read' } }));
  record(res);
  check(res, { 'read 200': (r) => r.status === 200 });
  if (res.status !== 200) return;
  readDuration.add(res.timings.duration);

  // 4번 중 1번은 다음 페이지까지 본다(캐시되지 않는 DB 조회 경로).
  const cursor = res.json('next_cursor');
  if (cursor && Math.random() < 0.25) {
    const next = http.get(
      `${BASE_URL}/api/board/posts?cursor=${encodeURIComponent(cursor)}`,
      params({ tags: { op: 'read_next' } }),
    );
    record(next);
    if (next.status === 200) readDuration.add(next.timings.duration);
  }
}

function write(user) {
  const res = http.post(
    `${BASE_URL}/api/board/posts`,
    JSON.stringify({
      title: `재난 상황 공유 ${uuidv4().slice(0, 8)}`,
      body: '현재 위치 상황을 공유합니다. (loadtest)',
    }),
    params({
      headers: { ...JSON_HEADERS, 'Idempotency-Key': uuidv4(), Cookie: `sid=${user.sid}` },
      tags: { op: 'write' },
    }),
  );
  record(res);
  check(res, { 'write 202': (r) => r.status === 202 });
  if (res.status === 202) writeAcceptedDuration.add(res.timings.duration);
}

function login(user) {
  const res = http.post(
    `${BASE_URL}/api/auth/login`,
    JSON.stringify({ email: user.email, password: PASSWORD }),
    params({ tags: { op: 'login' } }),
  );
  record(res);
  check(res, { 'login 200': (r) => r.status === 200 });
}

export default function (data) {
  // 반복 번호로 사용자를 고르게 돌려 써서, 사용자별 글쓰기 리밋(2r/s)에 걸리지 않게 한다.
  // 예: PEAK 400r/s × 쓰기 15% ÷ 500명 = 사용자당 0.12r/s
  const user = data.users[exec.scenario.iterationInTest % data.users.length];
  const r = Math.random();
  if (r < 0.8) read();
  else if (r < 0.95) write(user);
  else login(user);
}
```

- [ ] **Step 4: 스크립트 파싱 확인**

Run: `k6 inspect loadtest/disaster.js`
Expected: PASS — JSON이 출력되고, `scenarios.disaster.stages`에 단계 8개, `thresholds`에 `read_duration`, `write_accepted_duration`, `server_errors`가 있다.

Run: `k6 inspect -e QUICK=1 loadtest/disaster.js | grep -c '"10s"'`
Expected: `8`

- [ ] **Step 5: 단축 시나리오 실행**

선행: Task 5의 `make k8s-local-loadtest` 배포가 떠 있어야 한다.

Run: `make loadtest-quick`
Expected: setup이 사용자 20명을 만들고 시나리오를 끝까지 실행한다. 요약에 `read_duration`, `write_accepted_duration`, `server_errors`가 표시되고, `server_errors` 비율은 1% 미만이다. 노트북 자원 한계로 지연 시간 임계치를 넘으면 k6가 exit code 99로 끝날 수 있다. 이 경우 실패한 임계치와 수치를 기록해 두고, 앱 로직 에러(`server_errors`)가 없으면 통과로 본다.

- [ ] **Step 6: 전체 시나리오와 HPA 관찰 (수동 확인)**

터미널 두 개를 연다.
```bash
# 터미널 1
kubectl -n simple-web-app get hpa -w
# 터미널 2
make loadtest
```
Expected: 폭증 구간에서 `nginx`, `auth`, `board-api` HPA의 REPLICAS가 늘어나고, `keda-hpa-board-worker`는 스트림 길이가 500을 넘으면 늘어난다. 폭증이 끝나고 약 5분 뒤부터 천천히 줄어든다. kind 단일 노드에서는 CPU 요청량이 부족해 일부 Pod가 `Pending`으로 남을 수 있다. 이건 정상이며, 노드 확장은 클라우드 환경(cluster autoscaler)에서 확인한다.

- [ ] **Step 7: Commit**

```bash
git add Makefile loadtest/disaster.js
git commit -m "feat(loadtest): add k6 disaster spike scenario"
```

---

### Task 7: aws overlay (ECR, ALB Ingress)

**Files:**
- Create: `k8s/overlays/aws/kustomization.yaml`, `k8s/overlays/aws/ingress.yaml`
- Modify: `Makefile` (`K8S_TARGETS`)

**Interfaces:**
- Consumes: base, Terraform이 만드는 Secret `app-db`, `app-redis`, ACM 인증서, VPC CIDR
- Produces: Ingress `web`(ALB). 배포 파이프라인은 `kustomize edit set image`로 이미지 이름과 태그를, 패치로 host와 인증서 ARN을 덮어쓴다(`docs/deploy.md`)

- [ ] **Step 1: 실패하는 검증**

`K8S_TARGETS`를 바꾼다.
```make
K8S_TARGETS := k8s/base k8s/overlays/local k8s/overlays/local-loadtest k8s/overlays/aws
```
Run: `make k8s-validate`
Expected: FAIL — `k8s/overlays/aws` 없음

- [ ] **Step 2: overlay 작성**

`k8s/overlays/aws/kustomization.yaml`
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: simple-web-app
labels:
  - pairs:
      app.kubernetes.io/part-of: simple-web-app
    includeSelectors: false
resources:
  - ../../base
  - ingress.yaml
# KEDA가 설치된 클러스터라면 아래 주석을 풀고, docs/deploy.md대로 Redis 주소를 패치한다.
# components:
#   - ../../components/keda-worker
configMapGenerator:
  - name: nginx-config
    behavior: merge
    literals:
      # ALB 노드는 VPC 서브넷 안의 IP로 접속한다. Terraform VPC CIDR와 같아야 한다.
      - REAL_IP_FROM=10.0.0.0/16
images:
  - name: simple-web-app/frontend
    newName: 123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/simple-web-app/frontend
    newTag: "0.1.0"
  - name: simple-web-app/auth
    newName: 123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/simple-web-app/auth
    newTag: "0.1.0"
  - name: simple-web-app/board
    newName: 123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/simple-web-app/board
    newTag: "0.1.0"
```
계정 ID `123456789012`, 리전, 태그는 예시 기본값이다. 배포 파이프라인이 실제 값으로 덮어쓴다(`docs/deploy.md` "이미지 지정"). Secret `app-db`, `app-redis`는 이 overlay에서 만들지 않고, Terraform이 만든 것을 base의 `secretKeyRef`가 참조한다.

`k8s/overlays/aws/ingress.yaml`
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: web
  labels:
    app.kubernetes.io/name: web
  annotations:
    alb.ingress.kubernetes.io/scheme: internet-facing
    alb.ingress.kubernetes.io/target-type: ip
    alb.ingress.kubernetes.io/healthcheck-path: /nginx-health
    alb.ingress.kubernetes.io/listen-ports: '[{"HTTP": 80}, {"HTTPS": 443}]'
    alb.ingress.kubernetes.io/ssl-redirect: "443"
    alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:ap-northeast-2:123456789012:certificate/00000000-0000-0000-0000-000000000000
spec:
  ingressClassName: alb
  rules:
    - host: board.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: nginx
                port:
                  number: 80
```
HTTPS가 필수인 이유: 클라우드에서는 `COOKIE_SECURE=true`라서, HTTP로 접속하면 브라우저가 `sid` 쿠키를 저장하지 않고 로그인이 되지 않는다. `target-type: ip`는 ALB가 nginx Pod IP(포트 8080)로 바로 보내는 방식이라 헬스 체크도 Pod의 `/nginx-health`로 간다.

- [ ] **Step 3: 검증 통과 확인**

Run: `make k8s-validate`
Expected: PASS — aws도 `Invalid: 0, Errors: 0`

```bash
kubectl kustomize k8s/overlays/aws | grep -E 'image:|REAL_IP_FROM|kind: Secret'
```
Expected: `image:` 5줄(nginx, auth, board-api, board-worker, db-migrate)이 모두 ECR 주소, `REAL_IP_FROM: 10.0.0.0/16`, `kind: Secret`은 출력되지 않는다.

- [ ] **Step 4: Commit**

```bash
git add Makefile k8s/overlays/aws
git commit -m "feat(k8s): add aws overlay with ECR images and ALB ingress"
```

---

### Task 8: gcp overlay (Artifact Registry, GCE Ingress)

**Files:**
- Create: `k8s/overlays/gcp/kustomization.yaml`, `k8s/overlays/gcp/ingress.yaml`, `k8s/overlays/gcp/backendconfig.yaml`, `k8s/overlays/gcp/managedcertificate.yaml`, `k8s/overlays/gcp/nginx-service-patch.yaml`
- Modify: `Makefile` (`K8S_TARGETS`)

**Interfaces:**
- Consumes: base, Terraform이 만드는 Secret `app-db`, `app-redis`, 전역 고정 IP `simple-web-app-ip`, 도메인
- Produces: Ingress `web`(GCE), BackendConfig `nginx-backendconfig`, ManagedCertificate `web-cert`

- [ ] **Step 1: 실패하는 검증**

```make
K8S_TARGETS := k8s/base k8s/overlays/local k8s/overlays/local-loadtest k8s/overlays/aws k8s/overlays/gcp
```
Run: `make k8s-validate`
Expected: FAIL — `k8s/overlays/gcp` 없음

- [ ] **Step 2: overlay 작성**

`k8s/overlays/gcp/kustomization.yaml`
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: simple-web-app
labels:
  - pairs:
      app.kubernetes.io/part-of: simple-web-app
    includeSelectors: false
resources:
  - ../../base
  - ingress.yaml
  - backendconfig.yaml
  - managedcertificate.yaml
patches:
  - path: nginx-service-patch.yaml
# KEDA가 설치된 클러스터라면 아래 주석을 풀고, docs/deploy.md대로 Redis 주소를 패치한다.
# components:
#   - ../../components/keda-worker
configMapGenerator:
  - name: nginx-config
    behavior: merge
    literals:
      # Google Front End 프록시 대역 + LB 고정 IP(배포 시 추가, docs/deploy.md)
      - REAL_IP_FROM=130.211.0.0/22 35.191.0.0/16
images:
  - name: simple-web-app/frontend
    newName: asia-northeast3-docker.pkg.dev/my-gcp-project/simple-web-app/frontend
    newTag: "0.1.0"
  - name: simple-web-app/auth
    newName: asia-northeast3-docker.pkg.dev/my-gcp-project/simple-web-app/auth
    newTag: "0.1.0"
  - name: simple-web-app/board
    newName: asia-northeast3-docker.pkg.dev/my-gcp-project/simple-web-app/board
    newTag: "0.1.0"
```
`REAL_IP_FROM`에 CIDR 여러 개를 공백으로 구분해 넣는다. PLAN 2의 nginx가 이를 `set_real_ip_from` 여러 줄로 펼친다.

`k8s/overlays/gcp/ingress.yaml`
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: web
  labels:
    app.kubernetes.io/name: web
  annotations:
    kubernetes.io/ingress.class: gce
    kubernetes.io/ingress.global-static-ip-name: simple-web-app-ip
    networking.gke.io/managed-certificates: web-cert
spec:
  rules:
    - host: board.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: nginx
                port:
                  number: 80
```

`k8s/overlays/gcp/managedcertificate.yaml`
```yaml
apiVersion: networking.gke.io/v1
kind: ManagedCertificate
metadata:
  name: web-cert
spec:
  domains:
    - board.example.com
```

`k8s/overlays/gcp/backendconfig.yaml`
```yaml
apiVersion: cloud.google.com/v1
kind: BackendConfig
metadata:
  name: nginx-backendconfig
spec:
  healthCheck:
    type: HTTP
    requestPath: /nginx-health
    port: 8080
    checkIntervalSec: 5
    timeoutSec: 3
    healthyThreshold: 1
    unhealthyThreshold: 2
  timeoutSec: 30
```

`k8s/overlays/gcp/nginx-service-patch.yaml`
```yaml
apiVersion: v1
kind: Service
metadata:
  name: nginx
  annotations:
    cloud.google.com/neg: '{"ingress": true}'
    cloud.google.com/backend-config: '{"default": "nginx-backendconfig"}'
```
GCE 기본 헬스 체크는 `/`를 보기 때문에, nginx 전용 헬스 경로를 쓰도록 BackendConfig를 연결한다. NEG(컨테이너 네이티브 부하 분산)를 쓰면 LB가 Pod로 바로 보낸다.

- [ ] **Step 3: 검증 통과 확인**

Run: `make k8s-validate`
Expected: PASS — gcp도 `Invalid: 0, Errors: 0`. BackendConfig와 ManagedCertificate 스키마는 CRDs-catalog(`cloud.google.com/backendconfig_v1.json`, `networking.gke.io/managedcertificate_v1.json`)에서 가져온다. 카탈로그에 스키마가 없다는 에러(`could not find schema`)가 나면 `KUBECONFORM`에 `-skip BackendConfig,ManagedCertificate`를 추가하고, 그 사실을 `docs/deploy.md`에 적는다.

- [ ] **Step 4: Commit**

```bash
git add Makefile k8s/overlays/gcp
git commit -m "feat(k8s): add gcp overlay with Artifact Registry and GCE ingress"
```

---

### Task 9: 배포 문서 (`docs/deploy.md`)

**Files:**
- Create: `docs/deploy.md`

**Interfaces:**
- Consumes: Task 1~8의 overlay, Makefile 타깃, 스펙 §7, §12
- Produces: `one-click-deploy-k8s`가 따라야 하는 배포 절차와 계약

- [ ] **Step 1: 문서 작성**

`docs/deploy.md`
````markdown
# 배포 가이드

## overlay 목록
| overlay | 용도 | Postgres/Redis | worker 확장 |
|---|---|---|---|
| `k8s/overlays/local` | kind 로컬 실행 | 클러스터 내부 StatefulSet | CPU HPA |
| `k8s/overlays/local-loadtest` | 로컬 부하 테스트 (로그인 IP 리밋 완화) | 클러스터 내부 StatefulSet | KEDA (스트림 길이) |
| `k8s/overlays/aws` | EKS | RDS, ElastiCache (Terraform) | CPU HPA (KEDA 선택) |
| `k8s/overlays/gcp` | GKE | Cloud SQL, Memorystore (Terraform) | CPU HPA (KEDA 선택) |

## 로컬
```bash
brew install kind k6 kubeconform
docker compose down            # compose도 8080을 쓰므로 먼저 내린다
make k8s-local                 # http://localhost:8080
make k8s-smoke
make k8s-down
```
부하 테스트:
```bash
make k8s-local-loadtest        # KEDA 설치 + local-loadtest overlay
kubectl -n simple-web-app get hpa -w   # 다른 터미널
make loadtest                  # 약 14분. 동작 확인만 할 때는 make loadtest-quick
```
k6 조절: `make loadtest K6_ARGS="-e BASELINE=50 -e USERS=1000"` (PEAK 기본값은 BASELINE × 20).
k6 커스텀 메트릭: `read_duration`, `write_accepted_duration`(202만), `server_errors`(백프레셔 503 제외), `backpressure_503`, `rate_limited_429`.

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

# 4. 적용 → 마이그레이션 대기 → 롤아웃 대기
kubectl -n simple-web-app delete job db-migrate --ignore-not-found --wait=true
kubectl apply -k k8s/overlays/aws
kubectl -n simple-web-app wait --for=condition=complete job/db-migrate --timeout=300s
for d in nginx auth board-api board-worker; do
  kubectl -n simple-web-app rollout status deployment/$d --timeout=300s
done
```
Job의 Pod 템플릿은 변경할 수 없으므로, 기존 Job을 지운 뒤 적용한다.

**주의:** 마이그레이션 Job과 앱 Pod가 동시에 시작된다. 스키마 변경은 이전 버전 코드와 호환되게(컬럼 추가 → 코드 배포 → 이전 컬럼 제거 순서) 작성해야 한다. 첫 배포에서는 테이블이 만들어지기 전 몇 초 동안 API가 5xx를 낼 수 있다.

## 인프라 프로젝트(one-click-deploy-k8s)가 제공해야 하는 것
| 항목 | AWS | GCP | 앱 쪽 참조 위치 |
|---|---|---|---|
| 이미지 레지스트리 | ECR `simple-web-app/{frontend,auth,board}` | Artifact Registry `simple-web-app` 저장소 | overlay `images` |
| Secret `app-db` | `DATABASE_URL=postgresql+asyncpg://...` (RDS) | 동일 (Cloud SQL) | base `secretKeyRef` |
| Secret `app-redis` | `REDIS_URL=redis://...` 또는 `rediss://...` (ElastiCache) | 동일 (Memorystore) | base `secretKeyRef` |
| Ingress 컨트롤러 | AWS Load Balancer Controller (`ingressClassName: alb`) | GKE 기본 GCE Ingress | overlay `ingress.yaml` |
| 도메인 + 인증서 | ACM 인증서 ARN, 도메인 | 도메인 (ManagedCertificate가 발급), 전역 고정 IP `simple-web-app-ip` | `ingress.yaml`의 host, `certificate-arn`, `managedcertificate.yaml` |
| LB 대역 (`REAL_IP_FROM`) | VPC CIDR | GFE 대역(기본값 포함) + **LB 고정 IP/32 추가** | overlay `nginx-config` |
| metrics-server | 필수 (HPA) | GKE 기본 포함 | - |
| KEDA | 선택 | 선택 | `components/keda-worker` |
| Prometheus | 선택. 네임스페이스 이름은 `monitoring`이어야 NetworkPolicy가 수집을 허용 | 동일 | base `networkpolicy.yaml` |
| NetworkPolicy 적용 CNI | VPC CNI의 network policy 기능 활성화 | Dataplane V2 | - |

**Redis 요구사항**
- 버전 7 이상
- `maxmemory-policy noeviction` (ElastiCache 파라미터 그룹 / Memorystore 설정). 메모리가 부족할 때 글쓰기 큐나 세션이 임의로 삭제되면 안 된다.

**HTTPS 필수:** 클라우드 overlay는 `COOKIE_SECURE=true`라서 HTTP로 접속하면 로그인이 되지 않는다.

**GCP의 `REAL_IP_FROM`:** GCE LB는 `X-Forwarded-For` 끝에 LB 자신의 IP를 붙인다. 이 IP를 신뢰 대역에 넣지 않으면 모든 사용자의 IP가 LB IP로 인식되어 IP 리밋을 전원이 공유하게 된다. 배포할 때 overlay의 `nginx-config` 병합 값 끝에 ` <고정 IP>/32`를 추가한다.

### KEDA를 쓰는 경우
overlay의 `components` 주석을 풀고, 아래 패치를 overlay의 `patches`에 추가해서 스트림 길이를 읽을 Redis 주소를 지정한다(KEDA operator가 매니지드 Redis에 접속할 수 있어야 함).
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
Redis에 비밀번호나 TLS가 있으면 KEDA `TriggerAuthentication`을 추가해야 한다(`enableTLS`, `password`).

## DB 커넥션 계산 (스펙 §7)
| 워크로드 | 최대 Pod | Pod당 커넥션 (pool 5 + overflow 5) | 합계 |
|---|---|---|---|
| auth | 20 | 10 | 200 |
| board-api | 20 | 10 | 200 |
| board-worker | 10 | 10 | 100 |
| **합계** | | | **500** |

- Postgres `max_connections`는 **550 이상**으로 설정한다(500 + 마이그레이션·관리용 여유분). local overlay는 600이다.
- RDS/Cloud SQL 인스턴스 크기에 따라 기본 `max_connections`가 이보다 작을 수 있다. 이때는 파라미터를 올리거나, RDS Proxy / PgBouncer를 두고 `DATABASE_URL`을 프록시 주소로 바꾼다.
- HPA의 `maxReplicas`를 바꾸면 이 표도 다시 계산한다.
````

- [ ] **Step 2: 문서와 매니페스트 일치 확인**

```bash
grep -n 'max_connections=600' k8s/overlays/local/postgres.yaml
grep -n 'maxReplicas' k8s/base/hpa.yaml
grep -n 'streamLength' k8s/components/keda-worker/scaledobject.yaml
make k8s-validate
```
Expected: 각 grep이 한 줄 이상 출력(maxReplicas는 10, 20, 20, 10). 전체 검증 PASS.

- [ ] **Step 3: Commit**

```bash
git add docs/deploy.md
git commit -m "docs: add deployment guide and infra contract"
```

---

## 스펙 대비 차이 (의도적)
1. **worker PDB `maxUnavailable: 1`:** 스펙은 모든 앱 워크로드에 `minAvailable: 1`이지만, 최소 1개로 운영되는 worker에 적용하면 노드 drain이 막힌다.
2. **배포 순서:** 스펙 §12는 "마이그레이션 완료 대기 → apply"이지만, 이 계획은 전체 apply 후 Job 완료를 기다린다. 스키마 변경을 하위 호환으로 작성하는 조건을 `docs/deploy.md`에 적었다.
3. **인프라 계약 추가:** 스펙 §12에 없던 도메인·인증서, GCP 전역 고정 IP, `monitoring` 네임스페이스 이름, Redis `noeviction`과 버전 7 이상을 계약에 추가했다.
4. **local-loadtest overlay:** 한 대의 PC에서 부하 테스트를 할 수 있도록 로그인 IP 리밋만 완화한다.
