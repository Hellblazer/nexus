# SPDX-License-Identifier: AGPL-3.0-or-later
"""A ``runAlways`` changeset must not gate on ``onFail/onError="MARK_RAN"``.

WHY THIS EXISTS (nexus-ixsxa, found 2026-07-27 by the first-ever execution of a
Liquibase rollback round trip). Liquibase records MARK_RAN by INSERTING a
DATABASECHANGELOG row, never by updating one::

    ChangeSet.ExecType.RERAN    ("RERAN",    ranBefore=true,  ...)
    ChangeSet.ExecType.MARK_RAN ("MARK_RAN", ranBefore=false, ...)

    MarkChangeSetRanGenerator:65
        if (statement.getExecType().ranBefore) -> UpdateStatement (WHERE id/author/filename)
        else                                   -> InsertStatement (no existence check)

``runAlways`` makes ``ShouldRunChangeSetFilter`` accept the changeset on EVERY
boot (line 64, "Changeset always runs"), so its preconditions are re-evaluated
every boot. Combine the two and an unmet precondition appends one row per boot,
forever. A one-shot changeset with the same precondition is fine — it is
evaluated once and the single INSERT is correct bookkeeping.

WHAT IT COST. ``grants-nexus-diag-1`` and ``grants-nexus-diag-2`` were
era-EXCLUSIVE on the same probe (``nexus.diag_chash_conformance`` absent vs
present), so exactly one of them MARK_RANed on every boot of every cluster:
``-2`` on dev boxes and test containers, ``-1`` on any cluster where the
provisioning path had created the view. Measured at two boots of a fresh
container: 207 rows -> 208. Unbounded, and it also corrupts rollback depth
arithmetic, because ``rollbackCount(N)`` counts ROWS.

THE FIX SHAPE, for anyone tripping this lint. Move the test into the changeset
body as an early ``RETURN`` guard, so the changeset always executes and is
always recorded RERAN (one row, updated in place). Do NOT reach for
``runOnChange`` instead: ``ShouldRunChangeSetFilter`` line 69 then rejects the
changeset once ran, so the precondition is never re-evaluated — which silently
forfeits exactly the self-healing that ``runAlways`` was chosen for. Editing a
``runAlways`` body is checksum-safe (``ValidatingVisitor`` skips the MD5SUM
comparison for ``shouldAlwaysRun()``/``shouldRunOnChange()``), unlike the
shipped one-shot changesets.

WHAT THIS LINT DOES NOT COVER. It checks SHAPE. That the surviving body guard
actually implements the era logic correctly is a behavioural question, covered
by ``SchemaRollbackRoundTripIntegrationTest#eraTransitionRevokesTableSelectWithoutGrowingTheChangelog``
(real container, real transition) and by the row-count assertions in
``#runAlwaysChangesetsFloatToTheExecutionTail``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from tests.test_changelog_rls_lint import CHANGELOG_DIR

_NS = "{http://www.liquibase.org/xml/ns/dbchangelog}"

# Liquibase accepts both spellings; ``alwaysRun`` is the pre-3.x alias and is
# still honoured by the parser, so a lint that checked only ``runAlways`` could
# be sidestepped without anyone intending to.
_ALWAYS_ATTRS = ("runAlways", "alwaysRun")


def _runs_always(cs: ET.Element) -> bool:
    return any(cs.get(a, "false").strip().lower() == "true" for a in _ALWAYS_ATTRS)


def _violations(cs: ET.Element) -> list[str]:
    """THE RULE. One implementation, shared by the corpus and synthetic tests.

    Every ``preConditions`` DESCENDANT is examined, not just the direct child:
    a nested container carrying its own ``onFail`` gates the same changeset and
    resolves to the same ExecType.
    """
    if not _runs_always(cs):
        return []
    out = []
    for pre in cs.iter(f"{_NS}preConditions"):
        for attr in ("onFail", "onError"):
            if (pre.get(attr) or "").strip().upper() == "MARK_RAN":
                out.append(f"{cs.get('id', '?')}: runAlways + {attr}=MARK_RAN")
    return out


def _changesets() -> list[tuple[str, ET.Element]]:
    out = []
    for path in sorted(CHANGELOG_DIR.glob("*.xml")):
        # No `except ParseError: continue`, deliberately — an unparseable
        # changelog IS the finding, not something to step over. Same reasoning
        # as tests/test_changelog_rollback_lint.py.
        tree = ET.parse(path)
        for cs in tree.getroot().iter(f"{_NS}changeSet"):
            out.append((path.name, cs))
    return out


def test_no_run_always_changeset_gates_on_mark_ran() -> None:
    changesets = _changesets()

    # NON-VACUITY, in three parts. A glob typo, an XSD namespace bump, or a
    # world where nothing runs always would each make the assertion below pass
    # while examining nothing.
    assert len(changesets) > 100, (
        f"only {len(changesets)} changesets parsed — CHANGELOG_DIR is empty, "
        "misconfigured, or the dbchangelog namespace moved"
    )
    always = [(f, cs) for f, cs in changesets if _runs_always(cs)]
    assert len(always) >= 5, (
        f"only {len(always)} runAlways changesets found; there were 5 when this "
        "lint was written (staging-4-svc-grants, grants-nexus-svc-1, "
        "grants-002-changelog-read, grants-nexus-diag-1, grants-nexus-diag-2). "
        "A drop means the detector stopped recognising them, not that the "
        "codebase stopped using them"
    )
    assert any(
        cs.find(f"{_NS}preConditions") is not None for _, cs in changesets
    ), "no preConditions found anywhere — the onFail detector cannot be binding"

    found = [f"{fname}:{v}" for fname, cs in changesets for v in _violations(cs)]
    assert found == [], (
        "runAlways + MARK_RAN appends a DATABASECHANGELOG row on every boot "
        "where the precondition is unmet (nexus-ixsxa). Move the test into the "
        "changeset body as an early RETURN guard; see this module's docstring "
        f"for why runOnChange is the wrong alternative. Violations: {found}"
    )


def test_lint_detects_the_shape_it_claims_to() -> None:
    """Falsification: the corpus test above passes on a clean tree either way.

    Each fixture differs from its neighbour in exactly ONE dimension, so a
    detector that ignored ``runAlways``, ignored ``onError``, or matched only
    direct children would fail here rather than pass silently.
    """
    def cs(xml: str) -> ET.Element:
        return ET.fromstring(xml.replace("<changeSet", f"<changeSet xmlns='{_NS[1:-1]}'", 1))

    violating = cs(
        '<changeSet id="bad" author="t" runAlways="true">'
        '<preConditions onFail="MARK_RAN"><sqlCheck expectedResult="1">SELECT 1'
        "</sqlCheck></preConditions></changeSet>"
    )
    assert _violations(violating) == ["bad: runAlways + onFail=MARK_RAN"]

    # Same precondition, one-shot changeset: correct, evaluated once.
    one_shot = cs(
        '<changeSet id="ok-one-shot" author="t">'
        '<preConditions onFail="MARK_RAN"><sqlCheck expectedResult="1">SELECT 1'
        "</sqlCheck></preConditions></changeSet>"
    )
    assert _violations(one_shot) == []

    # runAlways with the era test in the body, which is the fix shape.
    body_guard = cs(
        '<changeSet id="ok-body-guard" author="t" runAlways="true">'
        "<sql>DO $$ BEGIN IF NOT EXISTS (SELECT 1) THEN RETURN; END IF; END $$;</sql>"
        "</changeSet>"
    )
    assert _violations(body_guard) == []

    # onError is the same ExecType by a different route (ChangeSet.java:737).
    on_error = cs(
        '<changeSet id="bad-onerror" author="t" runAlways="true">'
        '<preConditions onError="MARK_RAN"><sqlCheck expectedResult="1">SELECT 1'
        "</sqlCheck></preConditions></changeSet>"
    )
    assert _violations(on_error) == ["bad-onerror: runAlways + onError=MARK_RAN"]

    # Nested container: a direct-child-only detector would miss this.
    nested = cs(
        '<changeSet id="bad-nested" author="t" runAlways="true">'
        '<preConditions onFail="HALT"><and>'
        '<preConditions onFail="MARK_RAN"><sqlCheck expectedResult="1">SELECT 1'
        "</sqlCheck></preConditions></and></preConditions></changeSet>"
    )
    assert _violations(nested) == ["bad-nested: runAlways + onFail=MARK_RAN"]

    # The pre-3.x alias must not be an escape hatch.
    alias = cs(
        '<changeSet id="bad-alias" author="t" alwaysRun="true">'
        '<preConditions onFail="MARK_RAN"><sqlCheck expectedResult="1">SELECT 1'
        "</sqlCheck></preConditions></changeSet>"
    )
    assert _violations(alias) == ["bad-alias: runAlways + onFail=MARK_RAN"]
