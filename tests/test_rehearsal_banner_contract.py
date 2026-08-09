# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the nexus-elt26 dynamic-banner contract in
``tests/e2e/migration-rehearsal/rehearse.sh`` so the fix (commit 19e8bb2d)
cannot silently regress.

Review [22089] passed the commit; critic [22094] flagged (Significant, no
Critical) that nothing mechanically stopped a future edit from reverting the
closing banner to the original hardcoded literal — exactly the class of
defect nexus-elt26 closed. This module is that guard.

Two failure modes matter, and they are NOT symmetric in how easy they are to
catch by eye:

  * OVER-report (the original bug): the banner reverts to a fixed phase-name
    string, so a Phase-A-only run reads like a full-journey pass. Loud and
    obvious once you know to look — a code reviewer skimming a diff that adds
    back a literal ``printf`` line is likely to catch it.
  * UNDER-report (the inverse, and the harder one): a future phase gets a
    ``say "Phase X ..."`` entry point but nobody adds the matching
    ``RAN_PHASES+=("X")`` — the phase silently vanishes from the banner on a
    run that DID execute it. A reviewer has no reason to notice an append
    call that was never written.

Static regex over the script text — this module does not execute
rehearse.sh (it needs a live container; see run.sh --comprehensive/--stress
for that path). ``test_falsify_...`` proves the guard actually fires by
running the real check against a deliberately-reverted copy of the banner
line, in-memory — the non-vacuity half a purely descriptive test would be
missing.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).parent.parent
REHEARSE_SH = REPO_ROOT / "tests" / "e2e" / "migration-rehearsal" / "rehearse.sh"

#: `say "Phase A ..."`, `say "Phase D ..."`, `say "Phase B/C — RETIRED ..."`
#: (the last one is filtered out by the RETIRED check in
#: _declared_phase_letters — it NAMES the letters it did NOT run).
_PHASE_HEADER_RE = re.compile(r'say "Phase ([A-Z](?:/[A-Z])?) ')
_RAN_PHASES_APPEND_RE = re.compile(r"RAN_PHASES\+=\(([^)]*)\)")
_QUOTED_LETTER_RE = re.compile(r'"([A-Z])"')

#: The exact banner text the original bug printed unconditionally — the
#: literal this whole module exists to keep out.
_OLD_LITERAL_BANNER = (
    "install → provision → serve → seed → migrate → validate → rollback-safe"
)


def _declared_phase_letters(text: str) -> set[str]:
    """Every phase letter with a real (non-RETIRED) entry point."""
    letters: set[str] = set()
    for line in text.splitlines():
        m = _PHASE_HEADER_RE.search(line)
        if not m or "RETIRED" in line:
            continue
        letters.update(m.group(1).split("/"))
    return letters


def _appended_phase_letters(text: str) -> set[str]:
    """Every phase letter ever pushed onto RAN_PHASES."""
    letters: set[str] = set()
    for group in _RAN_PHASES_APPEND_RE.findall(text):
        letters.update(_QUOTED_LETTER_RE.findall(group))
    return letters


def _assert_banner_is_data_driven(text: str) -> None:
    """The single check both the real-file test and the falsification test
    run — kept as one function so the two can never drift apart."""
    assert _OLD_LITERAL_BANNER not in text, (
        f"the original hardcoded banner literal is back: {_OLD_LITERAL_BANNER!r} "
        "— the banner must be derived from RAN_PHASES, never a fixed string "
        "(nexus-elt26)"
    )
    assert "SOUP-TO-NUTS" not in text, (
        "the old 'SOUP-TO-NUTS REHEARSAL PASSED' marker is back — that name "
        "implied full-journey coverage regardless of which phases actually ran"
    )
    assert re.search(r'for p in "\$\{RAN_PHASES\[@\]\}"', text), (
        'no loop over "${RAN_PHASES[@]}" found — PHASES_DESC (or its '
        "replacement) is no longer derived from RAN_PHASES"
    )

    summary_marker = 'say "RESULT"'
    assert summary_marker in text, "no Summary section (say \"RESULT\") found"
    summary = text[text.index(summary_marker) :]
    pass_line = next(
        (ln for ln in summary.splitlines() if "REHEARSAL PASSED" in ln), None
    )
    fail_line = next(
        (ln for ln in summary.splitlines() if "REHEARSAL FAILED" in ln), None
    )
    assert pass_line is not None, "no REHEARSAL PASSED line found in the Summary section"
    assert fail_line is not None, "no REHEARSAL FAILED line found in the Summary section"
    assert "$PHASES_DESC" in pass_line, (
        f"PASSED banner does not interpolate $PHASES_DESC — looks hardcoded "
        f"again: {pass_line!r}"
    )
    assert "$PHASES_DESC" in fail_line, (
        f"FAILED banner does not interpolate $PHASES_DESC — looks hardcoded "
        f"again: {fail_line!r}"
    )


def test_extraction_is_not_vacuous() -> None:
    text = REHEARSE_SH.read_text()
    assert _declared_phase_letters(text), (
        'no \'say "Phase X ..."\' headers found — _PHASE_HEADER_RE broke'
    )
    assert _appended_phase_letters(text), (
        "no RAN_PHASES+=(...) calls found — _RAN_PHASES_APPEND_RE broke"
    )


def test_every_real_phase_entry_appends_to_ran_phases() -> None:
    """UNDER-report guard: a phase header with no matching append silently
    drops out of the banner on a run that DID execute it."""
    text = REHEARSE_SH.read_text()
    declared = _declared_phase_letters(text)
    appended = _appended_phase_letters(text)

    missing_append = declared - appended
    assert not missing_append, (
        f"phase(s) {sorted(missing_append)} have a 'say \"Phase X ...\"' "
        "entry point but no RAN_PHASES+=(...) append — they will silently "
        "vanish from the closing banner on a run that executed them "
        "(nexus-elt26 under-report class)"
    )

    stray_append = appended - declared
    assert not stray_append, (
        f"RAN_PHASES+=(...) names phase(s) {sorted(stray_append)} with no "
        "matching 'say \"Phase X ...\"' entry point — dead or stale append"
    )


def test_summary_banner_is_data_driven_not_a_literal() -> None:
    """OVER-report guard: the original nexus-elt26 bug."""
    _assert_banner_is_data_driven(REHEARSE_SH.read_text())


def test_falsify_reverting_to_a_literal_banner_is_caught() -> None:
    """Non-vacuity: prove the guard above actually fires. Reverts the PASSED
    banner line to the original hardcoded literal in-memory (never touches
    the real file) and asserts the exact check the previous test relies on
    raises against it."""
    text = REHEARSE_SH.read_text()
    reverted = re.sub(
        r"printf '\\033\[32mREHEARSAL PASSED\\033\[0m — phases executed: %s\\n' \"\$PHASES_DESC\"",
        "printf '\\\\033[32mSOUP-TO-NUTS REHEARSAL PASSED\\\\033[0m — "
        + _OLD_LITERAL_BANNER
        + "\\\\n'",
        text,
    )
    assert reverted != text, (
        "the in-memory substitution matched nothing — this falsification is "
        "not exercising the real banner line any more; the regex or the "
        "banner text drifted apart and the test needs updating"
    )
    with pytest.raises(AssertionError):
        _assert_banner_is_data_driven(reverted)


def test_voyage_runs_get_a_runtime_coverage_note() -> None:
    """nexus-f4apk(b): the Phase-D-vs-Voyage coverage gap must be visible at
    RUNTIME on a voyage run, not only in a source comment a reader choosing
    flags may never open."""
    text = REHEARSE_SH.read_text()
    assert "NOT exercised against Voyage" in text, (
        "the f4apk(b) coverage note text is gone — the voyage-leg coverage "
        "gap is no longer surfaced at runtime"
    )
    summary = text[text.index('say "RESULT"') :]
    assert "WITH_CLOUD" in summary and "NOT exercised against Voyage" in summary, (
        "the coverage note must live in the Summary section, gated on "
        "WITH_CLOUD, so it prints on every voyage-leg run"
    )
