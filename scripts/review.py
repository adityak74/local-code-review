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
