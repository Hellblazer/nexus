"""Termination semantics for ``nx_plan_audit`` (nexus-ll7zm).

WHY THIS MODULE EXISTS
======================

A plan audit is a defect-finding function. Run against any sufficiently
detailed plan it always finds something, so an audit loop with no
termination condition does not converge: each round returns real-in-
isolation findings, the caller treats every finding as blocking, and the
planner re-plans. On 2026-08-23 that loop ran five rounds against the
RDR-197 plan. Rounds one and two found genuine design defects. Rounds
three through five refined hypothetical CI semantics for a release cut
that had never been performed once, on a tests-first plan whose own first
test run would have surfaced them in minutes.

The defect was never any single finding. It was that no component read
the LOOP: nothing counted rounds, nothing separated "a bead would build
the wrong thing" from "the first test run tells you this", and the
audit's only verdicts were READY and NOT READY, of which one blocks.

This module supplies the two missing distinctions, as pure functions so
they are enforced in code rather than requested in a prompt. A prompt can
be argued with; ``resolve_verdict`` cannot.

THE TWO DISTINCTIONS
====================

1. Classification. Every finding is either :data:`BLOCKS_PLANNING` (the
   plan as written would cause someone to build the wrong thing, or it
   cannot be executed in the order given) or
   :data:`DISCOVER_AT_IMPLEMENTATION` (real, but the first test run,
   first CI run, or first hour at the keyboard surfaces it). Only the
   first class can block.

2. Round cap. From :data:`MAX_BLOCKING_ROUNDS` + 1 onward the audit
   cannot return a blocking verdict at all. Whatever it found is emitted
   as residuals to be recorded and carried into implementation. Two
   rounds is not a claim that two rounds suffice; it is the claim that a
   third round's findings are cheaper to discover by building.

The caller supplies the round number because the tool is stateless: it is
one ``claude -p`` dispatch with no memory between invocations. A caller
that never passes ``round_number`` gets round 1 semantics forever, which
is the pre-existing behaviour and is why the planner-facing guidance
(``conexus/agents/strategic-planner.md``) tells planners to pass it.

The count is honest-caller enforcement, not adversarial enforcement:
nothing binds ``round_number`` to plan identity, so a caller that resets
the count for a nominally "revised" plan re-opens the loop. The guidance
surfaces state the rule (a revision produced by an audit round is the
SAME plan; the count never resets); mechanizing plan identity would mean
persisting audit state, the state-duplication class RDR-197 just spent
three revisions deleting.
"""

from __future__ import annotations

from typing import Any, Final

#: A finding whose defect would make someone build the wrong thing, or
#: whose sequencing error makes the plan unexecutable as written. Only
#: this class can hold a plan back.
BLOCKS_PLANNING: Final = "BLOCKS-PLANNING"

#: A real finding that the first test run, first CI run, or first hour of
#: implementation surfaces on its own. Recorded, carried, not re-planned.
DISCOVER_AT_IMPLEMENTATION: Final = "DISCOVER-AT-IMPLEMENTATION"

VALID_CLASSIFICATIONS: Final = (BLOCKS_PLANNING, DISCOVER_AT_IMPLEMENTATION)

#: Rounds that may return a blocking verdict. Round 3 and later cannot.
MAX_BLOCKING_ROUNDS: Final = 2

#: Verdict emitted once the cap (or the caller's own budget) is reached.
#: Deliberately not "READY": the plan is releasable to implementation, but
#: the residuals are real and must be recorded, and a verdict of READY
#: would erase them.
RESIDUALS_ONLY: Final = "RESIDUALS-ONLY"

#: Verdict for a round that found nothing blocking within the cap.
READY: Final = "READY"

#: Verdict for a round within the cap that found at least one blocker.
NOT_READY: Final = "NOT READY"

_RECORDING_INSTRUCTION: Final = (
    "Record these residuals with the plan (T2 memory entry or bead notes) "
    "and carry them into implementation. Do NOT open another audit round "
    "for them, and do NOT re-plan: a residual is discovered and fixed at "
    "the keyboard, by the tests the plan already requires."
)


def _classification_of(finding: Any) -> str:
    """Return a finding's classification, defaulting to the blocking one.

    An unclassified or unparsable finding is treated as
    :data:`BLOCKS_PLANNING`. The conservative default is deliberate: a
    finding whose class nobody stated must not be silently downgraded
    into a residual, which would be the failure mode this module exists
    to prevent, running in the opposite direction.
    """
    if not isinstance(finding, dict):
        return BLOCKS_PLANNING
    raw = finding.get("classification")
    if not isinstance(raw, str):
        return BLOCKS_PLANNING
    normalised = raw.strip().upper().replace("_", "-")
    if normalised in VALID_CLASSIFICATIONS:
        return normalised
    return BLOCKS_PLANNING


def partition_findings(
    findings: list[Any],
) -> tuple[list[Any], list[Any]]:
    """Split *findings* into (blocking, residual), preserving order."""
    blocking = [f for f in findings if _classification_of(f) == BLOCKS_PLANNING]
    residual = [
        f for f in findings if _classification_of(f) == DISCOVER_AT_IMPLEMENTATION
    ]
    return blocking, residual


def blocking_round_cap(budget_rounds: int = 0) -> int:
    """Rounds allowed to block, honouring a caller-declared budget.

    *budget_rounds* is the effort budget the plan author declared up
    front from the feature's own stakes: a one-day change should not buy
    two blocking audit rounds. Zero — or any non-positive value — means
    "unstated", which falls back to :data:`MAX_BLOCKING_ROUNDS`. A budget
    may only TIGHTEN the cap, never widen it, so a caller cannot buy
    extra rounds by declaring a large budget.
    """
    if budget_rounds <= 0:
        return MAX_BLOCKING_ROUNDS
    return min(budget_rounds, MAX_BLOCKING_ROUNDS)


def resolve_verdict(
    findings: list[Any],
    round_number: int = 1,
    budget_rounds: int = 0,
) -> tuple[str, list[Any], list[Any], str]:
    """Decide the audit's verdict from its findings and its round.

    Returns ``(verdict, blocking, residual, reason)``. ``reason`` is
    empty except when the cap converted a would-be blocking verdict, in
    which case it states plainly why, so the caller is never left
    guessing whether the audit approved the plan or ran out of rounds.

    The model's own proposed verdict is not an input. It cannot be: the
    whole failure this module addresses is a defect-finder asked to judge
    when to stop finding defects.
    """
    blocking, residual = partition_findings(findings)
    cap = blocking_round_cap(budget_rounds)
    # A non-positive round is a caller bug; the safe reading is round-1
    # semantics (may block), never a silent slide past the cap.
    round_number = max(1, round_number)

    if round_number > cap:
        if not findings:
            # A clean re-audit past the cap is clean, full stop. Labeling
            # it RESIDUALS-ONLY would tell the reader there is something
            # to record when there is nothing.
            return READY, [], [], ""
        reason = (
            f"Round {round_number} exceeds the blocking-round cap of {cap}"
            + (
                f" (caller budget: {budget_rounds})"
                if 0 < budget_rounds < MAX_BLOCKING_ROUNDS
                else ""
            )
            + ". Every finding below is a residual, including any the audit "
            "classified as blocking: past the cap, the cheaper discovery "
            "path is implementation, not another planning round."
        )
        return RESIDUALS_ONLY, [], blocking + residual, reason

    if blocking:
        return NOT_READY, blocking, residual, ""
    return READY, [], residual, ""


def _render_finding(finding: Any) -> str:
    if not isinstance(finding, dict):
        return f"  - {finding}"
    severity = finding.get("severity", "?")
    title = finding.get("title", "")
    return f"  [{severity}] {title}"


def render_audit_report(
    verdict: str,
    blocking: list[Any],
    residual: list[Any],
    summary: str = "",
    reason: str = "",
    round_number: int = 1,
) -> str:
    """Render the human-readable audit report.

    Residuals are always shown with the recording instruction attached,
    so a reader cannot mistake "not blocking" for "not real".
    """
    lines = [f"Verdict: {verdict} (round {round_number})"]
    if summary:
        lines.append(summary)
    if reason:
        lines.append("")
        lines.append(reason)
    if blocking:
        lines.append("")
        lines.append(f"{BLOCKS_PLANNING} ({len(blocking)}) — these hold the plan:")
        lines.extend(_render_finding(f) for f in blocking)
    if residual:
        lines.append("")
        lines.append(
            f"{DISCOVER_AT_IMPLEMENTATION} ({len(residual)}) — real, not blocking:"
        )
        lines.extend(_render_finding(f) for f in residual)
        lines.append("")
        lines.append(_RECORDING_INSTRUCTION)
    if not blocking and not residual:
        lines.append("")
        lines.append("No findings.")
    return "\n".join(lines)


#: Prompt fragment naming the classification contract for the auditing
#: subprocess. Lives here, beside the enforcement, so the instruction and
#: the code that enforces it cannot drift apart.
CLASSIFICATION_PROMPT: Final = f"""
Classify EVERY finding with a `classification` field, exactly one of:

  {BLOCKS_PLANNING}
      The plan as written would cause someone to build the wrong thing,
      or it cannot be executed in the order given. A bead is missing,
      mis-sequenced, or specifies behaviour that contradicts the design
      of record.

  {DISCOVER_AT_IMPLEMENTATION}
      Real, but the first test run, the first CI run, or the first hour
      at the keyboard surfaces it. Missing fetches, unresolvable refs,
      wrong flags, environment specifics, and anything a failing test
      would name.

Default to {DISCOVER_AT_IMPLEMENTATION} for anything an implementer
would hit immediately. Reserve {BLOCKS_PLANNING} for defects that
survive contact with the keyboard because nothing forces them to
surface. Plans are cheap to amend during implementation and expensive to
re-plan; an audit that classifies everything as blocking is not being
rigorous, it is refusing to terminate.
""".strip()


def round_prompt(round_number: int, budget_rounds: int = 0) -> str:
    """Prompt fragment telling the subprocess where it is in the loop."""
    cap = blocking_round_cap(budget_rounds)
    round_number = max(1, round_number)
    if round_number > cap:
        return (
            f"This is audit round {round_number}, past the blocking-round "
            f"cap of {cap}. Your findings will be emitted as residuals for "
            "the implementer regardless of how you classify them. Report "
            "what you find plainly and do not argue for another round."
        )
    return (
        f"This is audit round {round_number} of at most {cap} rounds that "
        "may block. Findings you classify as "
        f"{BLOCKS_PLANNING} hold the plan; everything else is carried into "
        "implementation."
    )
