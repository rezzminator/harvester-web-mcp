"""The network layer: raw/byte fetchers, curl_cffi impersonation, Jina Reader, and the
private-host guard. Every function is resilient — it never raises, returning ""/b""/None
and an error_kind on failure. httpx and curl_cffi are imported lazily.
"""

import asyncio
import ipaddress
from typing import Tuple
from urllib.parse import urlparse, urlunparse

from .log import get_logger

log = get_logger("net")

DEFAULT_USER_AGENT_AUTONOMOUS = "Mozilla/5.0 (compatible; harvester/1.0)"
DEFAULT_USER_AGENT_MANUAL = "Mozilla/5.0 (compatible; harvester/1.0)"

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024

HTTP_STATUS_MEANINGS = {
    400: "bad request", 401: "unauthorized", 403: "forbidden", 404: "page not found",
    405: "method not allowed", 408: "request timeout", 410: "gone", 429: "too many requests",
    500: "internal server error", 502: "bad gateway", 503: "service unavailable",
    504: "gateway timeout",
}
CONNECTION_ERROR_REASONS = {
    "timeout": "the server did not respond in time (connection timed out)",
    "dns": "DNS resolution failed (host not found)",
    "connect": "the connection failed (refused or host unreachable)",
}


def get_robots_txt_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))


def _ext_from_content_type(ct: str) -> str:
    return {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
        "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
        "image/tiff": ".tiff", "image/svg+xml": ".svg",
    }.get(ct.split(";")[0].strip().lower(), ".png")


async def fetch_raw(url: str, user_agent: str, proxy_url: str | None = None) -> str:
    """Fetch the raw page body. Resilient: returns "" on a connection error."""
    from httpx import AsyncClient, HTTPError
    async with AsyncClient(proxy=proxy_url) as client:
        try:
            response = await client.get(
                url, follow_redirects=True, headers={"User-Agent": user_agent}, timeout=30
            )
        except HTTPError as e:
            log.warning("fetch_raw httpx error %s: %s", url, e)
            return ""
        log.debug("fetch_raw %s -> %d (%d bytes)", url, response.status_code, len(response.text))
        return response.text


async def fetch_raw_status(
    url: str, user_agent: str, proxy_url: str | None = None
) -> Tuple[str, int | None, str | None]:
    """Fetch raw text + diagnostics. Returns (text, http_status, error_kind). Never raises."""
    from httpx import (
        AsyncClient, ConnectError, HTTPError, InvalidURL, TimeoutException, UnsupportedProtocol,
    )
    async with AsyncClient(proxy=proxy_url) as client:
        try:
            response = await client.get(
                url, follow_redirects=True, headers={"User-Agent": user_agent}, timeout=30
            )
        except (InvalidURL, UnsupportedProtocol):
            log.warning("fetch_raw_status invalid url %s", url)
            return "", None, "invalid"
        except TimeoutException:
            log.warning("fetch_raw_status timeout %s", url)
            return "", None, "timeout"
        except ConnectError as e:
            s = str(e).lower()
            dns_markers = (
                "name or service not known", "nodename nor servname", "getaddrinfo",
                "name resolution", "no address associated", "temporary failure in name resolution",
            )
            kind = "dns" if any(m in s for m in dns_markers) else "connect"
            log.warning("fetch_raw_status %s %s: %s", kind, url, e)
            return "", None, kind
        except HTTPError as e:
            log.warning("fetch_raw_status connect error %s: %s", url, e)
            return "", None, "connect"
        log.debug("fetch_raw_status %s -> %d", url, response.status_code)
        return response.text, response.status_code, None


async def _stream_capped(client, url, user_agent) -> Tuple[bytes, int | None, str | None, str]:
    """GET streaming, stopping at MAX_DOWNLOAD_BYTES so an oversized body is never fully buffered.

    Returns (data, status, error_kind, content_type). Never raises.
    """
    from httpx import (
        ConnectError, HTTPError, InvalidURL, Timeout, TimeoutException, UnsupportedProtocol,
    )
    timeout = Timeout(60.0, connect=10.0)  # short connect → closed/filtered ports fail fast
    try:
        async with client.stream(
            "GET", url, follow_redirects=True, headers={"User-Agent": user_agent}, timeout=timeout
        ) as response:
            if response.status_code >= 400:
                log.warning("stream %s -> HTTP %d", url, response.status_code)
                return b"", response.status_code, None, ""
            ct = response.headers.get("content-type", "")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_DOWNLOAD_BYTES:
                    log.info("stream %s hit %d-byte cap — truncating", url, MAX_DOWNLOAD_BYTES)
                    break
            data = b"".join(chunks)[:MAX_DOWNLOAD_BYTES]
            log.debug("stream %s -> %d (%d bytes, ct=%s)", url, response.status_code, len(data), ct)
            return data, response.status_code, None, ct
    except (InvalidURL, UnsupportedProtocol):
        log.warning("stream invalid url %s", url)
        return b"", None, "invalid", ""
    except TimeoutException:
        log.warning("stream timeout %s", url)
        return b"", None, "timeout", ""
    except ConnectError as e:
        s = str(e).lower()
        dns = any(m in s for m in ("name or service not known", "getaddrinfo", "name resolution"))
        kind = "dns" if dns else "connect"
        log.warning("stream %s %s: %s", kind, url, e)
        return b"", None, kind, ""
    except HTTPError as e:
        log.warning("stream connect error %s: %s", url, e)
        return b"", None, "connect", ""


async def download_bytes(
    url: str, user_agent: str, proxy_url: str | None = None
) -> Tuple[bytes, int | None, str | None]:
    """Download binary content (streamed, capped). Returns (data, http_status, error_kind)."""
    from httpx import AsyncClient
    async with AsyncClient(proxy=proxy_url) as client:
        data, status, error_kind, _ct = await _stream_capped(client, url, user_agent)
        return data, status, error_kind


async def fetch_bytes_with_meta(
    url: str, user_agent: str, proxy_url: str | None = None
) -> Tuple[bytes, int | None, str | None, str]:
    """Download binary content (streamed, capped). Returns (data, http_status, error_kind, content_type).

    Never raises. Returns (b"", ...) on any error. Exposes Content-Type so callers can detect
    binary documents served at extensionless URLs (e.g. arxiv /pdf/... endpoints).
    """
    from httpx import AsyncClient
    async with AsyncClient(proxy=proxy_url) as client:
        return await _stream_capped(client, url, user_agent)


def _strip_jina_envelope(text: str) -> str:
    """r.jina.ai prefixes 'Title:/URL Source:/Published Time:/Markdown Content:' scaffolding — keep
    only the body so it doesn't leak into cached documents."""
    marker = "Markdown Content:"
    idx = text.find(marker)
    if idx != -1:
        return text[idx + len(marker):].lstrip("\n")
    # No marker — strip a leading run of envelope header lines if present.
    lines = text.splitlines()
    i = 0
    while i < len(lines) and lines[i].split(":", 1)[0] in (
            "Title", "URL Source", "Published Time", "Markdown Content", "Warning"):
        i += 1
    return "\n".join(lines[i:]).lstrip("\n") if i else text


# Strong, specific bot-wall phrases — multi-word, so safe to flag at ANY body length.
_CHALLENGE_PHRASES = (
    "just a moment",
    "checking your browser",
    "checking your browser before",
    "cf-browser-verification",
    "cf-chl-",  # Cloudflare challenge token / script markers
    "are you a robot",
    "confirm you are a human",
    "enable javascript and cookies",
    "captcha challenge",
    "completing the captcha",
    "verify you are human",
    "verifying you are human",
)
# Weak cues — common enough in real prose, so only treat them as a wall when the body is SHORT
# (a genuine interstitial is tiny; a real article that merely mentions the word "captcha" once,
# or whose prose says "attention required", is thousands of chars long). "attention required" is
# the Cloudflare 1020/block-page title but also plausible English, so it lives here, length-gated.
_CHALLENGE_WEAK = ("captcha", "cloudflare", "turnstile", "attention required")
_CHALLENGE_WEAK_MAX_LEN = 4000  # a few KB


def looks_like_challenge(html: str) -> bool:
    """Heuristic: does this raw HTML look like a Cloudflare / bot-wall interstitial?

    Strong multi-word phrases (e.g. "are you a robot", "enable javascript and cookies") flag at
    any length; bare single-word cues ("captcha", "cloudflare") flag only in a SHORT body, so a
    long real article that mentions the word in passing is not misread as a wall.
    """
    low = html.lower()
    if any(p in low for p in _CHALLENGE_PHRASES):
        return True
    if len(html) <= _CHALLENGE_WEAK_MAX_LEN and any(w in low for w in _CHALLENGE_WEAK):
        return True
    return False


def is_private_host(url: str) -> bool:
    """Return True if *url* targets a loopback, RFC-1918, link-local, or internal host.

    Covers: 127.x / ::1 loopback, 10.x / 172.16–31.x / 192.168.x RFC-1918, 169.254.x link-local,
    `.ts.net` / `.local` / `.internal` internal TLDs, and any URL that contains embedded credentials.
    Private hosts must never be sent to an external proxy like Jina Reader.
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        log.warning("is_private_host could not parse %s: %s — treating as private", url, e)
        return True  # malformed — be safe

    # Credentials in the URL → don't forward to an external service
    if parsed.username or parsed.password:
        return True

    host = (parsed.hostname or "").lower().strip("[]")
    if not host:
        return True  # no resolvable host → treat as private

    # Internal-only TLD suffixes
    for suffix in (".ts.net", ".local", ".internal"):
        if host.endswith(suffix):
            return True

    # Well-known loopback hostnames
    if host in ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"):
        return True

    # IP address — including OBFUSCATED forms (integer 2852039166, hex 0x..., octal) that
    # a naive string check misses. Normalise to a real address, then test the ranges.
    addr = None
    try:
        addr = ipaddress.ip_address(host)  # dotted v4 / bracketless v6
    except ValueError:
        try:
            if host.startswith("0x"):
                addr = ipaddress.ip_address(int(host, 16))
            elif host.isdigit():
                addr = ipaddress.ip_address(int(host))
            elif "." not in host and ":" not in host and host.startswith("0") and host != "0":
                addr = ipaddress.ip_address(int(host, 8))  # octal
        except (ValueError, OverflowError):
            addr = None
    if addr is not None:
        return (addr.is_loopback or addr.is_private or addr.is_link_local
                or addr.is_reserved or addr.is_unspecified or addr.is_multicast)

    return False


async def fetch_jina(url: str, user_agent: str, proxy_url: str | None = None) -> str:
    """Fetch via Jina Reader (https://r.jina.ai/<url>).

    Skips private hosts so internal URLs are never leaked to the external service.
    Returns the markdown body, or "" on any error or 4xx response. Never raises.
    """
    if is_private_host(url):
        log.debug("jina skipped private host %s", url)
        return ""
    from httpx import AsyncClient, HTTPError
    jina_url = f"https://r.jina.ai/{url}"
    try:
        async with AsyncClient(proxy=proxy_url) as client:
            response = await client.get(
                jina_url, follow_redirects=True,
                headers={"User-Agent": user_agent}, timeout=30,
            )
        if response.status_code >= 400:
            log.warning("jina %s -> HTTP %d", url, response.status_code)
            return ""
        log.debug("jina %s -> %d (%d chars)", url, response.status_code, len(response.text))
        return _strip_jina_envelope(response.text)
    except HTTPError as e:
        log.warning("jina httpx error %s: %s", url, e)
        return ""
    except Exception as e:
        log.warning("jina unexpected error %s: %s", url, e)
        return ""


async def fetch_impersonated(url: str, proxy_url: str | None = None) -> Tuple[str, int | None]:
    """Fetch with curl_cffi Chrome TLS/JA3 fingerprint impersonation.

    Uses AsyncSession when curl_cffi is installed; falls back to sync get() in a thread.
    Never raises — returns ("", None) on any failure including missing curl_cffi.
    """
    # curl_cffi ProxySpec expects {"http": ..., "https": ...} — structurally compatible
    # but pyright cannot verify it without the stubs' TypedDict, so we ignore arg-type below.
    prx = {"https": proxy_url, "http": proxy_url} if proxy_url else None
    try:
        from curl_cffi.requests import AsyncSession  # type: ignore[import-not-found]
        async with AsyncSession() as session:  # type: ignore[attr-defined]
            r = await session.get(url, impersonate="chrome", proxies=prx, timeout=45)  # type: ignore[arg-type]
        log.debug("curl_cffi %s -> %s (%d chars)", url, r.status_code, len(r.text))
        return r.text, r.status_code
    except ImportError:
        # Sync fallback via thread
        try:
            import curl_cffi.requests as _cffi  # type: ignore[import-not-found]
            r = await asyncio.to_thread(
                lambda: _cffi.get(url, impersonate="chrome", proxies=prx, timeout=45)  # type: ignore[arg-type]
            )
            log.debug("curl_cffi(sync) %s -> %s", url, r.status_code)
            return r.text, r.status_code
        except Exception as e:
            log.warning("curl_cffi(sync) failed %s: %s", url, e)
            return "", None
    except Exception as e:
        log.warning("curl_cffi failed %s: %s", url, e)
        return "", None


async def download_impersonated(url: str, proxy_url: str | None = None) -> Tuple[bytes, int | None]:
    """Download raw bytes with curl_cffi Chrome impersonation (for walled PDFs / CDN assets).

    Never raises — returns (b"", None) on any failure including missing curl_cffi.
    """
    prx = {"https": proxy_url, "http": proxy_url} if proxy_url else None
    try:
        from curl_cffi.requests import AsyncSession  # type: ignore[import-not-found]
        async with AsyncSession() as session:  # type: ignore[attr-defined]
            r = await session.get(url, impersonate="chrome", proxies=prx, timeout=60)  # type: ignore[arg-type]
        if r.status_code >= 400:
            log.warning("curl_cffi download %s -> HTTP %s", url, r.status_code)
            return b"", r.status_code
        log.debug("curl_cffi download %s -> %s (%d bytes)", url, r.status_code, len(r.content))
        return r.content[:MAX_DOWNLOAD_BYTES], r.status_code
    except ImportError:
        try:
            import curl_cffi.requests as _cffi  # type: ignore[import-not-found]
            r = await asyncio.to_thread(
                lambda: _cffi.get(url, impersonate="chrome", proxies=prx, timeout=60)  # type: ignore[arg-type]
            )
            if r.status_code >= 400:
                log.warning("curl_cffi(sync) download %s -> HTTP %s", url, r.status_code)
                return b"", r.status_code
            return r.content[:MAX_DOWNLOAD_BYTES], r.status_code
        except Exception as e:
            log.warning("curl_cffi(sync) download failed %s: %s", url, e)
            return b"", None
    except Exception as e:
        log.warning("curl_cffi download failed %s: %s", url, e)
        return b"", None
