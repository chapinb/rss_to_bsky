"""Tests for the bounded GET used for feeds and thumbnails."""

import gzip
from unittest.mock import patch

import httpx
import pytest

from rss_post import http


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def get(url="https://example.com/feed", handler=None, **kwargs):
    handler = handler or (lambda request: httpx.Response(200, content=b"ok"))
    kwargs.setdefault("max_bytes", 100)
    kwargs.setdefault("timeout", 5.0)
    return http.get(url, client=client(handler), **kwargs)


def test_returns_the_body_and_final_url():
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "/new"})
        return httpx.Response(200, content=b"hello")

    response = get("https://example.com/old", handler)

    assert response.content == b"hello"
    assert str(response.url) == "https://example.com/new"


def test_decompressed_body_is_returned_as_is():
    body = gzip.compress(b"<rss/>")
    response = get(
        handler=lambda request: httpx.Response(
            200, content=body, headers={"Content-Encoding": "gzip"}
        )
    )
    assert response.content == b"<rss/>"
    assert response.text == "<rss/>"


def test_body_over_the_cap_is_refused():
    with pytest.raises(http.FetchRefused, match="over the 100-byte limit"):
        get(handler=lambda request: httpx.Response(200, content=b"x" * 101))


def test_declared_length_over_the_cap_is_refused_before_reading():
    def handler(request):
        return httpx.Response(
            200, headers={"Content-Length": "1000000"}, content=b"x" * 1000000
        )

    with pytest.raises(http.FetchRefused, match="over the 100-byte limit"):
        get(handler=handler)


def test_slow_body_is_cut_off_at_the_total_deadline():
    # Each chunk arrives in time, but together they take too long.
    clock = iter(range(0, 100, 3))

    def handler(request):
        return httpx.Response(200, content=iter([b"a"] * 10))

    with (
        patch.object(http.time, "monotonic", side_effect=lambda: next(clock)),
        pytest.raises(http.FetchRefused, match="took longer than"),
    ):
        get(handler=handler, timeout=10.0)


def test_redirect_loop_is_refused():
    def handler(request):
        return httpx.Response(302, headers={"Location": "/again"})

    with pytest.raises(http.FetchRefused, match="redirects"):
        get(handler=handler)


def test_http_errors_raise():
    with pytest.raises(httpx.HTTPStatusError):
        get(handler=lambda request: httpx.Response(503))


def test_refusals_are_http_errors_for_callers():
    assert issubclass(http.FetchRefused, httpx.HTTPError)
