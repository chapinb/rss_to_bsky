"""GET with hard limits, for anything fetched from feeds or feed-chosen URLs.

httpx's timeouts bound each network operation, not the whole response: a
server that sends a byte every few seconds, or a body of many gigabytes, can
hold an hourly run until the pod's deadline kills it, losing every source
after it. ``get`` caps both the total time and the bytes read.
"""

import ipaddress
import os
import re
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from rss_post.logging_config import get_logger

logger = get_logger(__name__)

MAX_REDIRECTS = 5
# Longest wait for any single connect or read, within the total budget.
OPERATION_TIMEOUT = 10.0
USER_AGENT = "rss-post (Norwalk CT Bluesky bot)"


# The encodings ``get`` can inflate within its byte cap (zlib's ``max_length``);
# brotli and zstd aren't asked for, and are refused if sent anyway.
ACCEPT_ENCODING = "gzip, deflate"
# The well-known NAT64 prefix (RFC 6052), which Python counts as global.
NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _deflate(first: bytes):
    """ "deflate" as sent: zlib-wrapped as the RFC says, or (commonly) raw."""
    zlib_header = (
        len(first) >= 2
        and first[0] & 0x0F == 8
        and ((first[0] << 8) | first[1]) % 31 == 0
    )
    return zlib.decompressobj(zlib.MAX_WBITS if zlib_header else -zlib.MAX_WBITS)


# Each encoding's inflater, made from the body's first bytes (None: as is).
_DECOMPRESSORS = {
    "identity": lambda first: None,
    "gzip": lambda first: zlib.decompressobj(16 + zlib.MAX_WBITS),
    "x-gzip": lambda first: zlib.decompressobj(16 + zlib.MAX_WBITS),
    "deflate": _deflate,
}


class FetchRefused(httpx.HTTPError):
    """The response was too large or too slow, or the URL isn't allowed."""


# A top-level domain: letters (or an IDNA ``xn--`` label), never all digits,
# so a host like ``2130706433`` or ``0x7f000001`` (127.0.0.1) isn't a name.
TLD = re.compile(r"(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})")
# Suffixes used for private names (Kubernetes services, mDNS, home and
# corporate networks, RFC 6761/8375 special-use names): never public.
PRIVATE_SUFFIXES = (
    "localhost",
    "local",
    "svc",
    "cluster.local",
    "internal",
    "intranet",
    "lan",
    "home",
    "corp",
    "private",
    "arpa",
    "test",
    "invalid",
    "onion",
)


def public_https(url: httpx.URL) -> None:
    """Refuse (``FetchRefused``) anything but ``https`` on port 443 to a public host.

    For URLs taken from feeds or the pages they link (thumbnails), so a feed
    can't point the bot at the cluster or the LAN. Refused: IP literals that
    aren't global, names without a public-looking TLD, ``PRIVATE_SUFFIXES``,
    and names the environment sends around the egress proxy (``NO_PROXY``),
    which are internal by the operator's own definition. This judges the URL
    only: a public name that resolves to a private address has to be stopped
    where names are resolved (the egress proxy).
    """
    if url.scheme != "https":
        raise FetchRefused(f"Not an https URL: {url}")
    if url.port not in (None, 443):
        raise FetchRefused(f"Not the https port: {url}")
    # The ASCII (punycode) form, so internationalized TLDs match ``TLD``.
    host = url.raw_host.decode("ascii").lower().rstrip(".")
    bypass = ProxyBypass.from_environment()
    if bypass.everything:
        raise FetchRefused(f"NO_PROXY=* sends {url} around the egress proxy")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (
            "." not in host
            or not TLD.fullmatch(host.rsplit(".", 1)[-1])
            or _under(host, PRIVATE_SUFFIXES)
            or _under(host, bypass.names)
        ):
            raise FetchRefused(f"Not a public host: {url}") from None
        return
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            address = address.ipv4_mapped
        elif address in NAT64:
            # Reaches the IPv4 address in its last 32 bits.
            address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if not address.is_global or any(address in n for n in bypass.networks):
        raise FetchRefused(f"Not a public host: {url}")


def _under(host: str, suffixes) -> bool:
    """Whether ``host`` is one of ``suffixes`` or a name under one."""
    return any(host == s or host.endswith(f".{s}") for s in suffixes)


@dataclass(frozen=True)
class ProxyBypass:
    """What ``NO_PROXY``/``no_proxy`` sends around the egress proxy, read the
    way httpx reads it: ``*`` for everything, IPs and CIDR networks, and
    names (optionally ``*.``/``.``-prefixed, as URLs, or with a port), each
    covering the names under it."""

    everything: bool = False
    names: tuple[str, ...] = ()
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    @classmethod
    def from_environment(cls) -> "ProxyBypass":
        raw = os.environ.get("NO_PROXY", "") + "," + os.environ.get("no_proxy", "")
        everything, names, networks = False, [], []
        for entry in (e.strip().lower() for e in raw.split(",")):
            if not entry:
                continue
            if entry == "*":
                everything = True
                continue
            if "://" in entry:
                # The host alone: no userinfo, port or IPv6 brackets.
                host = urlsplit(entry).hostname or ""
            else:
                host = _without_port(entry)
            if not host:
                continue
            try:
                networks.append(ipaddress.ip_network(host, strict=False))
            except ValueError:
                names.append(host.removeprefix("*").strip("."))
        return cls(everything, tuple(n for n in names if n), tuple(networks))


def _without_port(entry: str) -> str:
    """``entry`` without a ``:port``, and without the brackets of an IPv6 host."""
    if entry.startswith("["):
        return entry[1 : entry.find("]")] if "]" in entry else entry[1:]
    if entry.count(":") == 1:
        return entry.split(":")[0]
    return entry  # A bare IPv6 address or network, or no port.


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
                headers={"User-Agent": user_agent, "Accept-Encoding": ACCEPT_ENCODING},
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
    """Read the body, inflating it ourselves so the cap holds for what is
    decompressed: httpx's own decoding inflates each received chunk whole,
    and a few kilobytes of gzip can inflate to gigabytes."""
    too_large = FetchRefused(f"{response.url} is over the {max_bytes}-byte limit")
    declared = response.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise too_large
    encoding = response.headers.get("Content-Encoding", "identity").lower().strip()
    if encoding not in _DECOMPRESSORS:
        raise FetchRefused(f"{response.url} has an unsupported encoding {encoding!r}")
    make_inflater = _DECOMPRESSORS[encoding]
    inflater = None
    body = bytearray()
    received = 0
    for raw in response.iter_raw():
        if not raw:
            continue
        if received == 0:
            inflater = make_inflater(raw)
        # Compressed or not, what arrives is capped too.
        received += len(raw)
        if received > max_bytes:
            raise too_large
        data = raw
        while data:
            if inflater is None:
                piece, data = data, b""
            else:
                if inflater.eof:
                    # zlib would keep anything after the end, uncapped.
                    raise FetchRefused(f"{response.url} has data after its body")
                # At most one byte past the cap, so going over is detected
                # without inflating any further.
                try:
                    piece = inflater.decompress(data, max_bytes - len(body) + 1)
                except zlib.error as e:
                    raise FetchRefused(
                        f"{response.url} has a corrupt compressed body ({e})"
                    ) from e
                data = inflater.unconsumed_tail
                if inflater.unused_data:
                    raise FetchRefused(f"{response.url} has data after its body")
            body += piece
            if len(body) > max_bytes:
                raise too_large
        _remaining(deadline, timeout, response.url)
    if inflater is not None and not inflater.eof:
        raise FetchRefused(f"{response.url} was cut off mid-body")
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
