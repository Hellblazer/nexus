# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The engine-release skill must not drift from the rehearsal harness it drives.

Instance of nexus-1e2eh ("release-only procedures rot silently: mechanize a
sweep so a deleted verb/contract cannot leave a gate naming it"). Release-only
scripts are exercised once per cut, by a human following a checklist, so nothing
catches a checklist that has gone stale — until the cut fails, or worse, until a
gate is silently skipped.

FOUR incidents, in BOTH directions:

  * v0.1.53 -> v0.1.54: a release-only script nobody swept when the contract
    under it changed.
  * RDR-155 P4b retired ``--guided`` / ``--cold`` / ``--hole-punch``; the skill
    kept prescribing ``--guided`` for one more cut and would have failed the
    next one at the gate.
  * The same retirement took the published-artifact gate with it.
  * 2026-07-25 (this file): the INVERSE. ``nexus-1ddsy`` rebuilt that gate as
    ``--acquire`` and used it in production to gate v0.1.55, but the skill still
    said "CURRENTLY NO LEG / BROKEN, escalate to Hal". A stale *banner* is as
    costly as a stale flag: it sends the operator to escalate a decision that
    was already made, and invites re-proposing an option Hal had refused.

So the guard is BIDIRECTIONAL — one direction per failure mode:

  forward  every flag the skill PRESCRIBES must be a live, non-retired flag.
           Catches the skill naming something deleted.
  reverse  every standalone journey the harness DEFINES must be named somewhere
           in the skill. Catches a gate landing in the harness that the skill
           never learns about — the 2026-07-25 class.

``run.sh`` is the single source of truth; the skill is checked against it. This
deliberately does NOT assert prose quality, only that no flag can be prescribed
that does not work and no journey can exist that the checklist has never heard
of.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
RUN_SH = REPO_ROOT / "tests" / "e2e" / "migration-rehearsal" / "run.sh"
SKILL = REPO_ROOT / ".claude" / "skills" / "engine-release" / "SKILL.md"

#: ``--flag)   VAR=1 ;;`` in the arg-parse case block. ``=1`` deliberately:
#: it selects behaviour flags and skips ``--no-build) DO_BUILD=0``.
_CASE_RE = re.compile(r"^\s*(--[a-z][a-z-]*)\)\s*([A-Z_]+)=1", re.M)

#: The retirement refusal: ``if [ "$GUIDED" = 1 ] || ... ; then`` followed by a
#: ``RETIRED`` echo. Matched on the echo so a renamed guard variable cannot
#: silently empty the retired set.
_RETIRED_BLOCK_RE = re.compile(
    r"^if ((?:\[ \"\$[A-Z_]+\" = 1 \](?: \|\| )?)+); then\n\s*echo \"RETIRED",
    re.M,
)
_VAR_IN_COND_RE = re.compile(r"\$([A-Z_]+)")

#: ``"--acquire is a standalone published-artifact gate..."`` — the harness's own
#: statement that a flag is a standalone journey.
_STANDALONE_GUARD_RE = re.compile(r"\"(--[a-z][a-z-]*) is a standalone")

_FENCE_RE = re.compile(r"```(?:bash|sh)?\n(.*?)```", re.S)
_PRESCRIBED_RE = re.compile(r"run\.sh\s+((?:--[a-z][a-z-]*\s*)+)")


def _run_sh() -> str:
    return RUN_SH.read_text(encoding="utf-8")


def _skill() -> str:
    return SKILL.read_text(encoding="utf-8")


def _live_flags() -> dict[str, str]:
    """flag -> guard variable, for every flag the arg loop accepts."""
    return {m.group(1): m.group(2) for m in _CASE_RE.finditer(_run_sh())}


def _retired_flags() -> set[str]:
    src = _run_sh()
    m = _RETIRED_BLOCK_RE.search(src)
    if m is None:
        return set()
    retired_vars = set(_VAR_IN_COND_RE.findall(m.group(1)))
    return {f for f, var in _live_flags().items() if var in retired_vars}


def _standalone_flags() -> set[str]:
    """Journeys the harness itself calls standalone — its own words, two ways."""
    src = _run_sh()
    flags = set(_STANDALONE_GUARD_RE.findall(src))
    for line in src.splitlines():
        m = _CASE_RE.match(line)
        if m and "standalone" in line.split("#", 1)[-1]:
            flags.add(m.group(1))
    return flags & set(_live_flags())


def _prescribed_flags() -> set[str]:
    """Flags the skill tells the operator to RUN — fenced commands only.

    Prose is excluded on purpose: the skill legitimately *names* retired flags
    when explaining why they are retired, and that must not read as a
    prescription.
    """
    out: set[str] = set()
    for fence in _FENCE_RE.findall(_skill()):
        for group in _PRESCRIBED_RE.findall(fence):
            out.update(re.findall(r"--[a-z][a-z-]*", group))
    return out


# ── Non-vacuity ─────────────────────────────────────────────────────────────


def test_both_files_exist() -> None:
    assert RUN_SH.is_file(), f"harness moved: {RUN_SH}"
    assert SKILL.is_file(), f"skill moved: {SKILL}"


def test_parsers_are_not_vacuous() -> None:
    """Every assertion below is `for x in <parsed set>` — an empty set passes
    vacuously. If the parse breaks (reworded guard, restructured case block),
    fail HERE rather than let the real guards go quietly green."""
    live = _live_flags()
    assert len(live) >= 10, f"live-flag parse looks broken: {sorted(live)}"
    assert "--shakeout" in live and "--acquire" in live, sorted(live)

    retired = _retired_flags()
    assert retired, "retired-flag parse found nothing; the RETIRED block moved"
    assert "--cold" in retired, sorted(retired)

    standalone = _standalone_flags()
    assert len(standalone) >= 5, f"standalone parse looks broken: {sorted(standalone)}"

    prescribed = _prescribed_flags()
    assert prescribed, "skill prescribes no run.sh flags; fence parse broke"
    assert "--shakeout" in prescribed, sorted(prescribed)


# ── Forward: the skill cannot prescribe something that does not work ────────


def test_skill_prescribes_only_live_flags() -> None:
    live = set(_live_flags())
    unknown = sorted(_prescribed_flags() - live)
    assert not unknown, (
        f"engine-release skill prescribes flag(s) run.sh does not accept: "
        f"{unknown}. run.sh exits 2 on an unknown arg, so the next cut fails at "
        f"the gate. Live flags: {sorted(live)}"
    )


def test_skill_does_not_prescribe_retired_flags() -> None:
    retired = _retired_flags()
    prescribed_retired = sorted(_prescribed_flags() & retired)
    assert not prescribed_retired, (
        f"engine-release skill prescribes RETIRED flag(s): {prescribed_retired}. "
        "run.sh refuses these pre-build with a RETIRED message and exits 2. "
        "This is the exact incident that left the skill naming --guided for a "
        "cut after RDR-155 P4b retired it."
    )


# ── Reverse: a journey cannot exist that the checklist never heard of ──────


def test_every_live_standalone_journey_is_named_in_the_skill() -> None:
    """The 2026-07-25 class: --acquire landed in the harness (nexus-1ddsy) and
    gated v0.1.55 in production while the skill still declared the step broken.

    'Named' is deliberately loose — a mention anywhere in the skill is enough.
    The point is that a new gate cannot land invisibly, not that the skill must
    describe it in any particular section.
    """
    skill = _skill()
    missing = sorted(
        f for f in _standalone_flags() - _retired_flags()
        if f not in skill
    )
    assert not missing, (
        f"run.sh defines standalone journey/gate flag(s) the engine-release "
        f"skill never mentions: {missing}. A gate the checklist does not name "
        f"is a gate that does not get run."
    )


@pytest.mark.parametrize("flag", ["--shakeout", "--acquire"])
def test_the_two_release_gates_are_prescribed_as_commands(flag: str) -> None:
    """--shakeout (pre-tag, local candidate) and --acquire (post-publish, the
    PUBLISHED bytes) are the two gates a cut must not ship without.

    Mentioning them is not enough — the skill must give the operator a runnable
    command, because the failure mode being guarded is a step degrading into
    prose nobody executes. Their coverage does not overlap: the shakeout drives
    the locally built -Ob candidate; the published artifact is different bytes
    from a different builder (full native build, codesign, cosign, PG-bundle
    packaging), so a workflow-introduced defect is invisible to the shakeout by
    construction (nexus-2oh5q).
    """
    assert flag in _prescribed_flags(), (
        f"{flag} is not prescribed as a runnable command in the engine-release "
        f"skill. Hal REFUSED accepting this gap on 2026-07-24 (nexus-1ddsy); "
        f"the gate historically caught nexus-pi3s3 + nexus-qeoxf, defects in "
        f"published bytes that every local suite missed."
    )
