# Local Review Council — v0 Design

**Date:** 2026-08-29
**Status:** Draft, pending review
**Scope:** Minimal council (approved): git diff + changed-file context → 3 parallel role reviewers → verifier → JSON. No static analysis, no symbol/test discovery in v0.

## What this is

A Claude Code skill (`/local-review`) whose entire review pipeline runs in one
local Python script against an oMLX server. Claude never orchestrates the
review micro-steps; it runs one command, receives structured JSON findings,
and acts as the explanation/fix layer. 100% local, parallel, verifier-gated.

## Environment facts this design relies on

- Apple M5 Pro, 48 GB RAM.
- oMLX running on `127.0.0.1:8000`, OpenAI-compatible `/v1/chat/completions`,
  auth enabled (API key in `~/.omlx/settings.json`), `max_concurrent_requests: 8`,
  continuous batching, `max_context_window: 32768`.
- Default model: `lmstudio-community/Qwen3.6-35B-A3B-MLX-4bit` (MoE, ~3B active
  params — fast decode, already on disk). Overridable via `LOCAL_REVIEW_MODEL`.

## Repository layout

This repo IS the engine. The skill is installed by symlinking the repo (or its
skill folder) into `~/.claude/skills/local-review`. Plugin packaging comes
later, after review quality is proven.

```
local-code-review/
├── SKILL.md                 # thin UX layer (see below)
├── scripts/
│   └── review.py            # the entire engine, single file, stdlib-only
├── prompts/
│   ├── correctness.md
│   ├── security.md
│   ├── regression.md
│   └── verifier.md
└── docs/superpowers/specs/  # this document
```

Deliberate deviations from the original 5-script sketch:

- **One Python file, not five.** `context.py`, `omlx.py`, `aggregate.py`,
  `static_analysis.py` collapse into sections of `review.py` (~300 lines).
  Split only when a second consumer of a section exists.
- **No `schemas/finding.json`.** The schema is a documented dict shape in
  `review.py` and this spec. A JSON Schema file earns its place when something
  machine-validates against it.
- **Stdlib only.** `concurrent.futures.ThreadPoolExecutor` + `urllib.request`
  instead of asyncio + httpx. At ≤8 concurrent non-streaming HTTP calls,
  asyncio buys nothing. No venv, no deps, `python3 scripts/review.py` just runs.
- **No `context: fork` in v0.** The script's output is compact (verified
  findings only), so main-context pollution is negligible, and fixing findings
  needs main-conversation awareness anyway. Add fork later if reviews get chatty.

## Pipeline

```
review.py [git-diff-args...]
  1. Collect diff        git diff HEAD (default) or git diff <args> verbatim
  2. Build context       diff + trimmed changed-file windows (budgeted)
  3. Council             3 parallel chat requests, one per role prompt
  4. Aggregate           parse findings, dedupe (same file+line+category)
  5. Verify              1 parallel request per candidate finding (≤8 in flight)
  6. Emit JSON to stdout
```

### 1. Diff collection

- No args → `git diff HEAD` (all uncommitted work, staged + unstaged).
- Any args are passed verbatim to `git diff` — so `--staged`, `HEAD~3`,
  `main...` all work for free. The only reserved flag is `--self-test`
  (see Testing); everything else goes to git untouched.
- Empty diff → exit 0 with `{"findings": [], "note": "nothing to review"}`.

### 2. Context building

- For each changed file, include windows of ±80 lines around each hunk
  (merged when overlapping), with line numbers. Whole file if ≤400 lines.
- Total context budget: ~80 KB of prompt text (≈20K tokens, safe inside the
  32K window with room for output). When over budget, shrink windows to ±20
  lines, then drop file content (keep the diff) for the largest files first,
  noting `"context_truncated": true` in output stats.
- Binary files and lockfiles (`*.lock`, `package-lock.json`, etc.) excluded
  from file context; their diff hunks are dropped too.

### 3. Council

Three concurrent `POST /v1/chat/completions` requests, identical context,
different system prompts loaded from `prompts/{role}.md`:

- **correctness** — logic errors, off-by-ones, race conditions, broken edge cases
- **security** — injection, authz/authn gaps, secrets, unsafe deserialization
- **regression** — API-contract breaks, behavior changes callers depend on

Each prompt instructs the model to return ONLY a JSON array of findings:

```json
{
  "file": "path/relative/to/repo",
  "line": 143,
  "severity": "high|medium|low",
  "title": "one-line issue statement",
  "explanation": "why it matters",
  "evidence": "the code path / values that trigger it"
}
```

Parsing is tolerant: extract the first JSON array in the response; malformed
findings are dropped (counted in stats). `temperature: 0.2`, `max_tokens: 4096`,
per-request timeout 300 s.

### 4. Aggregation

- Tag each finding with its `category` (the role that produced it).
- Dedupe on `(file, line ±2, category)`; on collision keep the higher severity
  and merge reviewer attribution.

### 5. Verifier

For each candidate finding, one request with the **verifier prompt**, the
finding, and only the relevant file window. Verdict shape:

```json
{"verified": true, "confidence": 0.94, "note": "reasoning in one paragraph"}
```

- Findings with `verified: false` or `confidence < 0.80` are rejected
  (threshold constant at top of `review.py`).
- Verifier requests run through the same ThreadPoolExecutor, max 8 workers —
  matches oMLX `max_concurrent_requests` so nothing queues client-side.

### 6. Output contract (stdout, single JSON object)

```json
{
  "findings": [
    {
      "file": "auth/session.py", "line": 143, "severity": "high",
      "category": "correctness", "title": "...", "explanation": "...",
      "evidence": "...", "confidence": 0.94, "verified": true,
      "reviewers": ["correctness", "verifier"]
    }
  ],
  "rejected_count": 3,
  "stats": {
    "model": "...", "duration_s": 41.2, "files_reviewed": 4,
    "context_truncated": false, "malformed_dropped": 0
  }
}
```

Everything else (progress, warnings) goes to stderr so stdout stays parseable.

## Configuration

- API key: read from `~/.omlx/settings.json` → `auth.api_key`; overridable via
  `OMLX_API_KEY`. Base URL via `OMLX_BASE_URL` (default `http://127.0.0.1:8000`).
- Model via `LOCAL_REVIEW_MODEL` (default the Qwen3.6-35B-A3B above).
- No config file. Three env vars and two constants (threshold, budget) cover v0.

## Error handling

- oMLX unreachable → exit 1, JSON error on stdout:
  `{"error": "oMLX server not reachable at ...; run 'omlx start'"}`.
- One council role fails/times out → warn on stderr, continue with the other
  two, record in stats. All three fail → exit 1 with error JSON.
- Verifier request fails for a finding → finding kept as
  `verified: false, confidence: 0.0` (rejected), never silently promoted.
- Not a git repo / git errors → exit 1 with the git message in error JSON.

## SKILL.md (thin, as proposed)

```yaml
---
name: local-review
description: Review code using a parallel council of local AI reviewers via oMLX.
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/review.py *)
---
```

Body: run the script with `$ARGUMENTS`, report only `verified: true` findings,
explain each with file:line and evidence, offer to fix, never invent findings
beyond the JSON, and surface the `rejected_count` line. `context: fork` and
`disable-model-invocation` loosening are post-v0 decisions.

## Testing

- `review.py` gets one self-check: `python3 scripts/review.py --self-test`
  runs the parsing, dedupe, windowing, and budget logic against fixed inputs
  with asserts. No live server needed; no test framework.
- Quality evaluation is manual for v0: a scratch repo with 3–5 seeded-bug
  diffs (a race condition, an off-by-one, a contract break, one clean diff as
  a false-positive control). Success bar: seeded bugs found and verified,
  clean diff produces zero verified findings.

## Explicitly out of scope for v0

Static analysis, symbol/test discovery, MCP server, plugin packaging, hooks,
`--fix`/`--deep`/`--security` flags, multiple models per role, CLI packaging,
JSON Schema validation. Each waits until council quality is proven.
