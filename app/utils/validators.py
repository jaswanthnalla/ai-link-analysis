"""URL validation and SSRF protection (step 2 of the processing flow).

Rules enforced here:
* only ``http`` and ``https`` URLs, no credentials in the URL, only common web ports;
* no ``localhost``-style or internal hostnames;
* IP literals and every address a hostname resolves to must be public (no loopback, private,
  link-local, cloud-metadata, multicast, reserved or otherwise non-global ranges);
* the same checks are re-applied by the fetcher on every redirect hop.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

from app.schemas import BlockedURLError, InvalidURLError, WebsiteFetchError

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})

BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "broadcasthost",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)
BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".localdomain",
    ".local",
    ".internal",
    ".intranet",
    ".private",
    ".corp",
    ".home",
    ".home.arpa",
    ".lan",
    ".in-addr.arpa",
    ".ip6.arpa",
)

# Ranges that must never be fetched, in addition to Python's own is_global / is_private checks.
BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",  # "this" network
        "10.0.0.0/8",  # private
        "100.64.0.0/10",  # carrier-grade NAT (shared address space)
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local, includes cloud metadata 169.254.169.254
        "172.16.0.0/12",  # private
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # documentation
        "192.88.99.0/24",  # 6to4 relay anycast
        "192.168.0.0/16",  # private
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # documentation
        "203.0.113.0/24",  # documentation
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved and broadcast
        "::/128",  # unspecified
        "::1/128",  # loopback
        "::ffff:0:0/96",  # IPv4-mapped (checked through the embedded IPv4 as well)
        "64:ff9b::/96",  # NAT64 well-known prefix
        "64:ff9b:1::/48",  # NAT64 local-use prefix
        "100::/64",  # discard-only
        "2001::/32",  # Teredo (embeds IPv4 addresses)
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4 (embeds IPv4 addresses)
        "fc00::/7",  # unique local (includes AWS IPv6 metadata fd00:ec2::254)
        "fe80::/10",  # link-local
        "ff00::/8",  # multicast
    )
)

_SCHEME_PREFIX_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
# A scheme without "//" (for example "javascript:alert(1)" or "mailto:x"), but not "host:port".
_BARE_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):(?!//)(?!\d)")
_HOST_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_HEX_LABEL_RE = re.compile(r"^0x[0-9a-f]+$")


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True only for globally routable unicast addresses."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is not None and not is_public_ip(embedded):
            return False
        if ip.teredo is not None:
            return False
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            # NAT64: the IPv4 address sits in the last 32 bits.
            if not is_public_ip(ipaddress.IPv4Address(ip.packed[-4:])):
                return False
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        return False
    return not any(ip in network for network in BLOCKED_NETWORKS)


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def normalize_url(raw_url: object) -> str:
    """Validate the submitted URL syntactically and return a normalized ``http(s)`` URL.

    Raises :class:`InvalidURLError` for malformed input and :class:`BlockedURLError` for URLs that
    point at private or internal destinations.
    """
    if not isinstance(raw_url, str):
        raise InvalidURLError("URL must be a string.")
    url = raw_url.strip()
    if not url:
        raise InvalidURLError("URL must not be empty.")
    if len(url) > MAX_URL_LENGTH:
        raise InvalidURLError(f"URL is too long (maximum {MAX_URL_LENGTH} characters).")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise InvalidURLError("URL must not contain whitespace or control characters.")
    if not _SCHEME_PREFIX_RE.match(url):
        bare_scheme = _BARE_SCHEME_RE.match(url)
        if bare_scheme and bare_scheme.group(1).lower() in ALLOWED_SCHEMES:
            # "https:example.com" -> "https://example.com"
            url = f"{bare_scheme.group(1).lower()}://{url[bare_scheme.end():]}"
        elif bare_scheme and "." not in bare_scheme.group(1):
            raise BlockedURLError(
                f"Only http and https URLs are allowed (got scheme '{bare_scheme.group(1)}')."
            )
        else:
            url = "https://" + url

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise InvalidURLError(f"URL could not be parsed: {exc}") from None

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedURLError(f"Only http and https URLs are allowed (got scheme '{parts.scheme}').")
    if parts.username is not None or parts.password is not None:
        raise BlockedURLError("URLs with embedded credentials are not allowed.")

    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise InvalidURLError("URL must include a hostname.")
    if "%" in host:
        raise InvalidURLError("URL hostname must not contain a zone identifier or percent-encoding.")

    try:
        port = parts.port
    except ValueError:
        raise InvalidURLError("URL port is invalid.") from None
    if port is not None and port not in ALLOWED_PORTS:
        raise BlockedURLError(f"Port {port} is not allowed; only {sorted(ALLOWED_PORTS)} are accepted.")

    ip_literal = _parse_ip_literal(host)
    if ip_literal is not None:
        if not is_public_ip(ip_literal):
            raise BlockedURLError("URL points to a private, loopback or internal IP address, which is not allowed.")
        netloc_host = f"[{host}]" if isinstance(ip_literal, ipaddress.IPv6Address) else host
    else:
        try:
            # IDNA normalization first, so fullwidth or otherwise disguised spellings of blocked
            # names are compared in their canonical ASCII form.
            ascii_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise InvalidURLError("URL hostname is not a valid domain name.") from None
        if ascii_host in BLOCKED_HOSTNAMES or ascii_host.endswith(BLOCKED_HOST_SUFFIXES):
            raise BlockedURLError("URL points to a local or internal hostname, which is not allowed.")
        labels = ascii_host.split(".")
        if len(labels) < 2 or not all(_HOST_LABEL_RE.match(label) for label in labels):
            raise InvalidURLError("URL hostname is not a valid public domain name.")
        if labels[-1].isdigit() or all(label.isdigit() or _HEX_LABEL_RE.match(label) for label in labels):
            # Top-level labels are never numeric, and a host made only of numeric or hex labels is an
            # obfuscated IP such as "127.1" or "0x7f.0x1"; both are rejected before DNS resolution.
            raise InvalidURLError("URL hostname is not a valid public domain name.")
        if len(ascii_host) > 253:
            raise InvalidURLError("URL hostname is too long.")
        netloc_host = ascii_host

    netloc = f"{netloc_host}:{port}" if port is not None else netloc_host
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


DNS_TIMEOUT_SECONDS = 10.0


async def resolve_addresses(hostname: str) -> list[str]:
    """DNS-resolve ``hostname`` (A and AAAA) and return the distinct addresses, unchecked."""
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(
        loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM), timeout=DNS_TIMEOUT_SECONDS
    )
    addresses: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in infos:
        address = str(sockaddr[0]).split("%", 1)[0]
        if address not in addresses:
            addresses.append(address)
    return addresses


async def resolve_public_ips(hostname: str) -> list[str]:
    """Resolve ``hostname`` and return its addresses, failing if any of them is not public."""
    literal = _parse_ip_literal(hostname.strip("[]"))
    if literal is not None:
        if not is_public_ip(literal):
            raise BlockedURLError("URL points to a private, loopback or internal IP address, which is not allowed.")
        return [str(literal)]

    try:
        addresses = await resolve_addresses(hostname)
    except TimeoutError:
        raise WebsiteFetchError(
            f"The hostname '{hostname}' could not be resolved within {DNS_TIMEOUT_SECONDS:g} seconds."
        ) from None
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise WebsiteFetchError(f"The hostname '{hostname}' could not be resolved ({exc}).") from None
    if not addresses:
        raise WebsiteFetchError(f"The hostname '{hostname}' did not resolve to any address.")

    for address in addresses:
        if not is_public_ip(ipaddress.ip_address(address)):
            raise BlockedURLError(
                f"The hostname '{hostname}' resolves to a private or internal address, which is not allowed."
            )
    # Prefer IPv4 for connectivity, keep the full list for transparency.
    addresses.sort(key=lambda a: ":" in a)
    return addresses


async def validate_url(raw_url: object) -> tuple[str, list[str]]:
    """Full validation: syntax + SSRF checks + DNS resolution. Returns (normalized_url, public_ips)."""
    url = normalize_url(raw_url)
    hostname = urlsplit(url).hostname or ""
    ips = await resolve_public_ips(hostname)
    return url, ips
