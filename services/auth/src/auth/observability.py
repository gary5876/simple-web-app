import asyncio
import contextvars
import json
import logging
import sys
import time
import uuid

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware

from auth import timing

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

HTTP_REQUESTS = Counter("http_requests_total", "HTTP 요청 수", ["method", "route", "status"])
HTTP_LATENCY = Histogram("http_request_duration_seconds", "HTTP 요청 처리 시간", ["method", "route"])
# 이벤트 루프가 밀린 정도. 요청이 앱 코드에 닿기 전 줄 서 있는 시간의 간접 지표다.
LOOP_LAG = Histogram(
    "event_loop_lag_seconds",
    "asyncio.sleep이 예정보다 늦게 깨어난 시간",
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
LOOP_LAG_INTERVAL_SECONDS = 0.1


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", "unmatched")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        spans, timing_token = timing.begin()
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            HTTP_REQUESTS.labels(request.method, _route_template(request), "500").inc()
            raise
        finally:
            request_id_var.reset(token)
            timing.end(timing_token)
        elapsed = time.perf_counter() - start
        route = _route_template(request)
        HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        HTTP_LATENCY.labels(request.method, route).observe(elapsed)
        response.headers["X-Request-ID"] = request_id
        response.headers["Server-Timing"] = timing.server_timing(spans, elapsed)
        return response


def install_metrics_route(app: FastAPI) -> None:
    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def monitor_loop_lag() -> None:
    """LOOP_LAG_INTERVAL_SECONDS마다 잠들었다 깨어나며, 예정보다 늦은 만큼을 기록한다."""
    loop = asyncio.get_running_loop()
    while True:
        start = loop.time()
        await asyncio.sleep(LOOP_LAG_INTERVAL_SECONDS)
        LOOP_LAG.observe(max(0.0, loop.time() - start - LOOP_LAG_INTERVAL_SECONDS))
