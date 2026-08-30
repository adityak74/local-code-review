# Local Review Council Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `/local-review` Claude Code skill whose whole review pipeline (git diff → 3 parallel role reviewers → verifier → JSON) runs in one stdlib-only Python script against a local oMLX server.

**Architecture:** Single engine file `scripts/review.py` (pure functions + a thin `main`), four prompt files, one thin `SKILL.md`. Claude runs one command and receives one JSON object on stdout; all reviewing happens in parallel HTTP calls to oMLX's OpenAI-compatible endpoint.

**Tech Stack:** Python 3 stdlib only (`urllib.request`, `concurrent.futures`, `json`, `subprocess`). oMLX server on `127.0.0.1:8000`. No pip dependencies, no venv, no test framework.

**Spec:** `docs/superpowers/specs/2026-08-29-local-review-council-design.md`

## Global Constraints

- Python 3 stdlib ONLY — no pip installs, no venv, `python3 scripts/review.py` must just run.
- One engine file: `scripts/review.py`. Do not split into modules.
- stdout carries exactly one JSON object per run; all progress/warnings go to stderr.
- Constants (top of `review.py`, exact values): `CONFIDENCE_THRESHOLD = 0.80`, `CONTEXT_BUDGET = 80_000` (chars), `WINDOW_PAD = 80`, `SHRUNK_PAD = 20`, `WHOLE_FILE_MAX = 400` (lines), `MAX_WORKERS = 8`, `REQUEST_TIMEOUT = 300` (seconds).
- Default model: `lmstudio-community/Qwen3.6-35B-A3B-MLX-4bit`. Env overrides: `LOCAL_REVIEW_MODEL`, `OMLX_BASE_URL` (default `http://127.0.0.1:8000`), `OMLX_API_KEY` (fallback: `auth.api_key` from `~/.omlx/settings.json`).
- The test suite is `python3 scripts/review.py --self-test` (assert-based, no live server, no git). `--self-test` is the ONLY reserved CLI flag; all other args pass verbatim to `git diff`.
- Sampling for all model calls: `temperature: 0.2`, `max_tokens: 4096`, `stream: false`.
- Commit at the end of every task. End each commit message with:
  `Claude-Session: https://claude.ai/code/session_01Rmsb5J6uiz6tSPcXTjyCYU`

## File Structure

```
local-code-review/
├── SKILL.md                  # Task 5 — thin UX layer
├── scripts/
│   └── review.py             # Tasks 1–4 — the entire engine
├── prompts/
│   ├── correctness.md        # Task 3
│   ├── security.md           # Task 3
│   ├── regression.md         # Task 3
│   └── verifier.md           # Task 4
├── docs/superpowers/specs/   # exists
└── docs/superpowers/plans/   # this document
```

---

### Task 1: Engine skeleton, config, git plumbing

**Files:**
- Create: `scripts/review.py`

**Interfaces:**
- Produces (later tasks rely on these exact names):
  - `ReviewError(Exception)` — raised for fatal errors; `main` converts it to error JSON + exit 1.
  - `load_api_key(settings_path=Path.home() / ".omlx/settings.json", env=os.environ) -> str`
  - `collect_diff(args: list[str]) -> str` — raw `git diff` text.
  - `repo_root() -> str`
  - `read_repo_file(path: str) -> list[str] | None` — file lines relative to repo root, `None` if unreadable.
  - `is_excluded(path: str) -> bool` — lockfile filter.
  - `self_test() -> int` — assert suite; later tasks append asserts inside it.
  - All constants from Global Constraints, plus `SKILL_DIR = Path(__file__).resolve().parent.parent`.

- [ ] **Step 1: Write `scripts/review.py` with skeleton + failing self-test**

The self-test calls `load_api_key`, `is_excluded` before they exist — that is the failing state.

```python
#!/usr/bin/env python3
"""Local Review Council engine: git diff -> parallel role reviewers -> verifier -> JSON.

Runs entirely against a local oMLX server. Stdlib only.
Spec: docs/superpowers/specs/2026-08-29-local-review-council-design.md
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

BASE_URL = os.environ.get("OMLX_BASE_URL", "http://127.0.0.1:8000")
MODEL = os.environ.get("LOCAL_REVIEW_MODEL", "lmstudio-community/Qwen3.6-35B-A3B-MLX-4bit")
CONFIDENCE_THRESHOLD = 0.80
CONTEXT_BUDGET = 80_000   # chars of prompt text
WINDOW_PAD = 80           # lines around each hunk
SHRUNK_PAD = 20           # pad after budget shrink
WHOLE_FILE_MAX = 400      # files at or under this many lines are included whole
MAX_WORKERS = 8           # matches oMLX max_concurrent_requests
REQUEST_TIMEOUT = 300     # seconds per model call
SKILL_DIR = Path(__file__).resolve().parent.parent
EXCLUDED_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.lock", "poetry.lock", "uv.lock", "Gemfile.lock", "composer.lock",
}


class ReviewError(Exception):
    """Fatal pipeline error; main() turns it into error JSON + exit 1."""


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def main(argv):
    if argv[:1] == ["--self-test"]:
        return self_test()
    try:
        diff = collect_diff(argv)
    except ReviewError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    if not diff.strip():
        print(json.dumps({"findings": [], "note": "nothing to review"}))
        return 0
    # Tasks 2-4 replace this stub with the full pipeline.
    print(json.dumps({"findings": [],
                      "note": f"engine incomplete: collected {len(diff)} diff bytes"}))
    return 0


def self_test():
    # -- Task 1: config + exclusions ------------------------------------
    with tempfile.TemporaryDirectory() as td:
        settings = Path(td) / "settings.json"
        settings.write_text(json.dumps({"auth": {"api_key": "from-file"}}))
        assert load_api_key(settings, env={}) == "from-file"
        assert load_api_key(settings, env={"OMLX_API_KEY": "from-env"}) == "from-env"
        assert load_api_key(Path(td) / "missing.json", env={}) == ""
        (Path(td) / "bad.json").write_text("not json")
        assert load_api_key(Path(td) / "bad.json", env={}) == ""

    assert is_excluded("package-lock.json")
    assert is_excluded("sub/dir/Cargo.lock")
    assert is_excluded("poetry.lock")
    assert not is_excluded("src/lock.py")
    assert not is_excluded("api/users.py")

    print("self-test OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 2: Run self-test, verify it fails**

Run: `python3 scripts/review.py --self-test`
Expected: FAIL with `NameError: name 'load_api_key' is not defined`

- [ ] **Step 3: Implement config + git plumbing**

Insert between `warn` and `main`:

```python
def load_api_key(settings_path=Path.home() / ".omlx/settings.json", env=os.environ):
    """OMLX_API_KEY env var wins; else auth.api_key from oMLX settings; else ''."""
    if env.get("OMLX_API_KEY"):
        return env["OMLX_API_KEY"]
    try:
        return str(json.loads(settings_path.read_text())["auth"]["api_key"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return ""


def is_excluded(path):
    name = Path(path).name
    return name in EXCLUDED_NAMES or name.endswith(".lock")


def collect_diff(args):
    """No args -> `git diff HEAD` (all uncommitted work). Args pass through verbatim."""
    cmd = ["git", "diff"] + (list(args) if args else ["HEAD"])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ReviewError(f"git diff failed: {proc.stderr.strip()}")
    return proc.stdout


@lru_cache(maxsize=1)
def repo_root():
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise ReviewError(f"not a git repository: {proc.stderr.strip()}")
    return proc.stdout.strip()


def read_repo_file(path):
    """Lines of a repo-relative file, or None if unreadable (e.g. deleted)."""
    try:
        return (Path(repo_root()) / path).read_text(errors="replace").splitlines()
    except OSError:
        return None
```

- [ ] **Step 4: Run self-test, verify it passes**

Run: `python3 scripts/review.py --self-test`
Expected: `self-test OK`, exit 0

- [ ] **Step 5: Verify git plumbing live (this repo is a git repo)**

Run: `python3 scripts/review.py`
Expected: `{"findings": [], "note": "nothing to review"}` on a clean tree (or the
`engine incomplete` note if the tree is dirty). Then run
`python3 scripts/review.py --bogus-flag`; expected: `{"error": "git diff failed: ..."}` and exit 1.

- [ ] **Step 6: Commit**

```bash
git add scripts/review.py
git commit -m "feat: review engine skeleton — config, git diff collection, self-test

Claude-Session: https://claude.ai/code/session_01Rmsb5J6uiz6tSPcXTjyCYU"
```

---

### Task 2: Diff parsing, hunk windows, budgeted context

**Files:**
- Modify: `scripts/review.py`

**Interfaces:**
- Consumes: `is_excluded`, constants (Task 1).
- Produces:
  - `parse_diff(diff_text: str) -> tuple[str, dict[str, list[tuple[int, int]]]]` — `(clean_diff, hunks)`. Binary and excluded files are dropped from both. `hunks` maps new-side path → 1-based inclusive `(start, end)` line ranges. Deleted files stay in `clean_diff` but get no `hunks` entry.
  - `merge_windows(ranges, pad, total_lines) -> list[tuple[int, int]]` — padded, clamped to `[1, total_lines]`, overlapping/adjacent ranges merged.
  - `format_windows(path, lines, windows) -> str` — numbered code block per window.
  - `build_context(diff_text, hunks, read_file) -> tuple[str, bool]` — `(context, truncated)`; `read_file` has the `read_repo_file` signature (injected for testability).

- [ ] **Step 1: Add failing self-test asserts**

Append inside `self_test()` before the `print`:

```python
    # -- Task 2: diff parsing + windows + budget ------------------------
    sample_diff = (
        "diff --git a/api/users.py b/api/users.py\n"
        "index 111..222 100644\n"
        "--- a/api/users.py\n"
        "+++ b/api/users.py\n"
        "@@ -10,3 +10,4 @@ def get_user():\n"
        " a\n-b\n+b2\n+b3\n a\n"
        "@@ -40,2 +41,2 @@ def list_users():\n"
        " x\n-y\n+y2\n"
        "diff --git a/package-lock.json b/package-lock.json\n"
        "--- a/package-lock.json\n"
        "+++ b/package-lock.json\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n"
        "diff --git a/logo.png b/logo.png\n"
        "index 333..444 100644\n"
        "Binary files a/logo.png and b/logo.png differ\n"
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-dead\n-code\n"
    )
    clean, hunks = parse_diff(sample_diff)
    assert list(hunks) == ["api/users.py"], hunks
    assert hunks["api/users.py"] == [(10, 13), (41, 42)], hunks
    assert "package-lock.json" not in clean and "logo.png" not in clean
    assert "gone.py" in clean          # deletions stay visible in the diff
    assert "api/users.py" in clean

    assert merge_windows([(10, 13), (41, 42)], 80, 500) == [(1, 122)]
    assert merge_windows([(10, 13), (300, 301)], 20, 500) == [(1, 33), (280, 321)]
    assert merge_windows([(490, 495)], 80, 500) == [(410, 500)]

    lines = [f"code line {i}" for i in range(1, 6)]
    block = format_windows("api/users.py", lines, [(2, 4)])
    assert "=== api/users.py ===" in block
    assert "2: code line 2" in block and "4: code line 4" in block
    assert "1: code line 1" not in block and "5:" not in block

    small = {"api/users.py": [(2, 3)]}
    reader = lambda p: lines                      # 5-line file -> included whole
    ctx, truncated = build_context("DIFFTEXT", small, reader)
    assert not truncated
    assert "DIFFTEXT" in ctx and "1: code line 1" in ctx and "5: code line 5" in ctx

    big_lines = [f"l{i}" for i in range(1, 20_001)]
    big = {f"f{n}.py": [(1000, 1001)] for n in range(60)}
    ctx, truncated = build_context("D" * 30_000, big, lambda p: big_lines)
    assert truncated
    assert len(ctx) <= CONTEXT_BUDGET + 30_000 + 200   # diff always kept
    assert "D" * 100 in ctx

    missing = {"api/users.py": [(2, 3)], "moved.py": [(1, 1)]}
    ctx, _ = build_context("DIFFTEXT", missing,
                           lambda p: lines if p == "api/users.py" else None)
    assert "moved.py" not in ctx.split("DIFFTEXT")[1]  # unreadable file skipped
```

- [ ] **Step 2: Run self-test, verify it fails**

Run: `python3 scripts/review.py --self-test`
Expected: FAIL with `NameError: name 'parse_diff' is not defined`

- [ ] **Step 3: Implement parsing, windows, context builder**

Insert after `read_repo_file`:

```python
def parse_diff(diff_text):
    """Split a unified diff into per-file segments, dropping binary and
    excluded (lockfile) segments entirely. Returns (clean_diff, hunks) where
    hunks maps new-side path -> [(start, end), ...] 1-based inclusive ranges.
    Deleted files (+++ /dev/null) stay in clean_diff but get no hunks entry."""
    kept, hunks = [], {}
    segment, new_path, old_path, ranges, binary = [], None, None, [], False

    def flush():
        nonlocal segment, new_path, old_path, ranges, binary
        path = new_path or old_path
        if segment and path and not binary and not is_excluded(path):
            kept.append("".join(segment))
            if new_path and ranges:
                hunks.setdefault(new_path, []).extend(ranges)
        segment, new_path, old_path, ranges, binary = [], None, None, [], False

    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            flush()
        segment.append(line)
        if line.startswith("+++ b/"):
            new_path = line[6:].strip()
        elif line.startswith("--- a/"):
            old_path = line[6:].strip()
        elif line.startswith("Binary files"):
            binary = True
        elif line.startswith("@@ "):
            plus = line.split("+", 1)[1].split(" ", 1)[0]      # "start,count" or "start"
            start = int(plus.split(",")[0])
            count = int(plus.split(",")[1]) if "," in plus else 1
            ranges.append((start, max(start, start + count - 1)))
    flush()
    return "".join(kept), hunks


def merge_windows(ranges, pad, total_lines):
    """Pad ranges, clamp to [1, total_lines], merge overlapping/adjacent."""
    spans = sorted([max(1, s - pad), min(total_lines, e + pad)] for s, e in ranges)
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(m) for m in merged]


def format_windows(path, lines, windows):
    parts = [f"=== {path} ==="]
    for s, e in windows:
        parts.append(f"--- lines {s}-{e} ---")
        parts.extend(f"{i}: {lines[i - 1]}" for i in range(s, e + 1))
    return "\n".join(parts)


def build_context(diff_text, hunks, read_file):
    """Diff + changed-file windows under CONTEXT_BUDGET.
    Over budget: shrink pads to SHRUNK_PAD, then drop largest file blocks.
    The diff itself is always kept. Returns (context, truncated)."""
    entries = []
    for path in sorted(hunks):
        lines = read_file(path)
        if lines is not None:
            entries.append((path, lines, hunks[path]))

    def render(pad):
        blocks = []
        for path, lines, ranges in entries:
            if len(lines) <= WHOLE_FILE_MAX:
                windows = [(1, len(lines))] if lines else []
            else:
                windows = merge_windows(ranges, pad, len(lines))
            blocks.append(format_windows(path, lines, windows))
        return blocks

    truncated = False
    blocks = render(WINDOW_PAD)
    if len(diff_text) + sum(map(len, blocks)) > CONTEXT_BUDGET:
        truncated = True
        blocks = render(SHRUNK_PAD)
        while blocks and len(diff_text) + sum(map(len, blocks)) > CONTEXT_BUDGET:
            blocks.remove(max(blocks, key=len))
    context = ("## Diff\n" + diff_text +
               "\n\n## Changed file context (line-numbered)\n" + "\n\n".join(blocks))
    return context, truncated
```

- [ ] **Step 4: Run self-test, verify it passes**

Run: `python3 scripts/review.py --self-test`
Expected: `self-test OK`, exit 0

- [ ] **Step 5: Commit**

```bash
git add scripts/review.py
git commit -m "feat: diff parsing, hunk windows, budgeted context builder

Claude-Session: https://claude.ai/code/session_01Rmsb5J6uiz6tSPcXTjyCYU"
```

---

### Task 3: Reviewer prompts, oMLX client, parallel council

**Files:**
- Create: `prompts/correctness.md`, `prompts/security.md`, `prompts/regression.md`
- Modify: `scripts/review.py`

**Interfaces:**
- Consumes: `warn`, `ReviewError`, `SKILL_DIR`, constants (Task 1).
- Produces:
  - `chat(system: str, user: str, api_key: str) -> str` — one blocking completion call; raises on HTTP/network error.
  - `load_prompt(name: str) -> str` — reads `prompts/{name}.md`.
  - `extract_json_array(text: str) -> list` — first parseable JSON array, else `[]`.
  - `normalize_findings(raw, category: str) -> tuple[list[dict], int]` — `(findings, dropped_count)`. Each finding dict has exactly: `file` (str), `line` (int), `severity` (`high|medium|low`), `category` (str), `title`, `explanation`, `evidence` (str), `reviewers` (list[str], starts as `[category]`).
  - `run_council(context: str, api_key: str) -> tuple[list[dict], int, list[str]]` — `(findings, malformed_dropped, failed_roles)`; raises `ReviewError` if ALL roles fail.
  - `ROLES = ("correctness", "security", "regression")`

- [ ] **Step 1: Write the three reviewer prompts**

`prompts/correctness.md`:

```markdown
You are a code reviewer on a local review council. Your ONLY dimension is
CORRECTNESS: logic errors, off-by-one errors, race conditions and other
concurrency bugs, broken edge cases, wrong operators, bad null/None
handling, unhandled error paths that lose data.

You receive a git diff plus line-numbered context from the changed files.
Review ONLY the changed code and its direct blast radius. Do not comment on
style, naming, formatting, or hypothetical improvements. Report only
defects you can point to in the code shown.

Respond with ONLY a JSON array — no prose, no markdown fences. Each element:
{"file": "path/relative/to/repo",
 "line": <int line number from the numbered context>,
 "severity": "high|medium|low",
 "title": "one-line issue statement",
 "explanation": "why it matters",
 "evidence": "the exact code path and input values that trigger it"}

If there are no correctness defects, respond with [].
```

`prompts/security.md`:

```markdown
You are a code reviewer on a local review council. Your ONLY dimension is
SECURITY: injection (SQL, shell, path, template), authn/authz gaps,
secrets or credentials in code, unsafe deserialization, SSRF, insecure
crypto or randomness, sensitive data leaking into logs or responses.

You receive a git diff plus line-numbered context from the changed files.
Review ONLY the changed code and its direct blast radius. Do not comment on
style or theoretical hardening. Report only vulnerabilities you can point
to in the code shown, with the input that exploits them.

Respond with ONLY a JSON array — no prose, no markdown fences. Each element:
{"file": "path/relative/to/repo",
 "line": <int line number from the numbered context>,
 "severity": "high|medium|low",
 "title": "one-line issue statement",
 "explanation": "why it matters",
 "evidence": "the exact code path and input values that trigger it"}

If there are no security defects, respond with [].
```

`prompts/regression.md`:

```markdown
You are a code reviewer on a local review council. Your ONLY dimension is
REGRESSION RISK: breaking changes to function/API signatures or return
shapes, changed error responses or status codes, removed or renamed public
symbols, altered defaults, behavior changes that existing callers visible
in the diff context depend on.

You receive a git diff plus line-numbered context from the changed files.
Compare old behavior (- lines) to new behavior (+ lines). Report only
breaks you can demonstrate from the code shown — name the caller or
contract that breaks. Do not comment on style or internal refactors that
preserve behavior.

Respond with ONLY a JSON array — no prose, no markdown fences. Each element:
{"file": "path/relative/to/repo",
 "line": <int line number from the numbered context>,
 "severity": "high|medium|low",
 "title": "one-line issue statement",
 "explanation": "why it matters",
 "evidence": "the exact code path and input values that trigger it"}

If there are no regression risks, respond with [].
```

- [ ] **Step 2: Add failing self-test asserts**

Append inside `self_test()` before the `print`:

```python
    # -- Task 3: JSON extraction + finding normalization ----------------
    assert extract_json_array('noise [1, 2] tail') == [1, 2]
    assert extract_json_array('```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert extract_json_array('broken [1,, then good ["x"]') == ["x"]
    assert extract_json_array('no array here {"a": 1}') == []
    assert extract_json_array('') == []

    raw = [
        {"file": "a.py", "line": 5, "severity": "HIGH", "title": "t",
         "explanation": "e", "evidence": "ev"},
        {"file": "a.py", "line": "12", "severity": "weird", "title": "t2",
         "explanation": "e2", "evidence": "ev2"},
        {"file": "a.py", "line": "not-a-number", "severity": "low",
         "title": "t3", "explanation": "e3", "evidence": "ev3"},
        {"file": "a.py", "title": "missing keys"},
        "not even a dict",
    ]
    good, dropped = normalize_findings(raw, "correctness")
    assert dropped == 3, (good, dropped)
    assert good[0]["severity"] == "high" and good[0]["line"] == 5
    assert good[1]["severity"] == "low" and good[1]["line"] == 12   # unknown -> low
    assert all(f["category"] == "correctness" and f["reviewers"] == ["correctness"]
               for f in good)
    assert normalize_findings("not a list", "security") == ([], 0)

    for role in ROLES:
        assert load_prompt(role).strip(), f"prompt {role}.md missing or empty"
```

- [ ] **Step 3: Run self-test, verify it fails**

Run: `python3 scripts/review.py --self-test`
Expected: FAIL with `NameError: name 'extract_json_array' is not defined`

- [ ] **Step 4: Implement client, extraction, normalization, council**

Insert after `build_context`:

```python
ROLES = ("correctness", "security", "regression")
REQUIRED_KEYS = {"file", "line", "severity", "title", "explanation", "evidence"}
SEVERITIES = {"high", "medium", "low"}


def load_prompt(name):
    return (SKILL_DIR / "prompts" / f"{name}.md").read_text()


def chat(system, user, api_key):
    """One blocking non-streaming completion against oMLX. Raises on failure."""
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": 4096,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def extract_json_array(text):
    """First parseable JSON array anywhere in text, else []. Tolerates prose,
    markdown fences, and broken candidates before the real one."""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "[":
            try:
                val, _ = decoder.raw_decode(text[i:])
            except ValueError:
                continue
            if isinstance(val, list):
                return val
    return []


def normalize_findings(raw, category):
    """Validate raw model findings. Returns (findings, dropped_count)."""
    good, dropped = [], 0
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict) or not REQUIRED_KEYS <= set(f):
            dropped += 1
            continue
        try:
            line = int(f["line"])
        except (TypeError, ValueError):
            dropped += 1
            continue
        sev = str(f["severity"]).lower()
        good.append({
            "file": str(f["file"]),
            "line": line,
            "severity": sev if sev in SEVERITIES else "low",
            "category": category,
            "title": str(f["title"]),
            "explanation": str(f["explanation"]),
            "evidence": str(f["evidence"]),
            "reviewers": [category],
        })
    return good, dropped


def run_council(context, api_key):
    """Run all roles concurrently. Returns (findings, malformed_dropped,
    failed_roles). Raises ReviewError only if every role fails."""
    findings, dropped, failed = [], 0, []
    with ThreadPoolExecutor(max_workers=len(ROLES)) as pool:
        futures = {pool.submit(chat, load_prompt(r), context, api_key): r
                   for r in ROLES}
        for fut in as_completed(futures):
            role = futures[fut]
            try:
                text = fut.result()
            except Exception as e:
                warn(f"{role} reviewer failed: {e}")
                failed.append(role)
                continue
            good, d = normalize_findings(extract_json_array(text), role)
            findings.extend(good)
            dropped += d
    if len(failed) == len(ROLES):
        raise ReviewError(
            f"all council reviewers failed; is oMLX up at {BASE_URL}? try 'omlx start'")
    return findings, dropped, failed
```

- [ ] **Step 5: Run self-test, verify it passes**

Run: `python3 scripts/review.py --self-test`
Expected: `self-test OK`, exit 0

- [ ] **Step 6: Commit**

```bash
git add scripts/review.py prompts/
git commit -m "feat: reviewer prompts, oMLX client, parallel council

Claude-Session: https://claude.ai/code/session_01Rmsb5J6uiz6tSPcXTjyCYU"
```

---

### Task 4: Dedupe, verifier pass, full pipeline wiring

**Files:**
- Create: `prompts/verifier.md`
- Modify: `scripts/review.py` (add functions; replace `main`'s stub body)

**Interfaces:**
- Consumes: everything from Tasks 1–3, exact signatures as listed there.
- Produces:
  - `dedupe(findings: list[dict]) -> list[dict]` — collapses same `(file, line ±2, category)`; keeps higher severity, merges `reviewers`.
  - `extract_json_object(text: str) -> dict | None` — first parseable JSON object.
  - `apply_verdict(finding: dict, verdict: dict | None) -> dict` — sets `verified` (bool), `confidence` (float), optional `verifier_note`; appends `"verifier"` to `reviewers` when verified. `None`/malformed verdict → `verified=False, confidence=0.0` (never silently promoted).
  - `run_verifier(candidates, read_file, api_key) -> list[dict]` — parallel, `MAX_WORKERS` cap.
  - Final stdout contract (spec §6): `{"findings": [only verified], "rejected_count": int, "stats": {"model", "duration_s", "files_reviewed", "context_truncated", "malformed_dropped", "failed_reviewers"}}`.

- [ ] **Step 1: Write the verifier prompt**

`prompts/verifier.md`:

```markdown
You are a skeptical verifier on a local review council. You receive ONE
candidate finding from another reviewer, plus the relevant line-numbered
code. Decide whether the finding is a real, demonstrable defect in this
exact code — not hypothetical, not style, not something the code already
handles.

Be strict. Reject findings that are speculative, rely on code not shown,
misread the code, or describe behavior that is actually correct. Verify
only when you can trace the exact code path that makes the issue real.

Respond with ONLY a JSON object — no prose, no markdown fences:
{"verified": true|false,
 "confidence": <0.0-1.0, your confidence that the defect is real>,
 "note": "one-paragraph reasoning for your verdict"}
```

- [ ] **Step 2: Add failing self-test asserts**

Append inside `self_test()` before the `print`:

```python
    # -- Task 4: dedupe + verdicts --------------------------------------
    def mk(file, line, sev, cat):
        return {"file": file, "line": line, "severity": sev, "category": cat,
                "title": "t", "explanation": "e", "evidence": "ev",
                "reviewers": [cat]}

    deduped = dedupe([
        mk("a.py", 10, "low", "correctness"),
        mk("a.py", 11, "high", "correctness"),   # within ±2, same cat -> merged
        mk("a.py", 10, "high", "security"),      # other category survives
        mk("a.py", 50, "low", "correctness"),    # far away -> survives
    ])
    assert len(deduped) == 3, deduped
    merged = next(f for f in deduped if f["category"] == "correctness"
                  and f["line"] == 10)
    assert merged["severity"] == "high"

    assert extract_json_object('ok {"verified": true} done') == {"verified": True}
    assert extract_json_object('nothing here') is None

    f = apply_verdict(mk("a.py", 1, "high", "correctness"),
                      {"verified": True, "confidence": 0.94, "note": "solid"})
    assert f["verified"] and f["confidence"] == 0.94
    assert f["verifier_note"] == "solid" and "verifier" in f["reviewers"]

    f = apply_verdict(mk("a.py", 1, "high", "correctness"),
                      {"verified": True, "confidence": 0.5})
    assert not f["verified"]                       # below CONFIDENCE_THRESHOLD

    f = apply_verdict(mk("a.py", 1, "high", "correctness"),
                      {"verified": False, "confidence": 0.99})
    assert not f["verified"]

    f = apply_verdict(mk("a.py", 1, "high", "correctness"), None)
    assert not f["verified"] and f["confidence"] == 0.0

    f = apply_verdict(mk("a.py", 1, "high", "correctness"),
                      {"verified": True, "confidence": "not-a-number"})
    assert not f["verified"] and f["confidence"] == 0.0
```

- [ ] **Step 3: Run self-test, verify it fails**

Run: `python3 scripts/review.py --self-test`
Expected: FAIL with `NameError: name 'dedupe' is not defined`

- [ ] **Step 4: Implement dedupe, verifier, verdicts**

Insert after `run_council`:

```python
SEV_RANK = {"high": 2, "medium": 1, "low": 0}


def dedupe(findings):
    """Collapse findings sharing (file, line within ±2, category). Keeps the
    higher severity and merges reviewer attribution."""
    out = []
    for f in findings:
        match = next((g for g in out
                      if g["file"] == f["file"] and g["category"] == f["category"]
                      and abs(g["line"] - f["line"]) <= 2), None)
        if match is None:
            out.append(dict(f, reviewers=list(f["reviewers"])))
            continue
        if SEV_RANK[f["severity"]] > SEV_RANK[match["severity"]]:
            match["severity"] = f["severity"]
        for r in f["reviewers"]:
            if r not in match["reviewers"]:
                match["reviewers"].append(r)
    return out


def extract_json_object(text):
    """First parseable JSON object anywhere in text, else None."""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                val, _ = decoder.raw_decode(text[i:])
            except ValueError:
                continue
            if isinstance(val, dict):
                return val
    return None


def apply_verdict(finding, verdict):
    """Fold a verifier verdict into a finding. Failed/malformed verdicts
    reject the finding — never silently promote."""
    v = verdict if isinstance(verdict, dict) else {}
    try:
        confidence = float(v.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    finding["confidence"] = confidence
    finding["verified"] = bool(v.get("verified")) and confidence >= CONFIDENCE_THRESHOLD
    if v.get("note"):
        finding["verifier_note"] = str(v["note"])
    if finding["verified"]:
        finding["reviewers"].append("verifier")
    return finding


def run_verifier(candidates, read_file, api_key):
    """Verify each candidate in parallel (MAX_WORKERS cap, matching oMLX
    max_concurrent_requests so nothing queues client-side)."""
    prompt = load_prompt("verifier")

    def one(finding):
        lines = read_file(finding["file"])
        if lines:
            windows = merge_windows([(finding["line"], finding["line"])],
                                    WINDOW_PAD, len(lines))
            code = format_windows(finding["file"], lines, windows)
        else:
            code = "(file content unavailable)"
        payload = {k: finding[k] for k in
                   ("file", "line", "severity", "category", "title",
                    "explanation", "evidence")}
        try:
            text = chat(prompt,
                        "Candidate finding:\n" + json.dumps(payload, indent=1) +
                        "\n\nRelevant code:\n" + code,
                        api_key)
            verdict = extract_json_object(text)
        except Exception as e:
            warn(f"verifier failed for {finding['file']}:{finding['line']}: {e}")
            verdict = None
        return apply_verdict(finding, verdict)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return list(pool.map(one, candidates))
```

- [ ] **Step 5: Replace `main`'s stub body with the full pipeline**

Replace the entire `main` function with:

```python
def main(argv):
    if argv[:1] == ["--self-test"]:
        return self_test()
    t0 = time.time()
    try:
        api_key = load_api_key()
        diff = collect_diff(argv)
        clean_diff, hunks = parse_diff(diff)
        if not clean_diff.strip():
            print(json.dumps({"findings": [], "note": "nothing to review"}))
            return 0
        context, truncated = build_context(clean_diff, hunks, read_repo_file)
        warn(f"reviewing {len(hunks)} file(s), context {len(context)} chars")
        candidates, malformed, failed_roles = run_council(context, api_key)
        candidates = dedupe(candidates)
        warn(f"council produced {len(candidates)} candidate finding(s); verifying")
        results = run_verifier(candidates, read_repo_file, api_key)
        kept = [f for f in results if f["verified"]]
        kept.sort(key=lambda f: (-SEV_RANK[f["severity"]], f["file"], f["line"]))
        print(json.dumps({
            "findings": kept,
            "rejected_count": len(results) - len(kept),
            "stats": {
                "model": MODEL,
                "duration_s": round(time.time() - t0, 1),
                "files_reviewed": len(hunks),
                "context_truncated": truncated,
                "malformed_dropped": malformed,
                "failed_reviewers": failed_roles,
            },
        }, indent=1))
        return 0
    except ReviewError as e:
        print(json.dumps({"error": str(e)}))
        return 1
```

- [ ] **Step 6: Run self-test, verify it passes**

Run: `python3 scripts/review.py --self-test`
Expected: `self-test OK`, exit 0

- [ ] **Step 7: Live smoke test against the running server**

Precondition: `curl -s -H "Authorization: Bearer $(python3 -c "import json,pathlib;print(json.loads((pathlib.Path.home()/'.omlx/settings.json').read_text())['auth']['api_key'])")" http://127.0.0.1:8000/v1/models` lists models. If not, run `omlx start` — if the default model is missing from the list, stop and report to the human partner rather than downloading anything.

In THIS repo, make a scratch edit, review it, revert:

```bash
printf '\ndef scratch_bug(items):\n    return items[1:len(items)]\n' >> scripts/review.py
python3 scripts/review.py > /tmp/lrc-smoke.json; echo "exit: $?"
git checkout -- scripts/review.py
python3 -c "import json; d=json.load(open('/tmp/lrc-smoke.json')); print(json.dumps({'findings': len(d['findings']), 'rejected': d.get('rejected_count'), 'stats': d.get('stats')}, indent=1))"
```

Expected: exit 0, valid JSON with `findings`, `rejected_count`, `stats` keys, `duration_s` populated. (Whether the scratch bug is caught is Task 5's quality eval, not this gate.)

- [ ] **Step 8: Commit**

```bash
git add scripts/review.py prompts/verifier.md
git commit -m "feat: dedupe, verifier pass, full review pipeline

Claude-Session: https://claude.ai/code/session_01Rmsb5J6uiz6tSPcXTjyCYU"
```

---

### Task 5: SKILL.md, install, seeded-bug quality eval

**Files:**
- Create: `SKILL.md`
- Create (outside repo): symlink `~/.claude/skills/local-review` → this repo

**Interfaces:**
- Consumes: the Task 4 stdout contract (`findings`/`rejected_count`/`stats`, error JSON on failure).

- [ ] **Step 1: Write `SKILL.md`**

```markdown
---
name: local-review
description: Review code using a parallel council of local AI reviewers (correctness, security, regression + verifier) running through oMLX. Use when the user asks for a local review of their diff, staged changes, or a ref range.
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/review.py *)
---

# Local Review Council

Run the review engine (args pass through to `git diff`; no args reviews all
uncommitted work):

    python3 ${CLAUDE_SKILL_DIR}/scripts/review.py $ARGUMENTS

The command prints one JSON object. Progress appears on stderr; ignore it.

If the JSON contains an `error` key, show the user the message and stop —
usually the fix is `omlx start`.

Otherwise, report ONLY the findings in the `findings` array (they are
verifier-approved). For each finding:

- Lead with severity, `file:line`, and the title.
- Explain why it matters using `explanation` and `evidence`.
- Mention which reviewers flagged it (`reviewers`) and the `confidence`.
- Offer to fix it.

After the findings, report the rejection line, e.g.
"N candidate findings rejected by verifier." (`rejected_count`).
If `findings` is empty, say the council found nothing verifiable — do NOT
add review comments of your own. Never invent findings beyond the JSON.
If `stats.failed_reviewers` is non-empty or `stats.context_truncated` is
true, mention it as a caveat.
```

- [ ] **Step 2: Install the skill via symlink and verify**

```bash
ln -sfn ~/Projects/local-code-review ~/.claude/skills/local-review
ls -l ~/.claude/skills/local-review/SKILL.md
python3 ~/.claude/skills/local-review/scripts/review.py --self-test
```

Expected: symlink resolves, SKILL.md visible, `self-test OK`. (The skill appears as `/local-review` in NEW Claude Code sessions; that's expected and not verifiable from this one.)

- [ ] **Step 3: Seeded-bug quality eval (spec's success bar)**

Create a scratch repo with two seeded bugs (a dropped lock on a shared dict → race; an off-by-one slice) and one clean control diff:

```bash
SCRATCH=$(mktemp -d)/seeded && mkdir -p "$SCRATCH" && cd "$SCRATCH" && git init -q
cat > store.py <<'EOF'
import threading

_counts = {}
_lock = threading.Lock()

def bump(key):
    with _lock:
        if key not in _counts:
            _counts[key] = 0
        _counts[key] += 1
        return _counts[key]

def top(n):
    with _lock:
        items = sorted(_counts.items(), key=lambda kv: kv[1], reverse=True)
        return items[:n]
EOF
git add . && git commit -qm base

# Seed bugs: bump loses the lock (race), top skips the #1 entry (off-by-one)
cat > store.py <<'EOF'
import threading

_counts = {}
_lock = threading.Lock()

def bump(key):
    if key not in _counts:
        _counts[key] = 0
    _counts[key] += 1
    return _counts[key]

def top(n):
    with _lock:
        items = sorted(_counts.items(), key=lambda kv: kv[1], reverse=True)
        return items[1:n]
EOF
python3 ~/Projects/local-code-review/scripts/review.py > seeded.json; echo "exit: $?"
python3 -c "import json; d=json.load(open('seeded.json')); [print(f['severity'], f['file']+':'+str(f['line']), f['title'], f['confidence']) for f in d['findings']]; print('rejected:', d['rejected_count'])"

# Clean control: comment-only change must produce zero verified findings
git checkout -q -- store.py
printf '\n# tallies are process-local\n' >> store.py
python3 ~/Projects/local-code-review/scripts/review.py > clean.json
python3 -c "import json; d=json.load(open('clean.json')); print('clean findings:', len(d['findings']), 'rejected:', d['rejected_count'])"
```

Success bar (from the spec): the race and the off-by-one both appear as verified findings; the clean diff yields `clean findings: 0`. If a seeded bug is missed or the clean diff produces verified findings, do NOT silently tune prompts/threshold — report the actual output to the human partner and decide together.

- [ ] **Step 4: Commit**

```bash
cd ~/Projects/local-code-review
git add SKILL.md
git commit -m "feat: local-review skill definition and install

Claude-Session: https://claude.ai/code/session_01Rmsb5J6uiz6tSPcXTjyCYU"
```

---

## Post-plan notes

- Out of scope (spec): static analysis, symbol/test discovery, MCP, plugin packaging, hooks, `--fix`/`--deep` flags, JSON Schema validation.
- Known ceilings, accepted for v0: dedupe is O(n²) over findings (fine at council scale); `top(n)`-style quality eval is manual; `extract_json_array` can grab an array inside model "thinking" text — the verifier pass is the guard.
