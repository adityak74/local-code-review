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

    print("self-test OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
