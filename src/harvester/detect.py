"""Pure (no-network) format detection and security guards.

Nothing here touches the network or heavy converter deps — it only inspects names, magic
bytes, and content-types. Importing this module stays cheap.
"""

import os
from pathlib import Path

from .log import get_logger

log = get_logger("detect")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}

_EXT_KIND = {
    ".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx",
    ".csv": "csv", ".json": "json", ".zip": "zip", ".7z": "7z", ".rar": "rar",
    ".htm": "html", ".html": "html",
}
_TAR_SUFFIXES = (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar")
ARCHIVE_KINDS = {"zip", "tar", "7z", "rar"}
DOC_KINDS = {"pdf", "docx", "xlsx", "pptx", "csv", "json"}


def detect_kind(location: str) -> str:
    """Best-effort source kind from a URL/path by extension. Defaults to 'html'."""
    name = location.split("?", 1)[0].split("#", 1)[0].lower().rstrip("/")
    if name.endswith(_TAR_SUFFIXES):
        return "tar"
    ext = os.path.splitext(name)[1]
    if ext in _EXT_KIND:
        return _EXT_KIND[ext]
    if ext in IMAGE_EXTS:
        return "image"
    if ext in {".gz", ".bz2", ".xz"}:
        return "tar"
    return "html"


def image_ext(location: str, default: str = ".png") -> str:
    """The image file extension for a URL/path (used as the cache subdir name too)."""
    ext = os.path.splitext(location.split("?", 1)[0].split("#", 1)[0])[1].lower()
    return ext if ext in IMAGE_EXTS else default


def sniff_magic(head: bytes) -> str | None:
    """Identify a file by leading magic bytes (for local files with no/odd extension)."""
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"  # also docx/xlsx/pptx — caller refines by extension
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if head.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if head.startswith(b"\x1f\x8b"):
        return "tar"  # gzip
    if head.startswith(b"BZh"):
        return "tar"  # bzip2
    if head.startswith(b"\xfd7zXZ\x00"):
        return "tar"  # xz
    if head.startswith(b"\xff\xd8\xff"):
        return "image"  # jpeg
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"  # png
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image"
    return None


# ── security: refuse secrets/keys on local reads ──────────────────────────────
_DENY_DIR_PARTS = {".ssh", ".gnupg", ".aws", ".password-store", ".docker"}
_DENY_NAMES = {"id_rsa", "id_ed25519", "id_dsa", "id_ecdsa", "credentials",
               ".netrc", ".pgpass", ".htpasswd", "shadow", "master.key"}
_DENY_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks",
                  ".asc", ".gpg", ".kdbx", ".ppk", ".env")
_DENY_KEY_MARKERS = ("_rsa", "_ed25519", "_dsa", "_ecdsa")


def deny_reason(path: Path) -> str | None:
    """Return a refusal reason if `path` looks like a credential/secret, else None.

    Conservative: matches whole directory names, whole file names, or file suffixes only —
    never an arbitrary substring — so ordinary documents pass through untouched.
    """
    try:
        rp = path.expanduser()
    except Exception as e:
        log.warning("deny_reason expanduser failed for %s: %s", path, e)
        rp = path
    hit = {p.lower() for p in rp.parts} & _DENY_DIR_PARTS
    if hit:
        return f"refusing to read inside a sensitive directory ({sorted(hit)[0]})"
    name = rp.name
    low = name.lower()
    if low in _DENY_NAMES:
        return f"refusing to read a sensitive file ({name})"
    if low.endswith(_DENY_SUFFIXES):
        return f"refusing to read a credential/secret file ({name})"
    if low == ".env" or low.startswith(".env."):
        return f"refusing to read an environment/secret file ({name})"
    if any(k in low for k in _DENY_KEY_MARKERS):
        return f"refusing to read a private key ({name})"
    return None


# ── content-type → kind mapping for binary/non-HTML responses ────────────────
_CT_BINARY_MAP: dict[str, str] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/zip": "zip",
    "application/x-zip-compressed": "zip",
    "application/x-zip": "zip",
    "application/x-7z-compressed": "7z",
    "application/x-rar-compressed": "rar",
    "application/vnd.rar": "rar",
    "application/x-tar": "tar",
    "application/gzip": "tar",
    "application/x-gzip": "tar",
    "application/x-bzip2": "tar",
    "application/x-xz": "tar",
}
_HTML_LIKE_CT = frozenset({
    "text/html", "text/plain", "application/xhtml+xml",
    "application/xml", "text/xml", "text/markdown",
    # NOTE: empty string is intentionally excluded — an absent Content-Type falls through
    # to magic-byte sniffing rather than being assumed as HTML.
})


def _sniff_kind(content_type: str, head: bytes) -> str | None:
    """Return the true non-HTML kind (e.g. 'pdf', 'zip', 'image') from Content-Type +
    magic bytes, or None when the response is genuinely HTML/text.

    Priority: explicit CT > openxmlformats sub-type > image/* > HTML CT (returns None) >
    magic bytes fallback when CT is absent/unknown.
    """
    ct_base = content_type.split(";")[0].strip().lower()
    if ct_base in _CT_BINARY_MAP:
        return _CT_BINARY_MAP[ct_base]
    if "openxmlformats-officedocument" in ct_base:
        if "wordprocessingml" in ct_base:
            return "docx"
        if "spreadsheetml" in ct_base:
            return "xlsx"
        if "presentationml" in ct_base:
            return "pptx"
        return "zip"
    if ct_base.startswith("image/"):
        return "image"
    if ct_base in ("application/json", "text/json", "application/ld+json"):
        return "json"
    if ct_base in ("text/csv", "application/csv"):
        return "csv"
    if ct_base in _HTML_LIKE_CT:
        return None  # unambiguously HTML/text — trust it, don't sniff further
    # Unknown or absent CT: fall back to magic bytes
    return sniff_magic(head)
