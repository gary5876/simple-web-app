import uuid
from urllib.parse import quote

import pytest
from starlette.requests import Request

from board.errors import ApiError
from board.identity import optional_user, require_user
from helpers import BROKEN_DATABASE_URL, BROKEN_REDIS_URL


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw})


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200


async def test_readyz_ok(client):
    r = await client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"redis": True, "db": True}


@pytest.mark.parametrize(
    "update, expected",
    [
        ({"redis_url": BROKEN_REDIS_URL}, {"redis": False, "db": True}),
        ({"database_url": BROKEN_DATABASE_URL}, {"redis": True, "db": False}),
    ],
)
async def test_readyz_stays_ready_with_one_dependency(make_client, settings, update, expected):
    async with make_client(settings.model_copy(update=update)) as c:
        r = await c.get("/readyz")
    assert r.status_code == 200
    assert r.json() == expected


async def test_readyz_503_when_both_down(make_client, settings):
    broken = settings.model_copy(update={"redis_url": BROKEN_REDIS_URL, "database_url": BROKEN_DATABASE_URL})
    async with make_client(broken) as c:
        r = await c.get("/readyz")
    assert r.status_code == 503


async def test_metrics_and_request_id(client):
    r = await client.get("/healthz", headers={"X-Request-ID": "abc"})
    assert r.headers["X-Request-ID"] == "abc"
    assert 'route="/healthz"' in (await client.get("/metrics")).text


def test_optional_user_decodes_headers():
    uid = str(uuid.uuid4())
    user = optional_user(_request({"X-User-Id": uid, "X-User-Nickname": quote("재난알림", safe="")}))
    assert user.id == uid
    assert user.nickname == "재난알림"


def test_optional_user_is_none_without_or_with_invalid_header():
    assert optional_user(_request({})) is None
    assert optional_user(_request({"X-User-Id": "not-a-uuid"})) is None


def test_require_user_raises_401_for_anonymous():
    with pytest.raises(ApiError) as e:
        require_user(_request({}))
    assert e.value.status == 401


def test_require_user_raises_503_when_auth_degraded():
    with pytest.raises(ApiError) as e:
        require_user(_request({"X-Auth-Degraded": "1", "X-User-Id": str(uuid.uuid4())}))
    assert e.value.status == 503
    assert e.value.headers["Retry-After"] == "5"
