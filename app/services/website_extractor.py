"""Steps 3 to 5 of the processing flow: fetch the website HTML, extract the relevant content, clean it.

Fetching is SSRF-safe: every hop (the submitted URL and each redirect target) is validated and
resolved by :mod:`app.utils.validators`, and the TCP connection is pinned to the validated IP
address while the original hostname is kept for the ``Host`` header and TLS SNI, so a DNS answer
cannot change between the check and the request. The response body is decompressed by this module
with a hard size cap, and the whole fetch runs under an overall deadline.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import zlib
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from app.config import Settings, get_settings
from app.schemas import InvalidURLError, WebsiteContentError, WebsiteFetchError
from app.utils.validators import validate_url

HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
SUPPORTED_CONTENT_ENCODINGS = frozenset({"", "identity", "gzip", "x-gzip", "deflate"})
NOISE_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "object",
    "embed",
    "video",
    "audio",
    "map",
    "input",
    "select",
    "textarea",
)
LAYOUT_TAGS = ("nav", "footer", "aside")
STRUCTURED_DATA_TYPES = {
    "organization",
    "corporation",
    "localbusiness",
    "professionalservice",
    "website",
    "webpage",
    "product",
    "service",
    "softwareapplication",
    "brand",
}
STRUCTURED_DATA_FIELDS = (
    "name",
    "alternateName",
    "legalName",
    "description",
    "slogan",
    "address",
    "areaServed",
    "telephone",
    "email",
    "sameAs",
    "foundingDate",
    "numberOfEmployees",
)
SOCIAL_DOMAINS = (
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "tiktok.com",
    "pinterest.com",
)
SKIPPED_HREF_PREFIXES = ("#", "mailto:", "tel:", "javascript:", "data:")

MIN_TEXT_LENGTH = 80
MAX_HEADINGS = 60
MAX_LINKS = 80
MAX_STRUCTURED_DATA_ENTRIES = 10
MAX_STRUCTURED_DATA_BYTES = 100_000
MAX_STRUCTURED_DATA_DEPTH = 8
MAX_CONTACT_HINTS = 12
MAX_SOCIAL_LINKS = 8
OVERALL_DEADLINE_FACTOR = 3  # whole fetch (all hops) must finish within this many fetch timeouts
TRUNCATION_MARKER = "\n[content truncated]"

_WS_RE = re.compile(r"\s+")
# Bounded repetitions only: unbounded runs before the "@" backtrack quadratically on long text.
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,16}\d")
_CHARSET_RE = re.compile(r"""charset=["']?([\w-]+)""", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _safe(text: object, limit: int = 120) -> str:
    """Make server-supplied text safe to echo in an error message: no control characters, bounded length."""
    cleaned = _WS_RE.sub(" ", _CONTROL_RE.sub("", str(text or ""))).strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3].rstrip() + "..."


@dataclass
class FetchResult:
    """Raw result of fetching the website (step 3)."""

    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    body: bytes
    truncated: bool
    resolved_ips: list[str]
    redirect_chain: list[str] = field(default_factory=list)


@dataclass
class ExtractedContent:
    """Relevant, cleaned website information (steps 4 and 5)."""

    url: str
    final_url: str
    title: str = ""
    meta_description: str = ""
    meta_keywords: str = ""
    canonical_url: str = ""
    language: str = ""
    og_site_name: str = ""
    og_title: str = ""
    og_description: str = ""
    structured_data: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    navigation_links: list[str] = field(default_factory=list)
    contact_hints: list[str] = field(default_factory=list)
    social_links: list[str] = field(default_factory=list)
    body_text: str = ""
    html_truncated: bool = False

    def total_text_length(self) -> int:
        parts = [self.title, self.meta_description, self.og_description, self.body_text, *self.headings]
        return sum(len(p) for p in parts)

    def to_prompt_text(self, max_chars: int) -> str:
        """Render the extracted information as compact plain text for the LLM, never longer than ``max_chars``."""
        head: list[str] = [f"Submitted URL: {self.url}"]
        if self.final_url and self.final_url != self.url:
            head.append(f"Final URL after redirects: {self.final_url}")
        if self.language:
            head.append(f"Page language: {self.language}")
        head.append(f"Page title: {self.title or '(none)'}")
        head.append(f"Meta description: {self.meta_description or '(none)'}")
        if self.meta_keywords:
            head.append(f"Meta keywords: {self.meta_keywords}")
        if self.canonical_url:
            head.append(f"Canonical URL: {self.canonical_url}")
        if self.og_site_name or self.og_title or self.og_description:
            head.append(
                "Open Graph: "
                f"site_name={self.og_site_name or '-'}; title={self.og_title or '-'}; "
                f"description={self.og_description or '-'}"
            )

        sections: list[tuple[str, list[str]]] = [
            ("Structured data (JSON-LD):", self.structured_data),
            ("Headings (document order):", self.headings),
            ("Navigation and internal links (link text (path)):", self.navigation_links),
            ("Contact and location hints:", self.contact_hints),
            ("Social profiles:", self.social_links),
        ]
        rendered = "\n".join(head)
        for heading, items in sections:
            if items:
                rendered += f"\n\n{heading}\n" + "\n".join(f"- {item}" for item in items)

        body_header = "\n\nMain page text:\n"
        body = self.body_text
        full = rendered + body_header + body
        if len(full) <= max_chars:
            return full

        room = max_chars - len(rendered) - len(body_header) - len(TRUNCATION_MARKER)
        if room > 40:
            # Cut the body on a word boundary close to the limit.
            cut = body.rfind(" ", max(room - 200, 0), room)
            body = body[: cut if cut > 0 else room].rstrip()
            return rendered + body_header + body + TRUNCATION_MARKER
        # The metadata alone (almost) fills the budget: cut whatever is left and mark it.
        return full[: max(max_chars - len(TRUNCATION_MARKER), 0)].rstrip() + TRUNCATION_MARKER


# --------------------------------------------------------------------------- step 3: fetch


def _charset_from_content_type(content_type: str) -> str | None:
    match = _CHARSET_RE.search(content_type or "")
    return match.group(1) if match else None


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """Build the TLS context once; loading the CA bundle on every request costs about a second."""
    return httpx.create_ssl_context()


def _strip_partial_utf8(body: bytearray) -> bytearray:
    """Drop a trailing incomplete multi-byte UTF-8 sequence left behind by a byte cut."""
    for i in range(1, min(4, len(body)) + 1):
        byte = body[-i]
        if byte < 0x80:
            break
        if byte >= 0xC0:
            needed = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            if i < needed:
                del body[-i:]
            break
    return body


async def _read_body(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """Read and decompress the response body with a hard cap on both raw and decoded size."""
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding not in SUPPORTED_CONTENT_ENCODINGS:
        raise WebsiteContentError(
            f"The website used an unsupported content encoding ({_safe(encoding, 20)})."
        )
    decoder = None
    if encoding in ("gzip", "x-gzip"):
        decoder = zlib.decompressobj(zlib.MAX_WBITS | 32)  # gzip or zlib header, auto-detected
    elif encoding == "deflate":
        decoder = zlib.decompressobj()

    body = bytearray()
    raw_total = 0
    truncated = False
    if response.is_stream_consumed:
        # Already read by the transport (never the case for a live stream): the content is decoded.
        content = response.content
        return bytes(_strip_partial_utf8(bytearray(content[:max_bytes]))) if len(content) > max_bytes else content, len(content) > max_bytes
    async for raw in response.aiter_raw():
        raw_total += len(raw)
        if decoder is None:
            chunk = raw
        else:
            try:
                chunk = decoder.decompress(raw, max_bytes - len(body) + 1)
            except zlib.error:
                if encoding == "deflate" and not body and raw_total == len(raw):
                    # Some servers send raw deflate streams without the zlib wrapper.
                    decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                    try:
                        chunk = decoder.decompress(raw, max_bytes - len(body) + 1)
                    except zlib.error:
                        raise WebsiteContentError("The website sent a corrupt compressed response.") from None
                else:
                    raise WebsiteContentError("The website sent a corrupt compressed response.") from None
        body.extend(chunk)
        if len(body) > max_bytes or raw_total > max_bytes or (decoder is not None and decoder.unconsumed_tail):
            truncated = True
            break
    del body[max_bytes:]
    if truncated:
        body = _strip_partial_utf8(body)
    return bytes(body), truncated


async def fetch_html(raw_url: str, settings: Settings | None = None) -> FetchResult:
    """Fetch the HTML of ``raw_url`` with SSRF protection on every hop, size caps and an overall deadline."""
    settings = settings or get_settings()
    base_headers = {
        "User-Agent": settings.user_agent,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",  # decoded by _read_body under the size cap
    }
    redirect_chain: list[str] = []
    current = raw_url
    overall_deadline = settings.fetch_timeout_seconds * OVERALL_DEADLINE_FACTOR

    try:
        async with asyncio.timeout(overall_deadline):
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(settings.fetch_timeout_seconds),
                verify=_ssl_context(),
            ) as client:
                for hop in range(settings.fetch_max_redirects + 1):
                    # Validation + DNS resolution + private-address checks happen here for every hop.
                    try:
                        url, ips = await validate_url(current)
                    except InvalidURLError:
                        if hop == 0:
                            raise
                        raise WebsiteFetchError("The website sent an invalid redirect target.") from None
                    redirect_chain.append(url)
                    parts = urlsplit(url)
                    hostname = parts.hostname or ""
                    ip = ips[0]
                    ip_host = f"[{ip}]" if ":" in ip else ip
                    pinned_netloc = f"{ip_host}:{parts.port}" if parts.port else ip_host
                    pinned_url = urlunsplit((parts.scheme, pinned_netloc, parts.path, parts.query, ""))
                    host_value = f"[{hostname}]" if ":" in hostname else hostname
                    host_header = f"{host_value}:{parts.port}" if parts.port else host_value
                    headers = {**base_headers, "Host": host_header}

                    try:
                        async with client.stream(
                            "GET", pinned_url, headers=headers, extensions={"sni_hostname": hostname}
                        ) as response:
                            if response.status_code in REDIRECT_STATUSES:
                                location = response.headers.get("location")
                                if not location:
                                    raise WebsiteFetchError(
                                        f"The website answered HTTP {response.status_code} without a redirect target."
                                    )
                                try:
                                    current = urljoin(url, location)
                                except ValueError:
                                    raise WebsiteFetchError("The website sent an invalid redirect target.") from None
                                continue
                            if response.status_code >= 400:
                                reason = _safe(response.reason_phrase, 40)
                                raise WebsiteFetchError(
                                    f"The website responded with HTTP {response.status_code}{' ' + reason if reason else ''}."
                                )
                            content_type = response.headers.get("content-type", "")
                            if content_type and not any(t in content_type.lower() for t in HTML_CONTENT_TYPES):
                                raise WebsiteContentError(
                                    "The URL did not return an HTML page "
                                    f"(content-type: {_safe(content_type.split(';')[0], 60)})."
                                )
                            body, truncated = await _read_body(response, settings.fetch_max_bytes)
                            return FetchResult(
                                requested_url=redirect_chain[0],
                                final_url=url,
                                status_code=response.status_code,
                                content_type=content_type,
                                body=body,
                                truncated=truncated,
                                resolved_ips=ips,
                                redirect_chain=redirect_chain,
                            )
                    except httpx.TimeoutException:
                        raise WebsiteFetchError(
                            f"The website did not respond within {settings.fetch_timeout_seconds:g} seconds."
                        ) from None
                    except httpx.ConnectError as exc:
                        detail = str(exc).strip()
                        if "CERTIFICATE" in detail.upper() or "SSL" in detail.upper():
                            raise WebsiteFetchError("The website's TLS certificate could not be verified.") from None
                        raise WebsiteFetchError("Could not connect to the website.") from None
                    except (httpx.InvalidURL, UnicodeError, ValueError):
                        raise WebsiteFetchError("The website sent an invalid redirect target.") from None
                    except httpx.HTTPError as exc:
                        raise WebsiteFetchError(
                            f"The website could not be fetched ({exc.__class__.__name__})."
                        ) from None

                raise WebsiteFetchError(
                    f"The website redirected more than {settings.fetch_max_redirects} times."
                )
    except TimeoutError:
        raise WebsiteFetchError(
            f"The website did not finish responding within {overall_deadline:g} seconds."
        ) from None


# --------------------------------------------------------------------------- steps 4 and 5: extract and clean


def _clean(text: str | None) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def _meta_content(soup: BeautifulSoup, **attrs: str) -> str:
    for key, value in attrs.items():
        tag = soup.find("meta", attrs={key: re.compile(rf"^{re.escape(value)}$", re.IGNORECASE)})
        if tag and tag.get("content"):
            return _clean(str(tag.get("content")))
    return ""


def _host_key(hostname: str | None) -> str:
    host = (hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _extract_structured_data(soup: BeautifulSoup) -> list[str]:
    entries: list[str] = []

    def visit(node: object, depth: int) -> None:
        if depth > MAX_STRUCTURED_DATA_DEPTH or len(entries) >= MAX_STRUCTURED_DATA_ENTRIES:
            return
        if isinstance(node, list):
            for item in node:
                visit(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        if "@graph" in node:
            visit(node["@graph"], depth + 1)
        node_type = node.get("@type")
        if isinstance(node_type, list):
            node_type = next((t for t in node_type if isinstance(t, str)), "")
        if isinstance(node_type, str) and node_type.lower() in STRUCTURED_DATA_TYPES:
            fields = []
            for key in STRUCTURED_DATA_FIELDS:
                value = node.get(key)
                if value in (None, "", [], {}):
                    continue
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                fields.append(f"{key}={_clean(str(value))[:300]}")
            if fields:
                entries.append(f"{node_type}: " + "; ".join(fields))

    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.IGNORECASE)}):
        raw = script.string or script.get_text() or ""
        if len(raw) > MAX_STRUCTURED_DATA_BYTES:
            continue
        try:
            visit(json.loads(raw), 0)
        except (ValueError, TypeError, RecursionError):
            continue
    return entries


def _extract_links(soup: BeautifulSoup, base_url: str) -> tuple[list[str], list[str]]:
    base_host = _host_key(urlsplit(base_url).hostname)
    primary: list[tuple[str, str]] = []
    secondary: list[tuple[str, str]] = []
    social: list[str] = []
    seen_links: set[tuple[str, str]] = set()
    seen_social: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href or href.lower().startswith(SKIPPED_HREF_PREFIXES):
            continue
        try:
            absolute = urljoin(base_url, href)
            parts = urlsplit(absolute)
        except ValueError:
            continue  # unparsable href (for example a bracketed placeholder host)
        if parts.scheme not in ("http", "https"):
            continue
        host = _host_key(parts.hostname)
        if any(host == d or host.endswith("." + d) for d in SOCIAL_DOMAINS):
            key = absolute.split("?")[0].rstrip("/")
            if key not in seen_social and len(social) < MAX_SOCIAL_LINKS:
                seen_social.add(key)
                social.append(key)
            continue
        if host != base_host and not host.endswith("." + base_host):
            continue
        text = _clean(anchor.get_text()) or _clean(str(anchor.get("aria-label") or anchor.get("title") or ""))
        if not text or len(text) > 80:
            continue
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query[:60]
        key = (text.lower(), path)
        if key in seen_links:
            continue
        seen_links.add(key)
        in_nav = anchor.find_parent(["nav", "header"]) is not None
        (primary if in_nav else secondary).append((text, path))

    ordered = primary + secondary
    links = [f"{text} ({path})" for text, path in ordered[:MAX_LINKS]]
    return links, social


def _clean_block_text(text: str) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = _clean(raw_line)
        if len(line) < 3:
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return "\n".join(lines)


def _is_canonical(rel: object) -> bool:
    return isinstance(rel, str) and "canonical" in rel.lower().split()


def extract_content(fetch: FetchResult) -> ExtractedContent:
    """Parse the fetched HTML and return the cleaned, de-duplicated content relevant for analysis."""
    soup = BeautifulSoup(fetch.body, "html.parser", from_encoding=_charset_from_content_type(fetch.content_type))
    if soup.find(["html", "body", "head", "title", "p", "div", "h1", "main"]) is None:
        raise WebsiteContentError("The URL did not return a readable HTML page.")

    content = ExtractedContent(url=fetch.requested_url, final_url=fetch.final_url, html_truncated=fetch.truncated)

    html_tag = soup.find("html")
    content.language = _clean(str(html_tag.get("lang", ""))) if html_tag else ""
    content.title = _clean(soup.title.get_text()) if soup.title else ""
    content.meta_description = _meta_content(soup, name="description")
    content.meta_keywords = _meta_content(soup, name="keywords")
    content.og_site_name = _meta_content(soup, property="og:site_name")
    content.og_title = _meta_content(soup, property="og:title")
    content.og_description = _meta_content(soup, property="og:description")
    canonical = soup.find("link", rel=_is_canonical)
    content.canonical_url = _clean(str(canonical.get("href", ""))) if canonical else ""
    content.structured_data = _extract_structured_data(soup)
    content.navigation_links, content.social_links = _extract_links(soup, fetch.final_url)

    for tag in soup(NOISE_TAGS):
        tag.decompose()

    # Contact and location hints from the whole page (addresses usually sit in footers).
    hints: list[str] = []
    for address in soup.find_all("address"):
        text = _clean(address.get_text(" "))
        if text and text not in hints:
            hints.append(f"Address: {text[:200]}")
    full_text = soup.get_text(" ")
    for email in dict.fromkeys(_EMAIL_RE.findall(full_text)):
        if len(hints) < MAX_CONTACT_HINTS:
            hints.append(f"Email: {email}")
    for phone in dict.fromkeys(m.group(0).strip() for m in _PHONE_RE.finditer(full_text)):
        digits = re.sub(r"\D", "", phone)
        if 8 <= len(digits) <= 15 and len(hints) < MAX_CONTACT_HINTS:
            hints.append(f"Phone: {phone}")
    content.contact_hints = hints[:MAX_CONTACT_HINTS]

    seen_headings: set[str] = set()
    headings: list[str] = []
    for heading in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = _clean(heading.get_text(" "))
        if not text or text.lower() in seen_headings or len(text) > 200:
            continue
        seen_headings.add(text.lower())
        headings.append(f"{heading.name.upper()}: {text}")
        if len(headings) >= MAX_HEADINGS:
            break
    content.headings = headings

    main = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.find("article")
    body_text = _clean_block_text(main.get_text("\n")) if main is not None else ""
    if main is None or len(body_text) < MIN_TEXT_LENGTH:
        # No main content element, or an (almost) empty one: use the body without layout chrome.
        for tag in soup(LAYOUT_TAGS):
            tag.decompose()
        container = soup.body or soup
        body_text = _clean_block_text(container.get_text("\n"))
    content.body_text = body_text

    if content.total_text_length() < MIN_TEXT_LENGTH:
        raise WebsiteContentError(
            "The page contains too little readable text to analyze; it may require JavaScript to render "
            "or may be an empty placeholder page."
        )
    return content
