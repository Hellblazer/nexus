# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f7dwp: DATA EFFECT disclosure lint for Liquibase changesets.

THE GAP THIS CLOSES. A changeset that changes or deletes EXISTING rows
(tuples-003-2's DELETE of over-4096-byte tuple bodies, tuples-004-1's
irreversible NULL of consumed tuple bodies) was disclosed, before this
lint, only in its own ``<comment>`` prose, a commit message, and
``docs/tuple-space.md`` — no record a DEPLOYER reads carried it
mechanically. ``docs/wire-contract-pending.md`` rightly excludes these (no
wire change) and ``conexus/PENDING_RELEASE.md`` is plugin-only, so there
was no ledger this class of change belonged in at all. For the v0.1.118
engine-release handoff, both effects were stated to conexus only because a
human typed them in by hand.

THE CONVENTION THIS ENFORCES. Every changeset whose forward-running SQL
modifies or removes rows that could already exist on a deployed cluster
must carry a ``DATA EFFECT: ...`` sentence inside its ``<comment>``
element, naming what rows it touches and whether the effect is
reversible. See ``scripts/data_effect_lint.py``'s module docstring for
the full classification rules (what counts as data-effecting, why the
line lives inside ``<comment>`` rather than a floating XML comment, and
the deliberate `ON CONFLICT DO UPDATE` exclusion) — this test file only
wires that classifier to the real changelog tree and enforces it.

NON-VACUITY. ``test_real_changelog_has_data_effecting_changesets`` pins a
floor: the analyzer must find AT LEAST one data-effecting changeset in the
real corpus (63 measured 2026-09-13), so a classifier that silently
matched nothing would fail loudly here rather than passing by finding
nothing to check (the nexus-moht0 vacuous-gate doctrine).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from data_effect_lint import (
    DATA_EFFECT_MARKER,
    analyze_changelog,
    classify_changeset,
    has_data_effect_line,
)

pytestmark = pytest.mark.lint


# ---------------------------------------------------------------------------
# Unit tests for the classifier itself
# ---------------------------------------------------------------------------


def test_has_data_effect_line_detects_marker():
    assert has_data_effect_line(f"some prose. {DATA_EFFECT_MARKER} deletes rows.")
    assert not has_data_effect_line("some prose with no marker at all.")
    assert not has_data_effect_line(None)
    assert not has_data_effect_line("")


@pytest.mark.parametrize(
    "sql,expected_kinds",
    [
        ("DELETE FROM nexus.widgets WHERE stale = true;", {"delete"}),
        ("UPDATE nexus.widgets SET name = 'x' WHERE id = 1;", {"update"}),
        ("TRUNCATE nexus.widgets;", {"truncate"}),
        ("DROP TABLE nexus.widgets;", {"drop_table"}),
        ("ALTER TABLE nexus.widgets DROP COLUMN stale;", {"drop_column"}),
        (
            "ALTER TABLE nexus.widgets ALTER COLUMN created_at TYPE timestamptz "
            "USING NULLIF(created_at, '')::timestamptz;",
            {"alter_type_using"},
        ),
    ],
)
def test_classify_sql_text_detects_each_data_effecting_shape(sql, expected_kinds):
    reasons = classify_changeset(sql, structured_tags=[])
    assert {r.kind for r in reasons} == expected_kinds


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE nexus.widgets (id int);",
        "ALTER TABLE nexus.widgets ADD COLUMN extra text;",
        "ALTER TABLE nexus.widgets ADD CONSTRAINT widgets_pk PRIMARY KEY (id);",
        "CREATE INDEX idx_widgets_id ON nexus.widgets (id);",
        "GRANT SELECT ON nexus.widgets TO PUBLIC;",
        "ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_chk;",
        # Upsert of NEW/backfill rows -- deliberately NOT flagged (see module
        # docstring): statement-initial keyword is INSERT, not UPDATE.
        "INSERT INTO nexus.widgets (id, name) VALUES (1, 'x') "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name;",
    ],
)
def test_classify_sql_text_ignores_additive_and_upsert_shapes(sql):
    reasons = classify_changeset(sql, structured_tags=[])
    assert reasons == [], reasons


def test_classify_sql_text_ignores_function_body_dml():
    """A CREATE FUNCTION ... $$ ... $$ body's internal DML is exempt (runs
    later, at call time, under the caller's own context) -- mirrors
    test_changelog_rls_lint.py's identical function-body exemption."""
    sql = """
CREATE OR REPLACE FUNCTION nexus.widget_trash(wid text)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
    UPDATE nexus.widgets SET deleted_at = NOW() WHERE id = wid;
END;
$$
"""
    reasons = classify_changeset(sql, structured_tags=[])
    assert reasons == [], reasons


def test_classify_sql_text_does_not_exempt_do_block_dml():
    """A DO $$ ... $$ anonymous block executes IMMEDIATELY at migration time
    (the taxonomy-004-1 ground-truth shape) -- its DML must be scanned like
    top-level SQL, not treated as an exempt function body."""
    sql = """
DO $$
BEGIN
    DELETE FROM nexus.widgets WHERE stale = true;
END $$;
"""
    reasons = classify_changeset(sql, structured_tags=[])
    assert {r.kind for r in reasons} == {"delete"}


def test_structured_change_tags_are_detected():
    reasons = classify_changeset("", structured_tags=["delete", "dropColumn"])
    assert {r.kind for r in reasons} == {"structured:delete", "structured:dropColumn"}


# ---------------------------------------------------------------------------
# Synthetic changelog tests
# ---------------------------------------------------------------------------


def _write_changelog(tmp_path: Path, changesets_xml: str) -> tuple[Path, Path]:
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    child = changelog_dir / "synthetic-001.xml"
    child.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<databaseChangeLog\n'
        '    xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
        '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '    xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog '
        'http://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-4.4.xsd">\n'
        f"{changesets_xml}\n"
        "</databaseChangeLog>\n"
    )
    master = changelog_dir / "db.changelog-master.xml"
    master.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<databaseChangeLog\n'
        '    xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
        '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '    xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog '
        'http://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-4.4.xsd">\n'
        '    <include file="synthetic-001.xml"/>\n'
        "</databaseChangeLog>\n"
    )
    return changelog_dir, master


def test_missing_data_effect_line_is_flagged(tmp_path):
    xml = """
    <changeSet id="cs-naked-delete" author="t">
        <comment>Deletes stale widgets, no disclosure line.</comment>
        <sql splitStatements="true">
DELETE FROM nexus.widgets WHERE stale = true;
        </sql>
    </changeSet>
    """
    changelog_dir, master = _write_changelog(tmp_path, xml)
    result = analyze_changelog(changelog_dir=changelog_dir, master_path=master)
    assert len(result.data_effecting) == 1
    assert len(result.missing_disclosure) == 1
    assert result.missing_disclosure[0].changeset.changeset_id == "cs-naked-delete"


def test_present_data_effect_line_is_accepted(tmp_path):
    xml = """
    <changeSet id="cs-disclosed-delete" author="t">
        <comment>Deletes stale widgets. DATA EFFECT: DELETEs nexus.widgets rows
            where stale = true; irreversible.</comment>
        <sql splitStatements="true">
DELETE FROM nexus.widgets WHERE stale = true;
        </sql>
    </changeSet>
    """
    changelog_dir, master = _write_changelog(tmp_path, xml)
    result = analyze_changelog(changelog_dir=changelog_dir, master_path=master)
    assert len(result.data_effecting) == 1
    assert result.missing_disclosure == []


def test_additive_changeset_needs_no_disclosure(tmp_path):
    xml = """
    <changeSet id="cs-additive" author="t">
        <comment>Adds a column, no data effect.</comment>
        <sql splitStatements="true">
ALTER TABLE nexus.widgets ADD COLUMN extra text;
        </sql>
    </changeSet>
    """
    changelog_dir, master = _write_changelog(tmp_path, xml)
    result = analyze_changelog(changelog_dir=changelog_dir, master_path=master)
    assert result.data_effecting == []
    assert result.missing_disclosure == []


def test_rollback_body_is_never_scanned(tmp_path):
    """DROP TABLE appearing only inside <rollback> must not be classified as
    a forward data effect -- rollback SQL never runs at migration time (the
    overwhelmingly common shape in the real corpus: a baseline changeset's
    rollback tears down what it just created)."""
    xml = """
    <changeSet id="cs-baseline" author="t">
        <comment>Creates a table, no data effect on the forward path.</comment>
        <sql splitStatements="true">
CREATE TABLE nexus.widgets (id int);
        </sql>
        <rollback>DROP TABLE IF EXISTS nexus.widgets CASCADE</rollback>
    </changeSet>
    """
    changelog_dir, master = _write_changelog(tmp_path, xml)
    result = analyze_changelog(changelog_dir=changelog_dir, master_path=master)
    assert result.data_effecting == []


# ---------------------------------------------------------------------------
# v0.1.117..v0.1.118 regression pin (bead nexus-f7dwp's own acceptance check)
# ---------------------------------------------------------------------------


def test_tuples_003_2_and_tuples_004_1_are_classified_and_disclosed():
    """The design's own acceptance check: tuples-003-2 (DELETE of oversized
    tuple bodies) and tuples-004-1 (UPDATE nulling consumed tuple bodies) --
    the exact two changesets nexus-f7dwp names as shipping between
    v0.1.117..v0.1.118 with no deploy-facing disclosure -- must both be
    classified as data-effecting AND carry the DATA EFFECT line today."""
    result = analyze_changelog()
    by_id = {
        f.changeset.changeset_id: f
        for f in result.data_effecting
        if f.changeset.changeset_id in ("tuples-003-2", "tuples-004-1")
    }
    assert set(by_id) == {"tuples-003-2", "tuples-004-1"}, (
        "tuples-003-2 and/or tuples-004-1 not classified as data-effecting "
        f"by the real-changelog walk: {sorted(f.changeset.changeset_id for f in result.data_effecting)}"
    )
    missing_ids = {f.changeset.changeset_id for f in result.missing_disclosure}
    assert not (missing_ids & {"tuples-003-2", "tuples-004-1"}), (
        "tuples-003-2 / tuples-004-1 lost their DATA EFFECT line: "
        f"{missing_ids & {'tuples-003-2', 'tuples-004-1'}}"
    )


# ---------------------------------------------------------------------------
# The real changelog — exact assertion, mechanically enforced
# ---------------------------------------------------------------------------


def test_real_changelog_has_data_effecting_changesets():
    """Non-vacuity floor (nexus-moht0 doctrine): the analyzer must actually
    find data-effecting changesets in the real corpus, not silently match
    nothing. 63 measured 2026-09-13 across the full backfill; pinned as a
    floor (not exact) since new data-effecting changesets are expected to
    land over time and each must independently satisfy the test below."""
    result = analyze_changelog()
    assert len(result.data_effecting) >= 60, (
        "suspiciously few data-effecting changesets found "
        f"({len(result.data_effecting)}) -- possible classifier regression"
    )


def test_real_changelog_every_data_effecting_changeset_is_disclosed():
    """The tripwire itself: every data-effecting changeset in the ACTUAL
    service/src/main/resources/db/changelog/ tree must carry a DATA EFFECT
    line in its <comment>. A changeset landing here without one is a defect
    to fix in the SAME commit that adds the data-modifying SQL -- see
    scripts/data_effect_lint.py's module docstring for the convention and
    scripts/list_data_effects.py for the deploy-handoff reporting tool this
    line feeds."""
    result = analyze_changelog()
    assert result.missing_disclosure == [], (
        "changeset(s) modify or remove rows with no DATA EFFECT: line in "
        "their <comment> -- see nexus-f7dwp / scripts/data_effect_lint.py: "
        + ", ".join(
            f"{f.changeset.file}:{f.changeset.changeset_id} ({','.join(r.kind for r in f.reasons)})"
            for f in result.missing_disclosure
        )
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
