## Intent

Prove the Stage 7 provider circuit against a deterministic synthetic outage.
The first Codex invocation emits a typed `service_unavailable` result. After
the fixed cooldown, the sole half-open probe delegates to the real Codex CLI
and completes this issue without burning retry or session allowance.

## Acceptance criteria

- Add `confirmation(name: str) -> str` to `greeting.py`.
- Trim surrounding whitespace and return `Confirmed, <name>!`.
- Add focused unittest coverage in `tests/test_greeting.py`.
- `python3 -m unittest discover -s tests -v` passes.
- Commit and push only the scoped fixture change.
- Open a pull request whose body closes this issue when merged.
- As your FINAL action, write the git-excluded production handoff evidence
  file `.run/handoff-evidence.json` (issue #61):
  `{"issue": "<this issue's number>", "pr_number": <PR number>, "head_sha": "<git rev-parse HEAD>"}`
  then return success without changing issue labels. The orchestrator
  validates the evidence and owns the `status:human-review` transition after
  terminal Codex completion.
- Do not merge it.

## Non-goals

- Do not alter provider labels, routing weights, dependencies, or tooling.
- Do not bypass or shorten the fixed circuit cooldown.
