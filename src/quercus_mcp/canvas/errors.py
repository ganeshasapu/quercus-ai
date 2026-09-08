"""Exceptions raised by the Canvas client."""

from __future__ import annotations


class CanvasError(Exception):
    """Non-retryable HTTP error from Canvas."""

    def __init__(self, status: int, body: str = "", url: str = ""):
        self.status = status
        self.body = body[:500]
        self.url = url
        super().__init__(f"Canvas HTTP {status} for {url}: {self.body}")


class AuthError(CanvasError):
    """401: the access token is missing, invalid or expired."""


class RateLimitError(CanvasError):
    """Rate limit still exceeded after all retries."""
