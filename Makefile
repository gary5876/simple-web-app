.PHONY: dev down lock smoke-backend test test-backend fe-dev test-frontend test-e2e test-nginx

dev:
	docker compose up --build

down:
	docker compose down

# pyproject.toml 의존성을 바꾼 뒤 실행한다. uv.lock 과 Docker 용 requirements.lock 을 함께 갱신한다.
lock:
	cd services/auth && uv lock && uv export --frozen --no-dev --no-hashes --no-emit-project -o requirements.lock
	cd services/board && uv lock && uv export --frozen --no-dev --no-hashes --no-emit-project -o requirements.lock

smoke-backend:
	docker compose up -d --build --wait postgres redis auth board-api board-worker
	docker compose exec -T auth python - < scripts/smoke_backend.py

test-backend:
	cd services/auth && uv run --extra dev pytest -q
	cd services/board && uv run --extra dev pytest -q

test: test-backend test-frontend test-e2e test-nginx

fe-dev:
	cd frontend && npm run dev

test-frontend:
	cd frontend && npm ci && npm run typecheck && npm test

test-e2e:
	docker compose up -d --build
	scripts/wait-http.sh http://localhost:8080/api/board/posts 120
	cd frontend && npm ci && npx playwright install chromium && npx playwright test

NGINX_TEST_VENV := tests/nginx/.venv

$(NGINX_TEST_VENV)/bin/pytest: tests/nginx/requirements.txt
	uv venv --clear --python 3.12 $(NGINX_TEST_VENV)
	uv pip install --python $(NGINX_TEST_VENV)/bin/python -q -r tests/nginx/requirements.txt
	touch $@

test-nginx: $(NGINX_TEST_VENV)/bin/pytest
	$(NGINX_TEST_VENV)/bin/pytest tests/nginx/test_config.py tests/nginx/test_routing.py tests/nginx/test_degraded.py tests/nginx/test_ratelimit.py -v

# ---------- k8s ----------
NS ?= simple-web-app
K8S_VERSION ?= 1.34.0
CRD_SCHEMA := https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json
KUBECONFORM := kubeconform -strict -summary -kubernetes-version $(K8S_VERSION) -schema-location default -schema-location '$(CRD_SCHEMA)'
K8S_TARGETS := k8s/base k8s/overlays/local k8s/overlays/local-loadtest \
	k8s/overlays/aws/dev k8s/overlays/aws/prod k8s/overlays/gcp/dev k8s/overlays/gcp/prod

.PHONY: k8s-validate
# 렌더 결과를 먼저 변수에 담아 kustomize 실패가 파이프에 묻히지 않게 한다(macOS make 3.81은 .SHELLFLAGS 미지원).
k8s-validate: ## 모든 kustomize 대상 렌더링 + 스키마 검증
	@set -e; for t in $(K8S_TARGETS); do \
		echo "== $$t"; \
		out=$$(kubectl kustomize $$t) || exit 1; \
		printf '%s\n' "$$out" | $(KUBECONFORM) || exit 1; \
	done

KIND_CLUSTER ?= simple-web-app
LOCAL_OVERLAY ?= local
METRICS_SERVER_URL := https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml
APP_DEPLOYMENTS := nginx auth auth-verify board-api board-worker
LOCAL_IMAGES := simple-web-app/auth:dev simple-web-app/board:dev simple-web-app/frontend:dev

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
	@# Docker 29 containerd 이미지 스토어에서 `kind load docker-image`가 실패하면 image-archive로 대체한다.
	kind load docker-image --name $(KIND_CLUSTER) $(LOCAL_IMAGES) || { \
		echo "kind load docker-image 실패 — image-archive로 재시도"; \
		docker save -o /tmp/simple-web-app-images.tar $(LOCAL_IMAGES) && \
		kind load image-archive --name $(KIND_CLUSTER) /tmp/simple-web-app-images.tar; \
		rc=$$?; rm -f /tmp/simple-web-app-images.tar; exit $$rc; }
	@# loadtest 오버레이가 남긴 KEDA ScaledObject가 있으면 지운다(HPA와 충돌). CRD가 없으면 건너뛴다.
	@if kubectl get crd scaledobjects.keda.sh >/dev/null 2>&1; then \
		kubectl -n $(NS) delete scaledobject board-worker --ignore-not-found; fi
	-kubectl -n $(NS) delete job db-migrate --ignore-not-found --wait=true
	kubectl apply -k k8s/overlays/$(LOCAL_OVERLAY)
	@# complete만 기다리면 Job이 실패해도 600초를 다 채운다. Complete/Failed 중 먼저 오는 쪽을 보고, 실패면 로그를 찍고 멈춘다.
	@echo "waiting for job/db-migrate"; \
	for i in $$(seq 1 122); do \
		st=$$(kubectl -n $(NS) get job db-migrate -o jsonpath='{range .status.conditions[?(@.status=="True")]}{.type}{" "}{end}'); \
		case " $$st" in \
			*" Complete "*) echo "db-migrate complete"; exit 0;; \
			*" Failed "*) echo "db-migrate FAILED" >&2; \
				kubectl -n $(NS) logs job/db-migrate --all-containers --tail=200 >&2; exit 1;; \
		esac; sleep 5; \
	done; echo "db-migrate timed out" >&2; kubectl -n $(NS) logs job/db-migrate --all-containers --tail=200 >&2; exit 1
	kubectl -n $(NS) rollout restart deployment $(APP_DEPLOYMENTS)
	@for d in $(APP_DEPLOYMENTS); do kubectl -n $(NS) rollout status deployment/$$d --timeout=180s || exit 1; done

k8s-smoke: ## 로컬 클러스터 스모크 테스트
	NS=$(NS) ./k8s/tests/smoke.sh

k8s-down: ## kind 클러스터 삭제
	kind delete cluster --name $(KIND_CLUSTER)

KEDA_URL := https://github.com/kedacore/keda/releases/download/v2.19.0/keda-2.19.0.yaml

.PHONY: k8s-keda-install k8s-local-loadtest
k8s-keda-install: ## 현재 kubectl 컨텍스트에 KEDA 설치 (v2.19: k8s 1.32~1.34 지원)
	kubectl apply --server-side -f $(KEDA_URL)
	kubectl -n keda rollout status deployment/keda-operator --timeout=180s
	kubectl -n keda rollout status deployment/keda-metrics-apiserver --timeout=180s
	kubectl -n keda rollout status deployment/keda-admission --timeout=180s

k8s-local-loadtest: ## kind에 부하 테스트용 overlay 배포 (KEDA 포함, 로그인·읽기 IP 리밋 완화)
	@kind get clusters 2>/dev/null | grep -qx $(KIND_CLUSTER) || \
		kind create cluster --name $(KIND_CLUSTER) --config k8s/kind/cluster.yaml
	$(MAKE) k8s-keda-install
	@# KEDA가 만드는 HPA와 충돌하지 않도록 base의 CPU HPA를 먼저 지운다.
	-kubectl -n $(NS) delete hpa board-worker --ignore-not-found
	$(MAKE) k8s-local LOCAL_OVERLAY=local-loadtest

BASE_URL ?= http://localhost:8080
K6_ARGS ?=

.PHONY: loadtest loadtest-quick loadtest-local
loadtest: ## k6 재난 시나리오 (약 14분). 먼저 make k8s-local-loadtest
	k6 run -e BASE_URL=$(BASE_URL) $(if $(USERS),-e USERS=$(USERS)) $(if $(PEAK),-e PEAK=$(PEAK)) $(K6_ARGS) loadtest/disaster.js

loadtest-quick: ## 단축 시나리오 (약 1분 30초, 동작 확인용)
	k6 run -e BASE_URL=$(BASE_URL) -e QUICK=1 $(if $(USERS),-e USERS=$(USERS)) $(if $(PEAK),-e PEAK=$(PEAK)) $(K6_ARGS) loadtest/disaster.js

loadtest-local: ## 노트북용 축소판 (약 14분, USERS=60 PEAK=80). 클라우드 임계치는 그대로
	k6 run -e BASE_URL=$(BASE_URL) -e PROFILE=local $(if $(USERS),-e USERS=$(USERS)) $(if $(PEAK),-e PEAK=$(PEAK)) $(K6_ARGS) loadtest/disaster.js
