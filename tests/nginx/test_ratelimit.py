import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

from conftest import BASE_URL, login_client


def post_once(client: httpx.Client) -> httpx.Response:
    return client.post(
        "/api/board/posts",
        json={"title": "rate", "body": "limit"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


def test_write_limit_is_per_session(credentials: dict[str, str]) -> None:
    flooding = login_client(credentials)
    responses = [post_once(flooding) for _ in range(15)]
    statuses = [r.status_code for r in responses]
    assert 202 in statuses
    limited = next(r for r in responses if r.status_code == 429)
    assert limited.json()["code"] == "RATE_LIMITED"
    assert limited.headers["retry-after"] == "2"

    # 같은 사용자라도 다른 세션은 영향을 받지 않는다.
    other = login_client(credentials)
    assert post_once(other).status_code == 202


def test_read_limit_blocks_a_flooding_ip() -> None:
    with httpx.Client(base_url=BASE_URL, timeout=10, limits=httpx.Limits(max_connections=32)) as client:
        with ThreadPoolExecutor(max_workers=32) as pool:
            statuses = list(pool.map(lambda _: client.get(f"/api/board/posts/{uuid.uuid4()}").status_code, range(800)))
    assert 404 in statuses
    assert 429 in statuses


def test_login_limit_ignores_spoofed_forwarded_for() -> None:
    # 로그인 리밋을 소진시키므로 이 파일의 마지막 테스트여야 한다.
    limited = None
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        for i in range(25):
            res = client.post(
                "/api/auth/login",
                json={"email": f"nobody-{uuid.uuid4().hex[:8]}@example.com", "password": "wrong-password"},
                headers={"X-Forwarded-For": f"203.0.113.{i}"},
            )
            if res.status_code == 429 and res.json().get("code") == "RATE_LIMITED":
                limited = res
                break
    assert limited is not None, "IP 기준 로그인 리밋이 걸리지 않았다 (X-Forwarded-For 위조로 우회됐을 수 있음)"
    assert limited.headers["retry-after"] == "2"
