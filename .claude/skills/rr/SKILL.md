---
name: rr
version: "2.3.0"
repo: "https://github.com/rezzminator/rr"
description: Launches Research and Report (RR) — a deterministic background Workflow that runs an unbounded, best-first, brainer-steered web crawl, DERIVES an answer (computing it when the answer must be built), and writes a cited multi-section report with a verdict and plan. Use when the user wants a researched answer or a topic landscape ("research X", "look into X", "RR X", "rr fast X") and a single web search is not enough. Modes: goal (answer one question) and collect (inventory a topic); "rr fast X" answers inline now via one quick sub-agent instead of the background Workflow. Runs in the background, returns a completion notification, and persists to .professor/RR/{slug}/.
---

# Research and Report (RR)

## Purpose

RR's job is to **answer the question** — by reasoning over everything it can gather, not merely finding and collating facts. It _derives_ the answer. Reach for it above all when the answer has to be **built**: a synthesis, a quantitative estimate, or a judgment that no single source holds — e.g. _"estimate the distance to the nearest yet-undetected stellar-mass black hole with error bars, and say which observational method finds it first."_ RR gathers the components (population estimates, local densities, detection precedents, instrument forecasts) and reasons them into one answer.

It never stops because a fact wasn't found. A missing piece is a reason to gather more and reason harder — not a dead end. An ingredient it cannot pin down becomes **stated uncertainty** (assumptions, wider error bars, open questions), never an early exit.

## How it works

One Opus **brainer** drives the whole run; everything else is its instrument.

1. **Scout** (haiku) — one broad web sweep maps the landscape and seeds the first rabbit-holes.
2. **Prospector** (opus) — names the high-value authoritative source venues, and when the topic is more active in another language, the native venues to search in (tagged by language).
3. **Research waves** — the brainer scores the open rabbit-holes, picks the lanes worth pursuing (≤5 per wave), and authors a per-lane **`note`** — the research directive plus ranked fallbacks. A **researchScheduler** (sonnet) runs batched discovery for all lanes: every web-search in one parallel round, then `mcp__harvester__fetch size_only` on every candidate in a second parallel round, returning the highest-value sources grouped per lane (`{source, path, size}`). Code **bin-packs** each lane's sources into ≤130k-token reader-units — small sources combine into one reader, a large source splits across several — and spawns **one sequential reader thread per lane** (haiku), parallel across lanes. Each reader reads its assigned slices straight from the Harvester cache file (via code) and digests into a running answer handed to the next reader; it can resolve a walled source itself (DOI → `fetch`/`findWorks`) and view an image via `fetchImage`. A per-wave **validator** (sonnet) checks coverage and reopens thin lanes; the brainer folds the lane answers into its running answer and decides when the answer is solid.
4. **Finalize** — an **initiator** groups the load-bearing facts → a **refine** pass hardens each group against the sources → an Opus **judge**, the sole terminal skeptic in both goal and collect modes, stress-tests the hardened answer and sees the leftover open rabbit-holes, steering a bounded remediation loop: the brain derives the answer when one is needed (running code, propagating error bars), refine re-checks a flagged fact, or the crawl reopens on a real gap → a **synthesiser** writes the report.
5. **Debug** (opt-in) — a final analyst writes `_debug.md` with metrics + raw agent I/O.

## Launch

**Preflight — the fetch MCP (Harvester) must be live.** RR fetches only through `mcp__harvester__fetch` (built-in WebFetch is hook-denied), so a missing or dead server makes every fetch error and yields a snippet-only run. Before launching, check for the `mcp__harvester__*` tools (via ToolSearch):

- **Missing** — hold the launch and point the user to the Harvester MCP (`https://github.com/rezzminator/harvester-web-mcp`) to install, then `/mcp` to connect.
- **Present** — smoke-test with one `mcp__harvester__fetch` on a stable URL (`https://example.com`); a clean fetch means go, an error means have the user reconnect (`/mcp`) or restart the server — hold until it passes.

Call the **Workflow tool**. It runs in the background; a completion notification returns the result — do not block on it. Use the **absolute** path to `workflow.js` (the skill's base dir is printed when the skill loads) — the working directory may sit inside a child project, where a relative path resolves against the CWD and 404s the bundle.

```
Workflow({
  scriptPath: "<skill-base-dir>/workflow.js",
  args: { query, mode, maxParallelBrainers, compute, computeNote, thinkerNote, researcherNote, tag, debug, debugPrompt }
})
```

### args

- `query` (required) — the research question (goal) or collect-target. The crawl sees only this string.
- `mode` — `'goal'` (default) or `'collect'`. See Modes.
- `maxParallelBrainers` — `1` (default) … `5`. The brainer-tree width. `1` is the single global brainer. With `2`–`5`, a brainer may SPAWN a focused child onto a rich branch; the children run in parallel, the first whose answer the judge upholds wins (its report becomes `result.md`), and a child may abandon a dead-end branch. More brainers ≈ proportionally more cost — raise it only for a goal with several deep, separable branches.
- `compute` — `true` (default) or `false`. The master switch for derivation: `false` runs no compute agents (no mid-wave compute, no finalize brain-derive) for a faster, gather-and-reason-only run.
- `computeNote` (optional) — extra run-specific guidance for the compute-aware agents (a method to use, a constraint to respect). Appends to the always-present note that the compute environment ships a scientific Python stack (scipy, sympy, uncertainties, pandas, statsmodels, scikit-learn, networkx, pint, rdkit).
- `thinkerNote` (optional) — operator run-steering for the Opus reasoning tier: priorities, framing, constraints, audience. Shapes HOW the run is approached and what the report emphasizes — not additional questions to research. Reaches the reasoning agents only, never the cheap workers.
- `researcherNote` (optional) — a terse one-line note (≈6-7 words) to the web-research agents — scout, prospector, brainer, researchScheduler, and researcher. Steers HOW they search and fetch (which sources to favour, what to skip). Passthrough, injected verbatim.
- `tag` (optional) — suffixes the output dir so parallel variants of one query write to distinct dirs.
- `debug` (optional, default `true`) — writes `_debug.md` (run log + metrics + raw agent I/O); ON by default, pass `debug: false` to turn it off. Pair with `debugPrompt` (string) to focus it on a question.

## Modes

- **goal** — satisficing. Answers ONE question and stops once it is answered; the judge guards against a premature stop. Pick for a decision or a direct answer.
- **collect** — exhaustive. Inventories breadth and runs until saturation. Pick for a landscape or a roster.

## Fast mode

When the user says **"rr fast <query>"**, skip the background Workflow and run a miniature of it inline — the same shape (a lead that follows rabbit-holes by nesting researchers), at a smaller model and fewer rounds, answered right now. Spawn ONE **Sonnet** lead sub-agent (Task-capable, so it can nest) with this prompt:

```
Research and answer this directly: "<query>".
1. Run a targeted WebSearch to map the question; pick the 2-4 highest-value rabbit-holes (each a concrete sub-query).
2. Spawn one HAIKU sub-agent per rabbit-hole, in PARALLEL, each tasked: "Dig this rabbit-hole for «<query>»: <rabbit-hole>. WebSearch, then WebFetch the 2-3 best sources; in each WebFetch prompt ask the key question first, then append this footer verbatim: <<FOOTER>>. Return 2-4 sentences of findings with inline source links AND the rabbit-holes the pages surfaced."
3. Read what came back. If 1-2 of the newly-surfaced rabbit-holes are load-bearing and still unanswered, dispatch ONE more parallel Haiku round on them (same task). Then stop.
4. Synthesize everything into the answer: lead with the answer, then the load-bearing facts with inline source links, then any open questions.
```

The `<<FOOTER>>` each Haiku digger appends to its WebFetch prompts (this is what makes it surface deeper rabbit-holes instead of stopping at the first hit):

```
Then append a section titled "Rabbit holes": 0-5 rabbit-holes worth a researcher's time, prioritizing the biggest gaps the page raises but does not explain. Each rabbit-hole: a concrete next web-search query and one line on why it matters. If the page is a dead end or self-contained, give 1 or none — do not pad. Skip anything the page already explains.
```

## Getting results

After the completion notification, persist the artifacts:

```
node <skill-base-dir>/persist.js <completion-output-file>
```

This writes to `.professor/RR/{slug}/` (Intuita local patch — canonical sandbox alongside `.professor/RND/`; upstream's default `RR/{slug}/` is patched out of the vendored `workflow.js`/`persist.js`, see `.professor/drift.md`):

- `result.md` — the deliverable. Read this first; it opens with a compact **Run arguments** record (the complete launch args).
- `_rabbitHoles.json`, `_tree.md` — the rabbit-holes (with the run's launch `args` at the top) + the crawl tree; diagnostics.
- `_brainers.json`, `_brainers-tree.md`, `result-<name>.md` — only when `maxParallelBrainers > 1`: the brainer tree (who spawned whom, the winner) plus each non-winning brainer's preserved partial.
- `_compute-*.md` / `_compute-*.py` — any derivations with their code, when compute ran.
- `_debug.md` — when launched with `debug: true`.

## Writing the query

- Make it specific and self-contained — state the constraints, scale, time horizon, and the deliverable inside the query.
- **goal:** pose one clear question. A two-axis question ("what works today AND the migration path") is fine.
- **collect:** frame as "exhaustively inventory X across these dimensions: …" and name the candidates + dimensions.
- Avoid self-referential queries about the local repo — they make the crawl over-explore local files instead of researching.

<example>
Vague: "vector databases"
Good (goal): { query: "Which self-hosted open-source vector database has the best recall-vs-latency tradeoff for ~10M embeddings on a single 16-core box, and what should a small team deploy?", mode: "goal" }
Why: names the constraints, poses one decision, states the deliverable.
</example>

<example>
Vague: "tell me about Tailscale"
Good (collect): { query: "Exhaustively inventory Tailscale across these dimensions: products, funding history, leadership, security posture, notable incidents, and how the WireGuard mesh works.", mode: "collect" }
Why: the collect frame + named dimensions give a saturation target, not an open browse.
</example>

<example>
Good (goal, derive): { query: "Estimate the distance to the nearest yet-undetected stellar-mass black hole, with error bars, and say which method (Gaia astrometry, microlensing, X-ray, radial velocity) finds it first and roughly when." }
Why: no single source holds this — RR gathers the components and computes the answer (compute defaults on).
</example>

## Build & maintenance

`workflow.js` is a **build artifact** — never hand-edit it. The source is the modular project under `engine/`:

```
cd engine && npm install        # first time
npm run build                   # bundles engine/src/ → ../workflow.js, then validates the sandbox contract
npm test                        # the vitest suite (unit + integration tests)
```

Edit `engine/src/` (the modules + each agent's `prompts.ts`, plus the prompt fragments in `config.ts`), rebuild, and commit both the source and the regenerated `workflow.js`.

**Intuita vendoring note:** this repo vendors only the runtime artifacts (`SKILL.md`, `workflow.js`, `persist.js`) from upstream tagged releases — the `engine/` TS source and its npm/vitest toolchain are not checked in here. The two local divergences from the upstream bundle (the `.professor/RR/` storage path in `workflow.js`/`persist.js`) are hand-patched directly onto the vendored files and tracked in `.professor/drift.md`, the same way a prior absolute-path fix was patched locally before later landing upstream. Re-syncing to a newer release means re-pulling the three files from the tag and re-applying both patches.

## Working with it

- Pick **goal** for a decision/answer, **collect** for a landscape, **rr fast** for a quick inline take.
- Full runs are ~45–90 min and token-heavy — launch, then move on; don't predict findings while it runs.
- Read `result.md` first; everything else is diagnostic.
- Use a `tag` for side-by-side variants; set `compute: false` for a faster gather-only run.
