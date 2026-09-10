import httpx
import pytest
import respx

from quercus_mcp.canvas.client import CanvasClient
from quercus_mcp.canvas.errors import AuthError, CanvasError

BASE = "https://q.utoronto.ca"


def make_client(**kw):
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    c = CanvasClient(BASE, "tok123", sleep=fake_sleep, **kw)
    return c, sleeps


@respx.mock
async def test_sends_bearer_and_user_agent():
    route = respx.get(f"{BASE}/api/v1/users/self").mock(return_value=httpx.Response(200, json={"id": 1}))
    client, _ = make_client()
    async with client:
        data = await client.get_json("/api/v1/users/self")
    assert data == {"id": 1}
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer tok123"
    assert req.headers["User-Agent"].startswith("quercus-mcp/")


@respx.mock
async def test_paginate_follows_link_next():
    page1 = respx.get(url__eq=f"{BASE}/api/v1/courses?per_page=100").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 1}, {"id": 2}],
            headers={"Link": f'<{BASE}/api/v1/courses?page=2&per_page=100>; rel="next", <{BASE}/api/v1/courses?page=1&per_page=100>; rel="current"'},
        )
    )
    page2 = respx.get(url__eq=f"{BASE}/api/v1/courses?page=2&per_page=100").mock(
        return_value=httpx.Response(200, json=[{"id": 3}], headers={"Link": f'<{BASE}/api/v1/courses?page=2&per_page=100>; rel="current"'})
    )
    client, _ = make_client()
    async with client:
        items = [x async for x in client.paginate("/api/v1/courses")]
    assert [i["id"] for i in items] == [1, 2, 3]
    assert page1.called and page2.called


@respx.mock
async def test_401_raises_auth_error():
    respx.get(f"{BASE}/api/v1/courses").mock(return_value=httpx.Response(401, json={"status": "unauthenticated"}))
    client, _ = make_client()
    async with client:
        with pytest.raises(AuthError):
            await client.get_json("/api/v1/courses")


@respx.mock
async def test_429_is_retried_with_backoff():
    route = respx.get(f"{BASE}/api/v1/courses").mock(
        side_effect=[httpx.Response(429, text="Rate Limit Exceeded"), httpx.Response(200, json=[{"id": 1}])]
    )
    client, sleeps = make_client()
    async with client:
        data = await client.get_json("/api/v1/courses")
    assert data == [{"id": 1}]
    assert route.call_count == 2
    assert len(sleeps) == 1 and sleeps[0] >= 1


@respx.mock
async def test_403_rate_limit_text_is_retried_but_plain_403_raises():
    respx.get(f"{BASE}/api/v1/a").mock(
        side_effect=[httpx.Response(403, text="403 Forbidden (Rate Limit Exceeded)"), httpx.Response(200, json={"ok": 1})]
    )
    respx.get(f"{BASE}/api/v1/b").mock(return_value=httpx.Response(403, json={"status": "unauthorized"}))
    client, _ = make_client()
    async with client:
        assert await client.get_json("/api/v1/a") == {"ok": 1}
        with pytest.raises(CanvasError) as ei:
            await client.get_json("/api/v1/b")
        assert ei.value.status == 403


@respx.mock
async def test_5xx_retried_then_raises():
    route = respx.get(f"{BASE}/api/v1/x").mock(return_value=httpx.Response(502, text="bad gateway"))
    client, sleeps = make_client(max_retries=3)
    async with client:
        with pytest.raises(CanvasError) as ei:
            await client.get_json("/api/v1/x")
    assert ei.value.status == 502
    assert route.call_count == 3


@respx.mock
async def test_low_rate_limit_remaining_triggers_pause():
    respx.get(f"{BASE}/api/v1/x").mock(
        return_value=httpx.Response(200, json={}, headers={"X-Rate-Limit-Remaining": "50.0"})
    )
    respx.get(f"{BASE}/api/v1/y").mock(return_value=httpx.Response(200, json={}))
    client, sleeps = make_client()
    async with client:
        await client.get_json("/api/v1/x")
        await client.get_json("/api/v1/y")
    assert sleeps == [2.0]
