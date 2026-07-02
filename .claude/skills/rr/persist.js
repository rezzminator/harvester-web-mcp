#!/usr/bin/env node
/**
 * persist.js — byte-exact persister for an RR workflow run.
 *
 * workflow.js runs in a sandbox with NO filesystem access, so it RETURNS its artifacts in
 * `result.files` (a { filename: content } map) instead of writing them. This script runs on
 * the HOST after the workflow completes, reads the run's output JSON, and writes every file
 * verbatim to {repo-root}/.professor/RR/{slug}/ (Intuita local patch — canonical sandbox, see
 * .professor/drift.md). No model, no re-reading — a deterministic parse-and-write
 * (the `cp` equivalent for files that are JSON-embedded rather than loose on disk).
 *
 * Usage:  node persist.js <run-output-file.json>
 * The main chat calls this at the end of every RR run with the completion notification's
 * <output-file> path. Prints a JSON summary: { dir, written, files[], verdict, confidence }.
 */
const fs = require('fs')
const path = require('path')
const { execSync } = require('child_process')

const outFile = process.argv[2]
if (!outFile) { console.error('usage: node persist.js <run-output-file>'); process.exit(1) }

let raw
try { raw = JSON.parse(fs.readFileSync(outFile, 'utf8')) }
catch (e) { console.error('cannot parse run output: ' + e.message); process.exit(1) }

const res = raw && raw.result ? raw.result : raw          // the workflow output is wrapped { …, result }
const files = (res && res.files) || {}
if (!Object.keys(files).length) { console.error('no files in run output (result.files empty)'); process.exit(1) }

const repoRoot = execSync('git rev-parse --show-toplevel', { cwd: __dirname }).toString().trim()
// Intuita local patch — canonical sandbox is .professor/RR/ (see .professor/drift.md), not the legacy root /RR/.
// The workflow.js DIR patch always emits a clean '.professor/RR/{slug}' here; guard against path traversal only.
const safeDir = res.dir && !res.dir.includes('..') ? res.dir : ('.professor/RR/' + (res.slug || 'run'))
const dir = path.join(repoRoot, safeDir)                   // {repo-root}/.professor/RR/{slug}
fs.mkdirSync(dir, { recursive: true })

const written = []
for (const [name, content] of Object.entries(files)) {
  const filePath = path.join(dir, name)
  fs.mkdirSync(path.dirname(filePath), { recursive: true }) // namespaced keys (multi-brainer: root/wave-0.md, b1-x/wave-1.md) need their subdir first
  fs.writeFileSync(filePath, typeof content === 'string' ? content : JSON.stringify(content, null, 2))
  written.push(name)
}

console.log(JSON.stringify({
  dir,
  written: written.length,
  files: written.sort(),
  verdict: res.verdict || null,
  confidence: res.confidence || null,
}, null, 2))
