.PHONY: test test-backend

test-backend:
	cd services/board && uv run --extra dev pytest -q

test: test-backend
