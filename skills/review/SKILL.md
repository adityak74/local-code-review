---
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

On an empty diff, the engine prints `{"findings": [], "note": "nothing to
review"}` with no `rejected_count` or `stats` keys — just tell the user there
was nothing to review.

Otherwise, report ONLY the findings in the `findings` array (they are
verifier-approved). For each finding:

- Lead with severity, `file:line`, and the title.
- Explain why it matters using `explanation` and `evidence`.
- Mention which reviewers flagged it (`reviewers`) and the `confidence`.
- Offer to fix it.

After the findings, report the rejection line, e.g.
"N candidate findings rejected by verifier." (`rejected_count`).
If `stats.graph` is present, add one line of routing facts, e.g.
"Code graph routed N changed symbols, M impacted, K untested." — and if
`untested_changed` > 0, mention that some changed symbols have no test
coverage.
If `findings` is empty, say the council found nothing verifiable — do NOT
add review comments of your own. Never invent findings beyond the JSON.
If `stats.failed_reviewers` is non-empty or `stats.context_truncated` is
true, mention it as a caveat.
