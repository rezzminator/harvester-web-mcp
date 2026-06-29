"""The dispatcher: route ONE input (URL / local path / `archive::member`) to its handler,
walk the wall-bypass ladder for HTML, and build the per-item result dict. Never raises —
failures come back as {"error": ...}.
"""

import asyncio
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import cache, convert, detect, html, mirror, net, oa, safe_archive, search
from .cache import CACHE_MIN_BODY, THIN_MIN_CHARS
from .log import get_logger

log = get_logger("dispatch")

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
# Bare scholarly identifiers handled by get_or_fetch's no-URL/no-DOI branch.
_PMCID_RE = re.compile(r"^PMC\d+$", re.IGNORECASE)
_PMID_RE = re.compile(r"^\d{7,9}$")

# ── negative-result cache + in-flight dedup (see get_or_fetch) ──────────────────
# A failing source must not be re-hammered: one DOI was fetched 18× in a real run.
DEFAULT_NEG_TTL = 120.0
_NEG_CACHE: dict[str, tuple[float, dict]] = {}
_INFLIGHT: dict[str, "asyncio.Future[dict]"] = {}


def _neg_ttl() -> float:
    """Negative-cache TTL in seconds (env HARVESTER_NEG_TTL); invalid → default."""
    raw = os.environ.get("HARVESTER_NEG_TTL")
    if raw is None:
        return DEFAULT_NEG_TTL
    try:
        return float(raw)
    except ValueError:
        log.warning("invalid HARVESTER_NEG_TTL=%r; using default %s", raw, DEFAULT_NEG_TTL)
        return DEFAULT_NEG_TTL


def _neg_cache_get(key: str) -> dict | None:
    """Return a COPY of a fresh cached error dict for *key* (annotated), evicting it if stale."""
    entry = _NEG_CACHE.get(key)
    if entry is None:
        return None
    ts, result = entry
    if time.monotonic() - ts >= _neg_ttl():
        _NEG_CACHE.pop(key, None)
        return None
    cached = dict(result)
    if cached.get("error"):
        cached["error"] = f"{cached['error']} (recently failed; cached)"
    return cached


def _neg_cache_evict_stale() -> None:
    """Drop expired negative-cache entries so the dict can't grow without bound."""
    ttl = _neg_ttl()
    now = time.monotonic()
    for k in [k for k, (ts, _r) in _NEG_CACHE.items() if now - ts >= ttl]:
        _NEG_CACHE.pop(k, None)


# ── result builders ───────────────────────────────────────────────────────────
def _hit(md_path: Path, method: str, body: str) -> dict:
    return {
        "cache_status": "hit", "method": method, "md_path": md_path, "body": body,
        "bytes": md_path.stat().st_size, "content_chars": len(body.strip()),
        "http_status": None, "error_kind": None, "challenge": False,
    }


def _ok(md_path, method: str, body: str, content_chars: int | None = None,
        http_status=None, error_kind=None, challenge=False, cache_status="miss") -> dict:
    try:
        size = os.path.getsize(md_path) if not isinstance(md_path, Path) else md_path.stat().st_size
    except OSError:
        size = len(body)
    return {
        "cache_status": cache_status, "method": method, "md_path": md_path, "body": body,
        "bytes": size, "content_chars": content_chars if content_chars is not None else len(body.strip()),
        "http_status": http_status, "error_kind": error_kind, "challenge": challenge,
    }


def _net_error(key: str, status: int | None, error_kind: str | None) -> dict:
    if error_kind == "invalid":
        msg = f"Invalid URL: {key}"
    elif error_kind in net.CONNECTION_ERROR_REASONS:
        msg = f"Could not reach {key}: {net.CONNECTION_ERROR_REASONS[error_kind]}."
    elif status is not None and status >= 400:
        meaning = net.HTTP_STATUS_MEANINGS.get(status, "request failed")
        note = " — likely a bot-block or rate limit" if status in (403, 429, 503) else ""
        msg = f"{key} returned HTTP {status} ({meaning}){note}."
    else:
        msg = f"Could not download {key}."
    log.warning("net error %s status=%s kind=%s", key, status, error_kind)
    return {"error": msg, "body": ""}


def _format_listing(members, key: str) -> str:
    lines = [
        f"# Archive: {key}", "",
        f"{len(members)} member(s). Fetch one with `{key}::<name>`.", "",
        "| name | size (bytes) | type |", "| --- | --- | --- |",
    ]
    for m in members:
        typ = "dir" if m.is_dir else ("symlink" if m.is_symlink else "file")
        safe_name = m.name.replace("|", "\\|")  # don't let a '|' in a name break the table
        lines.append(f"| {safe_name} | {m.uncompressed_size} | {typ} |")
    return "\n".join(lines) + "\n"


# ── per-kind handlers ─────────────────────────────────────────────────────────
async def _handle_binary_doc(
    data: bytes, src: str, key: str, kind: str, user_agent: str, proxy_url: str | None
) -> dict:
    """Save already-downloaded binary content and route to the correct local handler.

    Avoids a second HTTP download by writing the bytes to the cache path first, then
    calling the appropriate local converter. Used when `_html_result` discovers that an
    extensionless URL (e.g. https://arxiv.org/pdf/1706.03762) is actually a PDF/etc.
    """
    log.info("binary-doc sniff %s kind=%s (%d bytes)", key, kind, len(data))
    if kind in detect.ARCHIVE_KINDS:
        ext = ".tar" if kind == "tar" else f".{kind}"
        bin_path = cache.cache_file(key, kind, ext)
        bin_path.write_bytes(data)
        return await _archive_result(str(bin_path), key, kind, "", True, user_agent, proxy_url)

    if kind == "image":
        head = data[:16]
        if head.startswith(b"\xff\xd8\xff"):
            ext = ".jpg"
        elif head.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = ".png"
        elif head.startswith((b"GIF87a", b"GIF89a")):
            ext = ".gif"
        elif len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            ext = ".webp"
        else:
            ext = ".png"
        img_path = cache.cache_file(key, ext.lstrip("."), ext)
        img_path.write_bytes(data)
        local_path = str(img_path)
        body = (
            f"![{Path(key).name}]({local_path})\n\n"
            f"*Image saved locally at `{local_path}` — read it directly for the visual content "
            f"(OCR/markdown cannot convey a figure).*\n"
        )
        return _ok(local_path, "image", body, content_chars=THIN_MIN_CHARS + 1)

    # DOC kinds: pdf, docx, xlsx, pptx, csv, json
    _doc_ext = {"pdf": ".pdf", "docx": ".docx", "xlsx": ".xlsx", "pptx": ".pptx", "csv": ".csv", "json": ".json"}
    ext = _doc_ext.get(kind, f".{kind}")
    bin_path = cache.cache_file(key, kind, ext)
    bin_path.write_bytes(data)
    return await _doc_result(str(bin_path), key, kind, True, user_agent, proxy_url)


async def _html_result(src, key, local, user_agent, proxy_url) -> dict:
    md_path = cache.cache_file(key, "html", ".md")
    if md_path.exists():
        meta, body = cache.split_frontmatter(md_path.read_text(encoding="utf-8", errors="ignore"))
        if len(body.strip()) > CACHE_MIN_BODY:
            log.info("cache hit html %s", key)
            return _hit(md_path, meta.get("method", "cached"), body)

    if local:
        raw = Path(src).read_text(encoding="utf-8", errors="ignore")
        http_status, error_kind, challenge = None, None, False
    else:
        data, http_status, error_kind, ct = await net.fetch_bytes_with_meta(src, user_agent, proxy_url)
        # Extensionless URL sniff: if the server returns a binary document (e.g. arxiv PDF),
        # route to the correct LOCAL converter instead of trafilatura + Jina.
        if data:
            true_kind = detect._sniff_kind(ct, data[:16])
            if true_kind:
                return await _handle_binary_doc(data, src, key, true_kind, user_agent, proxy_url)
        raw = data.decode("utf-8", errors="replace") if data else ""
        challenge = net.looks_like_challenge(raw)

    # (b) trafilatura extract from the initial response
    body = html.extract_content_from_html(raw)
    method = "local-trafilatura"
    content_chars = len(body.strip())
    log.info("html %s httpx -> %s chars=%d challenge=%s", key, method, content_chars, challenge)

    # (c) thin or Cloudflare challenge → retry with curl_cffi Chrome impersonation
    if not local and (content_chars < THIN_MIN_CHARS or challenge):
        log.info("html %s thin/challenge -> curl_cffi", key)
        cffi_text, _cffi_status = await net.fetch_impersonated(src, proxy_url)
        if cffi_text:
            cffi_body = html.extract_content_from_html(cffi_text)
            cffi_chars = len(cffi_body.strip())
            if cffi_chars > content_chars:
                raw = cffi_text
                body = cffi_body
                content_chars = cffi_chars
                challenge = net.looks_like_challenge(cffi_text)
                method = "curl_cffi-trafilatura"
                log.info("html %s curl_cffi won chars=%d", key, content_chars)

    # (d) still thin → Jina Reader as final fallback (skips private hosts)
    if not local and content_chars < THIN_MIN_CHARS and not net.is_private_host(src):
        log.info("html %s still thin -> jina", key)
        jina_body = await net.fetch_jina(src, user_agent, proxy_url)
        jina_chars = len(jina_body.strip())
        if jina_chars > content_chars:
            body = jina_body
            content_chars = jina_chars
            method = "jina-reader"
            challenge = False  # Jina bypassed the wall
            log.info("html %s jina won chars=%d", key, content_chars)

    # (e) mirror fallback: scholarly DOI embedded in URL, or Wayback snapshot
    # Only when the full ladder still yields thin content or a bot challenge.
    if not local and (content_chars < THIN_MIN_CHARS or challenge) and not net.is_private_host(src):
        log.info("html %s ladder exhausted -> mirror fallback", key)
        _mirror_res = await _try_mirror_for_url(src, key, user_agent, proxy_url)
        if _mirror_res:
            return _mirror_res

    # A bot/Cloudflare challenge that survived the WHOLE ladder and the OA mirror fallback is
    # poison: it would be cached + returned as a "success" (the live S0306987718301051 captcha
    # bug). Never cache it; return a clean error (which P1 then negative-caches).
    if not local and challenge:
        log.info("html %s -> unresolved bot/Cloudflare challenge", key)
        return {"error": (
            f"{key} is behind a bot/Cloudflare challenge (e.g. 'Are you a robot?') and no "
            "open-access copy was found — content not retrievable."), "body": ""}

    # arXiv's "no article / invalid identifier" page extracts as a short success body — reject it
    # so a nonexistent id isn't returned (and cached) as a silent wrong document.
    if not local and _is_not_found_page(src, body):
        log.info("html %s -> not-found/error page", key)
        return {"error": (f"{key} returned a 'not found / invalid identifier' page — the "
                          "resource does not exist."), "body": ""}

    # (f) metadata prefix, tidy, cache write. Image refs are LEFT as URLs — `fetch` never
    # downloads image binaries and never OCRs; the model views one on demand via `fetchImage`.
    meta_block = html.extract_metadata_block(raw)
    if meta_block:
        body = meta_block + body
    body = html.tidy_markdown(body)

    cache._write_md(md_path, key, method, body)
    log.info("html %s done method=%s chars=%d", key, method, content_chars)
    return _ok(md_path, method, body, content_chars=content_chars,
               http_status=http_status, error_kind=error_kind, challenge=challenge)


async def _doc_result(src, key, kind, local, user_agent, proxy_url) -> dict:
    md_path = cache.cache_file(key, kind, ".md")
    if md_path.exists():
        meta, body = cache.split_frontmatter(md_path.read_text(encoding="utf-8", errors="ignore"))
        if body.strip():
            log.info("cache hit %s %s", kind, key)
            return _hit(md_path, meta.get("method", "cached"), body)

    if local:
        path = src
    else:
        ext = os.path.splitext(key.split("?", 1)[0])[1] or "." + kind
        bin_path = cache.cache_file(key, kind, ext)
        if not bin_path.exists():
            data, status, error_kind = await net.download_bytes(src, user_agent, proxy_url)
            if not data:
                # Retry with curl_cffi Chrome impersonation (handles walled CDNs / 403s)
                log.info("doc %s download empty -> curl_cffi", key)
                imp_data, imp_status = await net.download_impersonated(src, proxy_url)
                if imp_data:
                    data, status, error_kind = imp_data, imp_status, None
            if not data:
                # A walled publisher PDF URL (e.g. tandfonline/sciencedirect/cell) usually carries
                # an extractable DOI — pivot to the open-access mirror before giving up.
                if not local:
                    mirror_res = await _try_mirror_for_url(src, key, user_agent, proxy_url)
                    if mirror_res:
                        return mirror_res
                return _net_error(key, status, error_kind)
            # Verify the bytes match the declared kind — a .pdf URL that serves an HTML wall must
            # NOT be fed to pymupdf and returned as a "successful" PDF (silent wrong document).
            if kind == "pdf" and not data.startswith(b"%PDF"):
                sniffed = detect.sniff_magic(data[:16])
                if sniffed:  # genuinely a different binary type → convert it correctly
                    return await _handle_binary_doc(data, src, key, sniffed, user_agent, proxy_url)
                return {"error": (
                    f"{key} has a .pdf address but did not return a PDF ({len(data)} bytes of non-PDF "
                    "content — likely an HTML paywall/login wall or a bot-block)."), "body": ""}
            bin_path.write_bytes(data)
        path = str(bin_path)

    if local and kind == "pdf":  # a local .pdf must actually be a PDF, not HTML/text renamed
        try:
            with open(path, "rb") as fh:
                if not fh.read(5).startswith(b"%PDF"):
                    return {"error": f"{key} has a .pdf extension but is not a PDF file.", "body": ""}
        except OSError as e:
            return {"error": f"cannot read {key}: {e}", "body": ""}

    method = {
        "pdf": "pdf:pymupdf4llm", "docx": "office:docling", "xlsx": "office:docling",
        "pptx": "office:docling", "csv": "csv:markitdown", "json": "json",
    }[kind]
    body = await asyncio.to_thread(convert.convert_local_file, path, kind)
    body = html.tidy_markdown(body)
    if not body.strip():
        log.warning("doc %s (%s) converted to empty markdown", key, kind)
        hint = " — if it's a scanned/image-only PDF, set HARVESTER_PDF_OCR=1 to OCR it" if kind == "pdf" else ""
        return {"error": (
            f"Downloaded the {kind.upper()} from {key} but it converted to EMPTY text. It is "
            f"likely scanned/image-only, corrupt, or password-protected{hint}.")[:600], "body": ""}
    cache._write_md(md_path, key, method, body)
    log.info("doc %s done method=%s", key, method)
    return _ok(md_path, method, body)


async def _image_result(src, key, local, user_agent, proxy_url) -> dict:
    ext = detect.image_ext(key)
    if local:
        local_path = str(Path(src).resolve())
        cache_status = "local"
    else:
        img_path = cache.cache_file(key, ext.lstrip("."), ext)
        if not img_path.exists():
            data, status, error_kind = await net.download_bytes(src, user_agent, proxy_url)
            if not data:
                # Retry with curl_cffi Chrome impersonation (handles walled image CDNs)
                imp_data, imp_status = await net.download_impersonated(src, proxy_url)
                if imp_data:
                    data, status, error_kind = imp_data, imp_status, None
            if not data:
                return _net_error(key, status, error_kind)
            img_path.write_bytes(data)
        local_path = str(img_path)
        cache_status = "miss"
    body = (
        f"![{Path(key).name}]({local_path})\n\n"
        f"*Image saved locally at `{local_path}` — read it directly for the visual content "
        f"(OCR/markdown cannot convey a figure).*\n"
    )
    log.info("image %s -> %s (%s)", key, local_path, cache_status)
    # high content_chars so the short body is never misread as a "thin" failure
    return _ok(local_path, "image", body, content_chars=THIN_MIN_CHARS + 1, cache_status=cache_status)


async def _member_to_result(data: bytes, member: str, key: str) -> dict:
    label = f"{key}::{member}"
    mkind = detect.detect_kind(member)
    if mkind == "html":
        sniffed = detect.sniff_magic(data[:16])
        if sniffed:
            mkind = sniffed

    if mkind == "image":
        ext = detect.image_ext(member)
        img_path = cache.cache_file(label, ext.lstrip("."), ext)
        img_path.write_bytes(data)
        body = (
            f"![{member}]({img_path})\n\n"
            f"*Image extracted from the archive at `{img_path}` — read it directly.*\n"
        )
        return _ok(str(img_path), "archive:member:image", body, content_chars=THIN_MIN_CHARS + 1)

    if mkind in detect.DOC_KINDS:
        suffix = os.path.splitext(member)[1] or "." + mkind
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
            tf.write(data)
            tmp = tf.name
        try:
            body = await asyncio.to_thread(convert.convert_local_file, tmp, mkind)
        finally:
            os.unlink(tmp)
        body = html.tidy_markdown(body)
    else:
        text = data.decode("utf-8", errors="replace")
        body = html.tidy_markdown(html.extract_content_from_html(text) or text) if mkind == "html" else text

    md_path = cache.cache_file(label, "archive_member", ".md")
    cache._write_md(md_path, label, f"archive:member:{mkind}", body)
    log.info("archive member %s kind=%s", label, mkind)
    return _ok(md_path, f"archive:member:{mkind}", body)


async def _archive_result(src, key, kind, member, local, user_agent, proxy_url) -> dict:
    if local:
        arc_path = src
        cache_status = "local"
    else:
        ext = os.path.splitext(key.split("?", 1)[0])[1] or ("." + ("tar" if kind == "tar" else kind))
        arc_cache = cache.cache_file(key, kind, ext)
        if not arc_cache.exists():
            data, status, error_kind = await net.download_bytes(src, user_agent, proxy_url)
            if not data:
                # Retry with curl_cffi Chrome impersonation (handles walled archive downloads)
                imp_data, imp_status = await net.download_impersonated(src, proxy_url)
                if imp_data:
                    data, status, error_kind = imp_data, imp_status, None
            if not data:
                return _net_error(key, status, error_kind)
            arc_cache.write_bytes(data)
        arc_path = str(arc_cache)
        cache_status = "miss"

    if member:
        try:
            data = await asyncio.to_thread(safe_archive.read_archive_member, arc_path, member)
        except safe_archive.ArchiveError as e:
            return {"error": f"{e} — call `archive` on this source with no `member` to list what's inside.",
                    "body": ""}
        return await _member_to_result(data, member, key)

    try:
        members = await asyncio.to_thread(safe_archive.list_archive, arc_path)
    except safe_archive.ArchiveError as e:
        return {"error": f"{e} — refusing to list this archive for safety.", "body": ""}
    log.info("archive %s -> %d member(s)", key, len(members))
    body = _format_listing(members, key)
    return _ok(arc_path, "archive:listing", body, content_chars=THIN_MIN_CHARS + 1,
               cache_status=cache_status)


def _file_url_to_path(u: str) -> str:
    return unquote(urlparse(u).path)


# ── mirror / DOI helpers ───────────────────────────────────────────────────────
def _extract_doi_from_input(item: str) -> str | None:
    """Return the DOI string if *item* is a DOI-type input, else None.

    Recognised forms: bare DOI (10.xxxx/...), ``doi:`` prefix,
    ``https://doi.org/<doi>`` and ``https://www.doi.org/<doi>``.
    """
    stripped = item.strip()
    if stripped.lower().startswith("doi:"):
        return mirror.extract_doi(stripped[4:])
    parsed = urlparse(stripped)
    if parsed.netloc.lower() in ("doi.org", "www.doi.org"):
        return mirror.extract_doi(parsed.path.lstrip("/"))
    # bare DOI: no URL scheme, starts with "10."
    if not _SCHEME_RE.match(stripped) and stripped.startswith("10."):
        return mirror.extract_doi(stripped)
    return None


async def _mirror_pdf_result(
    pdf_bytes: bytes, key: str, pmcid: str, user_agent: str, proxy_url: str | None
) -> dict | None:
    """Save *pdf_bytes* to the cache, convert to markdown, append figures note.

    Returns a result dict (method ``mirror:europepmc-pdf``) or None on failure.
    """
    bin_path = cache.cache_file(key, "pdf", ".pdf")
    try:
        bin_path.write_bytes(pdf_bytes)
    except OSError as e:
        log.warning("mirror pdf cache write failed %s: %s", bin_path, e)
        return None
    body = await asyncio.to_thread(convert.pdf_to_md, str(bin_path))
    body = html.tidy_markdown(body)
    if not body.strip():
        log.warning("mirror pdf %s converted to empty markdown", key)
        return None
    figs_url = mirror.europepmc_figures_zip_url(pmcid)
    body += (
        f"\n\n---\n\n*Figures available as a ZIP: {figs_url}"
        " (fetch it to list+extract individual figures).*\n"
    )
    md_path = cache.cache_file(key, "pdf", ".md")
    cache._write_md(md_path, key, "mirror:europepmc-pdf", body)
    log.info("mirror %s -> europepmc-pdf (%s)", key, pmcid)
    return _ok(md_path, "mirror:europepmc-pdf", body)


async def _pmcid_to_result(
    pmcid: str, key: str, user_agent: str, proxy_url: str | None
) -> dict | None:
    """PMCID → open-access content: Europe PMC full-text PDF, else the PMC article HTML.

    Returns a result dict, or None when neither yields usable content. The artifact is cached
    under *key*. Shared by the DOI mirror and the bare-PMID/PMCID routing.
    """
    async with net._client(proxy_url) as client:
        pdf_bytes = await mirror.europepmc_pdf(pmcid, client)
    if pdf_bytes:
        result = await _mirror_pdf_result(pdf_bytes, key, pmcid, user_agent, proxy_url)
        if result:
            return result
    # PDF unavailable — try the PMC article HTML
    pmc_url = mirror.pmc_article_url(pmcid)
    pmc_raw = await net.fetch_raw(pmc_url, user_agent, proxy_url)
    if pmc_raw:
        pmc_body = html.extract_content_from_html(pmc_raw)
        if len(pmc_body.strip()) > THIN_MIN_CHARS:
            pmc_body = html.tidy_markdown(pmc_body)
            md_path = cache.cache_file(key, "html", ".md")
            cache._write_md(md_path, key, "mirror:pmc-html", pmc_body)
            log.info("pmcid %s -> pmc-html (%s)", pmcid, key)
            return _ok(md_path, "mirror:pmc-html", pmc_body)
    return None


# ── open-access resolver: candidate-URL → content ───────────────────────────────
async def _candidate_to_result(
    cand: "oa.Candidate", key: str, user_agent: str, proxy_url: str | None
) -> dict | None:
    """Fetch ONE oa.Candidate URL (httpx → curl_cffi), verify it's real content, convert it.

    Returns a result dict, or None when the candidate yields nothing usable (so the caller tries
    the next one). The artifact is cached under *key* (the identifier), not the candidate URL.
    """
    # SSRF/scheme chokepoint for EVERY candidate (OA resolvers + the scraped citation_pdf_url):
    # candidate URLs come from third-party JSON / attacker-influenced page <meta> tags and bypass
    # the dispatch-time guard. Cheap lexical first filter here; the net layer resolves DNS + every
    # redirect hop. Refusing here also stops a file:// candidate from reaching the curl_cffi retry.
    _scheme = urlparse(cand.url).scheme.lower()
    if _scheme not in ("http", "https") or net.is_private_host(cand.url):
        log.warning("oa candidate refused (scheme/host) %s (%s): %s", cand.source, cand.kind_hint, cand.url)
        return None

    data, _status, _ekind, ct = await net.fetch_bytes_with_meta(cand.url, user_agent, proxy_url)
    if not data:  # walled? retry with a Chrome TLS/JA3 fingerprint
        data, _ = await net.download_impersonated(cand.url, proxy_url)
        ct = ""
    if not data:
        log.info("oa candidate %s (%s) -> no bytes", cand.source, cand.url)
        return None

    head = data[:16]
    ct_base = (ct or "").split(";")[0].strip().lower()
    kind = "pdf" if (data[:5].startswith(b"%PDF") or ct_base == "application/pdf") else detect.sniff_magic(head)

    if kind in ("pdf", "image"):
        try:
            res = await _handle_binary_doc(data, cand.url, key, kind, user_agent, proxy_url)
        except Exception as e:
            log.warning("oa candidate convert failed %s: %s", cand.url, e)
            return None
        if res and not res.get("error") and (res.get("content_chars") or 0) > 0:
            log.info("oa candidate %s (%s) -> %s", cand.source, cand.url, kind)
            return res
        return None

    # Otherwise treat as HTML / plain text (publisher landing page, Gutenberg text, OCR .txt).
    text = data.decode("utf-8", "ignore")
    if not text.strip() or net.looks_like_challenge(text):
        log.info("oa candidate %s (%s) -> challenge/empty", cand.source, cand.url)
        return None
    low = text.lower()
    is_html = "<html" in low or "<!doctype html" in low or "<body" in low
    body = text if (cand.kind_hint == "txt" or not is_html) else html.extract_content_from_html(text)
    if len(body.strip()) <= THIN_MIN_CHARS:
        return None
    body = html.tidy_markdown(body)
    md_path = cache.cache_file(key, "html", ".md")
    cache._write_md(md_path, key, f"oa:{cand.source}", body)
    log.info("oa candidate %s (%s) -> %d chars", cand.source, cand.url, len(body))
    return _ok(md_path, f"oa:{cand.source}", body)


async def _resolve_and_fetch(
    candidates: "list[oa.Candidate]", key: str, label: str, user_agent: str, proxy_url: str | None
) -> dict | None:
    """Try each candidate in priority order; return the first that yields real content, else None."""
    for cand in candidates:
        res = await _candidate_to_result(cand, key, user_agent, proxy_url)
        if res:
            log.info("%s %s -> %s (%s)", label, key, cand.url, cand.source)
            return res
    return None


async def _resolve_input(
    kind: str, value: str, key: str, user_agent: str, proxy_url: str | None
) -> dict:
    """Resolve an ISBN/book identifier to content through the OA chain (books are unambiguous)."""
    async with net._client(proxy_url) as client:
        cands = await oa.resolve_book(value, client)
        res = await _resolve_and_fetch(cands, key, kind, user_agent, proxy_url)
    if res:
        return res
    log.info("%s resolver found no free full-text for %r", kind, value)
    return {"error": (
        f"No free, legal full text found for ISBN {value!r} (checked OAPEN, Internet Archive, "
        "Project Gutenberg, and DOAB). It may be an in-copyright book with no open edition — try a "
        "library, or use the `findWorks` tool with the title to see candidate editions."), "body": ""}


async def search_web(
    query: str, count: int = 8, lang: str = "", engines: str = "", proxy_url: str | None = None
) -> "tuple[list[dict] | None, str | None]":
    """Web search via SearXNG (then Brave). Returns (results, backend); (None, None) = unconfigured."""
    from httpx import AsyncClient
    async with AsyncClient(proxy=proxy_url) as client:
        return await search.web_search(query, client, count, lang, engines)


async def find_sources(query: str, limit: int = 8, proxy_url: str | None = None) -> list[dict]:
    """A title / free-text query → a ranked list of candidate works (papers + books).

    Like WebSearch, but for scholarship: returns candidates to CHOOSE from (each with a `fetch`
    handle), it does not download. The caller fetches the chosen handle with `get_or_fetch`.
    """
    from httpx import AsyncClient
    async with AsyncClient(proxy=proxy_url) as client:
        return await oa.find_works(query, client, limit)


# A title is ambiguous — `fetch` won't guess which work you mean; it routes you to `findWorks`.
_FIND_HINT = (
    "is a title — use the `findWorks` tool to list candidate works (it returns a fetch handle for "
    "each), then fetch the one you pick. `fetch` retrieves locations and UNAMBIGUOUS identifiers "
    "(URL, file path, DOI, ISBN), never a title."
)


def _looks_like_title(s: str) -> bool:
    """True if a bare (non-URL/DOI/ISBN) string should be resolved as a paper/book title."""
    if s.startswith((".", "~", "-")) or "\\" in s:
        return False  # path-like
    if detect.detect_kind(s) != "html":
        return False  # has a recognized file extension → a (missing) path
    if "/" in s and " " not in s:
        return False  # slashy with no spaces → a relative path (docs/foo), not a title
    return bool(re.search(r"[A-Za-z]", s))


def _is_not_found_page(url: str, body: str) -> bool:
    """True if a fetched HTML body is a known 'resource does not exist' page (e.g. arXiv's)."""
    low = body.lower()
    if "arxiv.org" in url.lower():
        return any(m in low for m in (
            "no article for this identifier", "invalid arxiv identifier",
            "article identifier", "no article found"))
    return False


def _is_pubmed_search_url(url: str) -> bool:
    """True for a PubMed *search/results* URL (not a specific article) — these 404 on fetch.

    Narrow on purpose: host pubmed.ncbi.nlm.nih.gov AND (a `/search` path OR a `term=` query).
    An article URL like .../30220343/ has neither, so it is left to fetch normally.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if (parsed.hostname or "").lower() != "pubmed.ncbi.nlm.nih.gov":
        return False
    return "/search" in parsed.path.lower() or "term=" in (parsed.query or "").lower()


def _path_exists(s: str) -> bool:
    """Path.exists() that can't raise — a very long string raises OSError(ENAMETOOLONG)."""
    try:
        return Path(s).expanduser().exists()
    except OSError:
        return False


def _is_file(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:
        return False


async def _doi_mirror_result(
    doi: str, key: str, user_agent: str, proxy_url: str | None
) -> dict:
    """Resolve a DOI to open-access content via Europe PMC, PMC HTML, or Wayback.

    Tries in order: Europe PMC PDF → PMC article HTML → Wayback snapshot of doi.org URL.
    """
    log.info("doi mirror %s", doi)
    # Cache-first: a repeated DOI must not re-download + re-convert (~40s) or write a second,
    # divergent artifact. DOI artifacts are keyed by the bare DOI (dedup across input forms).
    for ck in (doi, key):
        for kind in ("pdf", "html"):
            cached = cache.cache_file(ck, kind, ".md")
            if cached.exists():
                meta, body = cache.split_frontmatter(cached.read_text(encoding="utf-8", errors="ignore"))
                if body.strip():
                    log.info("doi mirror cache hit %s (%s)", doi, kind)
                    return _hit(cached, meta.get("method", "cached"), body)
    async with net._client(proxy_url) as client:
        pmcid = await mirror.doi_to_pmcid(doi, client)
        if pmcid:
            res = await _pmcid_to_result(pmcid, doi, user_agent, proxy_url)
            if res:
                return res

        # Full OA chain: Unpaywall / OpenAlex / Semantic-Scholar / CORE / DOAJ / arXiv / OSF.
        oa_res = await _resolve_and_fetch(
            await oa.resolve_doi(doi, client), doi, "doi-oa", user_agent, proxy_url)
        if oa_res:
            return oa_res

        # Still nothing free — fall back to a Wayback snapshot of the doi.org URL
        doi_url = f"https://doi.org/{doi}"
        wb_url = await mirror.wayback_raw_url(doi_url, client)
        if wb_url:
            wb_raw = await net.fetch_raw(wb_url, user_agent, proxy_url)
            if wb_raw:
                wb_body = html.extract_content_from_html(wb_raw)
                if len(wb_body.strip()) > THIN_MIN_CHARS:
                    wb_body = html.tidy_markdown(wb_body)
                    md_path = cache.cache_file(doi, "html", ".md")
                    cache._write_md(md_path, doi, "mirror:wayback", wb_body)
                    log.info("doi mirror %s -> wayback", doi)
                    return _ok(md_path, "mirror:wayback", wb_body)

    log.warning("doi mirror %s -> no open-access source", doi)
    return {"error": (
        f"Found DOI {doi}, but no free, legal full text exists in any open-access source (checked "
        "Unpaywall, OpenAlex, Semantic Scholar, Europe PMC, CORE, DOAJ, arXiv/OSF, and the Wayback "
        "Machine). The paper is likely paywalled — open the publisher's page directly, or look for "
        "an author preprint."), "body": ""}


async def _try_mirror_for_url(
    src: str, key: str, user_agent: str, proxy_url: str | None
) -> dict | None:
    """Wall fallback: extract DOI from a publisher URL and resolve via mirror, or try Wayback.

    Returns a result dict on success, or None when no better content is found.
    Only called for non-private, non-local URLs after the full httpx/curl_cffi/Jina ladder.
    """
    doi = mirror.extract_doi(src)
    meta_pdf = None
    if not doi:
        # Rabbit hole: scrape the (blocked) landing page for citation_pdf_url / citation_doi.
        page = await net.fetch_raw(src, user_agent, proxy_url)
        if page:
            m_doi, meta_pdf, _title = oa.extract_meta_links(page)
            doi = doi or m_doi
    if doi:
        log.info("mirror url %s doi=%s", key, doi)
    async with net._client(proxy_url) as client:
        if meta_pdf:
            res = await _candidate_to_result(
                oa.Candidate(oa._score("citation_pdf_url"), meta_pdf, "citation_pdf_url", kind_hint="pdf"),
                key, user_agent, proxy_url)
            if res:
                log.info("mirror url %s -> citation_pdf_url", key)
                return res
        if doi:
            pmcid = await mirror.doi_to_pmcid(doi, client)
            if pmcid:
                res = await _pmcid_to_result(pmcid, key, user_agent, proxy_url)
                if res:
                    return res

        if doi:
            oa_res = await _resolve_and_fetch(
                await oa.resolve_doi(doi, client), key, "mirror-oa", user_agent, proxy_url)
            if oa_res:
                return oa_res

        # No DOI found (or all OA candidates failed) — try a Wayback snapshot
        wb_url = await mirror.wayback_raw_url(src, client)
        if wb_url:
            wb_raw = await net.fetch_raw(wb_url, user_agent, proxy_url)
            if wb_raw:
                wb_body = html.extract_content_from_html(wb_raw)
                if len(wb_body.strip()) > THIN_MIN_CHARS:
                    wb_body = html.tidy_markdown(wb_body)
                    md_path = cache.cache_file(key, "html", ".md")
                    cache._write_md(md_path, key, "mirror:wayback", wb_body)
                    log.info("mirror url %s -> wayback", key)
                    return _ok(md_path, "mirror:wayback", wb_body)

    log.info("mirror url %s -> no better content", key)
    return None


async def _dispatch_one(
    item: str, user_agent: str, proxy_url: str | None, media: str
) -> dict:
    """Dispatch ONE input (URL or local path, optionally `archive::member`) to its handler
    and return a result dict. Never raises — failures come back as {"error": ...}.

    `media="deny"` (used by the `fetch` tool) redirects images → `fetchImage` and archives →
    `archive`, keeping `fetch`'s contract pure (returns document markdown, not a path/listing).
    """
    log.info("get_or_fetch %s media=%s", item, media)
    base, sep, member = item.partition("::")
    member = member if sep else ""
    stripped = base.strip()
    low = stripped.lower()

    if not stripped:
        return {"error": 'empty input — pass a URL, a file path, a DOI, an ISBN, or title:"...".', "body": ""}

    # A title is ambiguous — route it to the `findWorks` tool rather than guess which work it means.
    if low.startswith("title:"):
        val = stripped[len("title:"):].strip().strip('"').strip("'")
        log.info("routing -> find hint for title: %s", val)
        return {"error": f"{val!r} {_FIND_HINT}", "body": ""}
    # ISBN is unambiguous → fetch the book directly (after validating the checksum).
    if low.startswith("isbn:"):
        raw = stripped[len("isbn:"):].strip()
        norm = oa.normalize_isbn(raw)
        if not norm:
            return {"error": f"{raw!r} is not a valid ISBN-10/13 (check the digits / check digit).", "body": ""}
        log.info("routing -> book resolver (isbn): %s", norm)
        return await _resolve_input("isbn", norm, base, user_agent, proxy_url)

    # DOI fast-path: bare DOI (10.xxxx/...), doi: prefix, or doi.org URL
    _doi_str = _extract_doi_from_input(base)
    if _doi_str:
        log.info("routing %s -> doi mirror", base)
        return await _doi_mirror_result(_doi_str, base, user_agent, proxy_url)

    if low.startswith("file://"):
        local, src = True, _file_url_to_path(base)
    elif _URL_RE.match(base):
        local, src = False, base
    elif _SCHEME_RE.match(base):
        log.warning("unsupported URL scheme: %s", base)
        return {"error": (f"unsupported URL scheme in {base!r} — fetch handles http(s):// and "
                          "file:// URLs, local paths, DOIs, and ISBNs."), "body": ""}
    else:
        # Not a URL/scheme/DOI. A bare ISBN fetches a book directly; a bare title is ambiguous,
        # so point it at `findWorks`. Neither fires when the string is an existing local file/path.
        if not member and not _path_exists(stripped):
            bare_isbn = oa.normalize_isbn(stripped)
            if bare_isbn:
                log.info("routing -> book resolver (bare isbn): %s", bare_isbn)
                return await _resolve_input("isbn", bare_isbn, base, user_agent, proxy_url)
            # Bare scholarly IDs — checked BEFORE _looks_like_title because "PMC…" has letters
            # and would otherwise be misread as a title. A real local file already won above.
            if _PMCID_RE.match(stripped):
                pmcid = stripped.upper()
                log.info("routing -> pmcid resolver: %s", pmcid)
                res = await _pmcid_to_result(pmcid, base, user_agent, proxy_url)
                if res:
                    return res
                return {"error": (
                    f"{stripped} is a PMCID but no free full text was found in Europe PMC/PMC. "
                    "Try the `findWorks` tool."), "body": ""}
            if _PMID_RE.match(stripped):
                log.info("routing -> pmid resolver: %s", stripped)
                async with net._client(proxy_url) as client:
                    pmcid = await mirror.pmid_to_pmcid(stripped, client)
                res = await _pmcid_to_result(pmcid, base, user_agent, proxy_url) if pmcid else None
                if res:
                    return res
                return {"error": (
                    f"{stripped} looks like a PubMed ID but no open-access full text was found "
                    "(it may be abstract-only or paywalled). Try the `findWorks` tool with the title."),
                    "body": ""}
            if _looks_like_title(stripped):
                log.info("routing -> find hint for bare title: %s", stripped)
                return {"error": f"{stripped!r} {_FIND_HINT}", "body": ""}
        local, src = True, base

    # SSRF guard: never fetch a private / internal / link-local host (covers obfuscated IP forms).
    if not local and net.is_private_host(src):
        log.warning("refusing private/internal host: %s", src)
        return {"error": f"refusing to fetch a private or internal host: {base}", "body": ""}

    # A PubMed *search/results* URL is not an article — it 404s on fetch. Route to find/search.
    if not local and _is_pubmed_search_url(src):
        log.info("routing -> find/search hint for pubmed search url: %s", src)
        return {"error": (
            f"{base} is a PubMed search/results URL, not an article — use the `findWorks` tool (or "
            "`search`) to get candidate works, each with a fetch handle."), "body": ""}

    kind = detect.detect_kind(base)

    if local:
        p = Path(src).expanduser()
        try:
            rp = p.resolve()
        except OSError:
            rp = p
        # Deny on BOTH the path and its symlink target — a symlink with an innocent name must not
        # smuggle a secret (e.g. report.html → prod_creds.pem).
        reason = detect.deny_reason(p) or detect.deny_reason(rp)
        if reason:
            log.warning("deny %s: %s", p, reason)
            return {"error": reason, "body": ""}
        if not _is_file(rp):
            log.warning("local file not found: %s", base)
            return {"error": f"local file not found: {base}", "body": ""}
        if kind == "html":  # ambiguous local file — sniff magic bytes
            try:
                with rp.open("rb") as fh:
                    head = fh.read(16)
            except OSError as e:
                log.warning("cannot read %s: %s", rp, e)
                return {"error": f"cannot read {base}: {e}", "body": ""}
            sniffed = detect.sniff_magic(head)
            if sniffed:
                kind = sniffed
        src = str(rp)

    log.info("dispatch %s kind=%s local=%s member=%r", base, kind, local, member)
    if media == "deny" and (kind == "image" or kind in detect.ARCHIVE_KINDS):
        tool = "fetchImage" if kind == "image" else "archive"
        what = "an image" if kind == "image" else f"a {kind} archive"
        log.info("fetch redirect: %s is %s -> %s tool", base, what, tool)
        return {"error": f"{base} is {what} — use the `{tool}` tool, not `fetch`.", "body": ""}
    try:
        if kind in detect.ARCHIVE_KINDS:
            return await _archive_result(src, base, kind, member, local, user_agent, proxy_url)
        if member:
            return {"error": f"the '::' member syntax is only valid for archives, not {kind}", "body": ""}
        if kind == "image":
            return await _image_result(src, base, local, user_agent, proxy_url)
        if kind == "html":
            return await _html_result(src, base, local, user_agent, proxy_url)
        return await _doc_result(src, base, kind, local, user_agent, proxy_url)
    except Exception as e:  # converters/archive libs can raise — surface a clean per-item error
        log.exception("get_or_fetch %s failed: %s", base, e)
        return {"error": f"{type(e).__name__}: {e}", "body": ""}


async def get_or_fetch(
    item: str, user_agent: str, proxy_url: str | None = None, media: str = "allow"
) -> dict:
    """Dispatch ONE input to its handler (see `_dispatch_one`), with two guards against
    re-hammering a failing source:

    * **Negative cache** — a result with an ``"error"`` key is remembered for
      HARVESTER_NEG_TTL seconds (default 120); a repeat call inside the window returns a
      copy of that error instead of re-fetching. Successes are never cached.
    * **In-flight dedup** — concurrent calls for the same (media, item) share ONE fetch.

    Same signature/contract as the dispatch itself: never raises; failures are {"error": ...}.
    """
    key = f"{media}\x00{item}"

    cached = _neg_cache_get(key)
    if cached is not None:
        log.info("get_or_fetch %s media=%s -> negative-cache hit", item, media)
        return cached

    inflight = _INFLIGHT.get(key)
    if inflight is not None:
        log.info("get_or_fetch %s media=%s -> awaiting in-flight fetch", item, media)
        return await inflight

    fut: "asyncio.Future[dict]" = asyncio.get_running_loop().create_future()
    _INFLIGHT[key] = fut
    try:
        result = await _dispatch_one(item, user_agent, proxy_url, media)
    except BaseException as e:  # _dispatch_one shouldn't raise, but never leak the future
        if not fut.done():
            fut.set_exception(e)
        raise
    else:
        if isinstance(result, dict) and result.get("error"):
            _NEG_CACHE[key] = (time.monotonic(), result)
            _neg_cache_evict_stale()
        if not fut.done():
            fut.set_result(result)
        return result
    finally:
        _INFLIGHT.pop(key, None)
