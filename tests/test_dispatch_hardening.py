"""Tests for the dispatch hardening fixes:

* P1 — negative-result cache + in-flight dedup in get_or_fetch
* P2 — publisher-403 PDF → DOI → open-access pivot in _doc_result
* P2b — detected bot/Cloudflare challenge is never cached/returned as content
* P3 — bare PMID / PMCID routing
* P4a — PubMed search-results URL → clean redirect
* P4b — favicon / icon skip in image localisation
"""

import asyncio

from harvester import convert, dispatch, images, mirror, net


async def _noop_localise(md, *a, **kw):
    return md


# ── P1: negative-result cache + in-flight dedup ─────────────────────────────────

class TestNegativeCacheAndDedup:
    async def test_failure_is_cached_second_call_skips_fetch(self, monkeypatch):
        calls = 0

        async def fake_dispatch(item, ua, proxy, media):
            nonlocal calls
            calls += 1
            return {"error": f"boom for {item}", "body": ""}

        monkeypatch.setattr(dispatch, "_dispatch_one", fake_dispatch)

        r1 = await dispatch.get_or_fetch("https://neg.example/fail", "ua")
        r2 = await dispatch.get_or_fetch("https://neg.example/fail", "ua")
        assert calls == 1, "second failing call must hit the negative cache, not re-fetch"
        assert "boom" in r1["error"]
        assert "recently failed; cached" in r2["error"]

    async def test_success_is_not_cached(self, monkeypatch):
        calls = 0

        async def fake_dispatch(item, ua, proxy, media):
            nonlocal calls
            calls += 1
            return {"body": "ok content", "method": "x", "cache_status": "miss",
                    "md_path": "/tmp/x.md", "bytes": 10, "content_chars": 10,
                    "http_status": 200, "error_kind": None, "challenge": False}

        monkeypatch.setattr(dispatch, "_dispatch_one", fake_dispatch)

        await dispatch.get_or_fetch("https://ok.example/page", "ua")
        await dispatch.get_or_fetch("https://ok.example/page", "ua")
        assert calls == 2, "successes must never be negative-cached"

    async def test_concurrent_calls_dedup_to_one_fetch(self, monkeypatch):
        calls = 0

        async def fake_dispatch(item, ua, proxy, media):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)  # keep the fetch in flight while the twin arrives
            return {"error": "slow fail", "body": ""}

        monkeypatch.setattr(dispatch, "_dispatch_one", fake_dispatch)

        results = await asyncio.gather(
            dispatch.get_or_fetch("https://race.example/x", "ua"),
            dispatch.get_or_fetch("https://race.example/x", "ua"),
        )
        assert calls == 1, "concurrent calls for the same item must share one fetch"
        assert all(r["error"] == "slow fail" for r in results)

    async def test_allow_and_deny_media_do_not_alias(self, monkeypatch):
        seen = []

        async def fake_dispatch(item, ua, proxy, media):
            seen.append(media)
            return {"error": f"err {media}", "body": ""}

        monkeypatch.setattr(dispatch, "_dispatch_one", fake_dispatch)

        await dispatch.get_or_fetch("https://m.example/x", "ua", None, "allow")
        await dispatch.get_or_fetch("https://m.example/x", "ua", None, "deny")
        assert seen == ["allow", "deny"], "different media must not share a cache/in-flight key"

    async def test_expired_entry_refetches(self, monkeypatch):
        calls = 0

        async def fake_dispatch(item, ua, proxy, media):
            nonlocal calls
            calls += 1
            return {"error": "transient", "body": ""}

        clock = {"t": 1000.0}
        monkeypatch.setattr(dispatch.time, "monotonic", lambda: clock["t"])
        monkeypatch.setenv("HARVESTER_NEG_TTL", "10")
        monkeypatch.setattr(dispatch, "_dispatch_one", fake_dispatch)

        await dispatch.get_or_fetch("https://ttl.example/x", "ua")
        clock["t"] += 11  # advance past the TTL
        await dispatch.get_or_fetch("https://ttl.example/x", "ua")
        assert calls == 2, "an expired negative-cache entry must re-fetch"

    def test_invalid_neg_ttl_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HARVESTER_NEG_TTL", "not-a-number")
        assert dispatch._neg_ttl() == dispatch.DEFAULT_NEG_TTL


# ── P2: publisher-403 PDF → DOI → OA pivot in _doc_result ───────────────────────

class TestDocResultMirrorPivot:
    _FAKE_SUCCESS = {
        "body": "# Open Access\n\n" + "word " * 200, "method": "mirror:europepmc-pdf",
        "cache_status": "miss", "md_path": "/tmp/x.md", "bytes": 1000,
        "content_chars": 1000, "http_status": None, "error_kind": None, "challenge": False,
    }

    async def _no_bytes(self, monkeypatch):
        async def fake_download_bytes(url, ua, proxy=None):
            return b"", 403, None

        async def fake_download_impersonated(url, proxy=None):
            return b"", 403

        monkeypatch.setattr(net, "download_bytes", fake_download_bytes)
        monkeypatch.setattr(net, "download_impersonated", fake_download_impersonated)

    async def test_403_pdf_pivots_to_mirror(self, monkeypatch):
        await self._no_bytes(monkeypatch)
        called = []

        async def fake_mirror(src, key, ua, proxy):
            called.append(src)
            return dict(self._FAKE_SUCCESS)

        monkeypatch.setattr(dispatch, "_try_mirror_for_url", fake_mirror)

        url = "https://www.tandfonline.com/doi/pdf/10.1080/12345.2023.1"
        result = await dispatch._doc_result(url, url, "pdf", False, "ua", None)
        assert called, "the mirror pivot must be attempted on a 403 publisher PDF"
        assert result["method"] == "mirror:europepmc-pdf"
        assert "error" not in result

    async def test_403_pdf_no_mirror_returns_net_error(self, monkeypatch):
        await self._no_bytes(monkeypatch)

        async def fake_mirror(src, key, ua, proxy):
            return None

        monkeypatch.setattr(dispatch, "_try_mirror_for_url", fake_mirror)

        url = "https://www.sciencedirect.com/science/article/pii/S0001/pdf"
        result = await dispatch._doc_result(url, url, "pdf", False, "ua", None)
        assert "error" in result
        assert "403" in result["error"] or "forbidden" in result["error"].lower()


# ── P3: bare PMID / PMCID routing ───────────────────────────────────────────────

class TestPmidPmcidRouting:
    _PDF_BYTES = b"%PDF-1.4 fake content\n%%EOF"

    def _mock_pmc_pdf_chain(self, monkeypatch):
        async def fake_europepmc_pdf(pmcid, client):
            return self._PDF_BYTES

        def fake_pdf_to_md(path):
            return "# PMC Paper\n\n" + "word " * 200

        monkeypatch.setattr(mirror, "europepmc_pdf", fake_europepmc_pdf)
        monkeypatch.setattr(convert, "pdf_to_md", fake_pdf_to_md)

    async def test_bare_pmid_routes_to_resolver(self, monkeypatch):
        async def fake_pmid_to_pmcid(pmid, client):
            return "PMC11632837"

        monkeypatch.setattr(mirror, "pmid_to_pmcid", fake_pmid_to_pmcid)
        self._mock_pmc_pdf_chain(monkeypatch)

        result = await dispatch.get_or_fetch("30220343", "ua")
        assert "error" not in result, result.get("error")
        assert result["method"] == "mirror:europepmc-pdf"
        assert "local file not found" not in str(result)

    async def test_bare_pmid_no_pmcid_returns_clean_error(self, monkeypatch):
        async def fake_pmid_to_pmcid(pmid, client):
            return None

        monkeypatch.setattr(mirror, "pmid_to_pmcid", fake_pmid_to_pmcid)

        result = await dispatch.get_or_fetch("30220343", "ua")
        assert "error" in result
        assert "PubMed ID" in result["error"]
        assert "find" in result["error"]
        assert "local file not found" not in result["error"]

    async def test_pmcid_routes_as_pmcid(self, monkeypatch):
        self._mock_pmc_pdf_chain(monkeypatch)
        result = await dispatch.get_or_fetch("PMC11632837", "ua")
        assert "error" not in result, result.get("error")
        assert result["method"] == "mirror:europepmc-pdf"

    async def test_pmcid_not_found_returns_clean_error(self, monkeypatch):
        async def fake_europepmc_pdf(pmcid, client):
            return b""

        async def fake_fetch_raw(url, ua, proxy=None):
            return ""

        monkeypatch.setattr(mirror, "europepmc_pdf", fake_europepmc_pdf)
        monkeypatch.setattr(net, "fetch_raw", fake_fetch_raw)

        result = await dispatch.get_or_fetch("PMC99999999", "ua")
        assert "error" in result
        assert "PMCID" in result["error"]
        assert "find" in result["error"]

    async def test_pmcid_lowercase_is_recognised(self, monkeypatch):
        seen = []

        async def fake_pmcid_to_result(pmcid, key, ua, proxy):
            seen.append(pmcid)
            return {"body": "ok", "method": "mirror:pmc-html", "cache_status": "miss",
                    "md_path": "/tmp/x.md", "bytes": 2, "content_chars": 600,
                    "http_status": None, "error_kind": None, "challenge": False}

        monkeypatch.setattr(dispatch, "_pmcid_to_result", fake_pmcid_to_result)
        result = await dispatch.get_or_fetch("pmc11632837", "ua")
        assert "error" not in result
        assert seen == ["PMC11632837"], "PMCID must be upper-cased before resolving"


# ── P4a: PubMed search URL → redirect ───────────────────────────────────────────

class TestPubmedSearchUrl:
    def test_is_pubmed_search_url_positive(self):
        assert dispatch._is_pubmed_search_url("https://pubmed.ncbi.nlm.nih.gov/?term=cancer")
        assert dispatch._is_pubmed_search_url("https://pubmed.ncbi.nlm.nih.gov/search/?term=x")

    def test_is_pubmed_search_url_negative(self):
        # A specific-article URL must NOT be treated as a search URL.
        assert not dispatch._is_pubmed_search_url("https://pubmed.ncbi.nlm.nih.gov/30220343/")
        assert not dispatch._is_pubmed_search_url("https://example.com/?term=x")

    async def test_search_url_returns_find_hint(self, monkeypatch):
        async def boom(*a, **kw):
            raise AssertionError("must not fetch a PubMed search URL")

        # If routing is correct, the network is never touched.
        monkeypatch.setattr(net, "fetch_bytes_with_meta", boom)
        result = await dispatch.get_or_fetch(
            "https://pubmed.ncbi.nlm.nih.gov/?term=glp1+weight", "ua")
        assert "error" in result
        assert "find" in result["error"] and "search" in result["error"].lower()


# ── P4b: favicon / icon skip ────────────────────────────────────────────────────

class TestIconSkip:
    def test_is_icon_asset(self):
        assert images._is_icon_asset("https://x.com/favicon.ico")
        assert images._is_icon_asset("https://x.com/static/favicon-32x32.png")
        assert images._is_icon_asset("https://x.com/assets/sprite.svg")
        assert images._is_icon_asset("/img/icon.ico?v=2")
        assert not images._is_icon_asset("https://x.com/figures/fig1.png")
        assert not images._is_icon_asset("https://x.com/photo.jpg")

    async def test_favicon_is_not_downloaded(self, monkeypatch):
        requested = []
        png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        class FakeResponse:
            status_code = 200
            content = png_data
            headers = {"content-type": "image/png"}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def get(self, url, **kw):
                requested.append(url)
                return FakeResponse()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())

        md = ("![icon](https://example.com/favicon.ico)\n\n"
              "![fig](https://example.com/fig.png)\n")
        result = await images.localize_html_images(md, "https://example.com/p", "ua")
        assert not any("favicon" in u for u in requested), "favicon must be skipped pre-download"
        assert any("fig.png" in u for u in requested), "the real figure must still download"
        assert "https://example.com/favicon.ico" in result  # left untouched


# ── P2b: unresolved bot/Cloudflare challenge is poison, never cached/returned ────

class TestUnresolvedChallenge:
    # The live sciencedirect captcha variant (~620 bytes) that was wrongly cached as success.
    _CAPTCHA_HTML = (
        "<html><head><title>Just a moment...</title></head><body>"
        "<h1>Are you a robot?</h1>"
        "<p>Please confirm you are a human by completing the captcha challenge below. "
        "Enable JavaScript and cookies to continue.</p>"
        "<p>Reference number: 1234567890</p><p>IP Address: 1.2.3.4</p>"
        "</body></html>"
    ).encode()
    _RICH_HTML = ("<html><body><article><p>" + "word " * 300 + "</p></article></body></html>").encode()

    async def test_challenge_survives_ladder_returns_error_and_no_cache(self, monkeypatch, isolated_cache):
        async def fake_bytes(url, ua, proxy=None):
            return self._CAPTCHA_HTML, 200, None, "text/html"

        async def fake_impersonated(url, proxy=None):
            return self._CAPTCHA_HTML.decode(), 200  # curl_cffi also hits the wall

        async def fake_jina(url, ua, proxy=None):
            return ""

        async def fake_mirror(src, key, ua, proxy):
            return None  # no open-access copy

        monkeypatch.setattr(net, "fetch_bytes_with_meta", fake_bytes)
        monkeypatch.setattr(net, "fetch_impersonated", fake_impersonated)
        monkeypatch.setattr(net, "fetch_jina", fake_jina)
        monkeypatch.setattr(dispatch, "_try_mirror_for_url", fake_mirror)
        monkeypatch.setattr(images, "localize_html_images", _noop_localise)

        url = "https://www.sciencedirect.com/science/article/pii/S0306987718301051"
        result = await dispatch._html_result(url, url, False, "ua", None)

        assert "error" in result, "a surviving challenge must be an error, not a success"
        assert result["body"] == ""
        assert "challenge" in result["error"].lower()
        # The poison body must NOT have been written to the cache.
        assert not list(isolated_cache.rglob("*.md")), "challenge body must never be cached"

    async def test_rich_content_after_challenge_is_cached_ok(self, monkeypatch, isolated_cache):
        async def fake_bytes(url, ua, proxy=None):
            return self._RICH_HTML, 200, None, "text/html"

        monkeypatch.setattr(net, "fetch_bytes_with_meta", fake_bytes)
        monkeypatch.setattr(images, "localize_html_images", _noop_localise)

        url = "https://example.com/real-article"
        result = await dispatch._html_result(url, url, False, "ua", None)

        assert "error" not in result, result.get("error")
        assert result["method"] == "local-trafilatura"
        assert list(isolated_cache.rglob("*.md")), "genuine content must be cached"
