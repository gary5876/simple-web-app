import re
import uuid

import httpx


def new_post_headers() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def test_nginx_health(anon: httpx.Client) -> None:
    res = anon.get("/nginx-health")
    assert res.status_code == 200


def test_spa_fallback_serves_index_without_cache(anon: httpx.Client) -> None:
    res = anon.get("/posts/some-client-side-route")
    assert res.status_code == 200
    assert '<div id="root">' in res.text
    assert res.headers["cache-control"] == "no-cache"


def test_hashed_assets_are_immutable(anon: httpx.Client) -> None:
    index = anon.get("/").text
    match = re.search(r'src="(/assets/[^"]+\.js)"', index)
    assert match, index
    res = anon.get(match.group(1))
    assert res.status_code == 200
    assert "immutable" in res.headers["cache-control"]
    assert anon.get("/assets/does-not-exist.js").status_code == 404


def test_every_response_carries_a_request_id(anon: httpx.Client) -> None:
    res = anon.get("/api/board/posts")
    assert re.fullmatch(r"[0-9a-f]{32}", res.headers["x-request-id"])


def test_internal_endpoints_are_not_exposed(anon: httpx.Client) -> None:
    for path in ["/_verify", "/internal/verify", "/healthz", "/readyz", "/metrics"]:
        assert anon.get(path).status_code == 404, path


def test_unknown_api_path_returns_json_404(anon: httpx.Client) -> None:
    res = anon.get("/api/nope")
    assert res.status_code == 404
    assert res.json()["code"] == "NOT_FOUND"


def test_auth_routes_are_proxied(anon: httpx.Client) -> None:
    res = anon.get("/api/auth/me")
    assert res.status_code == 401
    assert res.json()["code"] == "UNAUTHORIZED"


def test_anonymous_can_read_posts(anon: httpx.Client) -> None:
    res = anon.get("/api/board/posts")
    assert res.status_code == 200
    assert "items" in res.json()


def test_spoofed_user_header_is_stripped(anon: httpx.Client) -> None:
    # nginx가 헤더를 그대로 넘기면 board-api는 이 요청을 로그인 사용자로 보고 202를 반환한다.
    res = anon.post(
        "/api/board/posts",
        json={"title": "spoof", "body": "spoof"},
        headers={**new_post_headers(), "X-User-Id": str(uuid.uuid4()), "X-User-Nickname": "evil"},
    )
    assert res.status_code == 401


def test_logged_in_user_can_post(session_client: httpx.Client) -> None:
    res = session_client.post("/api/board/posts", json={"title": "nginx", "body": "ok"}, headers=new_post_headers())
    assert res.status_code == 202, res.text
    assert res.json()["status"] == "pending"


def test_spoofed_degraded_header_is_stripped(session_client: httpx.Client) -> None:
    # 넘어가면 board-api가 세션 저장소 장애로 보고 503을 반환한다.
    res = session_client.post(
        "/api/board/posts",
        json={"title": "spoof", "body": "degraded"},
        headers={**new_post_headers(), "X-Auth-Degraded": "1"},
    )
    assert res.status_code == 202, res.text
