"""Unit tests for the fold apply step's pure surfaces (issue #126 part b).

The parser and the byte convention are tested DIRECTLY because neither can be
caught downstream: verify-after-write compares the stored bytes against the
INTENDED bytes, so it is structurally blind to a wrong payload reading, and an
implementation hashing `description.encode()` instead of `body + "\\n"`
mismatches every real verdict while every fake-backed test stays green.

Hermetic by construction: the expected digest below was produced OFFLINE by the
real Step 0 command pair (`git hash-object` is reproducible). Nothing here
shells out to `gh` or `git`, and nothing touches the network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from orchestrator.fold_apply import (
    FOLD_MARKER_PREFIX,
    PROPOSAL_CLOSE,
    PROPOSAL_OPEN,
    blob_sha1,
    body_digest,
    extract_proposal,
    is_fold_marker,
    marker_comment_body,
    marker_first_line,
)
from orchestrator.types import IssueComment

UTC = timezone.utc

_FIXTURE = Path(__file__).parent / "fixtures" / "fold_body.md"

# Pinned literal, produced offline by the Step 0 command pair:
#   gh issue view N --repo owner/name --json body -q .body > fold_body.md
#   git hash-object fold_body.md
# `jq -r` appends a trailing newline unconditionally, so the committed fixture
# IS `body + "\n"` and this digest is git's BLOB hash of exactly those bytes.
FIXTURE_SHA1 = "202a0c046796c41a4291d8551974598476ca9ffd"


def _fixture_bytes() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


def _fixture_payload() -> str:
    """The fixture as a BODY — i.e. without Step 0's trailing newline."""
    text = _fixture_bytes()
    assert text.endswith("\n") and not text.endswith("\n\n")
    return text[:-1]


def _proposal_comment(payload: str, *, sha1: str = "a" * 40, extra: str = "") -> str:
    return (
        "## Triage verdict\n"
        f"body-sha1: {sha1}\n\n"
        "NEEDS WORK — the revised body follows.\n\n"
        f"{PROPOSAL_OPEN}\n{payload}\n{PROPOSAL_CLOSE}\n{extra}"
    )


# --- byte convention (Mechanics 5) --------------------------------------------

def test_body_digest_is_gits_blob_hash_of_body_plus_newline():
    """The measured convention, against an offline-pinned literal.

    Two things are pinned at once: that the digest covers `body + "\\n"` (not
    the raw body), and that it is git's BLOB hash — `sha1("blob <len>\\0" +
    data)` — not a plain sha1 of the bytes.
    """
    assert body_digest(_fixture_payload()) == FIXTURE_SHA1
    assert blob_sha1(_fixture_bytes().encode("utf-8")) == FIXTURE_SHA1
    # A plain sha1 of the same bytes is a DIFFERENT value — the trap this
    # assertion exists to close.
    import hashlib
    assert hashlib.sha1(_fixture_bytes().encode()).hexdigest() != FIXTURE_SHA1
    # And hashing the body without the trailing newline (the
    # `description.encode()` mistake) mismatches every real verdict.
    assert blob_sha1(_fixture_payload().encode()) != FIXTURE_SHA1


def test_none_body_hashes_as_a_bare_newline():
    """`Issue.description` is `str | None` (types.py:38)."""
    assert body_digest(None) == body_digest("")
    assert body_digest(None) == blob_sha1(b"\n")


# --- extraction (Mechanics 3) --------------------------------------------------

def test_payload_is_byte_equal_to_the_committed_fixture():
    """Whole-body replacement + sentinel immunity in one fixture.

    The payload carries its own ``` fences AND a nested
    `<!-- switchboard:fold-applied … -->` line; exactly one newline after the
    open sentinel and one before the close are framing and excluded.
    """
    payload, error = extract_proposal(_proposal_comment(_fixture_payload()))
    assert error is None
    assert payload == _fixture_payload()          # BYTE-equal, not "contains"
    assert "```python" in payload
    assert FOLD_MARKER_PREFIX in payload           # quoted marker survives intact
    assert not payload.startswith("\n") and not payload.endswith("\n")
    # after-sha1 is computed from the PAYLOAD, against a pinned literal — never
    # against the stored body (which would make the check circular).
    assert body_digest(payload) == FIXTURE_SHA1


def test_exactly_one_newline_is_stripped_on_each_side():
    """A payload that itself begins and ends with a blank line keeps them."""
    body = _proposal_comment("\nkeep me\n")
    payload, error = extract_proposal(body)
    assert error is None
    assert payload == "\nkeep me\n"


def test_a_second_close_literal_is_malformed_not_a_truncated_payload():
    """Round 12: the naive non-greedy read TERMINATES at the first close
    literal and silently applies a truncated body, green on every downstream
    check. The count-based rule is the only formulation that catches it."""
    truncating = _proposal_comment(f"real body\n{PROPOSAL_CLOSE}\ntail")
    payload, error = extract_proposal(truncating)
    assert payload is None
    assert error is not None and "2 open / 2 close" not in error
    assert "close sentinel" in error or "open" in error
    # And prove the naive reading WOULD have truncated — i.e. this fixture
    # genuinely exercises the trap.
    import re
    naive = re.search(
        re.escape(PROPOSAL_OPEN) + r"\n(.*?)\n" + re.escape(PROPOSAL_CLOSE),
        truncating, re.DOTALL,
    )
    assert naive is not None and naive.group(1) == "real body"


def test_two_open_sentinels_are_malformed():
    body = _proposal_comment("body", extra=f"\n{PROPOSAL_OPEN}\nstray\n")
    payload, error = extract_proposal(body)
    assert payload is None and error is not None


def test_close_before_open_is_malformed():
    body = f"## Triage verdict\n{PROPOSAL_CLOSE}\nbody\n{PROPOSAL_OPEN}\n"
    payload, error = extract_proposal(body)
    assert payload is None
    assert "pair in order" in (error or "")


def test_empty_payload_is_malformed_never_a_blanked_body():
    payload, error = extract_proposal(_proposal_comment("   "))
    assert payload is None
    assert "empty payload" in (error or "")


def test_no_block_at_all_is_reported_distinctly_from_malformed():
    payload, error = extract_proposal("## Triage verdict\nbody-sha1: " + "a" * 40)
    assert payload is None
    assert error == "no proposal block"


def test_open_literal_is_not_a_substring_of_the_close_literal():
    """The count rule depends on the two literals not aliasing each other."""
    assert PROPOSAL_OPEN not in PROPOSAL_CLOSE
    assert PROPOSAL_CLOSE not in PROPOSAL_OPEN


# --- marker semantics (Mechanics 8) -------------------------------------------

def _comment(body: str) -> IssueComment:
    return IssueComment(id="IC_x", body=body, login="switchboard-agent[bot]",
                        created_at=datetime(2026, 8, 1, tzinfo=UTC))


def test_marker_is_emitted_as_a_single_first_line():
    body = marker_comment_body(
        bound_comment_id="IC_bound", before="a" * 40, after="b" * 40,
        issue_url="https://github.com/acme/api/issues/1",
    )
    first = body.splitlines()[0]
    assert first == marker_first_line("IC_bound", "a" * 40, "b" * 40)
    assert first.endswith("-->")
    # Part (a) re-scans this thread: the marker must not read as a verdict and
    # must not carry a line-initial /fold.
    assert not first.lower().startswith("## triage verdict")
    assert not any(l.strip().startswith("/fold") or l.strip().startswith("/no-fold")
                   for l in body.splitlines())


def test_marker_matches_on_a_first_line_prefix_not_equality():
    marker = _comment(marker_comment_body(
        bound_comment_id="IC_bound", before="a" * 40, after="b" * 40,
        issue_url=None))
    assert is_fold_marker(marker, "IC_bound")


def test_a_comment_quoting_a_marker_does_not_spoof_one():
    """Negative case (round 10): a substring scan would suppress a real fold.

    Verdicts embed whole revised bodies and bodies quote sentinels — this
    ticket's own fixture body does.
    """
    quoted = _comment(
        "## Triage verdict\nbody-sha1: " + "a" * 40 + "\n\n"
        + marker_first_line("IC_bound", "a" * 40, "b" * 40) + "\n"
    )
    assert not is_fold_marker(quoted, "IC_bound")
    assert marker_first_line("IC_bound", "a" * 40, "b" * 40) in quoted.body


def test_trailing_space_stops_a_prefix_collision_between_comment_ids():
    marker = _comment(marker_first_line("IC_abcdef", "a" * 40, "b" * 40))
    assert is_fold_marker(marker, "IC_abcdef")
    assert not is_fold_marker(marker, "IC_abc")


def test_whitespace_only_comment_body_is_not_a_marker_and_does_not_raise():
    """`fold.py:118`'s guard idiom: `splitlines()[0]` on "   " IndexErrors."""
    assert is_fold_marker(_comment("   \n  "), "IC_bound") is False
    assert is_fold_marker(_comment(""), "IC_bound") is False
