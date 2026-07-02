# Harvester redesign — implementation slices (Sonnet-executed)

Companion to `harvester-redesign.md`. Each slice is one Sonnet agent, independently shippable,
green tests before handoff. Contracts held sacred throughout: **non-raising `dispatch`/`net`
boundaries**, **no security-guard weakening**, **no shadow libraries**. Every slice ends with
`uv run pytest -q`, `uv run ruff check .`, `uv run pyright src/` clean.

Sequencing rule: 1 → 2 gate the rest (2 is the engine everything plugs into). 3–7 are parallel
after 2. 8 is cleanup, last.

---

## Slice 1 — Truth pass (model-facing strings + header). No behavior change.

**Files:** `server.py`, `describe.py`, `dispatch.py` (error text only), `cache.py` (frontmatter read), `README.md`, `tests/test_describe.py`, `tests/test_server.py`

- Kill "summarised" / "FULL content" contradictions; rewrite truncation note to point at the cache file + char offset; state searchCache doesn't return text.
- Archive listing teaches `archive(source=…, member=…)`, not the `::` form fetch refuses.
- Document PMID/PMCID inputs in the `fetch` description; fix empty-input error that advertises `title:"…"`.
- Result header gains `tokens` + `fetched_at` (already in frontmatter); `searchCache` output gains `md_path` per hit.
- Consolidate `dispatch._net_error` + `describe.py:68-79` into one helper, one voice.
- `fetch` prompt path uses `describe_fetch_result` rendering + `media="deny"` (align with the tool).
- Every terminal error names the next tool/arg; neg-cache annotation includes retry-after seconds.
  **Done when:** new assertions pin each rewritten string; no dead-end error remains without a named next move.

## Slice 2 — Rescue-graph engine (the core refactor).

**Files:** new `dispatch` internals (outcome classifier + `Attempt` trace + dispatch table), `cache.py` (write trace/aliases to frontmatter), `describe.py` (render `rungs:`), `tests/test_dispatch_hardening.py`

- Outcome enum: `ok|thin|challenge|http_4xx_with_body|wrong_kind|empty_convert|dead_net|not_found_page`.
- `Attempt(rung,target,outcome)` list threaded through `_html_result`/`_doc_result`/candidate paths; written to frontmatter; embedded in ladder-exhausted error text.
- Apply R1–R7 corrections: keep ≥400 bodies (R1); `.pdf`-serves-HTML → meta-scrape + mirror (R2); empty-convert PDF → mirror pivot (R3); meta-scrape reuses curl_cffi text (R4); sniff zip/epub in candidate text branch, never cache garbage (R5); thin-404 → error, neg-cached, no artifact (R6); transient error kinds get short TTL + DOI-form-canonical neg-cache key (R7).
  **Done when:** each R# has a regression test; a dead URL's error lists the rungs it walked.

## Slice 3 — Scholarly sources (`oa.py` + `mirror.py`).

- New resolvers: Zenodo, Fatcat, DataCite (routes 10.5281-class), OpenAIRE Graph (landing hop).
- Rung 0: fetch doi.org landing page through the HTML ladder + `extract_meta_links` before declaring dead.
- PMID→DOI pivot (read `records[0]["doi"]`); arXiv-ID regex recognition in `_dispatch_one`.
- Wire the already-written `mirror.europepmc_fulltext_xml` into `_pmcid_to_result`.
- Chapter-DOI (`10.1007/978-…`) → extract ISBN → OAPEN/DOAB.
  **Done when:** hermetic FakeClient tests per source; DataCite DOI no longer emits "no free copy exists".

## Slice 4 — Web-archive sub-ladder (`mirror.py` + dispatch).

- Wayback CDX iteration (status:200 filter) when "closest" is a challenge/redirect capture.
- archive.today timegate (read-only), flag `HARVESTER_ARCHIVE_TODAY` (default on).
- DOI inputs archived by resolved publisher URL, not `doi.org/…`.
- Docs: Jina reframed as JS-render rung, not wall-bypass.
  **Done when:** CDX-iteration test; archive.today mocked; no live-network in the suite.

## Slice 5 — Books (`oa.py`).

- ISBN→title fallback via the Open Library call already made → Gutendex title search.
- Wikisource (Validated-only) + Standard Ebooks (OPDS feed) title resolvers.
- Truthful ISBN error (names only sources queried; states Google-Books keyless skip).
  **Done when:** PD-classic-under-modern-ISBN test passes; error text asserted truthful.

## Slice 6 — Formats & params.

- EPUB (MarkItDown), OLE2 legacy Office (`D0CF11E0` → Docling); generalize `_is_not_found_page`.
- `fresh: bool` / `max_age_s` on `fetch` (cache-bypass); `offset/limit` chars (remote-client tails).
  **Done when:** epub + .doc convert; `fresh` re-fetches; `offset` returns a slice.

## Slice 7 — Search fallbacks (`search.py`).

- DDG-lite (keyless), Mojeek (`MOJEEK_API_KEY`), Marginalia (`MARGINALIA_API_KEY`) behind the existing `(results, backend)` contract.
  **Done when:** each backend mocked; chain-order test green.

## Slice 8 — Hardening & hygiene.

- Delete `images.py` + its 9 stale monkeypatches (or gate behind `localize_images` flag — default delete); fix README ladder §1.
- Add tests: curl_cffi redirect SSRF loop, `_stream_capped` 50MB cap, converter stdout-purity, `.rar` fixtures, `search_cache()`, e2e MCP drives for the 4 untested tools + prompt, decimal/octal IP forms, `pmid_to_pmcid` body.
- Widen `_PMID_RE` to `\d{1,9}`; `ftp://` known-mirror HTTPS rewrite.
  **Done when:** coverage gaps from the inventory closed; suite green.

---

### Dispatch protocol

- One Sonnet agent per slice, `isolation: worktree` for 3–7 (parallel, avoid file conflicts).
- Each agent gets: this file + `harvester-redesign.md` + CLAUDE.md conventions + "green all three
  checks before returning; report the diff summary + any contract you had to bend."
- Review each returned diff before merging the worktree; run the full suite on the integrated tree.
