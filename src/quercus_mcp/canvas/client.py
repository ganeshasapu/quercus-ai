"""Thin async Canvas LMS REST client.

Handles bearer auth, the mandatory User-Agent header, Link-header pagination,
rate-limit backoff, a global concurrency cap, and file downloads that follow
redirects to S3/InstFS without leaking the Authorization header.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from quercus_mcp import __version__
from quercus_mcp.canvas.errors import AuthError, CanvasError, RateLimitError
from quercus_mcp.canvas.models import Assignment, Course, DiscussionTopic, File, Folder, Module, ModuleItem, Page

USER_AGENT = f"quercus-mcp/{__version__} (+https://github.com/quercus-mcp)"
PER_PAGE = 100
RATE_LIMIT_TEXT = "rate limit exceeded"
LOW_RATE_LIMIT = 100.0
LOW_RATE_LIMIT_PAUSE = 2.0
SERVER_ERROR_RETRIES = 3

_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')


def parse_next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for url, rel in _LINK_RE.findall(link_header):
        if rel == "next":
            return url
    return None


class CanvasClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        max_concurrency: int = 4,
        max_retries: int = 5,
        timeout: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        user_agent: str = USER_AGENT,
    ):
        self.base_url = base_url.rstrip("/")
        self.host = urlparse(self.base_url).netloc
        self._token = token
        self._sem = asyncio.Semaphore(max_concurrency)
        self._max_retries = max_retries
        self._sleep = sleep
        self._pause_before_next = 0.0
        self._http = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )

    async def __aenter__(self) -> "CanvasClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ core

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        auth: bool = True,
        stream_to: Path | None = None,
    ) -> httpx.Response:
        """Perform one request with retries. Redirects are *not* followed here."""
        headers = self._auth_headers() if auth else {}
        attempt = 0
        while True:
            attempt += 1
            async with self._sem:
                if self._pause_before_next:
                    pause, self._pause_before_next = self._pause_before_next, 0.0
                    await self._sleep(pause)
                try:
                    if stream_to is None:
                        resp = await self._http.request(method, url, params=params, headers=headers)
                    else:
                        resp = await self._stream_download(method, url, params, headers, stream_to)
                except (httpx.TransportError,) as exc:
                    if attempt >= self._max_retries:
                        raise CanvasError(0, str(exc), url) from exc
                    await self._sleep(min(2 ** (attempt - 1), 30))
                    continue

            remaining = resp.headers.get("X-Rate-Limit-Remaining")
            if remaining is not None:
                try:
                    if float(remaining) < LOW_RATE_LIMIT:
                        self._pause_before_next = LOW_RATE_LIMIT_PAUSE
                except ValueError:
                    pass

            if self._is_rate_limited(resp):
                if attempt >= self._max_retries:
                    raise RateLimitError(resp.status_code, resp.text, url)
                await self._sleep(min(2 ** (attempt - 1), 30))
                continue
            if 500 <= resp.status_code < 600:
                if attempt >= min(self._max_retries, SERVER_ERROR_RETRIES):
                    raise CanvasError(resp.status_code, resp.text, url)
                await self._sleep(min(2 ** (attempt - 1), 30))
                continue
            return resp

    async def _stream_download(
        self, method: str, url: str, params: dict[str, Any] | None, headers: dict[str, str], dest: Path
    ) -> httpx.Response:
        async with self._http.stream(method, url, params=params, headers=headers) as resp:
            if resp.status_code != 200:
                await resp.aread()
                return resp
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".part")
            n = 0
            with tmp.open("wb") as fh:
                async for chunk in resp.aiter_bytes():
                    fh.write(chunk)
                    n += len(chunk)
            tmp.replace(dest)
            resp.extensions["bytes_written"] = n
            return resp

    @staticmethod
    def _is_rate_limited(resp: httpx.Response) -> bool:
        if resp.status_code == 429:
            return True
        return resp.status_code == 403 and RATE_LIMIT_TEXT in resp.text.lower()

    @staticmethod
    def _raise_for_status(resp: httpx.Response, url: str) -> None:
        if resp.status_code == 401:
            raise AuthError(401, resp.text, url)
        if resp.status_code >= 400:
            raise CanvasError(resp.status_code, resp.text, url)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        url = self._url(path)
        resp = await self._request("GET", url, params=params)
        self._raise_for_status(resp, url)
        return resp

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self.get(path, params)
        return resp.json()

    async def paginate(self, path: str, params: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
        params = dict(params or {})
        params.setdefault("per_page", PER_PAGE)
        url: str | None = self._url(path)
        first = True
        while url:
            resp = await self._request("GET", url, params=params if first else None)
            self._raise_for_status(resp, url)
            data = resp.json()
            if isinstance(data, dict):  # some endpoints wrap lists; be tolerant
                data = next((v for v in data.values() if isinstance(v, list)), [])
            for item in data:
                yield item
            url = parse_next_link(resp.headers.get("Link"))
            first = False

    # -------------------------------------------------------------- download

    async def download_file(self, file_id: int, dest: Path, *, course_id: int | None = None, max_hops: int = 5) -> int:
        """Download a file's bytes to *dest*. Returns bytes written.

        Sends the bearer token to the Canvas host and drops it when redirected
        to another host (S3/InstFS). Falls back to the public_url endpoint when
        the direct download is refused.
        """
        url = f"{self.base_url}/files/{file_id}/download"
        try:
            return await self._follow_download(url, {"download_frd": "1"}, dest, max_hops)
        except CanvasError as first_exc:
            if first_exc.status not in (401, 403, 404):
                raise
            try:
                data = await self.get_json(f"/api/v1/files/{file_id}/public_url")
            except AuthError as exc:
                raise CanvasError(exc.status, exc.body, exc.url) from exc
            public = data.get("public_url") if isinstance(data, dict) else None
            if not public:
                raise CanvasError(first_exc.status, "no public_url available", url) from first_exc
            return await self._follow_download(public, None, dest, max_hops)

    async def _follow_download(self, url: str, params: dict[str, Any] | None, dest: Path, max_hops: int) -> int:
        for _ in range(max_hops):
            same_host = urlparse(url).netloc == self.host
            resp = await self._request("GET", url, params=params, auth=same_host, stream_to=dest)
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                if not loc:
                    raise CanvasError(resp.status_code, "redirect without Location", url)
                url = str(httpx.URL(url).join(loc))
                params = None
                continue
            if resp.status_code == 200:
                return int(resp.extensions.get("bytes_written", 0))
            # Never map a per-file 401 to AuthError: it usually means "not
            # allowed to download this file", not "token expired".
            raise CanvasError(resp.status_code, resp.text, url)
        raise CanvasError(0, "too many redirects", url)

    # ---------------------------------------------------------- endpoints

    async def get_self(self) -> dict[str, Any]:
        return await self.get_json("/api/v1/users/self")

    async def list_courses(self, *, include_completed: bool = False) -> list[Course]:
        params: dict[str, Any] = {
            "enrollment_type": "student",
            "include[]": ["term", "syllabus_body"],
        }
        if not include_completed:
            params["enrollment_state"] = "active"
        return [
            Course.from_api(c)
            async for c in self.paginate("/api/v1/courses", params)
            if "id" in c and c.get("access_restricted_by_date") is not True
        ]

    async def get_course(self, course_id: int) -> Course | None:
        try:
            return Course.from_api(await self.get_json(f"/api/v1/courses/{course_id}", {"include[]": ["term", "syllabus_body"]}))
        except CanvasError as exc:
            if isinstance(exc, AuthError):
                raise
            if exc.status in (403, 404):
                return None
            raise

    # Files/folders: Canvas answers 401 (not 403) when the Files tab is hidden
    # for students, so 401 is *not* treated as an auth failure here.
    async def list_folders(self, course_id: int) -> list[Folder] | None:
        try:
            return [Folder.from_api(f) async for f in self.paginate(f"/api/v1/courses/{course_id}/folders")]
        except CanvasError as exc:
            if exc.status in (401, 403, 404):
                return None
            raise

    async def list_files(self, course_id: int) -> list[File] | None:
        """All files visible via the Files tab, or None if the tab is hidden."""
        try:
            return [File.from_api(f) async for f in self.paginate(f"/api/v1/courses/{course_id}/files")]
        except CanvasError as exc:
            if exc.status in (401, 403, 404):
                return None
            raise

    async def get_file(self, course_id: int | None, file_id: int) -> File | None:
        path = f"/api/v1/courses/{course_id}/files/{file_id}" if course_id else f"/api/v1/files/{file_id}"
        try:
            return File.from_api(await self.get_json(path))
        except CanvasError as exc:
            if exc.status in (401, 403, 404):
                return None
            raise

    # Everything below: 401 means the token is bad → AuthError propagates.
    # 403/404 mean the tool/tab is disabled for this course → None ("denied").

    @staticmethod
    def _denied(exc: CanvasError) -> bool:
        if isinstance(exc, AuthError):
            raise exc
        return exc.status in (403, 404)

    async def list_modules(self, course_id: int) -> list[Module] | None:
        try:
            mods = [Module.from_api(m) async for m in self.paginate(f"/api/v1/courses/{course_id}/modules", {"include[]": ["items", "content_details"]})]
            for m in mods:
                if m.items is None:
                    m.items = [
                        ModuleItem.from_api({**it, "module_id": m.id})
                        async for it in self.paginate(f"/api/v1/courses/{course_id}/modules/{m.id}/items", {"include[]": ["content_details"]})
                    ]
            return mods
        except CanvasError as exc:
            if self._denied(exc):
                return None
            raise

    async def list_pages(self, course_id: int) -> list[Page] | None:
        try:
            pages = [Page.from_api(p) async for p in self.paginate(f"/api/v1/courses/{course_id}/pages", {"include[]": ["body"], "published": "true"})]
        except CanvasError as exc:
            if self._denied(exc):
                return None
            raise
        # Older Canvas builds ignore include[]=body; fetch individually if needed.
        for p in pages:
            if p.body is None:
                try:
                    full = await self.get_json(f"/api/v1/courses/{course_id}/pages/{p.url}")
                    p.body = full.get("body")
                except CanvasError as exc:
                    if isinstance(exc, AuthError):
                        raise
                    p.body = ""
        return pages

    async def list_assignments(self, course_id: int) -> list[Assignment] | None:
        try:
            return [Assignment.from_api(a) async for a in self.paginate(f"/api/v1/courses/{course_id}/assignments", {"include[]": ["submission"], "order_by": "due_at"})]
        except CanvasError as exc:
            if self._denied(exc):
                return None
            raise

    async def list_announcements(self, course_id: int, *, start_date: str, end_date: str | None = None) -> list[DiscussionTopic] | None:
        # Canvas defaults end_date to start_date + 28 days, which would hide
        # everything after the first month of term.
        end_date = end_date or (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        params = {"context_codes[]": [f"course_{course_id}"], "start_date": start_date, "end_date": end_date, "active_only": "true"}
        try:
            return [DiscussionTopic.from_api(a, is_announcement=True) async for a in self.paginate("/api/v1/announcements", params)]
        except CanvasError as exc:
            if self._denied(exc):
                return None
            raise

    async def list_discussions(self, course_id: int) -> list[DiscussionTopic] | None:
        try:
            return [DiscussionTopic.from_api(t, is_announcement=False) async for t in self.paginate(f"/api/v1/courses/{course_id}/discussion_topics")]
        except CanvasError as exc:
            if self._denied(exc):
                return None
            raise

    async def get_discussion_view(self, course_id: int, topic_id: int) -> dict[str, Any]:
        try:
            return await self.get_json(f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/view")
        except CanvasError as exc:
            if self._denied(exc):
                return {}
            raise
