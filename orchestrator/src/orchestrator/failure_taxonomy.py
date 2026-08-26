"""Cap-hit failure taxonomy — the importable contract (issue #169, from #16).

Why this is its own module rather than prose in a report template: two features
need the same closed class set, and one of them (#31, the fail-review verifier)
*routes* on it. A prose-only class list drifts silently — a reworded bullet in a
prompt is invisible to a consumer that switches on it. So the set, and each
class's recovery implication, ship as literals with a test pinning every one.

`CapFailureClass` answers "why did this work session run out of road?" It is a
**different axis** from `types.FailureClass`, which answers "how did the provider
turn fail?" #16's Addendum 3 forbids merging them, and
`test_failure_taxonomy.py` asserts the two value sets stay disjoint.

Both attributes are discriminated fields, never prose:

- `.approach_trusted` — is the session's own model of the problem still worth
  keeping? This is the brief-shaping question.
- `.recovery` — the re-dispatch decision.

`quota` and the two `blockage:*` classes deliberately share
`RETRY_SAME_APPROACH`. Addendum 2 distinguishes them by *cause* and by *when*
re-dispatch is safe, not by how the retry is briefed; the class string carries
that distinction, and `recovery` carries only the brief-and-approach decision.
The collapse is intended, not a lost row.

Nothing in this module acts. `recovery` is data; the policy engine that reads it
lives in #16 and the routing that branches on it lives in #31.

Stability: the five class values and the three `Recovery` tokens are stable
identifiers from merge onward — #12 will parse the class strings out of a
rendered YAML block, which makes them a wire format. Changing one is a breaking
change, and adding a sixth class is a change to #16's Addendum 2, not a silent
addition here.
"""

from __future__ import annotations

from enum import StrEnum


class ApproachTrust(StrEnum):
    """Whether the capped session's own approach survives the failure."""

    YES = "yes"                        # the wall was external; the model held
    NO = "no"                          # the session's own reasoning is suspect
    NOT_APPLICABLE = "not_applicable"  # retrying at all is the wrong move


class Recovery(StrEnum):
    """What to do about re-dispatch. #31 branches on exactly these three."""

    RETRY_SAME_APPROACH = "retry_same_approach"    # full brief, same approach
    RETRY_FRESH_CONTEXT = "retry_fresh_context"    # facts-only brief, no prior conclusions
    DO_NOT_RETRY = "do_not_retry"                  # route to triage/human instead


class CapFailureClass(StrEnum):
    """Closed set of reasons a work session hits its cap (#16 Addendum 2).

    Each member carries `.approach_trusted` and `.recovery`.
    """

    def __new__(cls, value: str, approach_trusted: ApproachTrust, recovery: Recovery):
        member = str.__new__(cls, value)
        member._value_ = value
        member.approach_trusted = approach_trusted
        member.recovery = recovery
        return member

    # An artificial wall (a denied command, a missing dependency): the session
    # was right and got fenced. Fix the wall, re-dispatch with the full brief.
    BLOCKAGE_PERMISSION = (
        "blockage:permission",
        ApproachTrust.YES,
        Recovery.RETRY_SAME_APPROACH,
    )
    BLOCKAGE_DEPENDENCY = (
        "blockage:dependency",
        ApproachTrust.YES,
        Recovery.RETRY_SAME_APPROACH,
    )
    # No verdict was reached at all, so there is nothing to distrust. Differs
    # from blockage only in *when* re-dispatch is safe (budget returns), which
    # the class string carries — not in how the retry is briefed.
    QUOTA = (
        "quota",
        ApproachTrust.YES,
        Recovery.RETRY_SAME_APPROACH,
    )
    # The session burned its turns going in circles: its conclusions are the
    # suspect part, so a fresh session gets facts only and no prior reasoning.
    ITERATION = (
        "iteration",
        ApproachTrust.NO,
        Recovery.RETRY_FRESH_CONTEXT,
    )
    # The ticket is too big for one session. Another attempt spends the same
    # budget for the same outcome; a human splits or re-scopes it.
    COMPLEXITY = (
        "complexity",
        ApproachTrust.NOT_APPLICABLE,
        Recovery.DO_NOT_RETRY,
    )
