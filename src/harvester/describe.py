"""Render one get_or_fetch result as MCP TextContent: success → header + full body;
failure → one clear, descriptive error line.
"""

import os

from mcp.types import TextContent

from .cache import THIN_MIN_CHARS
from .log import get_logger
from .net import CONNECTION_ERROR_REASONS, HTTP_STATUS_MEANINGS

log = get_logger("describe")

DEFAULT_MAX_INLINE_CHARS = 50000


def _max_inline_chars() -> int:
    """Inline-body cap in chars (env HARVESTER_MAX_INLINE_CHARS); the cache file keeps the full text."""
    raw = os.environ.get("HARVESTER_MAX_INLINE_CHARS")
    if raw is None:
        return DEFAULT_MAX_INLINE_CHARS
    try:
        return int(raw)
    except ValueError:
        log.warning("invalid HARVESTER_MAX_INLINE_CHARS=%r; using default %d", raw, DEFAULT_MAX_INLINE_CHARS)
        return DEFAULT_MAX_INLINE_CHARS


def describe_fetch_result(item: str, result: "dict | BaseException") -> TextContent:
    """Render one result as TextContent: success → header + full body; failure → one clear line."""
    if isinstance(result, BaseException):
        detail = str(result).strip() or "unknown error"
        log.warning("describe %s exception: %s: %s", item, type(result).__name__, detail)
        return TextContent(type="text", text=f"# {item}\nERROR: could not fetch — {type(result).__name__}: {detail}")

    if result.get("error"):
        log.info("describe %s error: %s", item, result["error"])
        return TextContent(type="text", text=f"# {item}\nERROR: {result['error']}")

    body = result.get("body") or ""
    stripped = body.strip()
    status = result.get("http_status")
    error_kind = result.get("error_kind")
    challenge = bool(result.get("challenge"))
    core_chars = result.get("content_chars")
    thin = (core_chars if core_chars is not None else len(stripped)) < THIN_MIN_CHARS
    negative = challenge or error_kind is not None or (status is not None and status >= 400)

    if stripped and not (thin and negative):
        header = (
            f"# {item}\n"
            f"cache_status: {result['cache_status']} / method: {result['method']} / "
            f"bytes: {result['bytes']} / path: {result['md_path']}"
        )
        cap = _max_inline_chars()
        if cap > 0 and len(body) > cap:
            note = (
                f"\n\n— [truncated: first {cap} of {len(body)} chars. "
                f'Full text cached at {result["md_path"]}; use grep_cache("<term>") to search it, '
                f"or re-fetch a narrower target.]"
            )
            body = body[:cap] + note
        return TextContent(type="text", text=f"{header}\n\n{body}")

    if error_kind == "invalid":
        msg = f"Invalid URL: {item}"
    elif error_kind in CONNECTION_ERROR_REASONS:
        msg = f"Could not reach {item}: {CONNECTION_ERROR_REASONS[error_kind]}."
    elif challenge:
        msg = f"Blocked by a Cloudflare/bot challenge at {item} — content not retrievable from this datacenter server."
    elif status is not None and status >= 400:
        meaning = HTTP_STATUS_MEANINGS.get(status, "request failed")
        note = " — likely a bot-block or rate limit" if status in (403, 429, 503) else ""
        msg = f"{item} returned HTTP {status} ({meaning}){note}."
    else:
        msg = f"Fetched {item} but no readable content could be extracted (JS-rendered or bot-blocked — not retrievable from this datacenter IP)."
    log.info("describe %s -> failure: %s", item, msg)
    return TextContent(type="text", text=f"# {item}\nERROR: {msg}")
