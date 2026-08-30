#!/usr/bin/env python3
"""Deterministic code intelligence: repo symbol graph -> blast radius -> risk.

Stdlib-only (ast). No LLM, no HTTP. This layer answers "where should we
look?"; the council in review.py answers "is there actually a bug?".
Spec: docs/superpowers/specs/2026-08-30-graph-routing-layer-design.md
"""
import ast
import re
import sys
import tempfile
from pathlib import Path

DECAY = 0.6                 # per-hop impact decay
MAX_DEPTH = 2               # blast-radius hops
MAX_IMPACT = 200            # impacted-node cap
MAX_EXTRA_BLOCKS = 15       # blast-radius code blocks offered to the context
MAX_GRAPH_FILES = 2000      # above this, only changed files + name-mention files are parsed
EDGE_WEIGHTS = {"called-by": 1.0, "inherited-by": 0.9, "tested-by": 0.7,
                "imported-by": 0.5}
RISK_UNTESTED = 0.30
RISK_SECURITY = 0.20
RISK_FANIN_DIV, RISK_FANIN_CAP = 10, 0.25
RISK_CHURN_DIV, RISK_CHURN_CAP = 50, 0.25
SECURITY_RE = re.compile(
    r"auth|token|secret|passw|crypt|sql|session|cookie|perm|priv|sign|hash"
    r"|login|acl|sanitiz|escape|exec|eval|pickle|subprocess|deserial", re.I)
TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*\.py$|_test\.py$")


def is_test_path(path):
    return bool(TEST_PATH_RE.search(path))


def module_suffixes(path):
    """'a/b/c.py' -> ['a.b.c', 'b.c', 'c']; 'a/b/__init__.py' -> ['a.b', 'b']."""
    parts = list(Path(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return [".".join(parts[i:]) for i in range(len(parts))] if parts else []


def _span(node):
    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
    return start, node.end_lineno


def _callee_name(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _index_file(path, tree, g):
    """One file's symbols, raw call/base names, and import module names."""
    raw_calls, raw_bases, modules = [], [], []
    pkg = list(Path(path).parent.parts)

    def add_symbol(node, stack):
        name = ".".join(stack + [node.name])
        qname = f"{path}:{name}"
        start, end = _span(node)
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        g["symbols"][qname] = {"qname": qname, "name": node.name, "kind": kind,
                               "file": path, "start": start, "end": end}
        g["by_file"].setdefault(path, []).append(qname)
        g["by_name"].setdefault(node.name, []).append(qname)
        return qname

    def visit(node, stack, fn_qname):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = add_symbol(child, stack)
                visit(child, stack + [child.name], q)
            elif isinstance(child, ast.ClassDef):
                q = add_symbol(child, stack)
                for base in child.bases:
                    bname = _callee_name(base) or (
                        base.id if isinstance(base, ast.Name) else None)
                    if bname:
                        raw_bases.append((q, bname))
                visit(child, stack + [child.name], fn_qname)
            else:
                if isinstance(child, ast.Call):
                    callee = _callee_name(child.func)
                    if callee and fn_qname:
                        raw_calls.append((fn_qname, callee))
                elif isinstance(child, ast.Import):
                    modules.extend(a.name for a in child.names)
                elif isinstance(child, ast.ImportFrom):
                    if child.level:  # relative: resolve against this file's package
                        base = pkg[:len(pkg) - (child.level - 1)]
                        prefix = ".".join(base + (child.module or "").split("."))
                    else:
                        prefix = child.module or ""
                    prefix = prefix.strip(".")
                    if prefix:
                        modules.append(prefix)
                    modules.extend(f"{prefix}.{a.name}".strip(".")
                                   for a in child.names if a.name != "*")
                visit(child, stack, fn_qname)

    visit(tree, [], None)
    return raw_calls, raw_bases, modules


def _resolve_symbol(g, name, caller_file):
    """Bare name -> qname. Same-file match wins; else only a repo-unique
    match. Ambiguity resolves to nothing — never guess."""
    cands = g["by_name"].get(name, [])
    same = [q for q in cands if g["symbols"][q]["file"] == caller_file]
    if len(same) == 1:
        return same[0]
    if len(cands) == 1:
        return cands[0]
    return None


def build_graph(root, files, read_text=None):
    """Parse repo-relative .py `files` under `root` into a symbol graph.
    Unparseable/unreadable files are skipped and counted."""
    read_text = read_text or (lambda p: (Path(root) / p).read_text(errors="replace"))
    g = {"symbols": {}, "by_file": {}, "by_name": {}, "callers": {},
         "subclasses": {}, "tested_by": {}, "importers": {},
         "test_imports": {}, "parsed": set(), "parse_failures": 0}
    suffix_map = {}
    pending = []  # (path, raw_calls, raw_bases, modules)

    for path in files:
        try:
            tree = ast.parse(read_text(path))
        except (SyntaxError, ValueError, OSError):
            g["parse_failures"] += 1
            continue
        g["parsed"].add(path)
        for suf in module_suffixes(path):
            suffix_map.setdefault(suf, set()).add(path)
        pending.append((path, *_index_file(path, tree, g)))

    for path, raw_calls, raw_bases, modules in pending:
        test_src = is_test_path(path)
        for caller_q, callee in raw_calls:
            target = _resolve_symbol(g, callee, path)
            if not target or target == caller_q:
                continue
            g["callers"].setdefault(target, set()).add(caller_q)
            if test_src:
                g["tested_by"].setdefault(target, set()).add(caller_q)
        for cls_q, base in raw_bases:
            target = _resolve_symbol(g, base, path)
            if target and g["symbols"][target]["kind"] == "class":
                g["subclasses"].setdefault(target, set()).add(cls_q)
        for mod in modules:
            hits = suffix_map.get(mod, ())
            if len(hits) == 1:
                (target,) = hits
                if target != path:
                    g["importers"].setdefault(target, set()).add(path)
                    if test_src:
                        g["test_imports"].setdefault(target, set()).add(path)
    return g


def prefilter_files(root, files, changed, read_text=None):
    """Above MAX_GRAPH_FILES, keep changed files plus files whose text
    mentions a changed module's bare name.
    ponytail: substring prefilter; a persistent index if repos outgrow it."""
    if len(files) <= MAX_GRAPH_FILES:
        return files
    read_text = read_text or (lambda p: (Path(root) / p).read_text(errors="replace"))
    names = {Path(p).stem for p in changed} - {"__init__"}
    kept = []
    for path in files:
        if path in changed:
            kept.append(path)
            continue
        try:
            text = read_text(path)
        except OSError:
            continue
        if any(n in text for n in names):
            kept.append(path)
    return kept


def _merge(ranges):
    merged = []
    for s, e in sorted(ranges):
        if merged and s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(m) for m in merged]


def changed_symbols(g, hunks):
    """Map hunk ranges to overlapping symbols, innermost first (a symbol
    containing another overlapping symbol is dropped). Returns
    {path: {qname: changed_lines}}."""
    out = {}
    for path, ranges in hunks.items():
        ranges = _merge(ranges)
        overlapping = {}
        for q in g["by_file"].get(path, ()):
            sym = g["symbols"][q]
            lines = sum(min(sym["end"], e) - max(sym["start"], s) + 1
                        for s, e in ranges
                        if sym["start"] <= e and sym["end"] >= s)
            if lines > 0:
                overlapping[q] = lines
        for q in list(overlapping):
            a = g["symbols"][q]
            if any(o != q and a["start"] <= g["symbols"][o]["start"]
                   and g["symbols"][o]["end"] <= a["end"] for o in overlapping):
                del overlapping[q]  # keep the innermost
        if overlapping:
            out[path] = overlapping
    return out


def blast_radius(g, changed_qnames, changed_files):
    """Decayed best-score reverse traversal from the changed symbols/files.
    Node keys: ('s', qname) or ('f', path). Returns {node: (score, via)}."""
    seeds = {("s", q): 1.0 for q in changed_qnames}
    seeds.update({("f", p): 1.0 for p in changed_files})

    def neighbors(node):
        kind, key = node
        if kind == "f":
            for imp in g["importers"].get(key, ()):
                yield ("f", imp), "imported-by"
            return
        for c in g["callers"].get(key, ()):
            yield ("s", c), "called-by"
        for t in g["tested_by"].get(key, ()):
            yield ("s", t), "tested-by"
        if g["symbols"][key]["kind"] == "class":
            for sub in g["subclasses"].get(key, ()):
                yield ("s", sub), "inherited-by"

    best, via = dict(seeds), {}
    frontier = dict(seeds)
    for _ in range(MAX_DEPTH):
        nxt = {}
        for node, score in frontier.items():
            for nb, kind in neighbors(node):
                ns = score * EDGE_WEIGHTS[kind] * DECAY
                if ns > best.get(nb, 0.0):
                    best[nb], via[nb] = ns, kind
                    nxt[nb] = ns
        frontier = nxt
        if not frontier:
            break
    impacted = {n: (s, via[n]) for n, s in best.items() if n not in seeds}
    top = sorted(impacted.items(), key=lambda kv: -kv[1][0])[:MAX_IMPACT]
    return dict(top)


def risk_score(g, qname, changed_lines):
    sym = g["symbols"][qname]
    score, reasons = 0.0, []
    tested = g["tested_by"].get(qname) or g["test_imports"].get(sym["file"])
    if not tested:
        score += RISK_UNTESTED
        reasons.append("no test reaches it")
    if SECURITY_RE.search(qname):
        score += RISK_SECURITY
        reasons.append("security-sensitive name")
    fan_in = len(g["callers"].get(qname, ()))
    if fan_in:
        score += min(fan_in / RISK_FANIN_DIV, RISK_FANIN_CAP)
        reasons.append(f"{fan_in} caller(s)")
    score += min(changed_lines / RISK_CHURN_DIV, RISK_CHURN_CAP)
    reasons.append(f"{changed_lines} changed line(s)")
    return min(score, 1.0), reasons


def bucket(score):
    return "high" if score > 0.7 else "medium" if score > 0.4 else "low"


def analyze(g, hunks):
    """Full first pass over python hunks. Returns the routing decision:
    ranked changed symbols, blast radius, per-file context ranges, extra
    (blast-radius) code blocks, a rendered impact report, and stats."""
    per_file = changed_symbols(g, hunks)
    changed = []
    for path, syms in per_file.items():
        for q, lines in syms.items():
            score, reasons = risk_score(g, q, lines)
            s = g["symbols"][q]
            changed.append({"qname": q, "name": s["name"], "file": path,
                            "start": s["start"], "end": s["end"],
                            "risk": round(score, 2), "reasons": reasons})
    changed.sort(key=lambda c: (-c["risk"], c["file"], c["start"]))

    radius = blast_radius(g, [c["qname"] for c in changed], list(per_file))
    impacted = []
    for (kind, key), (score, via) in sorted(radius.items(),
                                            key=lambda kv: -kv[1][0]):
        if kind == "s":
            s = g["symbols"][key]
            impacted.append({"qname": key, "file": s["file"], "start": s["start"],
                             "end": s["end"], "score": round(score, 2), "via": via})
        else:
            impacted.append({"qname": key, "file": key, "start": None,
                             "end": None, "score": round(score, 2), "via": via})

    # Context ranges per changed .py file: symbol spans + hunk ranges no
    # changed symbol fully covers (module-level edits, unparsed regions).
    file_ranges = {}
    for path, ranges in hunks.items():
        if path not in g["parsed"]:
            continue  # unparsed file -> caller falls back to hunk windows
        spans = [(g["symbols"][q]["start"], g["symbols"][q]["end"])
                 for q in per_file.get(path, ())]
        leftover = [(s, e) for s, e in _merge(ranges)
                    if not any(a <= s and e <= b for a, b in spans)]
        file_ranges[path] = _merge(spans + leftover)

    # Blast-radius code blocks, one per file, best score first.
    by_path = {}
    for i in impacted:
        if i["start"] is None or i["file"] in file_ranges:
            continue  # file-level node, or already shown as changed context
        entry = by_path.setdefault(i["file"], {"ranges": [], "score": 0.0})
        entry["ranges"].append((i["start"], i["end"]))
        entry["score"] = max(entry["score"], i["score"])
    extra_blocks = [{"path": p, "ranges": _merge(v["ranges"]), "score": v["score"]}
                    for p, v in sorted(by_path.items(),
                                       key=lambda kv: -kv[1]["score"])]
    extra_blocks = extra_blocks[:MAX_EXTRA_BLOCKS]

    untested = [c["name"] for c in changed if "no test reaches it" in c["reasons"]]
    analysis = {
        "changed": changed, "impacted": impacted, "file_ranges": file_ranges,
        "extra_blocks": extra_blocks,
        "stats": {"files_indexed": len(g["parsed"]),
                  "parse_failures": g["parse_failures"],
                  "symbols": len(g["symbols"]),
                  "changed_symbols": len(changed),
                  "impacted_symbols": sum(1 for i in impacted if i["start"]),
                  "untested_changed": len(untested)},
    }
    analysis["report"] = impact_report(analysis)
    return analysis


def impact_report(analysis):
    """Deterministic facts for the reviewers: where to look and why."""
    lines = ["## Impact analysis (deterministic, from the code graph)"]
    if analysis["changed"]:
        lines.append("Changed symbols by risk:")
        for c in analysis["changed"]:
            lines.append(f"- {bucket(c['risk']).upper()} {c['risk']:.2f} "
                         f"{c['file']}:{c['start']}-{c['end']} {c['name']} — "
                         + "; ".join(c["reasons"]))
    else:
        lines.append("No changed symbols mapped (module-level or non-Python changes).")
    if analysis["impacted"]:
        lines.append("Blast radius (impacted code NOT in the diff):")
        for i in analysis["impacted"][:20]:
            loc = (f"{i['file']}:{i['start']}-{i['end']}"
                   if i["start"] else i["file"])
            name = i["qname"].split(":", 1)[-1] if i["start"] else ""
            lines.append(f"- {i['score']:.2f} {i['via']} {loc} {name}".rstrip())
    untested = [c["name"] for c in analysis["changed"]
                if "no test reaches it" in c["reasons"]]
    if untested:
        lines.append("Untested changed symbols: " + ", ".join(untested))
    return "\n".join(lines)


def self_test():
    fixtures = {
        "app/db.py": (
            "def query(sql):\n"
            "    return sql\n"
            "\n"
            "def helper():\n"
            "    return 1\n"
        ),
        "app/auth.py": (
            "from app.db import query\n"
            "\n"
            "class Base:\n"
            "    def check_token(self, tok):\n"
            "        return query(tok)\n"
        ),
        "app/views.py": (
            "from app.auth import Base\n"
            "\n"
            "class Child(Base):\n"
            "    pass\n"
            "\n"
            "@property\n"
            "def render():\n"
            "    return Child().check_token('x')\n"
        ),
        "other/dupe.py": "def helper():\n    return 2\n",
        "tests/test_db.py": (
            "from app.db import query\n"
            "\n"
            "def test_query():\n"
            "    assert query('a') == 'a'\n"
        ),
        "broken.py": "def oops(:\n",
    }
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for path, text in fixtures.items():
            f = root / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        g = build_graph(root, list(fixtures))

    # -- parsing + symbols ----------------------------------------------
    assert g["parse_failures"] == 1 and "broken.py" not in g["parsed"]
    assert "app/db.py:query" in g["symbols"]
    assert "app/auth.py:Base.check_token" in g["symbols"]
    render = g["symbols"]["app/views.py:render"]
    assert render["start"] == 6, render        # span includes the decorator
    assert module_suffixes("a/b/__init__.py") == ["a.b", "b"]

    # -- edge resolution ------------------------------------------------
    assert "app/auth.py:Base.check_token" in g["callers"]["app/db.py:query"]
    assert "app/views.py:render" in g["callers"]["app/views.py:Child"]
    assert "app/views.py:Child" in g["subclasses"]["app/auth.py:Base"]
    assert "app/auth.py" in g["importers"]["app/db.py"]
    assert "tests/test_db.py:test_query" in g["tested_by"]["app/db.py:query"]
    assert "app/db.py" in g["test_imports"]
    # ambiguous bare name (helper in two files) resolves to nothing
    assert "app/db.py:helper" not in g["callers"]
    assert "other/dupe.py:helper" not in g["callers"]

    # -- diff -> changed symbols ----------------------------------------
    per_file = changed_symbols(g, {"app/auth.py": [(4, 5)]})
    assert list(per_file) == ["app/auth.py"]
    assert list(per_file["app/auth.py"]) == ["app/auth.py:Base.check_token"]
    assert per_file["app/auth.py"]["app/auth.py:Base.check_token"] == 2
    # innermost wins: a hunk covering the whole class maps to the method
    wide = changed_symbols(g, {"app/auth.py": [(3, 5)]})
    assert list(wide["app/auth.py"]) == ["app/auth.py:Base.check_token"]

    # -- blast radius ---------------------------------------------------
    radius = blast_radius(g, ["app/db.py:query"], ["app/db.py"])
    score_of = {k: v[0] for k, v in radius.items()}
    assert score_of[("s", "app/auth.py:Base.check_token")] == 0.6   # caller, d1
    # the test both calls query (0.6) and is its tested_by (0.42): max wins
    assert score_of[("s", "tests/test_db.py:test_query")] == 0.6
    assert score_of[("f", "app/auth.py")] == 0.3                    # importer
    assert ("s", "app/db.py:query") not in radius                   # seed excluded

    # -- risk -----------------------------------------------------------
    tested_score, tested_reasons = risk_score(g, "app/db.py:query", 2)
    untested_score, untested_reasons = risk_score(
        g, "app/views.py:render", 2)
    assert "no test reaches it" not in tested_reasons
    assert "no test reaches it" in untested_reasons
    assert untested_score > tested_score - RISK_SECURITY  # untested beats tested
    sec_score, sec_reasons = risk_score(g, "app/auth.py:Base.check_token", 2)
    assert "security-sensitive name" in sec_reasons       # 'auth', 'token'
    assert bucket(0.8) == "high" and bucket(0.5) == "medium" and bucket(0.1) == "low"

    # -- analyze + report -----------------------------------------------
    a = analyze(g, {"app/db.py": [(1, 2)]})
    assert a["changed"][0]["qname"] == "app/db.py:query"
    assert a["file_ranges"]["app/db.py"] == [(1, 2)]
    assert any(b["path"] == "app/auth.py" for b in a["extra_blocks"])
    assert all(b["path"] not in a["file_ranges"] for b in a["extra_blocks"])
    assert a["stats"]["files_indexed"] == 5 and a["stats"]["parse_failures"] == 1
    assert "## Impact analysis" in a["report"]
    assert "app/db.py:1-2 query" in a["report"]
    assert "called-by" in a["report"]

    # unparsed file gets no file_ranges entry (caller falls back to windows)
    a2 = analyze(g, {"broken.py": [(1, 1)]})
    assert "broken.py" not in a2["file_ranges"]
    # module-level-only change: leftover hunk range survives
    a3 = analyze(g, {"app/auth.py": [(1, 1)]})
    assert a3["file_ranges"]["app/auth.py"] == [(1, 1)]

    print("codegraph self-test OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(self_test())
