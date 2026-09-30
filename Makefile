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
