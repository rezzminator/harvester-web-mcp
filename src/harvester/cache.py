"""The on-disk cache layer.

Default root is `.fetch/` at the PROJECT ROOT (the dir holding pyproject.toml), overridable
with the WEBFETCH_DIR env var. The cache is type-partitioned: every artifact lives under a
per-kind subdir, e.g. `.fetch/html/<slug>.md`, `.fetch/pdf/<slug>.md`, `.fetch/png/<slug>.png`.
"""

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

from .log import get_logger
from .tokens import estimate_tokens

log = get_logger("cache")

# Treat a cached/extracted body shorter than this as "thin" or "trivial".
THIN_MIN_CHARS = 500
# A cached HTML body must exceed this to count as a usable hit (else re-fetch).
CACHE_MIN_BODY = 200


def _project_root() -> Path:
    """The dir containing pyproject.toml, found by walking up from this module.

    Robust to layout changes — no hardcoded parent index. Falls back to two levels up
    (src/harvester/ -> repo root) when no pyproject.toml is found (e.g. an installed wheel).
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return here.parents[2]


def cache_root() -> Path:
    """Root of the type-partitioned cache: `.fetch/` at the project root by default,
    or the WEBFETCH_DIR env var when set."""
    env = os.environ.get("WEBFETCH_DIR")
    p = Path(env) if env else _project_root() / ".fetch"
    p.mkdir(parents=True, exist_ok=True)
    return p


# Backwards-compatible alias (older code/tests referenced harvester_dir()).
def harvester_dir() -> Path:
    return cache_root()


def slugify(key: str) -> str:
    """URL/path -> a filesystem-safe slug + 10-char sha1 of the key for collision safety.

    Strips any scheme, replaces every char outside [A-Za-z0-9._-] with '_', collapses runs,
    trims, caps the slug at 150 chars, and appends `__<sha1[:10]>`.
    """
    no_scheme = re.sub(r"^[a-z][a-z0-9+.-]*://", "", key, flags=re.IGNORECASE)
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", no_scheme)
    slug = re.sub(r"_+", "_", slug).strip("_")
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return f"{slug[:150]}__{h}"


def cache_file(key: str, kind: str, ext: str = ".md") -> Path:
    """Path for a cached artifact: `<root>/<kind>/<slug><ext>`. Creates the subdir."""
    sub = cache_root() / kind
    sub.mkdir(parents=True, exist_ok=True)
    return sub / f"{slugify(key)}{ext}"


def split_frontmatter(text: str) -> Tuple[dict, str]:
    """Split a `---`-delimited YAML frontmatter header from the markdown body."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\n") != "---":
        return {}, text
    meta: dict = {}
    i = 1
    while i < len(lines) and lines[i].rstrip("\n") != "---":
        line = lines[i].rstrip("\n")
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
        i += 1
    body = "".join(lines[i + 1:]) if i < len(lines) else ""
    return meta, body.lstrip("\n")


def read_frontmatter(md_path: str | Path) -> dict:
    """Read just the YAML frontmatter of a cached artifact, without loading the whole body.

    Used by `describe.py` to enrich a result header with `tokens` / `fetched_at` straight from
    what `_write_md` already recorded. Returns `{}` (never raises) when the path doesn't exist,
    isn't readable as text, or carries no frontmatter block — callers must tolerate a missing
    key (non-`.md` cache artifacts like images/archives have no frontmatter at all).
    """
    try:
        with open(md_path, "r", encoding="utf-8", errors="ignore") as fh:
            head = fh.read(4096)  # the frontmatter block is always tiny; never read a huge body
    except OSError as e:
        log.debug("read_frontmatter cannot read %s: %s", md_path, e)
        return {}
    meta, _body = split_frontmatter(head)
    return meta


def _write_md(md_path: Path, key: str, method: str, body: str, extra: "dict[str, str] | None" = None) -> None:
    """Write a markdown artifact with a small YAML provenance header.

    `extra` adds optional additional frontmatter fields (e.g. `{"rungs": "direct, jina"}` — the
    Slice 2 rescue-graph trace) without changing the shape of an artifact that has none: existing
    readers (`split_frontmatter`/`read_frontmatter`) already parse arbitrary `key: value` lines,
    so old artifacts with no `extra` are byte-identical to before.
    """
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    extra_lines = "".join(f"{k}: {v}\n" for k, v in (extra or {}).items())
    header = (
        "---\n"
        f"url: {key}\n"
        f"fetched_at: {fetched_at}\n"
        "source: harvester\n"
        f"method: {method}\n"
        f"token_count: {estimate_tokens(body)}\n"
        f"{extra_lines}"
        "---\n\n"
    )
    md_path.write_text(header + body, encoding="utf-8")
    log.debug("cache write %s method=%s chars=%d", md_path, method, len(body))


def search_cache(pattern: str, max_results: int = 50, ignore_case: bool = True) -> list[dict]:
    """Search every cached markdown body under the .fetch tree for `pattern` (regex)."""
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        log.warning("search_cache invalid regex %r: %s", pattern, e)
        raise ValueError(f"invalid regex pattern: {e}")
    results: list[dict] = []
    for md in sorted(cache_root().rglob("*.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            log.warning("search_cache cannot read %s: %s", md, e)
            continue
        _meta, body = split_frontmatter(text)
        hits = rx.findall(body)
        if not hits:
            continue
        sample = ""
        for line in body.splitlines():
            if rx.search(line):
                sample = line.strip()[:200]
                break
        results.append({
            "url": _meta.get("url") or str(md),
            "md_path": str(md),
            "matches": len(hits),
            "sample": sample,
        })
        if len(results) >= max_results:
            break
    log.info("search_cache /%s/ -> %d page(s)", pattern, len(results))
    return results
