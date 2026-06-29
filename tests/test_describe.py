"""Tests for harvester.describe — the inline-body cap and its truncation note (FIX P0)."""

from pathlib import Path

from harvester.describe import DEFAULT_MAX_INLINE_CHARS, describe_fetch_result


def _ok_result(body: str, *, content_chars: int | None = None) -> dict:
    """A success result dict shaped like dispatch._ok/_hit (see dispatch.py)."""
    return {
        "cache_status": "miss",
        "method": "local-trafilatura",
        "md_path": Path("/tmp/cache/example.md"),
        "body": body,
        "bytes": len(body),
        "content_chars": content_chars if content_chars is not None else len(body.strip()),
        "http_status": 200,
        "error_kind": None,
        "challenge": False,
    }


class TestInlineCap:
    def test_body_over_default_cap_is_truncated_with_note(self):
        body = "A" * (DEFAULT_MAX_INLINE_CHARS + 10_000)
        out = describe_fetch_result("https://example.com/big", _ok_result(body)).text
        # The full body must NOT be inlined.
        assert body not in out
        # The truncation note names the cap, the true length, the md_path, and grep_cache.
        assert f"first {DEFAULT_MAX_INLINE_CHARS} of {len(body)} chars" in out
        assert "/tmp/cache/example.md" in out
        assert "grep_cache" in out

    def test_body_under_cap_is_returned_unchanged(self):
        body = "word " * 200  # 1000 chars: over THIN_MIN_CHARS, under the cap
        out = describe_fetch_result("https://example.com/small", _ok_result(body)).text
        assert body in out
        assert "truncated" not in out

    def test_env_override_changes_threshold(self, monkeypatch):
        monkeypatch.setenv("HARVESTER_MAX_INLINE_CHARS", "100")
        body = "word " * 200  # 1000 chars, content_chars defaults to 1000 (not thin)
        out = describe_fetch_result("https://example.com/x", _ok_result(body)).text
        assert f"first 100 of {len(body)} chars" in out
        assert body not in out

    def test_cap_disabled_when_zero(self, monkeypatch):
        monkeypatch.setenv("HARVESTER_MAX_INLINE_CHARS", "0")
        body = "B" * (DEFAULT_MAX_INLINE_CHARS + 5_000)
        out = describe_fetch_result("https://example.com/full", _ok_result(body)).text
        assert body in out
        assert "truncated" not in out
