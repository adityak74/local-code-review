# Local Review Council

**A 100% local, parallel, evidence-verified code review council for Claude Code — powered by [oMLX](https://github.com/jundot/omlx) on Apple Silicon.**

[![Status](https://img.shields.io/badge/status-v0_in_development-orange)](docs/superpowers/specs/2026-08-29-local-review-council-design.md)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](#requirements)
[![Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen)](#how-it-works)
[![Platform](https://img.shields.io/badge/platform-Apple_Silicon-black)](#requirements)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

One command runs your diff through **three specialized local reviewers in parallel** — correctness, security, regression — then a **skeptical verifier** re-examines every candidate finding against the actual code. Only findings that survive verification reach you. Your code never leaves your machine.

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

- **Private.** The diff, the context, the findings — everything stays on your Mac. Nothing is sent to any cloud.
- **Parallel.** Three reviewers hit oMLX's continuous-batching server simultaneously; one loaded model serves the whole council at aggregate throughput a sequential loop can't touch.
- **Verified.** A dedicated verifier pass adversarially re-checks each candidate against the real code and rejects anything speculative (confidence gate: 0.80).
- **Honest architecture.** Claude Code is the interface and action layer; the local models are the analysis engine. Claude runs *one* command and interprets *one* JSON object — it never puppeteers the review micro-steps.

## How it works

```text
Claude Code ──► /local-review ──► review.py
                                     │
                            git diff + changed-file
                            context (budgeted windows)
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

The entire engine is **one stdlib-only Python file** (`scripts/review.py`). No pip installs, no venv, no framework. `SKILL.md` is a thin UX layer; the deterministic pipeline lives in Python where it belongs.

## Quickstart

**1. Serve a local model with oMLX** (one loaded model serves the whole council):

```bash
pip install omlx        # or: brew install omlx
omlx start              # OpenAI-compatible server on 127.0.0.1:8000
```

**2. Install the skill:**

```bash
git clone https://github.com/adityak74/local-code-review.git ~/Projects/local-code-review
ln -sfn ~/Projects/local-code-review ~/.claude/skills/local-review
```

**3. Review from Claude Code:**

```text
/local-review              # all uncommitted work
/local-review --staged     # staged changes only
/local-review HEAD~3       # last three commits
/local-review main...      # your whole branch
```

Arguments pass straight through to `git diff` — every range you already know just works.

**Standalone (no Claude Code):** the engine is a plain script that prints JSON:

```bash
python3 scripts/review.py --staged | jq '.findings'
```

## Configuration

Three environment variables. That's all of it.

| Variable | Default | Purpose |
|---|---|---|
| `LOCAL_REVIEW_MODEL` | `lmstudio-community/Qwen3.6-35B-A3B-MLX-4bit` | Model the council uses |
| `OMLX_BASE_URL` | `http://127.0.0.1:8000` | oMLX server address |
| `OMLX_API_KEY` | read from `~/.omlx/settings.json` | Auth, if you've enabled it |

Reviewer behavior lives in plain-markdown prompts (`prompts/*.md`) — edit them, no code changes required.

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
  "stats": { "model": "…", "duration_s": 41.2, "files_reviewed": 4 }
}
```

Machine-readable by design — the Claude Code skill is the first consumer, not the only one.

## Requirements

- Apple Silicon Mac (the council was built on an M-series with 48 GB; smaller models fit smaller machines)
- Python 3.9+ (stdlib only — no packages)
- [oMLX](https://github.com/jundot/omlx) serving any instruct model you like
- git

## Roadmap

The Claude Code skill is the first distribution surface, not the architecture. The engine is deliberately a standalone JSON-emitting program so more surfaces can wrap it:

- [x] Design spec ([docs/superpowers/specs](docs/superpowers/specs/2026-08-29-local-review-council-design.md))
- [ ] v0: council + verifier engine, Claude Code skill
- [ ] Standalone CLI polish (`local-review` entry point)
- [ ] Static analysis + changed-symbol/test discovery feeding reviewer context
- [ ] MCP server (one reviewer for Claude Code, Codex, OpenCode, Zed, …)
- [ ] Claude Code plugin packaging (`/local-review:review`, `/local-review:security`)
- [ ] GitHub Action & pre-commit hook
- [ ] llama.cpp backend (the engine only speaks OpenAI-compatible HTTP)

## Contributing

Issues and PRs welcome. The design spec and implementation plan live in [`docs/superpowers/`](docs/superpowers/) — read the spec first; it's the binding authority for how the pipeline behaves. Keep the engine stdlib-only and single-file until a second consumer forces a split.

## License

[MIT](LICENSE) © Aditya Karnam
