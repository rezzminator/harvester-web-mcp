# Drift — local customizations diverging from the Professor blueprint

This is a **scoped install**: only the `/pcm` change-manager and the `/quality:prompt` discipline
were pulled from Professor, not the full `/build` pipeline. Every line below is a deliberate
divergence the update merge must treat as KEEP-LOCAL.

- pcm — adapted to a single-project, no-pipeline install: dropped the System-Wiring pipeline map, the agent/gitter/wave/worktree/port/epic invariants, the inventory-count block, and the `/pcm:update`·`/pcm:release` blueprint-bus section. Ownership narrowed to `.claude/`, `CLAUDE.md`, commands, skills, output-styles, scripts, settings. Audit scoped to commands · skills · scripts · structure · cross-refs.
- quality:prompt — dropped the `/km` knowledge-file content (no knowledge base here). Sacred ground reset to harvester's: secrets / credential files, the SSRF (`assert_fetchable`) and LFI (`deny_reason`) guards, and stdout-is-JSON-RPC.
- CLAUDE.md — authored plain, with no session persona (install choice). Carries the security sacred ground and the `/pcm` framework-routing obligation; no command/skill roster (self-indexed).
- dr-house — `/pcm` persona overlay; blueprint placeholders bound to harvester (founder, users, sacred ground).
- Not installed — the full pipeline (`/build`, `/wave`, `/jc`, agents, worktrees, ports), `/quality:doc`, and the blueprint-bus subcommands. Re-run the Professor installer if you later want any of these.
- settings.local — PreToolUse deny hook on WebFetch (mirrors host-ops), redirects to mcp__harvester__fetch with `sources` param wording
