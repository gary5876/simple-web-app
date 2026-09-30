.PHONY: dev down lock smoke-backend test test-backend

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

test: test-backend
