# Deterministic Graph Routing Layer — v1 Design

**Date:** 2026-08-30
**Status:** Approved direction (user directive), implemented alongside this spec
**Supersedes:** the "Context building" stage of
[2026-08-29-local-review-council-design.md](2026-08-29-local-review-council-design.md).
Everything downstream (council, dedupe, verifier, output contract) is unchanged.
**Inspiration:** [tirth8205/code-review-graph](https://github.com/tirth8205/code-review-graph)
— its diff→node overlap mapping, decayed impact traversal, additive risk score,
and reversed TESTED_BY edges are borrowed in simplified stdlib form. Its
tree-sitter/SQLite machinery is deliberately not.

## The principle

The graph layer answers **"where should we look?"** deterministically; the LLM
layer answers **"is there actually a bug here, why, and how confident are we?"**
The first pass is never LLM-based. This is the differentiation: a local-first
review engine where deterministic code intelligence is the routing layer for a
fast ensemble of MLX reviewers — not another generic LLM PR reviewer.

## Pipeline (v1)

```
review.py [git-diff-args...]
  1. Collect diff          git diff HEAD (default) or verbatim args
  2. Code graph            stdlib-ast index of repo *.py: symbols + call/
                           import/inheritance/test edges          (codegraph.py)
  3. Blast radius + risk   changed symbols via line-overlap; decayed reverse
                           traversal; additive risk score          (codegraph.py)
  4. Route context         impact report + changed-symbol bodies + top-ranked
                           caller/test code, budgeted; hunk windows for
                           non-Python files and as total fallback   (review.py)
  5. Council               3 parallel role reviewers (unchanged)
  6. Aggregate + Verify    dedupe, per-finding verifier (unchanged)
  7. Emit JSON             + stats.graph
```

## codegraph.py (new file, stdlib-only)

Second file in the engine. The split IS the architecture: `codegraph.py` is the
deterministic intelligence (no HTTP, no LLM, pure functions), `review.py` is
orchestration + rendering + LLM plumbing. `review.py` imports `codegraph`;
never the reverse.

### Graph build — `build_graph(root, files)`

- Input: repo root + repo-relative `*.py` paths (from `git ls-files`).
- `ast.parse` each file (syntax errors: file skipped, counted).
- **Nodes:** functions, methods, classes, with 1-based line spans
  (`lineno..end_lineno`, decorators included). Qualified name =
  `path:Class.name` — unique by construction.
- **Edges:**
  - *calls* — every `ast.Call` in a body, callee taken as bare name
    (`Name.id` or `Attribute.attr`). Resolved to a symbol only when
    unambiguous: exactly one repo-wide match, or a same-file match wins.
    Ambiguous names are dropped, never guessed (the reference repo's
    unique-suffix rule, applied to symbols).
  - *imports* — `import`/`from … import` module names resolved to repo files
    by unique module-path suffix; file-level reverse map (who imports me).
  - *inherits* — `ClassDef.bases` bare names, same resolution rule.
  - *tested_by* — a file is a test file by path pattern (`test_*.py`,
    `*_test.py`, `tests/`, `test/`, `__tests__/`). Every resolved call edge
    whose caller lives in a test file becomes a reversed production→test edge.
- Scale guard: above `MAX_GRAPH_FILES` (2000) only changed files plus files
  whose raw text mentions a changed module's name are parsed (grep prefilter).

### Changed symbols — line overlap

Diff hunks map to symbols by pure interval overlap. `parse_diff` now records
the lines actually touched (+ lines at their new-side position, deletions at
the seam) rather than whole-hunk spans — hunk context lines must not mark
neighboring symbols as changed. Mapping is (`sym.start <= hunk_end and sym.end >= hunk_start`), innermost first
(method over class). A hunk overlapping no symbol stays file-level and keeps
the v0 window treatment.

### Blast radius — decayed reverse traversal

From each changed symbol, walk reverse edges up to depth 2:

```
score(neighbor) = score(node) * weight(edge) * DECAY        DECAY = 0.6
weights: called-by 1.0 · inherited-by 0.9 · imported-by 0.5
```

Best score per symbol wins; results capped at `MAX_IMPACT` (200). Changed
symbols are excluded from the impacted set.

### Risk score — additive, clamped to 1.0, per changed symbol

| signal | contribution |
|---|---|
| no test reaches it (no tested_by, and no test file imports its file) | +0.30 |
| security-sensitive name/path (auth, token, secret, sql, crypt, …) | +0.20 |
| fan-in | `min(callers / 10, 0.25)` |
| churn | `min(changed_lines / 50, 0.25)` |

File risk = max of its symbols. Buckets: >0.7 high, >0.4 medium, else low.
Every score comes with its reasons spelled out — the impact report never says
"0.72" without saying why.

### Output — `analyze(graph, hunks)`

A plain dict: `changed` (risk-ranked symbols with spans, reasons, caller and
test qnames), `impacted` (score-ranked, with the edge kind that reached them),
`stats` (files_indexed, parse_failures, symbols, changed_symbols,
impacted_symbols, untested_changed). Plus `impact_report(analysis)` rendering
the deterministic facts as the `## Impact analysis` prompt section.

## review.py integration

`build_context` gains a graph-aware path:

1. `## Diff` — unchanged, capped at `CONTEXT_BUDGET`.
2. `## Impact analysis` — deterministic facts (small, always fits).
3. `## Changed code` — changed-symbol spans (semantic units, small pad)
   instead of blind ±80-line hunk windows; whole file if ≤400 lines as before.
   Non-Python changed files keep the v0 hunk windows.
4. `## Blast radius` — bodies of impacted callers and covering tests, highest
   score first, dropped lowest-first under budget (replacing v0's
   "drop largest block" heuristic with risk-aware dropping for Python).

Any exception inside codegraph degrades to the v0 windows path with a stderr
warning — the routing layer must never kill a review. `stats.graph` is added
to the output JSON when the graph path ran.

## Explicitly not doing (and why)

- **No tree-sitter / multi-language symbol graph of our own.** Stdlib-only is
  a README promise. Non-Python files keep v0 hunk windows — amended
  2026-08-31, see below: an already-installed external graph may fill in.
- **No SQLite persistence / incremental index.** A full `ast.parse` of a
  ≤2000-file repo is seconds; caching earns its place when someone measures it.
- **No betweenness centrality, communities, flow criticality.** Fan-in, tests,
  names, and churn already separate high-risk from low-risk changes at diff
  scale.
- **No LLM anywhere in stages 2–4.** By definition.

## Testing

`codegraph.self_test()` builds a fixture repo in a temp dir (modules with
calls, inheritance, imports, an ambiguous name, a test file) and asserts:
symbol spans, edge resolution (including ambiguity refusal), hunk→symbol
mapping, blast-radius membership and ordering, risk ordering (untested >
tested, security-named > plain), and report rendering.
`review.py --self-test` runs it plus integration asserts (graph context
sections present for Python, v0 fallback for non-Python and on failure).


## Amendment 2026-08-31 — optional `code-review-graph` adapter

Multi-language routing arrived as a *delegation*, not a build. When the
external [`code-review-graph`](https://github.com/tirth8205/code-review-graph)
CLI is on `PATH` and has a graph for the repo, `crg_analysis()` shells out to
`detect-changes --base <base>` and `crg_report()` normalizes the JSON into the
existing analysis contract (`report`, `file_ranges`, `extra_blocks`, `stats`).
`graph_analysis()` merges the two passes; their `file_ranges` are disjoint by
construction (`.py` vs everything else).

Binding constraints:

- **Python is never delegated.** `codegraph.py` is more precise on `.py` —
  it seeds from touched lines, not whole files, and it is the only source of
  blast radius. The adapter covers non-`.py` files only, and contributes no
  `extra_blocks`.
- **Trust nothing that does not overlap our own diff.** The external graph
  may be stale or built against a different base. Every reported symbol is
  intersected with the lines `parse_diff` recorded as touched; non-overlapping
  symbols are dropped. A wrong symbol is worse than no symbol.
- **Symbol spans never swallow a hunk.** As in `analyze()`, touched ranges no
  span fully covers are kept alongside the spans, so top-level edits survive.
- **Read-only.** The engine calls `detect-changes` only; it never runs
  `build`, `update`, or `watch`, and never writes `.code-review-graph/`.
  Keeping that graph fresh is the user's business.
- **Still optional.** Not installed, no graph, non-zero exit, bad JSON, over
  `CRG_TIMEOUT` (60s), a `--`-prefixed diff spec, or `LOCAL_REVIEW_CRG=0` →
  `None` and those files keep hunk windows. The engine stays stdlib-only; the
  dependency is one the user either already has or does not.

`stats.graph.crg` is added when the adapter contributed. `crg_report` is pure
and self-tested on a fixture: overlap filtering, stale-symbol rejection,
foreign-file rejection, test-gap reasons, leftover-hunk preservation.