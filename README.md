# Local Review Council

**A 100% local, parallel, evidence-verified code review council for Claude Code — powered by [oMLX](https://github.com/jundot/omlx) on Apple Silicon.**

[![Status](https://img.shields.io/badge/status-v0-brightgreen)](docs/superpowers/specs/2026-08-29-local-review-council-design.md)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](#requirements)
[![Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen)](#how-it-works)
[![Platform](https://img.shields.io/badge/platform-Apple_Silicon-black)](#requirements)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

One command runs your diff through a **deterministic code graph** (AST symbol index → blast radius → risk ranking) that decides *where to look*, routes only the relevant functions, callers, and tests to **three specialized local reviewers in parallel** — correctness, security, regression — then a **skeptical verifier** re-examines every candidate finding against the actual code. Only findings that survive verification reach you. Your code never leaves your machine.

The graph layer answers *"where should we look?"*; the LLM layer answers *"is there actually a bug, why, and how confident are we?"* — deterministic code intelligence as the routing layer for a fast ensemble of MLX reviewers, not another generic LLM PR reviewer.

```text
> /local-review --staged

Local Review Council — 2 verified findings

HIGH    auth/session.py:143
        Race condition when refreshing session tokens
        Confidence: 0.94 · Reviewers: correctness + verifier

MEDIUM  api/users.py:71
        Error response breaks existing API contract
        Confidence: 0.87 · Reviewers: regression + verifier

3 candidate findings rejected by verifier.
```

That last line is the point: this is not another LLM spraying review comments. Every finding is independently re-verified, and the rejects are counted in the open.

## Why

- **Private.** The diff, the context, the findings — everything stays on your Mac, as long as `OMLX_BASE_URL` points at a local server. Nothing is sent to any cloud.
- **Parallel.** Three reviewers hit oMLX's continuous-batching server simultaneously; one loaded model serves the whole council at aggregate throughput a sequential loop can't touch.
- **Verified.** A dedicated verifier pass adversarially re-checks each candidate against the real code and rejects anything speculative (confidence gate: 0.80).
- **Honest architecture.** Claude Code is the interface and action layer; the local models are the analysis engine. Claude runs *one* command and interprets *one* JSON object — it never puppeteers the review micro-steps.

## How it works

```text
Claude Code ──► /local-review ──► review.py
                                     │
                                  git diff
                                     │
                          code graph (codegraph.py)
                     AST symbols · calls · imports ·
                     inheritance · tests  — stdlib ast
                                     │
                          blast radius + risk ranking
                       changed symbols → callers → tests
                                     │
                          route ONLY relevant context
                     impact report + symbol bodies + blast
                     radius code, risk-budgeted
                                     │
                     ┌───────────────┼───────────────┐
                     ▼               ▼               ▼
                correctness      security        regression
                     └───────────────┼───────────────┘
                                     ▼   oMLX · continuous batching
                                 verifier    localhost:8000
                                     │
                                     ▼
                          verified JSON findings
                                     │
                                     ▼
                    Claude explains · fixes · comments
```

The entire engine is **two stdlib-only Python files**: `codegraph.py` (the deterministic intelligence — no LLM, no HTTP) and `review.py` (orchestration + oMLX council). No pip installs, no venv, no framework. `SKILL.md` is a thin UX layer; the deterministic pipeline lives in Python where it belongs.

## Quickstart

**1. Serve a local model with oMLX** (one loaded model serves the whole council):

```bash
pip install omlx        # or: brew install omlx
omlx start              # OpenAI-compatible server on 127.0.0.1:8000
```

**2. Install in Claude Code — as a plugin** (this repo is its own marketplace):

```text
/plugin marketplace add adityak74/local-code-review
/plugin install local-review@local-code-review
```

Or as a personal skill via symlink:

```bash
git clone https://github.com/adityak74/local-code-review.git ~/Projects/local-code-review
ln -sfn ~/Projects/local-code-review/skills/review ~/.claude/skills/local-review
```

**3. Review from Claude Code:**

```text
/local-review:review           # plugin install (use /local-review for the symlink install)
/local-review:review --staged  # staged changes only
/local-review:review HEAD~3    # last three commits
/local-review:review main...   # your whole branch
```

Arguments pass straight through to `git diff` — every range you already know just works.

**Standalone (no Claude Code):** the engine is a plain script that prints JSON:

```bash
python3 skills/review/scripts/review.py --staged | jq '.findings'
```

## Use it with any coding agent

The engine has no Claude dependency — it's one command that prints one JSON object, so any agent that can run shell commands can use it. Paste this block into the instruction file your agent reads:

```markdown
## Local code review

To review code changes with a local AI review council, run:

    python3 ~/Projects/local-code-review/skills/review/scripts/review.py [git-diff-args]

(no args = all uncommitted work; `--staged`, `HEAD~3`, `main...` etc. pass through to git diff).
It prints one JSON object; progress on stderr can be ignored. Report ONLY the entries in
`findings` (they are verifier-approved) with file:line, severity, and evidence; mention the
`rejected_count`. If the JSON has an `error` key, show it and stop (usually fixed by `omlx start`).
Never invent findings beyond the JSON.
```

Where to paste it:

| Agent | Instruction file |
|---|---|
| **Claude Code** | none needed — install the plugin or skill above |
| **Codex CLI** | `AGENTS.md` (repo root or `~/.codex/AGENTS.md`) |
| **OpenCode** | `AGENTS.md` in the project root |
| **GitHub Copilot** | `.github/copilot-instructions.md` |
| **Cursor** | `AGENTS.md` or a rule in `.cursor/rules/` |
| **Anything else** | wherever it reads custom instructions — the engine is just a shell command |

## Configuration

Three environment variables. That's all of it.

| Variable | Default | Purpose |
|---|---|---|
| `LOCAL_REVIEW_MODEL` | `lmstudio-community/Qwen3.6-35B-A3B-MLX-4bit` | Model the council uses |
| `OMLX_BASE_URL` | `http://127.0.0.1:8000` | oMLX server address |
| `OMLX_API_KEY` | read from `~/.omlx/settings.json` | Auth, if you've enabled it |

Reviewer behavior lives in plain-markdown prompts (`skills/review/prompts/*.md`) — edit them, no code changes required.

## Output contract

`review.py` prints exactly one JSON object to stdout (progress goes to stderr):

```json
{
  "findings": [
    {
      "file": "auth/session.py", "line": 143, "severity": "high",
      "category": "correctness", "title": "…", "explanation": "…",
      "evidence": "…", "confidence": 0.94, "verified": true,
      "reviewers": ["correctness", "verifier"]
    }
  ],
  "rejected_count": 3,
  "stats": {
    "model": "…", "duration_s": 41.2, "files_reviewed": 4,
    "context_truncated": false, "malformed_dropped": 0, "failed_reviewers": [],
    "graph": {
      "files_indexed": 212, "parse_failures": 0, "symbols": 1840,
      "changed_symbols": 3, "impacted_symbols": 11, "untested_changed": 1
    }
  }
}
```

`stats.graph` appears when the deterministic first pass ran (the diff touched
Python files); if the graph fails for any reason the engine falls back to plain
hunk windows and reviews anyway.

On an empty diff, it short-circuits to `{"findings": [], "note": "nothing to
review"}` — no `rejected_count` or `stats` keys. On a fatal error (git failure,
oMLX unreachable, or anything unexpected), it prints `{"error": "…"}` and
exits 1.

Machine-readable by design — the Claude Code skill is the first consumer, not the only one.

## Requirements

- Apple Silicon Mac (the council was built on an M-series with 48 GB; smaller models fit smaller machines)
- Python 3.9+ (stdlib only — no packages)
- [oMLX](https://github.com/jundot/omlx) serving any instruct model you like
- git

## Roadmap

The Claude Code skill is the first distribution surface, not the architecture. The engine is deliberately a standalone JSON-emitting program so more surfaces can wrap it:

- [x] Design spec ([docs/superpowers/specs](docs/superpowers/specs/2026-08-29-local-review-council-design.md))
- [x] v0: council + verifier engine, Claude Code skill
- [x] Claude Code plugin + self-hosted marketplace (`/local-review:review`)
- [x] v1: deterministic code graph routing — changed symbols, blast radius, risk ranking ([spec](docs/superpowers/specs/2026-08-30-graph-routing-layer-design.md))
- [ ] Standalone CLI polish (`local-review` entry point)
- [ ] MCP server (one reviewer for Claude Code, Codex, OpenCode, Zed, …)
- [ ] GitHub Action & pre-commit hook
- [ ] llama.cpp backend (the engine only speaks OpenAI-compatible HTTP)

## Known limitations

- The symbol graph is Python-only (stdlib `ast`); other languages fall back to hunk-window context. Call/inheritance edges are name-based — ambiguous bare names are deliberately dropped, never guessed, so dynamic dispatch and duplicate names thin the blast radius.
- Pure renames and git-quoted paths are skipped by the diff parser.
- The same bug flagged by two categories surfaces as two findings — dedupe is per-category by design.
- Diff content is not defended against prompt injection; don't point it at untrusted PRs expecting adversarial robustness.
- No overall wall-clock timeout.

## Contributing

Issues and PRs welcome. The design specs and implementation plan live in [`docs/superpowers/`](docs/superpowers/) — read the specs first; they're the binding authority for how the pipeline behaves. Keep the engine stdlib-only: deterministic intelligence in `codegraph.py`, LLM orchestration in `review.py`, and never an LLM call in the first pass.

## License

[MIT](LICENSE) © Aditya Karnam
