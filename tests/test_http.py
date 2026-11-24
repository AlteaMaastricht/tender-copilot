"""Tests for ingest/http.py with a fake transport. Nothing here touches the network."""

import urllib.request

import pytest

from ingest.http import HttpClient, HttpError, HttpResponse

TENDERNED = "https://www.tenderned.nl/papi/tenderned-rs-tns/v2/publicaties/1"
TED = "https://ted.europa.eu/en/notice/1-2026/xml"


class FakeTransport:
    def __init__(self, *answers: HttpResponse | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []

    def __call__(self, req, timeout, max_bytes):
        self.requests.append(req)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def ok(body: bytes = b"{}", content_type: str = "application/json") -> HttpResponse:
    return HttpResponse(200, content_type, {}, body)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def client(transport: FakeTransport, clock: Clock | None = None, **kwargs) -> HttpClient:
    clock = clock or Clock()
    return HttpClient(transport=transport, sleep=clock.sleep, clock=clock.time, **kwargs)


@pytest.mark.parametrize(
    "url", ["http://www.tenderned.nl/x", "https://example.com/x", "https://evil.tenderned.nl/x"]
)
def test_disallowed_url_is_refused_without_a_request(url: str) -> None:
    transport = FakeTransport(ok())
    with pytest.raises(HttpError):
        client(transport).get_bytes(url)
    assert transport.requests == []


def test_waits_so_that_requests_are_two_seconds_apart() -> None:
    clock = Clock()
    http = client(FakeTransport(ok()), clock)
    http.get_bytes(TENDERNED)
    assert clock.sleeps == []  # the first request does not wait
    clock.now += 0.5
    http.get_bytes(TED)
    assert clock.sleeps == [pytest.approx(1.5)]
    assert http.requests == 2


def test_no_wait_when_enough_time_has_passed() -> None:
    clock = Clock()
    http = client(FakeTransport(ok()), clock)
    http.get_bytes(TENDERNED)
    clock.now += 5
    http.get_bytes(TENDERNED)
    assert clock.sleeps == []


@pytest.mark.parametrize("status", [302, 404, 429, 503])
def test_error_status_raises_after_one_request(status: int) -> None:
    transport = FakeTransport(HttpResponse(status, "", {"location": "https://x/"}, b""))
    with pytest.raises(HttpError) as info:
        client(transport).get_bytes(TENDERNED)
    assert info.value.status == status
    assert len(transport.requests) == 1  # no retry, no redirect followed


def test_body_over_the_cap_raises() -> None:
    with pytest.raises(HttpError):
        client(FakeTransport(ok(b"x" * 11))).get_bytes(TENDERNED, max_bytes=10)


def test_request_budget_stops_before_the_request() -> None:
    transport = FakeTransport(ok())
    http = client(transport, max_requests=2)
    http.get_bytes(TENDERNED)
    http.get_bytes(TENDERNED)
    with pytest.raises(HttpError):
        http.get_bytes(TENDERNED)
    assert len(transport.requests) == 2


def test_error_message_has_status_and_path_only() -> None:
    url = "https://www.tenderned.nl/papi/x?token=SECRET123#frag"
    transport = FakeTransport(HttpResponse(403, "", {"x-secret": "SECRET123"}, b"SECRET123"))
    with pytest.raises(HttpError) as info:
        client(transport).get_bytes(url)
    message = str(info.value)
    assert "403" in message and "/papi/x" in message and "SECRET123" not in message


def test_transport_failure_becomes_http_error_with_path() -> None:
    with pytest.raises(HttpError, match="/papi/x"):
        client(FakeTransport(HttpError("network error: TimeoutError"))).get_bytes(
            "https://www.tenderned.nl/papi/x"
        )


def test_get_json_parses_and_rejects_html() -> None:
    assert client(FakeTransport(ok(b'{"a": 1}'))).get_json(TENDERNED) == {"a": 1}
    with pytest.raises(HttpError):
        client(FakeTransport(ok(b"<html>", "text/html"))).get_json(TENDERNED)


def test_sends_a_user_agent_and_no_credentials() -> None:
    transport = FakeTransport(ok())
    client(transport).get_bytes(TENDERNED)
    headers = {k.lower(): v for k, v in transport.requests[0].header_items()}
    assert headers["user-agent"].startswith("tender-copilot")
    assert "authorization" not in headers
