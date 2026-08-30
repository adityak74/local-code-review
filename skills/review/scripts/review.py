#!/usr/bin/env python3
"""Local Review Council engine: git diff -> code graph routing -> parallel
role reviewers -> verifier -> JSON.

A deterministic first pass (codegraph.py: AST symbol graph, blast radius,
risk ranking) decides WHERE to look; the local oMLX council decides whether
there is actually a bug. Stdlib only.
Specs: docs/superpowers/specs/2026-08-29-local-review-council-design.md
       docs/superpowers/specs/2026-08-30-graph-routing-layer-design.md
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

import codegraph  # sibling module; script dir is on sys.path when run directly

BASE_URL = os.environ.get("OMLX_BASE_URL", "http://127.0.0.1:8000")
MODEL = os.environ.get("LOCAL_REVIEW_MODEL", "lmstudio-community/Qwen3.6-35B-A3B-MLX-4bit")
CONFIDENCE_THRESHOLD = 0.80
CONTEXT_BUDGET = int(os.environ.get("LOCAL_REVIEW_CONTEXT_BUDGET", 80_000))  # chars of prompt text
WINDOW_PAD = 80           # lines around each hunk (no-graph fallback)
SHRUNK_PAD = 20           # pad after budget shrink
SYMBOL_PAD = 5            # pad around symbol spans (already semantic units)
WHOLE_FILE_MAX = 400      # files at or under this many lines are included whole
MAX_WORKERS = 8           # matches oMLX max_concurrent_requests
REQUEST_TIMEOUT = int(os.environ.get("LOCAL_REVIEW_REQUEST_TIMEOUT", 300))  # seconds per model call
SKILL_DIR = Path(__file__).resolve().parent.parent
EXCLUDED_NAMES = {"package-lock.json", "pnpm-lock.yaml"}


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
        raise ReviewError(f"git diff failed: {proc.stderr.strip()[:400]}")
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
    hunks maps new-side path -> [(start, end), ...] 1-based inclusive ranges
    of the lines actually touched (+ lines at their new position, deletions
    at the seam) — NOT whole-hunk spans, which would include context lines
    and pollute the changed-symbol mapping.
    Deleted files (+++ /dev/null) stay in clean_diff but get no hunks entry."""
    kept, hunks = [], {}
    segment, new_path, old_path, ranges, binary = [], None, None, [], False
    new_lineno = None  # new-side position inside the current hunk

    def touch(n):
        n = max(1, n)
        if ranges and n <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], n))
        else:
            ranges.append((n, n))

    def flush():
        nonlocal segment, new_path, old_path, ranges, binary, new_lineno
        path = new_path or old_path
        if segment and path and not binary and not is_excluded(path):
            kept.append("".join(segment))
            if new_path and ranges:
                hunks.setdefault(new_path, []).extend(ranges)
        segment, new_path, old_path, ranges, binary = [], None, None, [], False
        new_lineno = None

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
            new_lineno = int(plus.split(",")[0])
        elif new_lineno is not None and line.startswith("+"):
            touch(new_lineno)
            new_lineno += 1
        elif new_lineno is not None and line.startswith("-"):
            touch(new_lineno)
        elif new_lineno is not None and line.startswith(" "):
            new_lineno += 1
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


def build_context(diff_text, hunks, read_file, analysis=None):
    """Diff (+ impact report + blast-radius code when a graph analysis is
    given) + changed-file windows under CONTEXT_BUDGET. With analysis,
    changed .py files use symbol-span ranges (SYMBOL_PAD); everything else
    keeps hunk windows (WINDOW_PAD). Over budget: cap the diff, drop blast
    blocks lowest-score-first (not counted as truncation — they are bonus
    context), then shrink pads, then drop largest changed blocks.
    Returns (context, truncated)."""
    truncated = False
    if len(diff_text) > CONTEXT_BUDGET:
        diff_text = diff_text[:CONTEXT_BUDGET] + "\n[diff truncated: exceeded context budget]\n"
        truncated = True
    report = analysis["report"] if analysis else ""
    file_ranges = analysis["file_ranges"] if analysis else {}

    entries = []
    for path in sorted(hunks):
        lines = read_file(path)
        if lines is not None:
            ranges = file_ranges.get(path)
            entries.append((path, lines, ranges or hunks[path],
                            SYMBOL_PAD if ranges else WINDOW_PAD))

    def render(shrunk):
        blocks = []
        for path, lines, ranges, pad in entries:
            if len(lines) <= WHOLE_FILE_MAX:
                windows = [(1, len(lines))] if lines else []
            else:
                windows = merge_windows(ranges, min(pad, SHRUNK_PAD) if shrunk else pad,
                                        len(lines))
            blocks.append(format_windows(path, lines, windows))
        return blocks

    extra = []  # blast-radius blocks, already ordered best score first
    for b in (analysis["extra_blocks"] if analysis else []):
        lines = read_file(b["path"])
        if lines:
            windows = merge_windows(b["ranges"], SYMBOL_PAD, len(lines))
            extra.append(format_windows(b["path"], lines, windows))

    def total(blocks):
        return (len(diff_text) + len(report) +
                sum(map(len, blocks)) + sum(map(len, extra)))

    blocks = render(False)
    while extra and total(blocks) > CONTEXT_BUDGET:
        extra.pop()
    if total(blocks) > CONTEXT_BUDGET:
        truncated = True
        blocks = render(True)
        while blocks and total(blocks) > CONTEXT_BUDGET:
            blocks.remove(max(blocks, key=len))

    parts = ["## Diff\n" + diff_text]
    if report:
        parts.append(report)
    parts.append("## Changed file context (line-numbered)\n" + "\n\n".join(blocks))
    if extra:
        parts.append("## Blast radius context (line-numbered, code NOT in the diff)\n"
                     + "\n\n".join(extra))
    return "\n\n".join(parts), truncated


def graph_analysis(hunks):
    """Deterministic first pass (codegraph) over the .py hunks. Returns the
    analysis dict or None; any failure degrades to v0 hunk windows — the
    routing layer must never kill a review."""
    py_hunks = {p: r for p, r in hunks.items() if p.endswith(".py")}
    if not py_hunks:
        return None
    try:
        proc = subprocess.run(["git", "ls-files", "*.py"], cwd=repo_root(),
                              capture_output=True, text=True)
        files = proc.stdout.splitlines() if proc.returncode == 0 else list(py_hunks)
        files = codegraph.prefilter_files(repo_root(), files, set(py_hunks))
        analysis = codegraph.analyze(codegraph.build_graph(repo_root(), files),
                                     py_hunks)
        st = analysis["stats"]
        warn(f"code graph: {st['files_indexed']} files, {st['symbols']} symbols, "
             f"{st['changed_symbols']} changed, {st['impacted_symbols']} impacted, "
             f"{st['untested_changed']} untested")
        return analysis
    except Exception as e:
        warn(f"code graph failed ({type(e).__name__}: {e}); "
             "falling back to hunk windows")
        return None


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
    last_exc = None
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
                last_exc = e
                continue
            good, d = normalize_findings(extract_json_array(text), role)
            findings.extend(good)
            dropped += d
    if len(failed) == len(ROLES):
        raise ReviewError(
            f"all council reviewers failed; is oMLX up at {BASE_URL}? "
            f"try 'omlx start' (last error: {last_exc})")
    return findings, dropped, failed


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
            match.update({k: f[k] for k in
                          ("line", "severity", "title", "explanation", "evidence")})
        for r in f["reviewers"]:
            if r not in match["reviewers"]:
                match["reviewers"].append(r)
    return out


def extract_json_object(text):
    """Last parseable JSON object anywhere in text, else None. The real
    verdict comes last in the response; earlier braces can be template/
    example JSON inside a reasoning model's think-text."""
    decoder = json.JSONDecoder()
    positions = [i for i, ch in enumerate(text) if ch == "{"]
    for i in reversed(positions):
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
    finding["verified"] = v.get("verified") is True and confidence >= CONFIDENCE_THRESHOLD
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
        if lines and finding["line"] <= len(lines):
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
        analysis = graph_analysis(hunks)
        context, truncated = build_context(clean_diff, hunks, read_repo_file, analysis)
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
                **({"graph": analysis["stats"]} if analysis else {}),
            },
        }, indent=1))
        return 0
    except ReviewError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1


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
    # actual touched lines, not whole-hunk spans (context lines excluded)
    assert hunks["api/users.py"] == [(11, 12), (42, 42)], hunks
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
    ctx, truncated = build_context("D" * 70_000, big, lambda p: big_lines)
    assert truncated
    # invariant: once over budget, diff (kept in full here since 70k <
    # CONTEXT_BUDGET) + retained block chars stays within CONTEXT_BUDGET
    # plus a small fixed header/separator overhead -- only a meaningful
    # check if the drop-largest-block loop actually ran.
    assert len(ctx) <= CONTEXT_BUDGET + 300
    assert ctx.count("=== f") < 60             # some file blocks were dropped
    assert "D" * 100 in ctx

    # diff itself exceeds CONTEXT_BUDGET -> capped with an explicit marker
    ctx, truncated = build_context("D" * 500_000, {}, lambda p: [])
    assert truncated is True
    assert len(ctx) < CONTEXT_BUDGET + 1000

    missing = {"api/users.py": [(2, 3)], "moved.py": [(1, 1)]}
    ctx, _ = build_context("DIFFTEXT", missing,
                           lambda p: lines if p == "api/users.py" else None)
    assert "moved.py" not in ctx.split("DIFFTEXT")[1]  # unreadable file skipped

    # -- graph routing integration --------------------------------------
    assert codegraph.self_test() == 0

    numbered = [f"x{i}" for i in range(1, 1001)]       # > WHOLE_FILE_MAX
    fake = {
        "report": "## Impact analysis (deterministic, from the code graph)\n"
                  "- HIGH 0.90 big.py:100-120 f — no test reaches it",
        "file_ranges": {"big.py": [(100, 120)]},
        "extra_blocks": [{"path": "caller.py", "ranges": [(50, 60)], "score": 0.6}],
        "stats": {"changed_symbols": 1},
    }
    ctx, truncated = build_context(
        "DIFF", {"big.py": [(100, 105)], "style.css": [(100, 105)]},
        lambda p: numbered, fake)
    assert not truncated
    assert "## Impact analysis" in ctx and "## Blast radius context" in ctx
    changed_part = ctx.split("## Changed file context")[1].split("## Blast radius")[0]
    big_part = changed_part.split("=== style.css ===")[0]
    css_part = changed_part.split("=== style.css ===")[1]
    assert "95: x95" in big_part and "125: x125" in big_part   # SYMBOL_PAD spans
    assert "94: x94" not in big_part                            # not hunk windows
    assert "25: x25" in css_part                # non-.py keeps WINDOW_PAD windows
    blast_part = ctx.split("## Blast radius")[1]
    assert "45: x45" in blast_part and "65: x65" in blast_part

    # over budget: blast blocks are dropped first, silently (no truncation flag)
    many = dict(fake, extra_blocks=[{"path": f"c{n}.py", "ranges": [(1, 1000)],
                                     "score": 0.5} for n in range(60)])
    ctx, truncated = build_context("DIFF", {"big.py": [(100, 105)]},
                                   lambda p: numbered, many)
    assert not truncated
    assert len(ctx) <= CONTEXT_BUDGET + 300
    assert "=== big.py ===" in ctx                 # changed context survives
    assert ctx.count("=== c") < 60                 # most blast blocks dropped

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

    for role in ROLES + ("verifier",):
        assert load_prompt(role).strip(), f"prompt {role}.md missing or empty"

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
                  and f["line"] == 11)            # newcomer's whole record wins
    assert merged["severity"] == "high"

    assert extract_json_object('ok {"verified": true} done') == {"verified": True}
    assert extract_json_object('nothing here') is None
    assert extract_json_object(
        '<think>maybe {"verified": true, "confidence": 0.95}</think>\n'
        '{"verified": false, "confidence": 0.1, "note": "not real"}'
    ) == {"verified": False, "confidence": 0.1, "note": "not real"}

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

    f = apply_verdict(mk("a.py", 1, "high", "correctness"),
                      {"verified": "false", "confidence": 0.99})
    assert not f["verified"]

    f = apply_verdict(mk("a.py", 1, "high", "correctness"),
                      {"verified": "true", "confidence": 0.99})
    assert not f["verified"]                 # stringified booleans are malformed too

    print("self-test OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
