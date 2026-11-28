"""GET with hard limits, for anything fetched from feeds or feed-chosen URLs.

httpx's timeouts bound each network operation, not the whole response: a
server that sends a byte every few seconds, or a body of many gigabytes, can
hold an hourly run until the pod's deadline kills it, losing every source
after it. ``get`` caps both the total time and the bytes read.
"""

import time
from collections.abc import Callable

import httpx

from rss_post.logging_config import get_logger

logger = get_logger(__name__)

MAX_REDIRECTS = 5
# Longest wait for any single connect or read, within the total budget.
OPERATION_TIMEOUT = 10.0
USER_AGENT = "rss-post (Norwalk CT Bluesky bot)"


class FetchRefused(httpx.HTTPError):
    """The response was too large or too slow, or the URL isn't allowed."""


def get(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    check_url: Callable[[httpx.URL], None] | None = None,
    user_agent: str = USER_AGENT,
    client: httpx.Client | None = None,
) -> httpx.Response:
    """GET ``url``, following up to ``MAX_REDIRECTS`` redirects.

    The whole exchange, redirects included, must finish within ``timeout``
    seconds and the body must be at most ``max_bytes`` (decompressed), else
    ``FetchRefused``. ``check_url`` vets the URL and every redirect target
    before it is requested, raising ``FetchRefused`` to refuse one. HTTP
    error statuses raise ``httpx.HTTPStatusError``. The returned response's
    body is already read; its ``url`` is the final one.
    """
    deadline = time.monotonic() + timeout
    owned = client is None
    client = client or httpx.Client()
    try:
        target = httpx.URL(url)
        for _ in range(MAX_REDIRECTS + 1):
            if check_url is not None:
                check_url(target)
            remaining = _remaining(deadline, timeout, target)
            with client.stream(
                "GET",
                target,
                headers={"User-Agent": user_agent},
                timeout=min(remaining, OPERATION_TIMEOUT),
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    target = response.url.join(response.headers["Location"])
                    continue
                response.raise_for_status()
                return _read_bounded(response, max_bytes, deadline, timeout)
        raise FetchRefused(f"More than {MAX_REDIRECTS} redirects from {url}")
    finally:
        if owned:
            client.close()


def _remaining(deadline: float, timeout: float, url: httpx.URL) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FetchRefused(f"{url} took longer than {timeout}s")
    return remaining


def _read_bounded(
    response: httpx.Response, max_bytes: int, deadline: float, timeout: float
) -> httpx.Response:
    declared = response.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise FetchRefused(f"{response.url} is over the {max_bytes}-byte limit")
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) > max_bytes:
            raise FetchRefused(f"{response.url} is over the {max_bytes}-byte limit")
        _remaining(deadline, timeout, response.url)
    # The body is decoded already, so it must not be decoded again.
    headers = [
        (name, value)
        for name, value in response.headers.multi_items()
        if name.lower() not in ("content-encoding", "content-length")
    ]
    return httpx.Response(
        response.status_code,
        headers=headers,
        content=bytes(body),
        request=response.request,
    )
