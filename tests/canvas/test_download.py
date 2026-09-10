import httpx
import pytest
import respx

from quercus_mcp.canvas.client import CanvasClient
from quercus_mcp.canvas.errors import CanvasError

BASE = "https://q.utoronto.ca"
S3 = "https://inst-fs.s3.amazonaws.com/blob?X-Amz-Signature=abc"


async def noop_sleep(_):
    pass


@respx.mock
async def test_download_strips_auth_on_cross_host_redirect(tmp_path):
    respx.get(f"{BASE}/files/42/download", params={"download_frd": "1"}).mock(
        return_value=httpx.Response(302, headers={"Location": S3})
    )
    s3 = respx.get(S3).mock(return_value=httpx.Response(200, content=b"%PDF-1.4 hello"))
    client = CanvasClient(BASE, "tok", sleep=noop_sleep)
    dest = tmp_path / "a.pdf"
    async with client:
        n = await client.download_file(42, dest)
    assert n == 14
    assert dest.read_bytes() == b"%PDF-1.4 hello"
    assert "Authorization" not in s3.calls.last.request.headers


@respx.mock
async def test_download_keeps_auth_on_same_host_redirect(tmp_path):
    respx.get(f"{BASE}/files/42/download", params={"download_frd": "1"}).mock(
        return_value=httpx.Response(302, headers={"Location": f"{BASE}/files/42/real"})
    )
    real = respx.get(f"{BASE}/files/42/real").mock(return_value=httpx.Response(200, content=b"x"))
    client = CanvasClient(BASE, "tok", sleep=noop_sleep)
    async with client:
        await client.download_file(42, tmp_path / "a")
    assert real.calls.last.request.headers["Authorization"] == "Bearer tok"


@respx.mock
async def test_download_falls_back_to_public_url(tmp_path):
    respx.get(f"{BASE}/files/42/download", params={"download_frd": "1"}).mock(return_value=httpx.Response(403))
    respx.get(f"{BASE}/api/v1/files/42/public_url").mock(return_value=httpx.Response(200, json={"public_url": S3}))
    s3 = respx.get(S3).mock(return_value=httpx.Response(200, content=b"data"))
    client = CanvasClient(BASE, "tok", sleep=noop_sleep)
    dest = tmp_path / "sub" / "a.bin"
    async with client:
        n = await client.download_file(42, dest)
    assert n == 4 and dest.read_bytes() == b"data"
    assert "Authorization" not in s3.calls.last.request.headers


@respx.mock
async def test_download_both_paths_fail_raises(tmp_path):
    respx.get(f"{BASE}/files/42/download", params={"download_frd": "1"}).mock(return_value=httpx.Response(401))
    respx.get(f"{BASE}/api/v1/files/42/public_url").mock(return_value=httpx.Response(401, json={"status": "unauthorized"}))
    client = CanvasClient(BASE, "tok", sleep=noop_sleep)
    async with client:
        with pytest.raises(CanvasError):
            await client.download_file(42, tmp_path / "a")
    assert not (tmp_path / "a").exists()
