# Harvester redesign — the no-terminal-404 plan

**Inputs:** full source read (all 16 modules), failure-surface audit (code-verified, line-anchored),
model-ergonomics audit (28 error strings inventoried), test-coverage inventory (387 tests mapped),
and five RR-fast research runs (scholarly OA APIs, books/ISBN, dead-URL recovery, preprint/repo
APIs, search fallbacks) — all 2026-07-02.

---

## 0. Verdict on the current design

**Keep — these are right and rare:**

- Strict layering with **non-raising boundaries** (`dispatch`/`net` return `{"error": …}`, never raise).
- The **two-step Candidate model** in `oa.py` — research confirmed the whole 2026 OA landscape is
  metadata-API → full-text-URL → fetch; harvester's architecture already matches it exactly.
- Security chokepoints (`assert_fetchable` per-hop, `deny_reason`, `safe_archive`) — sacred, untouched.
- Type-partitioned cache, DOI-keyed dedup, negative cache + in-flight dedup, token over-counting.

**Broken — the three structural defects everything below fixes:**

1. **Rescue logic is scattered and asymmetric.** Whether a failure gets rescued depends on _which
   branch_ it happens to die in, not on what the failure _is_: the zero-bytes PDF branch pivots to
   the mirror, the wrong-content branch four lines away doesn't; thin-HTML walks the ladder, but a
   ≥400 body is discarded before extraction; challenge errors are neg-cached, thin-404s aren't.
2. **The model-facing surface contradicts the behavior.** "FULL content" vs 50k truncation;
   "summarised" (never happens); a truncation note whose both suggested recoveries fail; an archive
   listing teaching syntax `fetch` refuses; PMID/PMCID accepted but undocumented.
3. **Coverage gaps are misreported as nonexistence.** "No free, legal full text exists" is emitted
   for DataCite DOIs (Zenodo — free by construction), for ISBNs where Gutenberg was never actually
   checked, and for DOIs whose open landing page was simply never fetched.

---

## 1. The core design change: the **rescue graph** (novelty #1)

Replace the implicit, branch-local fallback logic with one declarative engine in `dispatch.py`:

- **Classify every fetch outcome** into one enum: `ok | thin | challenge | http_4xx_with_body |
wrong_kind | empty_convert | dead_net (timeout/dns/connect) | not_found_page`.
- **Map (input-class × outcome) → ordered rescue moves.** Input classes: `doi | datacite_doi |
arxiv_id | pmid | pmcid | isbn | chapter_doi | url_html | url_doc | url_image | url_archive |
local`. Rescue moves are small async functions with one contract (return result-dict or None),
  exactly like today's `_candidate_to_result`.
- **Record an `Attempt` trace** — every rung appends `(rung, target, outcome)`. The trace is:
  written into cache frontmatter, shown in the result header, and embedded in error text
  ("after direct, chrome-impersonation, jina, oa-mirror(7 sources), wayback — re-fetching will
  not help; use `search`"). No silent caps, no wasted model turns re-asking for rungs already run.

This is one refactor that structurally eliminates the "this branch forgot to pivot" class of bug,
makes every new source a one-line graph entry, and turns errors into instructions.

### Immediate rescue-graph corrections (from the audit, all code-verified)

| #   | Defect                                                                          | Fix                                                                                                                     |
| --- | ------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| R1  | ≥400 response bodies discarded (`net.py:128`)                                   | Keep the body; a soft-404/403-with-content still goes through extraction + meta-scrape                                  |
| R2  | `.pdf`-serves-HTML terminal (`dispatch.py:303`)                                 | Run `oa.extract_meta_links` on the bytes in hand + `_try_mirror_for_url`                                                |
| R3  | Empty-convert PDF terminal (`dispatch.py:326`)                                  | Pivot to mirror before erroring; error keeps the operator OCR hint but gives the _model_ a next move                    |
| R4  | Meta-scrape re-fetches blocked page with plain httpx (`dispatch.py:749`)        | Reuse the curl_cffi text already in scope; else `fetch_impersonated`                                                    |
| R5  | OA candidate serving zip/epub cached as garbage success (`dispatch.py:561-574`) | Sniff archive/epub magic in the text branch → convert or reject, never cache                                            |
| R6  | Thin-404 writes empty artifact + not neg-cached (`dispatch.py:262`)             | Classify as error → neg-cached symmetrically, no artifact                                                               |
| R7  | Neg-cache blocks transient recovery for 120s                                    | `timeout/connect/429` get short TTL (15s default, `HARVESTER_NEG_TTL_TRANSIENT`); neg-cache key canonicalizes DOI forms |

---

## 2. New sources (the RR-verified matrix)

### 2.1 Scholarly chain (`oa.py` — new per-source resolvers, priority after existing seven)

| Source                            | Endpoint                                                               | Auth                               | Why                                                                                                                |
| --------------------------------- | ---------------------------------------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| **Zenodo**                        | `zenodo.org/api/records/?q=doi:…` → `/records/{id}/files/{f}/content`  | keyless, 60/min                    | Direct file URLs; 10M+ deposits; rescues the DataCite class                                                        |
| **Fatcat (IA Scholar)**           | `api.fatcat.wiki/v0/release/lookup?doi=…&expand=files`                 | keyless                            | Preserved copy of record — rescues dead/rotted PDF links, also by arXiv ID                                         |
| **DataCite**                      | `api.datacite.org/dois/{doi}` → `attributes.url`                       | keyless                            | Routes 10.5281-class prefixes; landing URL feeds the HTML ladder                                                   |
| **OpenAIRE Graph**                | new Graph API (legacy sunsets 2026-05-31)                              | keyless 60/hr, free token 7,200/hr | EU + non-English discovery hop → landing URL into ladder                                                           |
| **Rung 0 — doi.org landing page** | follow redirect through the HTML ladder + `extract_meta_links`         | —                                  | Fixes bronze OA, week-old DOIs, diamond journals; also gives the _resolved publisher URL_ Wayback actually indexes |
| **PMID → DOI pivot**              | `records[0]["doi"]` already in the idconv JSON (`mirror.py:79-84`)     | —                                  | One field read unlocks the whole chain for ~60% of PubMed                                                          |
| **arXiv ID recognition**          | regex `\d{4}\.\d{4,5}(v\d+)?` + legacy `cs/9901002` in `_dispatch_one` | —                                  | `1706.03762` currently returns "local file not found"                                                              |
| **Europe PMC full-text XML**      | `mirror.europepmc_fulltext_xml` — already written, zero callers        | —                                  | Wire into `_pmcid_to_result` as rung 3 (EBI host, rarely blocked)                                                  |
| **Chapter-DOI → ISBN**            | extract ISBN embedded in `10.1007/978-…`-style DOIs → OAPEN/DOAB       | —                                  | OA book chapters currently die in the article chain                                                                |

Verified exclusions (don't build): CiteSeerX (dead), BASE (IP-whitelist gate), Lens/Dimensions
(paid), OpenCitations (citation-only), SciELO ArticleMeta direct (no DOI lookup, PDF-link bug —
reach LatAm via OpenAIRE/CORE instead), HAL direct (metadata-only — same), bioRxiv API (S3-bulk
only; their PDF URLs already resolve via the URL ladder), SSRN/NBER/RePEc/CyberLeninka (no
programmatic full text).

### 2.2 Books (`resolve_book`)

- **ISBN → title fallback**: the Open Library call already made (`oa.py:602`) returns
  title/author → Gutendex **title** search. Rescues every PD classic reissued under a modern ISBN.
- **Wikisource** (title): MediaWiki API; accept only ProofreadPage `Validated` works.
- **Standard Ebooks** (title): OPDS feed (~900 works, 100% full text); cache the feed daily.
- **NCBI Bookshelf** (title/PMID, biomedical monographs) — optional, niche.
- **Truthful error text**: name only sources actually queried; Google Books keyless-skip stated.
- Verified exclusions: HathiTrust (Data API retired 2024-07-17 — Bibliographic API is
  rights-metadata only; usable later as a PD-confirmation signal, never as a text source),
  OpenStax/OTL/Runeberg (no queryable API).

### 2.3 Web-archive sub-ladder (replaces the single Wayback-availability call)

1. **Wayback Availability** (current — keep as cheap first probe).
2. **Wayback CDX** — `web.archive.org/cdx/search/cdx?url=…&filter=statuscode:200&…`: iterate
   snapshots when "closest" is a challenge/redirect capture (today: discarded → terminal).
   ~60 req/min budget; polite pacing.
3. **archive.today timegate** — `archive.ph/newest/{url}`, read-only (capture-triggering is
   CAPTCHA-gated; never attempt). Deliberately holds paywalled-news snapshots Wayback lacks.
   Flag-gated: `HARVESTER_ARCHIVE_TODAY` (default on).

- For DOI inputs, query archives with the **resolved publisher URL** (rung 0), not `doi.org/…`.
- Verified dead, never add: Memento TimeTravel (DNS gone), Google/Bing cache, 12ft. Common Crawl:
  batch-only staleness + IP bans — excluded from the live ladder.
- Docs correction: Jina is a JS-rendering rung, not a wall-bypass (respects robots.txt, fails
  silently on Cloudflare; keyless ≈20 RPM — batch fetches must not assume it fires for every item).

### 2.4 Formats

- **EPUB**: detect (`PK` + mimetype or `.epub`) → MarkItDown converts epub already. Today it
  dead-ends as "use the archive tool".
- **OLE2 legacy Office** (`.doc/.rtf/.odt/.xls/.ppt`): magic `D0 CF 11 E0` → Docling/MarkItDown.
- `_is_not_found_page`: generalize beyond arXiv with length-gated common phrases.

### 2.5 Search (`search.py`)

Chain becomes: SearXNG → Brave → **DuckDuckGo lite** (keyless, fragile — last-resort) with
optional keyed rungs **Mojeek** (`MOJEEK_API_KEY`) and **Marginalia** (`MARGINALIA_API_KEY`,
non-SEO indie web — genuinely complementary). Exa/Tavily noted as operator options; Startpage /
Yandex / Kagi / SearchApi excluded (no API / geo-locked / no free tier).

---

## 3. Identity-first fetching (novelty #2)

Normalize every input to a **WorkKey** early in `_dispatch_one`:
`{kind: doi|isbn|pmid|pmcid|arxiv|url|path, value, aliases[]}`.

- Cross-identifier pivots become graph edges (PMID→DOI, DOI→PMCID, chapter-DOI→ISBN,
  URL→citation_doi) — resolved once, cached in frontmatter (`aliases:`), so the _positive and
  negative caches unify across input forms_ (today `10.x/y`, `doi:10.x/y`, and the doi.org URL
  neg-cache separately and re-run the failed chain three times).
- The wrong-document guard generalizes: after any identifier-driven resolve, check
  `_similar(requested_title, artifact_title) ` when a title is known — extending the existing
  similarity gate from title-search to all resolves.

---

## 4. Model-facing surface (the "prompts" of the MCP)

String/renderer fixes (all verified against current text):

1. **Truncation truth**: kill "summarised" and "FULL content"; describe truncation + cache path.
   Truncation note rewrite: "COMPLETE text at {path} — read that file from char {cap}; searchCache
   locates which cached pages match, it does not return text."
2. **Archive listing**: teach `archive(source=…, member=…)`, not the `::` form `fetch` refuses.
3. **Document PMID/PMCID (and new arXiv-ID) inputs** in the `fetch` description; fix the
   empty-input error that advertises `title:"…"` (always errors).
4. **Result header**: add `tokens` and `fetched_at` (both already computed, sitting in
   frontmatter) + `rungs:` trace. `searchCache` output: add `md_path` per hit.
5. **Errors as instructions**: every terminal error names the next tool/argument; negative-cache
   annotation includes retry-after seconds; ladder-exhausted errors enumerate the rungs run.
6. **Consolidate error derivation** — `dispatch._net_error` and `describe.py:68-79` are parallel
   and drifting; one helper, one voice.
7. **`fetch` prompt path**: use `describe_fetch_result` rendering (currently loses status/kind
   diagnostics and runs `media="allow"` — align with the tool).

New parameters (the only schema changes):

- `fresh: bool` (or `max_age_s: int`) on `fetch` — today successful cache hits are permanent;
  a model cannot refresh a changing page or even detect staleness.
- `offset/limit` (chars) on `fetch` — remote MCP clients cannot "slice the cached path from
  disk"; tails of >50k-char documents are currently unreachable for them.

---

## 5. Hygiene (from the coverage inventory)

- **Delete `images.py`** (109 lines, unreachable from any tool since the media split) and the 9
  stale monkeypatches that still pretend it's on the fetch path — or explicitly re-wire it behind
  a `fetch(localize_images=true)` flag. Current half-state misleads. README ladder §1 still
  describes the old behavior — fix.
- **Tests to add (highest risk first):** curl_cffi redirect-loop SSRF recheck (the security
  property has zero tests); `_stream_capped` 50MB cap with real bytes; converter smoke tests
  incl. a **stdout-purity test** (assert nothing writes to fd1 during convert — protects the
  JSON-RPC channel); `.rar` fixture suite; `search_cache()` unit tests; end-to-end MCP drives for
  `search`/`fetchImage`/`archive`/`searchCache` + the `fetch` prompt; decimal/octal obfuscated-IP
  forms; `pmid_to_pmcid` real body.
- Widen `_PMID_RE` to `\d{1,9}` (pre-1977 PMIDs); `ftp://` known-mirror HTTPS rewrite (NCBI/NOAA).

## 6. Env additions

`HARVESTER_NEG_TTL_TRANSIENT` (15) · `HARVESTER_ARCHIVE_TODAY` (on) · `HARVESTER_FRESH_DEFAULT`
(off) · `MOJEEK_API_KEY` · `MARGINALIA_API_KEY` · (existing keys unchanged; CORE_API_KEY gets a
README callout as the single highest-leverage optional key — 49M full-text aggregation).

## 7. Sequencing — eight Sonnet-sized batches, each independently shippable

1. **Truth pass** — all §4 string/renderer fixes + header enrichment + error consolidation + tests.
2. **Rescue-graph engine** — outcome classifier + Attempt trace + R1–R7 corrections in dispatch.
3. **Scholarly sources** — Zenodo, Fatcat, DataCite, OpenAIRE, PMID→DOI, arXiv-ID, EPMC-XML wiring.
4. **Archive sub-ladder** — Wayback CDX, archive.today timegate, resolved-URL archiving, neg-cache policy.
5. **Books** — ISBN→title chain, chapter-DOI→ISBN, Wikisource + Standard Ebooks, truthful errors.
6. **Formats & params** — EPUB, OLE2, `fresh`, `offset/limit`, prompt-path alignment.
7. **Search fallbacks** — DDG-lite, Mojeek, Marginalia behind the existing `(results, backend)` contract.
8. **Hardening** — the §5 test list, images.py removal, README truth pass.

Every batch keeps the two contracts sacred: non-raising boundaries, and no security-guard
weakening to make a fetch succeed. No shadow libraries — the legality gate stays.
