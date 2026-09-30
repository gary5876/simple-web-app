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
K8S_VERSION ?= 1.31.0
CRD_SCHEMA := https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json
KUBECONFORM := kubeconform -strict -summary -kubernetes-version $(K8S_VERSION) -schema-location default -schema-location '$(CRD_SCHEMA)'
K8S_TARGETS := k8s/base k8s/overlays/local

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
APP_DEPLOYMENTS := nginx auth board-api board-worker
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
	kubectl -n $(NS) wait --for=condition=complete job/db-migrate --timeout=600s
	kubectl -n $(NS) rollout restart deployment $(APP_DEPLOYMENTS)
	@for d in $(APP_DEPLOYMENTS); do kubectl -n $(NS) rollout status deployment/$$d --timeout=180s || exit 1; done

k8s-smoke: ## 로컬 클러스터 스모크 테스트
	NS=$(NS) ./k8s/tests/smoke.sh

k8s-down: ## kind 클러스터 삭제
	kind delete cluster --name $(KIND_CLUSTER)
