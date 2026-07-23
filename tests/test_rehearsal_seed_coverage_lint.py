# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-gm38i: rehearsal seed-coverage parity lint.

Provenance: substantive-critic Significant on the nexus-u5dln data-bearing
rehearsal leg. ``SchemaUpgradeRehearsalIntegrationTest``'s data leg seeds
inputs for the FORCE-RLS row-DML changesets of the CURRENT old-tag-to-HEAD
hop (catalog-013 chash normalization, catalog-014-0 manifest stamp), but
that coverage list was maintained by convention only: a FUTURE hop's new
row-DML changeset could pass the syntactic FORCE-RLS lint
(``tests/test_changelog_rls_lint.py`` — toggle-wrap/backstop shape, a
different property) while the rehearsal silently reverts to under-covering
the new hop — the exact generalization-overclaim class nexus-u5dln closed,
one hop later.

This lint closes that loop mechanically:

1. Parse OLD_TAG out of the Java rehearsal test (strict — a parse miss
   FAILS, never falls back; the run.sh nexus-b6qlf lesson). The parse and
   the integrity canonicalization are IMPORTED from
   ``scripts/gen_rehearsal_hop_manifest.py`` (``pyproject.toml`` puts
   ``scripts`` on the test pythonpath), so the generator and this lint can
   never regex- or hash-drift.
2. Load the committed OLD_TAG changeset snapshot
   (``tests/data/rehearsal_old_tag_changesets.json``) and verify THREE
   properties: its tag matches the Java OLD_TAG (release tags are
   immutable, so a tag-matched snapshot cannot rot; a rotated OLD_TAG with
   a stale manifest fails with regeneration instructions), its content
   hash matches the embedded integrity field (nexus-4sl9k: everything in
   the snapshot is SUBTRACTED from the required-coverage set, so a single
   hand-added entry would otherwise silently defeat the gate — the hash is
   computed only by the generator, from git truth; the documented residual
   is deliberate hand-forgery of the hash, which carries the same intent
   as editing this lint's assertions), and its ``(id, author)`` pairs are
   unique (Liquibase identity is ``(id, author, filename)``; a cross-file
   duplicate pair would make the hop subtraction silently drop a genuinely
   new changeset whose key collides with a pre-OLD_TAG one). No
   git/network at test time — this lint has NO skip path (feedback: gates
   are scripted, never ambient).
3. Derive, from the HEAD changelog, every changeset carrying migration-time
   row-DML risk on a FORCE-RLS table. Two detection rules:
   - a literal top-level (or DO-block) INSERT/UPDATE/DELETE whose target OR
     any referenced ``nexus.``/``t1.`` table was FORCE at changeset entry
     (in-changeset toggles do not exempt — the table is FORCE-managed and
     the rehearsal must prove the toggle discipline dynamically);
   - any ``NO FORCE ROW LEVEL SECURITY`` toggle in the changeset — the tell
     for the ``SELECT some_fn()`` static blind spot (catalog-014-0's shape:
     the DML hides inside a function body the analyzer cannot see, but that
     one real instance toggles FORCE in its own ``<sql>`` body, making the
     toggle visible). This is an EMPIRICAL tell, not a structural
     guarantee (same hedge as the RLS lint's own SELECT-fn() disclosure):
     a future fn-hiding changeset needing NO caller-side toggle (e.g. a
     SECURITY DEFINER function, or one toggling FORCE inside its own body)
     would escape both rules.
4. Hop set = derived(HEAD) minus the snapshot's changesets. Assert it
   EQUALS (exact ``==``) the rehearsal's declared seed coverage — which is
   itself asserted equal to the SEED-COVERAGE contract block inside the
   Java data leg, so the declaration cannot move without a diff to the
   rehearsal file the coverage claim is about.

Inherited/documented blind spots (mirroring the RLS lint's own admissions):

- **CTE-prefixed DML** (``WITH x AS (DELETE FROM nexus.t ...) INSERT ...``)
  is invisible to the shared ``_DML_TARGET_RE`` (literal leading
  INSERT/UPDATE/DELETE only) — inherited verbatim from the RLS lint, which
  documents it as grep-verified absent from the real corpus; if such a
  statement also carries no FORCE toggle it escapes BOTH rules here.
- **``sqlFile`` / ``customChange`` / ``createProcedure``** elements cannot
  be scanned; unlike the RLS lint (which reports them as findings), this
  derivation RAISES on sight — extend both lints before using one.
- Rule (a)'s entry-state semantics implicitly lean on the RLS lint
  independently gating the one shape entry-state cannot see: a changeset
  that establishes FORCE for the first time and row-DMLs pre-existing rows
  in the SAME changeset is entry-False/toggle-free here, but is a live
  NAKED_DML finding there (php10 forces it into toggle-wrap — then caught
  by rule (b) — or a same-changeset backstop, which is safe-or-loud without
  rehearsal data). Loosening the RLS lint loosens this lint's coverage.
- **Accepted residual** (same class as the hash-forgery one above): the
  contract-block↔declaration parity cannot verify the Java data leg
  SEMANTICALLY seeds what the block claims — adding a new hop changeset to
  both declarations without writing real seed code satisfies every
  mechanical check here. What the parity buys is the diff locus: that
  fabrication now requires an edit inside the rehearsal file itself,
  adjacent to the real seed code, in front of the reviewer.

When the hop gate fails, the fix is never to edit the declarations alone:
extend the rehearsal's data leg to seed the new changeset's input shape and
assert its effect (see the data-leg javadoc's "template to extend"), THEN
update the Java SEED-COVERAGE block and the declaration below, together.
On OLD_TAG rotation: regenerate the manifest
(``uv run python scripts/gen_rehearsal_hop_manifest.py``), re-derive the new
hop's coverage, and re-point all three (Java seeding, Java contract block,
Python declaration).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from gen_rehearsal_hop_manifest import manifest_integrity, parse_old_tag
from tests.test_changelog_rls_lint import (
    CHANGELOG_DIR,
    MASTER_CHANGELOG,
    _DML_TARGET_RE,
    _FORCE_TOGGLE_RE,
    _LEADING_DO_BEGIN_RE,
    _TABLE_MENTION_RE,
    _split_statements,
    _table_key,
    parse_master_include_order,
)

REPO_ROOT = Path(__file__).parent.parent
JAVA_REHEARSAL_TEST = (
    REPO_ROOT
    / "service/src/test/java/dev/nexus/service/SchemaUpgradeRehearsalIntegrationTest.java"
)
MANIFEST_PATH = REPO_ROOT / "tests/data/rehearsal_old_tag_changesets.json"

_XSD_NS = "{http://www.liquibase.org/xml/ns/dbchangelog}"
_UNSCANNED_ELEMENT_TAGS = ("sqlFile", "customChange", "createProcedure")

_SEED_COVERAGE_BLOCK_RE = re.compile(
    r"SEED-COVERAGE-BEGIN.*?\):(?P<body>.*?)SEED-COVERAGE-END", re.DOTALL
)
_SEED_COVERAGE_LINE_RE = re.compile(r"//\s+(\S+)\s+(\S+)\s*$", re.MULTILINE)

#: The rehearsal data leg's DECLARED seed coverage — the (id, author) pairs of
#: every hop changeset whose row-DML the leg seeds inputs for and asserts the
#: effect of. Kept in mechanical parity with the SEED-COVERAGE contract block
#: inside SchemaUpgradeRehearsalIntegrationTest's data-leg method (asserted
#: below) — neither side can move alone:
#:   catalog-013-0 / catalog-013-1b — legacy 64-char chash_index rows
#:   catalog-014-0                  — un-stamped manifest rows (collection NULL)
DECLARED_SEED_COVERAGE: frozenset[tuple[str, str]] = frozenset(
    {
        ("catalog-013-0", "nexus-e0hd2"),
        ("catalog-013-1b", "nexus-1wjmq"),
        ("catalog-014-0", "nexus-x6kdz"),
        # nexus-78n33: the source_uri dedup backfill (FORCE-RLS-toggled row
        # DML) — seeded as duplicate live-uri docs 1.1.201/1.1.202 in the
        # data leg; effect-asserted (loser tombstoned, winner survives,
        # 016-1 index exists).
        ("catalog-016-0", "nexus-78n33"),
    }
)


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def parse_java_seed_coverage(java_source: str) -> set[tuple[str, str]]:
    """The SEED-COVERAGE contract block inside the Java data leg (strict)."""
    m = _SEED_COVERAGE_BLOCK_RE.search(java_source)
    if m is None:
        raise AssertionError(
            "could not find the SEED-COVERAGE-BEGIN/END contract block in "
            "SchemaUpgradeRehearsalIntegrationTest.java — it moved or was "
            "deleted; the block is this lint's mechanical tie to the actual "
            "rehearsal seeding (no fallback)"
        )
    pairs = {
        (mid, author)
        for mid, author in _SEED_COVERAGE_LINE_RE.findall(m.group("body"))
        if mid != "SEED-COVERAGE-END"
    }
    if not pairs:
        raise AssertionError(
            "SEED-COVERAGE contract block parsed empty — malformed block"
        )
    return pairs


def _iter_changesets_with_author(changelog_dir: Path, basename: str):
    """Yield ``(id, author, sql_text)`` per changeSet, document order.

    Own iterator (the shared one yields no author): only direct ``<sql>``
    children are read, so ``<rollback>`` bodies are structurally excluded —
    rollback SQL never runs at migration time. An element kind this
    analyzer family cannot see into RAISES (the RLS lint reports the same
    class as an unsuppressible finding; this derivation must not silently
    under-derive past one).
    """
    root = ET.parse(changelog_dir / basename).getroot()
    for cs in root.iter(f"{_XSD_NS}changeSet"):
        for tag in _UNSCANNED_ELEMENT_TAGS:
            if cs.find(f"{_XSD_NS}{tag}") is not None:
                raise AssertionError(
                    f"changeset {cs.get('id')!r} in {basename} uses <{tag}> — "
                    "unscanned Liquibase element; extend this lint (and the "
                    "RLS lint) before relying on either for FORCE-RLS DML "
                    "coverage"
                )
        sql_texts = [el.text or "" for el in cs.findall(f"{_XSD_NS}sql") if el.text]
        yield cs.get("id", ""), cs.get("author", ""), "\n".join(sql_texts)


def derive_force_row_dml_changesets(
    changelog_dir: Path = CHANGELOG_DIR,
    master_path: Path = MASTER_CHANGELOG,
) -> set[tuple[str, str]]:
    """Every (id, author) whose changeset carries FORCE-RLS row-DML risk.

    Rule (a): a literal DML statement whose target or any referenced table
    was FORCE at changeset ENTRY (pre-changeset state — in-changeset toggles
    do not exempt). Rule (b): the changeset issues any ``NO FORCE`` toggle
    (the SELECT-fn() blind-spot tell — empirical, see module docstring).

    Self-checks: the master include list must match the directory contents
    (a file on disk but not included would silently escape the walk), and
    ``(id, author)`` pairs must be unique across the whole walked tree —
    the hop subtraction keys on that pair, so a cross-file duplicate would
    let a NEW changeset silently hide behind a pre-OLD_TAG key.
    """
    include_order = parse_master_include_order(master_path)
    on_disk = {
        p.name for p in changelog_dir.glob("*.xml") if p.name != master_path.name
    }
    assert set(include_order) == on_disk, (
        "master include list drifted from the changelog directory: "
        f"included-not-on-disk={set(include_order) - on_disk}, "
        f"on-disk-not-included={on_disk - set(include_order)}"
    )

    global_force: dict[str, bool] = {}
    marked: set[tuple[str, str]] = set()
    seen_keys: set[tuple[str, str]] = set()

    for basename in include_order:
        for cs_id, author, sql_text in _iter_changesets_with_author(
            changelog_dir, basename
        ):
            key = (cs_id, author)
            assert key not in seen_keys, (
                f"duplicate changeset key {key} (second occurrence in "
                f"{basename}) — Liquibase identity is (id, author, filename) "
                "but the hop subtraction keys on (id, author); a cross-file "
                "duplicate would silently hide a new changeset behind a "
                "pre-OLD_TAG one. Rename the new changeset id."
            )
            seen_keys.add(key)

            entry_force = dict(global_force)
            live_force = dict(global_force)
            toggled_no_force = False

            for stmt in _split_statements(sql_text):
                m = _FORCE_TOGGLE_RE.search(stmt)
                if m and stmt.upper().lstrip().startswith("ALTER TABLE"):
                    tkey = _table_key(m.group(1), m.group(2))
                    is_no_force = bool(m.group(3))
                    live_force[tkey] = not is_no_force
                    if is_no_force:
                        toggled_no_force = True
                    continue

                stmt_for_dml = _LEADING_DO_BEGIN_RE.sub("", stmt)
                dml = _DML_TARGET_RE.match(stmt_for_dml)
                if dml:
                    touched = {_table_key(dml.group(1), dml.group(2))}
                    touched.update(
                        _table_key(g[0], g[1])
                        for g in _TABLE_MENTION_RE.findall(stmt)
                    )
                    if any(entry_force.get(t) for t in touched):
                        marked.add(key)

            if toggled_no_force:
                marked.add(key)
            global_force = live_force

    return marked


def load_manifest() -> tuple[str, set[tuple[str, str]]]:
    """Load + integrity-verify the OLD_TAG snapshot (nexus-4sl9k)."""
    data = json.loads(MANIFEST_PATH.read_text())
    tag = data["tag"]
    changesets = data["changesets"]
    expected = manifest_integrity(tag, changesets)
    assert data.get("integrity") == expected, (
        "rehearsal OLD_TAG snapshot failed its integrity check — the file "
        "was edited by hand (everything in it is SUBTRACTED from the "
        "required seed coverage, so a hand-added entry silently defeats the "
        "gate). NEVER hand-edit; regenerate from the immutable tag: "
        "uv run python scripts/gen_rehearsal_hop_manifest.py"
    )
    pairs = {(c["id"], c["author"]) for c in changesets}
    assert len(pairs) == len(changesets), (
        "duplicate (id, author) pairs inside the OLD_TAG snapshot — "
        "regenerate and investigate (Liquibase identity is per-file; the "
        "subtraction keys on the pair)"
    )
    return tag, pairs


# ---------------------------------------------------------------------------
# Synthetic detector tests (fixtures mirror the RLS lint's harness)
# ---------------------------------------------------------------------------


def _write_changelog(tmp_path: Path, changesets_xml: str) -> tuple[Path, Path]:
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    header = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<databaseChangeLog\n"
        '    xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
        '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '    xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog '
        'http://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-4.4.xsd">\n'
    )
    (changelog_dir / "synthetic-001.xml").write_text(
        f"{header}{changesets_xml}\n</databaseChangeLog>\n"
    )
    master = changelog_dir / "db.changelog-master.xml"
    master.write_text(
        f'{header}    <include file="synthetic-001.xml"/>\n</databaseChangeLog>\n'
    )
    return changelog_dir, master


_FORCE_WIDGETS = """
    <changeSet id="cs-force" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
"""


def test_naked_dml_on_force_table_is_derived(tmp_path):
    xml = _FORCE_WIDGETS + """
    <changeSet id="cs-dml" author="t">
        <sql splitStatements="true">
DELETE FROM nexus.widgets WHERE stale = true;
        </sql>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    assert derive_force_row_dml_changesets(d, m) == {("cs-dml", "t")}


def test_toggle_wrapped_dml_is_still_derived(tmp_path):
    """The rehearsal must seed toggle-wrapped changesets too — the leg is the
    DYNAMIC proof the toggle discipline works (catalog-013-1b shape)."""
    xml = _FORCE_WIDGETS + """
    <changeSet id="cs-wrapped" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets NO FORCE ROW LEVEL SECURITY;
UPDATE nexus.widgets SET name = substr(name, 1, 8);
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    assert derive_force_row_dml_changesets(d, m) == {("cs-wrapped", "t")}


def test_toggle_only_changeset_is_derived_via_the_fn_call_tell(tmp_path):
    """catalog-014-0's exact blind-spot shape: no literal DML keyword at all
    (the DML hides inside SELECT some_fn()), but the NO FORCE toggle is the
    visible tell."""
    xml = _FORCE_WIDGETS + """
    <changeSet id="cs-fn" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets NO FORCE ROW LEVEL SECURITY;
SELECT nexus.widget_backfill();
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    assert derive_force_row_dml_changesets(d, m) == {("cs-fn", "t")}


def test_dml_on_never_force_table_is_not_derived(tmp_path):
    xml = """
    <changeSet id="cs-plain" author="t">
        <sql splitStatements="true">
UPDATE nexus.service_tokens SET scope = 'root' WHERE label = 'x';
        </sql>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    assert derive_force_row_dml_changesets(d, m) == set()


def test_do_block_dml_on_force_table_is_derived(tmp_path):
    """taxonomy-004-1 shape: DML inside DO $$ ... $$ executes at migration
    time and must be derived, never stripped as an exempt function body."""
    xml = _FORCE_WIDGETS + """
    <changeSet id="cs-do" author="t">
        <sql splitStatements="false">
DO $$
BEGIN
    DELETE FROM nexus.widgets WHERE stale = true;
END $$;
        </sql>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    assert derive_force_row_dml_changesets(d, m) == {("cs-do", "t")}


def test_create_function_body_dml_is_not_derived(tmp_path):
    xml = _FORCE_WIDGETS + """
    <changeSet id="cs-createfn" author="t">
        <sql splitStatements="false">
CREATE OR REPLACE FUNCTION nexus.widget_trash(wid text)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER
AS $$
BEGIN
    UPDATE nexus.widgets SET deleted_at = NOW() WHERE id = wid;
END;
$$
        </sql>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    assert derive_force_row_dml_changesets(d, m) == set()


def test_dml_reading_a_force_table_is_derived(tmp_path):
    """INSERT into a never-FORCE table SELECTing from a FORCE one is still
    data-dependent under RLS (the read side sees zero rows) — derived."""
    xml = _FORCE_WIDGETS + """
    <changeSet id="cs-read" author="t">
        <sql splitStatements="true">
INSERT INTO nexus.summary (name) SELECT name FROM nexus.widgets;
        </sql>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    assert derive_force_row_dml_changesets(d, m) == {("cs-read", "t")}


def test_duplicate_changeset_key_fails_loud(tmp_path):
    """A cross-changeset (id, author) duplicate would let a NEW changeset
    hide behind a pre-OLD_TAG key in the hop subtraction — hard error."""
    xml = """
    <changeSet id="cs-dup" author="t">
        <sql splitStatements="true">CREATE TABLE nexus.a (id int);</sql>
    </changeSet>
    <changeSet id="cs-dup" author="t">
        <sql splitStatements="true">CREATE TABLE nexus.b (id int);</sql>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    with pytest.raises(AssertionError, match="duplicate changeset key"):
        derive_force_row_dml_changesets(d, m)


def test_unscanned_element_fails_loud(tmp_path):
    xml = """
    <changeSet id="cs-unscanned" author="t">
        <sqlFile path="some.sql"/>
    </changeSet>
    """
    d, m = _write_changelog(tmp_path, xml)
    with pytest.raises(AssertionError, match="unscanned Liquibase element"):
        derive_force_row_dml_changesets(d, m)


def test_old_tag_parse_miss_fails_loud():
    with pytest.raises(AssertionError, match="could not parse OLD_TAG"):
        parse_old_tag("public class Foo { /* no constant here */ }")


def test_manifest_tamper_fails_integrity(tmp_path, monkeypatch):
    """nexus-4sl9k regression: a single hand-added snapshot entry (claiming
    a hop changeset already existed at OLD_TAG) must fail the integrity
    check — the exact silent-defeat the critic reproduced on the first cut."""
    data = json.loads(MANIFEST_PATH.read_text())
    data["changesets"].append(
        {"id": "catalog-099-fabricated", "author": "attacker", "file": "x.xml"}
    )
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(data))
    monkeypatch.setattr(
        "tests.test_rehearsal_seed_coverage_lint.MANIFEST_PATH", tampered
    )
    with pytest.raises(AssertionError, match="integrity check"):
        load_manifest()


def test_java_contract_block_parse_miss_fails_loud():
    with pytest.raises(AssertionError, match="SEED-COVERAGE"):
        parse_java_seed_coverage("class Foo {}")


# ---------------------------------------------------------------------------
# The real-corpus gate
# ---------------------------------------------------------------------------


def test_manifest_tag_matches_java_old_tag():
    """OLD_TAG rotation tripwire: a rotated Java OLD_TAG with a stale
    snapshot fails HERE, with the regeneration instruction."""
    java_tag = parse_old_tag(JAVA_REHEARSAL_TEST.read_text())
    manifest_tag, changesets = load_manifest()
    assert manifest_tag == java_tag, (
        f"rehearsal OLD_TAG rotated to {java_tag!r} but the changeset "
        f"snapshot is still for {manifest_tag!r} — regenerate it: "
        "uv run python scripts/gen_rehearsal_hop_manifest.py, then re-derive "
        "the new hop's seed coverage (the Java data leg's seeding/gates, its "
        "SEED-COVERAGE block, and this lint's DECLARED_SEED_COVERAGE all "
        "need re-pointing)"
    )
    # Non-vacuity floor: an empty/truncated snapshot must never silently
    # subtract nothing (the v0.1.17 tree has 142; any real tag has >= 100).
    # Secondary to the integrity hash — kept as an independent belt.
    assert len(changesets) >= 100, (
        f"snapshot holds only {len(changesets)} changesets — truncated or "
        "misgenerated; regenerate via scripts/gen_rehearsal_hop_manifest.py"
    )


def test_declaration_matches_java_contract_block():
    """The Python declaration and the Java data leg's SEED-COVERAGE block
    must agree — neither can be edited alone (the block lives next to the
    seeding code, so moving coverage requires a diff to the rehearsal file
    the claim is about)."""
    java_pairs = parse_java_seed_coverage(JAVA_REHEARSAL_TEST.read_text())
    assert java_pairs == set(DECLARED_SEED_COVERAGE), (
        "DECLARED_SEED_COVERAGE and the Java SEED-COVERAGE contract block "
        "disagree — update them TOGETHER, alongside the actual seeding:\n"
        f"  Python-only: {sorted(set(DECLARED_SEED_COVERAGE) - java_pairs)}\n"
        f"  Java-only:   {sorted(java_pairs - set(DECLARED_SEED_COVERAGE))}"
    )


def test_detector_reproduces_known_historical_members():
    """Detector sanity against the real corpus: every historically-known
    FORCE-RLS row-DML member must be derived (containment, not exact — the
    full set legitimately GROWS with new safe changesets; the exact gate is
    the hop assertion below, and the synthetic tests pin detector behavior
    exactly)."""
    derived = derive_force_row_dml_changesets()
    known = {
        ("catalog-013-0", "nexus-e0hd2"),
        ("catalog-013-1b", "nexus-1wjmq"),
        ("catalog-014-0", "nexus-x6kdz"),
        ("taxonomy-004-1", "nexus-slcn7"),
        ("fk-002-0-backfill-stubs", "nexus-70r3c.2"),
        ("fk-002-6-reconcile", "nexus-70r3c.3"),
        ("fk-003-0-backfill-stubs", "nexus-dcqml"),
        ("fk-003-6-reconcile", "nexus-p9aw6"),
    }
    missing = known - derived
    assert not missing, (
        f"detector no longer derives known FORCE-RLS row-DML members: "
        f"{sorted(missing)} — the classification logic regressed"
    )


def test_hop_row_dml_changesets_equal_declared_rehearsal_seed_coverage():
    """THE GATE (nexus-gm38i): every FORCE-RLS row-DML changeset the
    old-tag-to-HEAD hop runs must be seed-covered by the rehearsal's data
    leg. Exact ``==``: a NEW hop changeset fails until the rehearsal is
    extended; a REMOVED one (OLD_TAG rotation) fails until coverage is
    re-derived."""
    _, old_tag_changesets = load_manifest()
    derived = derive_force_row_dml_changesets()
    hop = derived - old_tag_changesets
    assert hop == DECLARED_SEED_COVERAGE, (
        "the old-tag-to-HEAD hop's FORCE-RLS row-DML changesets drifted from "
        "the rehearsal's declared seed coverage.\n"
        f"  in hop but NOT seed-covered: {sorted(hop - DECLARED_SEED_COVERAGE)}\n"
        f"  declared but no longer in hop: {sorted(DECLARED_SEED_COVERAGE - hop)}\n"
        "Fix: extend SchemaUpgradeRehearsalIntegrationTest's data leg to seed "
        "the new changeset's input shape and assert its EFFECT (see the "
        "data-leg javadoc's 'template to extend'), then update the Java "
        "SEED-COVERAGE block and DECLARED_SEED_COVERAGE here, together. "
        "Never update the declarations alone."
    )
