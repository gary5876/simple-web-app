"""부하 테스트 분석용 access log(perf 형식) 검증."""

import json
import subprocess
import uuid

import httpx

from conftest import ROOT, wait_until


def perf_log_line(request_id: str) -> dict:
    """nginx 로그에서 request_id가 같은 perf 줄을 찾는다. 로그는 약간 늦게 flush될 수 있어 잠깐 기다린다."""
    found: dict = {}

    def check() -> bool:
        out = subprocess.run(
            ["docker", "compose", "logs", "nginx", "--no-log-prefix", "--since", "5m"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for line in out.splitlines():
            if line.startswith("{") and f'"rid":"{request_id}"' in line:
                found.update(json.loads(line))
                return True
        return False

    wait_until(check, timeout=10)
    return found


def test_server_timing_is_hidden_from_clients(anon: httpx.Client) -> None:
    res = anon.get("/api/board/posts")
    assert res.status_code == 200
    assert "server-timing" not in res.headers


def test_board_request_logs_verify_and_app_timings(session_client: httpx.Client) -> None:
    res = session_client.post(
        "/api/board/posts", json={"title": "perf", "body": "log"}, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    assert res.status_code == 202
    line = perf_log_line(res.headers["x-request-id"])

    assert line["status"] == 202
    # 본 요청 upstream 시간은 board-api 한 곳만 담아야 한다(verify 시간이 섞이면 "0.001, 0.003"처럼 두 값이 된다).
    assert "," not in line["urt"]
    assert float(line["vt"]) <= line["rt"]
    assert 'redis;dur=' in line["st_app"] and 'desc="4"' in line["st_app"]
    assert 'redis;dur=' in line["st_verify"] and 'desc="1"' in line["st_verify"]


def test_auth_request_logs_app_timing_without_verify(anon: httpx.Client) -> None:
    res = anon.post("/api/auth/login", json={"email": "nobody@example.com", "password": "x"})
    assert res.status_code == 401
    line = perf_log_line(res.headers["x-request-id"])

    assert line["vt"] == ""
    assert "hash;dur=" in line["st_app"]
