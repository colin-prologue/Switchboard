# L-06 — Adversarial triage before implementation spend

**Context.** Switchboard made triage an *active state*: tickets filed for
triage get dispatched — on the same machinery as implementation — to an
independent verifier session whose prompt says "adversarially scrutinize this
ticket": assumptions, acceptance-criteria shape, testability, sizing,
boundaries. Verdicts: PASS (promote to dispatchable), NEEDS WORK (back to
drafting with feedback), SPLIT (spawn children, park parent).

**Why it earns its cost.** A badly-shaped ticket burns implementation
sessions — real money — and parks with nothing to show. Triage moves the
failure earlier and cheaper. Operational experience: multi-round spec
revisions through the triage gate are *the system working*, not waste. The
strictness pays for itself; only non-substantive verdicts (ceremony without
findings) are a defect signal.

**The PASS-promotes decision.** Letting the verifier's PASS make a ticket
dispatchable hands an agent promotion authority — real spend on an agent's
judgment. This was accepted deliberately: only the strictest verdict
promotes; humans control the gate earlier (choosing to file into triage at
all); caps + parking bound the downside. Known weakness: a lenient verifier
converts bad tickets into burned sessions with no human checkpoint until PR
review, and the calibration signal is trailing. Verifier trustworthiness is
an empirical, corpus-driven question — expect to tune the rubric.

**Portable.** The gate, the verdict taxonomy, PASS-promotes (bounded by
caps), and the calibration mindset. Team twist for L&W: the ticket author is
now one of N engineers, so triage doubles as the enforcement of "well-
documented tickets" — the protocol's teaching mechanism, per Colin's intent.

**Not portable.** The exact rubric wording; recalibrate on your own tickets.
