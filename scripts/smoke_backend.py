"""compose 네트워크 안(auth 컨테이너)에서 실행한다: 회원가입 → 로그인 → 글쓰기 → 워커 반영 확인.

nginx 가 붙여 주는 X-User-* 헤더는 직접 넣어서 흉내 낸다. (nginx 경유 테스트는 계획 2)
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.cookiejar import CookieJar
from urllib.parse import quote

AUTH = "http://auth:8000"
BOARD = "http://board-api:8000"
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def call(method: str, url: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict | None]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with opener.open(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


def wait_ready(url: str) -> None:
    for _ in range(60):
        try:
            if call("GET", f"{url}/readyz")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(1)
    sys.exit(f"not ready: {url}")


def main() -> None:
    wait_ready(AUTH)
    wait_ready(BOARD)
    suffix = uuid.uuid4().hex[:8]
    email, password, nickname = f"smoke-{suffix}@example.com", "smoke-password-1", f"sm{suffix}"

    status, user = call("POST", f"{AUTH}/api/auth/signup", {"email": email, "password": password, "nickname": nickname})
    assert status == 201, (status, user)
    status, _ = call("POST", f"{AUTH}/api/auth/login", {"email": email, "password": password})
    assert status == 200, status

    headers = {"X-User-Id": user["id"], "X-User-Nickname": quote(nickname, safe=""), "Idempotency-Key": str(uuid.uuid4())}
    status, created = call("POST", f"{BOARD}/api/board/posts", {"title": "smoke", "body": "hello"}, headers)
    assert status == 202, (status, created)

    for _ in range(20):
        status, detail = call("GET", f"{BOARD}/api/board/posts/{created['id']}")
        if status == 200 and detail["status"] == "published":
            print("SMOKE OK", created["id"])
            return
        time.sleep(0.5)
    sys.exit(f"post was not published: {detail}")


if __name__ == "__main__":
    main()
