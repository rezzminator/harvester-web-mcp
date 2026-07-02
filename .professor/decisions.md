# Professor Decisions

What makes this install different from the upstream blueprint. Machine state lives in
`manifest.json`; this file is for humans. Updated at install and on every `/pcm` change.

## Install profile

- **Project:** harvester — multi-format MCP fetch + search server (Python, single project)
- **Scope:** partial — `/pcm` + `/quality:prompt` only, not the full `/build` pipeline
- **Character:** none at session level (plain CLAUDE.md); Dr. House overlay loads only inside `/pcm`
- **Sacred ground:** secrets / credential files, the SSRF (`assert_fetchable`) + LFI (`deny_reason`) guards, stdout-is-JSON-RPC
- **Installed:** `/pcm`, `/quality:prompt`, `.claude/output-styles/dr-house.md`, `pcm-guard.sh` (PreToolUse), `format-md.sh` (PostToolUse), a Stop hook that clears the gate marker
- **Not installed:** full pipeline (`/build`, `/wave`, `/jc`, agents, worktrees), `/quality:doc`, blueprint-bus (`/pcm:update`, `/pcm:release`), statusline, notifications, Codex layer
- **Installed from:** Professor v0.40.0

## How the guard works

Editing `.claude/**` or `CLAUDE.md` outside `/pcm` is denied by `pcm-guard.sh`. To change framework
files: run `/pcm` (it stamps `tmp/professor_pcm_active`, opening the gate for its edit pass); the Stop
hook clears the marker at turn end (600s TTL as a fallback). Product code under `src/`, `tests/`,
`docs/` is unaffected.

## Post-install customizations

_None yet. `/pcm` appends here whenever it changes the framework. Per-change divergences from the
blueprint are logged in `drift.md`._
