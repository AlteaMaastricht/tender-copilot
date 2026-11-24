"""One polite HTTP client for TED and TenderNed (D1, minimal).

GET only, https only, no redirects, a global wait between requests, a size cap and a request
budget. The transport, sleep and clock can be replaced, so tests never touch the network.
Errors carry the status and the URL path only: never headers, query strings or bodies.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

ALLOWED_HOSTS = frozenset({"www.tenderned.nl", "ted.europa.eu"})
USER_AGENT = "tender-copilot/0.1"
DEFAULT_DELAY_SECONDS = 2.0
TIMEOUT_SECONDS = 60
MAX_METADATA_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_REQUESTS = 150


class HttpError(Exception):
    """A refused URL, a non-2xx answer, a network error or a body over the cap."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    content_type: str
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[urllib.request.Request, float, int], HttpResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(req: urllib.request.Request, timeout: float, max_bytes: int) -> HttpResponse:
    """The real transport. Reads at most `max_bytes + 1` bytes, so the caller can see an overrun."""
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read(max_bytes + 1)
            return HttpResponse(resp.status, headers.get("content-type", ""), headers, body)
    except urllib.error.HTTPError as exc:
        headers = {k.lower(): v for k, v in exc.headers.items()}
        status = exc.code
        exc.close()
        return HttpResponse(status, headers.get("content-type", ""), headers, b"")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HttpError(f"network error: {type(exc).__name__}") from None


class HttpClient:
    def __init__(
        self,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        delay: float = DEFAULT_DELAY_SECONDS,
        max_requests: int = DEFAULT_MAX_REQUESTS,
    ) -> None:
        self._transport = transport or urllib_transport
        self._sleep = sleep
        self._clock = clock
        self._delay = delay
        self._max_requests = max_requests
        self._last: float | None = None
        self._requests = 0

    @property
    def requests(self) -> int:
        return self._requests

    def get_bytes(self, url: str, *, max_bytes: int = MAX_METADATA_BYTES) -> HttpResponse:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
            raise HttpError(f"refusing to request {parts.hostname or 'a URL'} ({parts.scheme})")
        path = parts.path
        if self._requests >= self._max_requests:
            raise HttpError(f"request budget of {self._max_requests} reached before {path}")
        if self._last is not None:
            wait = self._delay - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()
        self._requests += 1
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            response = self._transport(req, TIMEOUT_SECONDS, max_bytes)
        except HttpError as exc:
            raise HttpError(f"{exc} for {path}") from None
        if not 200 <= response.status < 300:
            kind = "redirect not followed" if 300 <= response.status < 400 else "HTTP error"
            raise HttpError(f"{kind} {response.status} for {path}", response.status)
        if len(response.body) > max_bytes:
            raise HttpError(f"response over {max_bytes} bytes for {path}", response.status)
        return response

    def get_json(self, url: str) -> Any:
        response = self.get_bytes(url)
        try:
            return json.loads(response.body)
        except ValueError:
            raise HttpError(f"not JSON for {urlsplit(url).path}", response.status) from None
