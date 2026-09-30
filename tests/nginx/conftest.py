import os
import pathlib
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest

BASE_URL = os.environ.get("NGINX_BASE_URL", "http://localhost:8080")
ROOT = pathlib.Path(__file__).resolve().parents[2]
PASSWORD = "nginx-test-password"


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True)


def wait_until(check: Callable[[], bool], timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(1)
    raise TimeoutError(f"condition not met within {timeout}s (last error: {last_error})")


def stack_ready() -> bool:
    return httpx.get(f"{BASE_URL}/api/board/posts", timeout=5).status_code == 200


@pytest.fixture(scope="session", autouse=True)
def stack() -> None:
    # 이미 떠 있는 스택에 붙이고 싶으면 NGINX_TEST_SKIP_COMPOSE=1
    if os.environ.get("NGINX_TEST_SKIP_COMPOSE") != "1":
        compose("up", "-d", "--build")
    wait_until(stack_ready)


def new_credentials() -> dict[str, str]:
    suffix = uuid.uuid4().hex[:10]
    return {"email": f"nginx-{suffix}@example.com", "password": PASSWORD, "nickname": f"ng{suffix}"}


def login_client(creds: dict[str, str]) -> httpx.Client:
    res = httpx.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": creds["email"], "password": creds["password"]},
        timeout=10,
    )
    assert res.status_code == 200, res.text
    # 쿠키 jar의 localhost 도메인 처리 차이를 피하려고 Cookie 헤더를 직접 넣는다.
    return httpx.Client(base_url=BASE_URL, timeout=10, headers={"Cookie": f"sid={res.cookies['sid']}"})


@pytest.fixture(scope="session")
def credentials() -> dict[str, str]:
    creds = new_credentials()
    res = httpx.post(f"{BASE_URL}/api/auth/signup", json=creds, timeout=10)
    assert res.status_code == 201, res.text
    return creds


@pytest.fixture(scope="session")
def session_client(credentials: dict[str, str]) -> Iterator[httpx.Client]:
    client = login_client(credentials)
    yield client
    client.close()


@pytest.fixture
def anon() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        yield client
