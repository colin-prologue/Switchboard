"""Tests for prompt rendering.

implements: core §17.1 (prompt template renders issue/attempt; strict-mode
failure on unknown variables), core §12.4 (rendering failure semantics).
"""

from __future__ import annotations

import pytest

from orchestrator.prompt import render_prompt
from orchestrator.types import BlockerRef, Issue, WorkflowError


def make_issue(**overrides) -> Issue:
    defaults = dict(
        id="I_1",
        identifier="42",
        title="Fix the thing",
        description="Some description.",
        priority=None,
        state="todo",
        branch_name="switchboard/issue-42",
        url="https://github.com/acme/widgets/issues/42",
        labels=["bug", "status:todo"],
        blocked_by=[BlockerRef(id="I_0", identifier="41", state="closed")],
        created_at=None,
        updated_at=None,
    )
    defaults.update(overrides)
    return Issue(**defaults)


def test_renders_issue_fields_and_attempt():
    template = "{{ issue.identifier }}: {{ issue.title }} — {{ issue.description }} (attempt {{ attempt }})"
    out = render_prompt(template, make_issue(), attempt=2)
    assert out == "42: Fix the thing — Some description. (attempt 2)"


def test_attempt_none_on_first_attempt():
    template = "attempt={{ attempt }}"
    out = render_prompt(template, make_issue(), attempt=None)
    assert out == "attempt="


def test_labels_are_iterable():
    template = "{% for l in issue.labels %}{{ l }},{% endfor %}"
    out = render_prompt(template, make_issue(), attempt=None)
    assert out == "bug,status:todo,"


def test_blocked_by_nested_iterable():
    template = "{% for b in issue.blocked_by %}{{ b.identifier }}:{{ b.state }}{% endfor %}"
    out = render_prompt(template, make_issue(), attempt=None)
    assert out == "41:closed"


def test_unknown_variable_raises_template_render_error():
    template = "{{ issue.identifier }} {{ nonexistent_var }}"
    with pytest.raises(WorkflowError) as exc_info:
        render_prompt(template, make_issue(), attempt=None)
    assert exc_info.value.code == "template_render_error"


def test_unknown_nested_issue_field_raises():
    template = "{{ issue.not_a_real_field }}"
    with pytest.raises(WorkflowError) as exc_info:
        render_prompt(template, make_issue(), attempt=None)
    assert exc_info.value.code == "template_render_error"


def test_unknown_filter_raises_template_parse_error():
    template = "{{ issue.title | totally_made_up_filter }}"
    with pytest.raises(WorkflowError) as exc_info:
        render_prompt(template, make_issue(), attempt=None)
    assert exc_info.value.code == "template_parse_error"


def test_empty_template_returns_default_prompt():
    assert render_prompt("", make_issue(), attempt=None) == "You are working on an issue from GitHub."


def test_whitespace_only_template_returns_default_prompt():
    assert render_prompt("   \n\t  ", make_issue(), attempt=None) == "You are working on an issue from GitHub."


def test_datetime_fields_render_safely():
    from datetime import datetime, timezone

    issue = make_issue(created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
    template = "created={{ issue.created_at }}"
    out = render_prompt(template, issue, attempt=None)
    assert "2024-01-01" in out


def test_none_priority_renders_without_raising():
    template = "priority={{ issue.priority }}"
    out = render_prompt(template, make_issue(priority=None), attempt=None)
    assert out == "priority="


# --- triage branch: the real WORKFLOW.base.md body must gate on the label -----

def _real_base_body() -> str:
    """The real prompt body, with scaffold placeholders substituted as
    register-project.sh would (leaving only render-time Liquid)."""
    from pathlib import Path

    real_path = Path(__file__).resolve().parents[2] / "workflow" / "WORKFLOW.base.md"
    text = real_path.read_text(encoding="utf-8")
    substituted = (
        text.replace("{{REPO}}", "acme/widgets")
        .replace("{{WORKSPACE_ROOT}}", "/tmp/ws")
        .replace("{{MAX_AGENTS}}", "10")
        .replace("{{CONVENTION_ROOT}}", "")
    )
    # Drop the YAML front matter; keep the prompt body Symphony would render.
    return substituted.split("---", 2)[2].strip()


def test_triage_label_selects_verifier_branch():
    out = render_prompt(_real_base_body(), make_issue(labels=["status:triage"]), attempt=None)
    assert "Triage mode" in out
    assert "Triage verdict" in out
    assert "## How to work it" not in out  # implementer branch suppressed


def test_absent_triage_label_selects_implementer_branch():
    out = render_prompt(_real_base_body(), make_issue(labels=["bug", "status:todo"]), attempt=None)
    assert "## How to work it" in out
    assert "Triage mode" not in out


# --- triage PASS/NEEDS-WORK stamp the gate:triage-passed marker (issue #29) ----

def _pass_command_line(rendered: str) -> str:
    """The single `gh issue edit ... --add-label status:todo...` PASS line."""
    for line in rendered.splitlines():
        if "gh issue edit" in line and "status:todo" in line:
            return line
    raise AssertionError("PASS relabel command not found in rendered verifier prompt")


def _needs_work_edit_line(rendered: str) -> str:
    """The `gh issue edit ... --add-label status:drafting` NEEDS-WORK line."""
    for line in rendered.splitlines():
        if "gh issue edit" in line and "status:drafting" in line:
            return line
    raise AssertionError("NEEDS-WORK relabel command not found in rendered verifier prompt")


def test_triage_pass_adds_marker_in_same_command_as_todo():
    out = render_prompt(_real_base_body(), make_issue(labels=["status:triage"]), attempt=None)
    line = _pass_command_line(out)
    # The marker must ride the SAME gh edit that adds status:todo (one atomic
    # relabel), not a separate command.
    assert "--add-label status:todo,gate:triage-passed" in line
    assert "--remove-label status:triage" in line


def test_triage_needs_work_removes_marker_in_same_command():
    out = render_prompt(_real_base_body(), make_issue(labels=["status:triage"]), attempt=None)
    line = _needs_work_edit_line(out)
    # Routing back to drafting drops the provenance marker in the same command.
    assert "--remove-label status:triage,gate:triage-passed" in line
    assert "--add-label status:drafting" in line


# --- NEEDS DECISION + unchanged-body fast-path (issue #55) --------------------

def _verifier_prompt() -> str:
    return render_prompt(_real_base_body(), make_issue(labels=["status:triage"]), attempt=None)


def test_body_hash_command_is_embedded_verbatim():
    """The ONE literal command, byte-for-byte. `git hash-object` is allowlisted
    under Bash(git:*); `shasum` is NOT, and a denied command strands the
    session. Any drift here (a jq variant, a different digest tool) breaks the
    cross-session comparability the fast-path depends on."""
    # In the template, verbatim with the Liquid placeholder...
    template = _real_base_body()
    assert (
        "gh issue view {{ issue.identifier }} --repo acme/widgets "
        "--json body -q .body | git hash-object --stdin"
    ) in template
    # ...and in the rendered prompt the agent actually reads, with the issue
    # number substituted and nothing else changed.
    out = _verifier_prompt()
    assert (
        "gh issue view 42 --repo acme/widgets "
        "--json body -q .body | git hash-object --stdin"
    ) in out
    # The allowlist trap is named so no session substitutes a denied variant.
    assert "`shasum` is **not**" in out


def test_needs_decision_verdict_present_with_routing_and_structure():
    out = _verifier_prompt()
    assert "NEEDS DECISION" in out
    # Routes triage -> decision, and only that (no marker churn on this edge).
    line = next(l for l in out.splitlines()
                if "gh issue edit" in l and "status:decision" in l)
    assert "--remove-label status:triage" in line
    assert "--add-label status:decision" in line
    assert "gate:triage-passed" not in line
    # Required comment structure per Mechanics 1.
    for required in (
        "## Triage verdict",
        "body-sha1:",
        "steelmanned",
        "acceptance-criteria implications",
        "reply on this issue with the chosen option",
    ):
        assert required in out, f"missing decision-request element: {required!r}"


def test_needs_decision_boundary_against_needs_work_is_stated():
    out = _verifier_prompt()
    assert "determinate answer" in out
    assert "unmade human decision" in out


def test_fast_path_covers_all_five_table_rows():
    out = _verifier_prompt()
    assert "Unchanged-body fast-path" in out
    table = out.split("Unchanged-body fast-path", 1)[1]
    rows = [l for l in table.splitlines() if l.startswith("| ")]
    joined = "\n".join(rows)
    # NEEDS WORK / SPLIT -> drafting + marker cleared; NEEDS DECISION -> decision;
    # PASS -> todo + marker in the same command; no-hash -> full review.
    assert joined.count(
        "--remove-label status:triage,gate:triage-passed --add-label status:drafting"
    ) == 2
    assert "--remove-label status:triage --add-label status:decision" in joined
    assert "--remove-label status:triage --add-label status:todo,gate:triage-passed" in joined
    no_hash = [r for r in rows if "no parseable" in r]
    assert no_hash and "full review" in no_hash[0], (
        "the retrofit fall-through row must send pre-#55 verdicts to a full review"
    )


def test_every_verdict_carries_the_body_sha1_block():
    out = _verifier_prompt()
    assert "body-sha1: <the 40-hex digest from Step 0>" in out
    # PASS posts a comment too — otherwise the next re-triage has nothing to
    # compare against and the fast-path can never fire on a PASSed ticket.
    assert "That includes PASS" in out
