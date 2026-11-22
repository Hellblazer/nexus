# SPDX-License-Identifier: AGPL-3.0-or-later
"""A changeset with ``preConditions onFail="CONTINUE"`` must not touch the chunk tables or the index builder.

WHY THIS EXISTS (nexus-f3yqd.3). ``SchemaMigrator.migrate`` ends the per-collection index builders at boot when
the walk has a schema changeset to run. A changeset whose precondition is ``onFail="CONTINUE"`` is skipped
WITHOUT a ``DATABASECHANGELOG`` row, so it is unrun on every boot until its guard passes (``aspects-005-2`` waits
for ``nexus.aspect_extraction_queue`` to empty). The migrator therefore does not count such a changeset as schema
work: that is what lets a restart leave a build alone while the guard waits. The exemption is sound only because
the body of such a changeset can never conflict with a ``CREATE INDEX CONCURRENTLY``. A body that names
``nexus.chunks``, a chunk leaf (``chunks_m...``), the partition helpers (``nexus.partition_...``) or the builder
(``pci_...``) could take a lock the build waits behind, and the migrator would then queue behind the build it
was told to keep. This lint is the other half of that exemption.

Scanned: the preconditions and the body. Not scanned: ``<comment>`` (prose) and ``<rollback>`` (never runs at boot).

ONE EXEMPTION (RDR-229 P3.3c, nexus-p2m1x.21). ``CREATE INDEX CONCURRENTLY [IF NOT EXISTS] <name> ON <table with a foreign key into
nexus.chunks> (...)`` is not a violation of the foreign-key-table name rule, and only of that rule. The rule exists because a DROP or
ALTER of such a table takes ACCESS EXCLUSIVE on every chunk leaf (the foreign-key triggers go with it); CREATE INDEX CONCURRENTLY takes
SHARE UPDATE EXCLUSIVE on the one table it indexes and nothing on nexus.chunks or a leaf, and a CREATE INDEX CONCURRENTLY on a leaf
holds no lock on that table. The statement shape is matched exactly (``_EXEMPT_INDEX_BUILD``); any other mention of the table, in the
same changeset or in the statement's own columns, and the same statement on nexus.chunks itself, still fail.

THREE GUARDS, none of them a snapshot of today's corpus:

1. NAMES. The executable text is normalised (double quotes dropped, whitespace around dots collapsed, lowercased)
   and matched against word-bounded names, with or without a schema: ``chunks``, ``chunks_m...``, ``partition_...``,
   ``pci_...`` and every table that holds a foreign key INTO ``nexus.chunks`` (a DROP or ALTER of one takes ACCESS
   EXCLUSIVE on every chunk leaf). A second test derives that last set from the changelog, so a new such table
   fails here until it is added to ``_FK_TABLES``.
2. THE CENSUS. The CONTINUE changesets must equal ``_CONTINUE_ALLOWLIST`` exactly. A new one fails until someone
   reads its body and adds it, with a one-line lock argument. A CONTINUE changeset that uses ``<sqlFile>``,
   ``<customChange>`` or ``<executeCommand>`` fails outright: the lint cannot see inside those. Dynamic SQL
   (``format('%I.%I', ...)``) is also invisible to a name match; the allowlist review is what covers it.
3. THE PREDICATE. ``RetryingChangesets.isSelfRetrying`` reads ``onFail="CONTINUE"`` of the top precondition
   container and nothing else. A changeset that can stay unrecorded in another way (``onError="CONTINUE"`` with any
   other ``onFail``, or ``failOnError="false"`` with no ``onFail="CONTINUE"`` precondition) is pending on every boot
   yet invisible to it, so it would end every build. Such a changeset fails here.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from tests.test_changelog_rls_lint import CHANGELOG_DIR

_NS = "{http://www.liquibase.org/xml/ns/dbchangelog}"

# Tables that hold a foreign key INTO nexus.chunks. DROP or ALTER of one takes ACCESS EXCLUSIVE on every chunk leaf
# (the parent's foreign-key triggers go with it). test_fk_tables_list_covers_the_changelog derives the real set.
_FK_TABLES = ("catalog_document_chunks", "topic_assignments", "chunk_orphaned_at")

# Matched against the NORMALISED text (see _normalise), so each is lowercase, unquoted and schema-agnostic.
_FORBIDDEN = {
    "nexus.chunks": re.compile(r"\bchunks\b"),
    "chunk leaf (chunks_m)": re.compile(r"\bchunks_m"),
    "partition helper (partition_)": re.compile(r"\bpartition_"),
    "index builder (pci_)": re.compile(r"\bpci_"),
    "table with a foreign key into nexus.chunks": re.compile(r"\b(?:" + "|".join(_FK_TABLES) + r")\b"),
}

# The CONTINUE census, exactly. A new CONTINUE changeset must be read, then added here with the argument that its
# body takes no lock a CREATE INDEX CONCURRENTLY on a chunk leaf waits behind (the migrator does not count it as
# schema work, so a restart keeps the build through it).
_CONTINUE_ALLOWLIST: dict[tuple[str, str], str] = {
    # DROP TABLE nexus.aspect_extraction_queue (AEL on it and, for its two FKs, on catalog_documents and
    # catalog_collections) and DROP FUNCTION of its counting helper: no chunk table, leaf or builder object.
    ("aspects-005-retire-aspect-queue-table.xml", "aspects-005-2"): "aspect queue retirement",
    # ADD COLUMN and a NOT VALID CHECK on nexus.aspect_extraction_queue (AEL on that table only), inside a DO block
    # that does nothing when the table is absent; skipped while the migration role does not own a kept table. The
    # body names no chunk table, leaf or builder object (RDR-229 P3.1, residual R3).
    ("vectors-068-collection-id-expand.xml", "vectors-068-18"): "aspect queue id column",
    # A NOT VALID CHECK on nexus.catalog_collections (AEL on that one table), skipped while a tombstone with a blank
    # model_version and no superseded_by_id exists, so D2's repair UPDATE cannot meet it (RDR-229 P3.1, item e). The
    # precondition is a read of catalog_collections; the body names no chunk table, leaf or builder object.
    ("vectors-068-collection-id-expand.xml", "vectors-068-19"): "superseded_by CHECK waits on D2 data",
    # One INSERT into nexus.data_moves (ROW EXCLUSIVE on that registry table only), inside a DO block that does nothing
    # when nexus.aspect_extraction_queue is absent; the precondition and the block read pg_class and pg_attribute for the
    # queue and take no lock on it. The body names no chunk table, leaf or builder object (RDR-229 P3.1, residual R3).
    ("vectors-070-collection-id-stamp-moves.xml", "vectors-070-30"): "aspect queue stamp move registration",
    # SET LOCAL lock_timeout = '5s', then DROP POLICY IF EXISTS and CREATE POLICY on nexus.aspect_extraction_queue only
    # (ACCESS EXCLUSIVE on that one table, bounded by the 5 s lock_timeout), inside a DO block that does nothing when the
    # table is absent; the rest is reads of pg_roles, pg_policy and pg_class. The body names no chunk table, leaf or
    # builder object (RDR-229 P3.1, residual R3).
    ("vectors-070-collection-id-stamp-moves.xml", "vectors-070-31"): "aspect queue owner-read policy",
    # SET LOCAL lock_timeout = '5s', then ALTER TABLE nexus.aspect_extraction_queue ADD CONSTRAINT ... FOREIGN KEY ... NOT VALID
    # (SHARE ROW EXCLUSIVE on that table and on nexus.catalog_collections for an instant, bounded by the 5 s lock_timeout),
    # inside a DO block that does nothing when the table is absent or has no collection_id column; the CONTINUE
    # precondition is a read of pg_class and pg_attribute. The body names no chunk table, leaf or builder object, and
    # the registry is not a chunk-family table, so the lock cannot wait behind or hold up a builder (RDR-229 P3.3a, residual R3).
    ("vectors-072-collection-id-fk-twins.xml", "vectors-072-7"): "aspect queue id twin foreign key",
    # One INSERT into nexus.data_moves (ROW EXCLUSIVE on that registry table only), inside a DO block that does nothing
    # when nexus.aspect_extraction_queue or its twin is absent; the precondition and the block read pg_class and
    # pg_constraint and take no lock on the queue. The body names no chunk table, leaf or builder object (RDR-229 P3.3a, residual R3).
    ("vectors-072-collection-id-fk-twins.xml", "vectors-072-17"): "aspect queue twin validation move registration",
    # CREATE UNIQUE INDEX on nexus.catalog_collections (SHARE on it for one build of a registry of hundreds of rows) and,
    # in its guard, the counter's ALTER TABLE on that table (ACCESS EXCLUSIVE for one count) plus a read of
    # nexus.data_moves. No chunk table, leaf or builder object is named, and a CREATE INDEX CONCURRENTLY on a leaf holds
    # no lock on the registry, so the build cannot be what either statement waits behind.
    ("collections-001-tuple-unique-index.xml", "collections-001-2"): "gated live-tuple unique index (RDR-229 P3.8)",
    # CREATE UNIQUE INDEX on nexus.catalog_collections (tenant_id, name) under SET LOCAL lock_timeout = '5s' (SHARE on
    # that one table for one build of a registry of hundreds of rows), failOnError="false" so a build that cannot get
    # its lock is retried at the next boot; the guard reads pg_class through to_regclass and takes no lock. No chunk
    # table, leaf or builder object is named, and a CREATE INDEX CONCURRENTLY on a leaf holds no lock on the registry.
    ("catalog-053-live-label-unique.xml", "catalog-053-1"): "live-label unique index (RDR-229 P3.6)",
    # SET lock_timeout = '30s', DROP INDEX CONCURRENTLY IF EXISTS and CREATE INDEX CONCURRENTLY IF NOT EXISTS on nexus.chunk_orphaned_at
    # (tenant_id, chash): SHARE UPDATE EXCLUSIVE on that one table, which holds a foreign key into nexus.chunks but is not locked by a
    # chunk leaf's build, and nothing on nexus.chunks or a leaf; the wait for older transactions ends at the 30 s lock_timeout and the
    # changeset is skipped (failOnError false), not failed. The one table name in the body is the exempt statement shape
    # (_EXEMPT_INDEX_BUILD). No chunk table, leaf, partition helper or builder object is named (RDR-229 P3.3c, nexus-p2m1x.21).
    ("vectors-074-chunk-orphaned-at-chash-index.xml", "vectors-074-1"): "child index of the chunk id key",
    # The supporting indexes of P3.3a's id twins (RDR-229 P3.4, nexus-p2m1x.22), each shaped like vectors-074-1: SET lock_timeout =
    # '30s', DROP INDEX CONCURRENTLY IF EXISTS and CREATE INDEX CONCURRENTLY IF NOT EXISTS on one table (tenant_id, <id column>):
    # SHARE UPDATE EXCLUSIVE on that table and nothing on nexus.chunks or a leaf; a lock wait that ends skips the boot's build
    # (failOnError false). topic_assignments (vectors-075-1) holds a foreign key into nexus.chunks and its build is the exempt
    # statement shape; the other five tables hold no such key. The precondition reads pg_index by the index's name only.
    ("vectors-075-collection-id-fk-twin-indexes.xml", "vectors-075-1"): "id-twin index, topic_assignments",
    ("vectors-075-collection-id-fk-twin-indexes.xml", "vectors-075-2"): "id-twin index, document_aspects",
    ("vectors-075-collection-id-fk-twin-indexes.xml", "vectors-075-3"): "id-twin index, topics",
    ("vectors-075-collection-id-fk-twin-indexes.xml", "vectors-075-4"): "id-twin index, taxonomy_meta",
    ("vectors-075-collection-id-fk-twin-indexes.xml", "vectors-075-5"): "id-twin index, document_highlights",
    ("vectors-075-collection-id-fk-twin-indexes.xml", "vectors-075-6"): "id-twin index, canary_query_ring",
    # The retired queue's (vectors-075-7): a DO-guarded plain CREATE INDEX IF NOT EXISTS on nexus.aspect_extraction_queue
    # (tenant_id, collection_id) under SET LOCAL lock_timeout = '5s', guarded like vectors-072-7; that table holds no key into chunks.
    ("vectors-075-collection-id-fk-twin-indexes.xml", "vectors-075-7"): "id-twin index, retired aspect_extraction_queue",
}

# The one statement shape exempt from the foreign-key-table rule (see the module docstring), matched on the NORMALISED text.
_EXEMPT_INDEX_BUILD = re.compile(
    r"create\s+index\s+concurrently\s+(?:if\s+not\s+exists\s+)?\w+\s+on\s+(?:nexus\.)?(?:" + "|".join(_FK_TABLES) + r")\s*\([\w\s,]*\)"
)

# Elements the lint cannot read into: what they run is not in the changelog text.
_OPAQUE = ("sqlFile", "customChange", "executeCommand")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _normalise(text: str) -> str:
    """Lowercase, drop double quotes, collapse whitespace around dots: ``"NEXUS" . "Chunks"`` becomes ``nexus.chunks``."""
    text = text.replace('"', "").lower()
    return re.sub(r"\s*\.\s*", ".", text)


def _preconditions(cs: ET.Element) -> list[ET.Element]:
    """The changeset's own precondition containers: what the engine's classifier reads (the top one)."""
    return cs.findall(f"{_NS}preConditions")


def _is_continue(cs: ET.Element) -> bool:
    """True when a precondition container of the changeset has onFail="CONTINUE" (RetryingChangesets.isSelfRetrying)."""
    return any((pre.get("onFail") or "").strip().upper() == "CONTINUE" for pre in _preconditions(cs))


def _can_stay_unrecorded(cs: ET.Element) -> bool:
    """True when the changeset can be pending on every boot with no DATABASECHANGELOG row."""
    return (
        _is_continue(cs)
        or any((pre.get("onError") or "").strip().upper() == "CONTINUE" for pre in _preconditions(cs))
        or (cs.get("failOnError") or "").strip().lower() == "false"
    )


def _executable_children(cs: ET.Element) -> list[ET.Element]:
    return [c for c in cs if c.tag not in (f"{_NS}comment", f"{_NS}rollback")]


def _executable_text(cs: ET.Element) -> str:
    """Everything the changeset runs at boot: every child but comment and rollback, serialised (attributes included)."""
    return "\n".join(ET.tostring(child, encoding="unicode") for child in _executable_children(cs))


def _violations(cs: ET.Element) -> list[str]:
    """THE RULES. One implementation, shared by the corpus test and the fixture tests."""
    cid = cs.get("id", "?")
    out: list[str] = []
    if _is_continue(cs):
        opaque = sorted(
            {
                _local(e.tag)
                for child in _executable_children(cs)
                for e in child.iter()
                if _local(e.tag) in _OPAQUE
            }
        )
        out += [f"{cid}: onFail=CONTINUE changeset uses <{tag}>, which this lint cannot read into" for tag in opaque]
        text = _normalise(_executable_text(cs))
        fk_label = "table with a foreign key into nexus.chunks"
        # The exempt statement is removed for the foreign-key-table rule ONLY; every other rule reads the whole text.
        text_without_exempt = _EXEMPT_INDEX_BUILD.sub("create index concurrently <exempt>", text)
        out += [
            f"{cid}: onFail=CONTINUE body names {label}"
            for label, pat in _FORBIDDEN.items()
            if pat.search(text_without_exempt if label == fk_label else text)
        ]
    elif _can_stay_unrecorded(cs):
        # Unrecorded when it skips or fails, yet RetryingChangesets does not see it, so it would end every build.
        out.append(
            f"{cid}: can stay unrecorded (onError=CONTINUE or failOnError=false) without an onFail=CONTINUE "
            "precondition, so the engine counts it as schema work on every boot"
        )
    return out


def _changesets(changelog_dir: Path) -> list[tuple[str, ET.Element]]:
    out = []
    for path in sorted(changelog_dir.glob("*.xml")):
        # No `except ParseError: continue`: an unparseable changelog IS the finding.
        for cs in ET.parse(path).getroot().iter(f"{_NS}changeSet"):
            out.append((path.name, cs))
    return out


def _allowlist_problem(cont: list[tuple[str, str]]) -> str | None:
    """None when the CONTINUE census is exactly the allowlist; else what to do about the difference."""
    if sorted(cont) == sorted(_CONTINUE_ALLOWLIST):
        return None
    return (
        f"the onFail=CONTINUE changesets are {sorted(cont)}, the allowlist is {sorted(_CONTINUE_ALLOWLIST)}. "
        "SchemaMigrator does not count a CONTINUE changeset as schema work, so a restart keeps the index build "
        "through it. Read its body, then add it to _CONTINUE_ALLOWLIST with the lock argument (or remove the "
        "entry that is gone), and see RetryingChangesets"
    )


def _census(changelog_dir: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[str]]:
    """(continue changesets as (file, id), can-stay-unrecorded changesets as (file, id), violations)."""
    changesets = _changesets(changelog_dir)
    assert len(changesets) > 100, (
        f"only {len(changesets)} changesets parsed from {changelog_dir}: empty, misconfigured, "
        "or the dbchangelog namespace moved"
    )
    cont = [(f, cs.get("id", "?")) for f, cs in changesets if _is_continue(cs)]
    unrecorded = [(f, cs.get("id", "?")) for f, cs in changesets if _can_stay_unrecorded(cs)]
    found = [f"{f}:{v}" for f, cs in changesets for v in _violations(cs)]
    return cont, unrecorded, found


def test_no_continue_precondition_changeset_touches_chunks_or_the_builder(capsys) -> None:
    cont, unrecorded, found = _census(CHANGELOG_DIR)
    with capsys.disabled():
        sys.stderr.write(
            f"\ncontinue-precondition census: {len(cont)} changeset(s): {cont}"
            f"\ncan-stay-unrecorded census: {len(unrecorded)} changeset(s): {unrecorded}\n"
        )

    # THE CENSUS IS A GUARD: exactly the allowlist (also the non-vacuity check: aspects-005-2 must still be seen).
    assert _allowlist_problem(cont) is None, _allowlist_problem(cont)
    assert found == [], (
        "a changeset skipped with onFail=CONTINUE is not counted as schema work by SchemaMigrator, so a restart "
        "keeps the index builder through it; its body must not touch the chunk tables or the builder, and every "
        f"changeset that can stay unrecorded must say onFail=CONTINUE (nexus-f3yqd.3). Violations: {found}"
    )
    # Nothing can stay unrecorded without being a CONTINUE changeset (violations above would say so).
    assert sorted(unrecorded) == sorted(cont)


def _fk_tables_into_chunks(changelog_dir: Path) -> set[str]:
    """Owners of every ``REFERENCES [nexus.]chunks (`` in an executable body, by the nearest ALTER/CREATE TABLE before it."""
    fk = re.compile(r"references\s+(?:nexus\.)?chunks\s*\(")
    owner = re.compile(
        r"(?:alter\s+table(?:\s+only)?(?:\s+if\s+exists)?|create\s+table(?:\s+if\s+not\s+exists)?)\s+(?:nexus\.)?(\w+)"
    )
    tables: set[str] = set()
    hits = 0
    for _, cs in _changesets(changelog_dir):
        text = _normalise(_executable_text(cs))
        for m in fk.finditer(text):
            hits += 1
            owners = list(owner.finditer(text, 0, m.start()))
            assert owners, f"{cs.get('id')}: a REFERENCES nexus.chunks with no ALTER/CREATE TABLE before it"
            tables.add(owners[-1].group(1))
    assert hits >= 5, f"only {hits} REFERENCES nexus.chunks found: the scan stopped seeing them"
    return tables


def test_fk_tables_list_covers_the_changelog() -> None:
    """A table that gains a foreign key into nexus.chunks must be named in _FK_TABLES, or the name lint misses a DROP of it."""
    found = _fk_tables_into_chunks(CHANGELOG_DIR)
    assert found <= set(_FK_TABLES), f"tables with a foreign key into nexus.chunks not in _FK_TABLES: {sorted(found - set(_FK_TABLES))}"
    assert set(_FK_TABLES) <= found, f"_FK_TABLES names tables the changelog no longer points at chunks: {sorted(set(_FK_TABLES) - found)}"


def test_lint_detects_a_chunks_reference_under_a_continue_precondition(tmp_path: Path) -> None:
    """Falsification: copy the real changelog, append a CONTINUE changeset that names nexus.chunks, expect it found."""
    for p in CHANGELOG_DIR.glob("*.xml"):
        (tmp_path / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "zz-fixture.xml").write_text(
        f'<databaseChangeLog xmlns="{_NS[1:-1]}">'
        '<changeSet id="fixture-bad" author="t">'
        '<preConditions onFail="CONTINUE" onError="CONTINUE"><sqlCheck expectedResult="0">SELECT 0</sqlCheck>'
        "</preConditions>"
        "<sql>CREATE INDEX x ON nexus.chunks (id)</sql>"
        "</changeSet></databaseChangeLog>",
        encoding="utf-8",
    )
    cont, _, found = _census(tmp_path)
    assert ("zz-fixture.xml", "fixture-bad") in cont
    assert found == ["zz-fixture.xml:fixture-bad: onFail=CONTINUE body names nexus.chunks"]


def test_a_new_continue_changeset_fails_until_it_is_on_the_allowlist(tmp_path: Path) -> None:
    """A CONTINUE changeset with a harmless body has no violation, and still fails the census: a human adds it."""
    for p in CHANGELOG_DIR.glob("*.xml"):
        (tmp_path / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "zz-fixture.xml").write_text(
        f'<databaseChangeLog xmlns="{_NS[1:-1]}">'
        '<changeSet id="fixture-new" author="t">'
        '<preConditions onFail="CONTINUE"><sqlCheck expectedResult="0">SELECT 0</sqlCheck></preConditions>'
        "<sql>DROP TABLE IF EXISTS nexus.some_other_table</sql>"
        "</changeSet></databaseChangeLog>",
        encoding="utf-8",
    )
    cont, _, found = _census(tmp_path)
    assert found == []
    problem = _allowlist_problem(cont)
    assert problem is not None and "fixture-new" in problem
    assert _allowlist_problem([k for k in cont if k[1] != "fixture-new"]) is None


def _cs(xml: str) -> ET.Element:
    return ET.fromstring(xml.replace("<changeSet", f"<changeSet xmlns='{_NS[1:-1]}'", 1))


_PRE = '<preConditions onFail="CONTINUE"><sqlCheck expectedResult="0">SELECT 0</sqlCheck></preConditions>'


def _continue_cs(body: str, pre: str = _PRE) -> ET.Element:
    return _cs(f'<changeSet id="b" author="t">{pre}<sql>{body}</sql></changeSet>')


def test_each_forbidden_name_is_caught_and_only_under_continue() -> None:
    for name, body in {
        "chunk leaf (chunks_m)": "ALTER TABLE nexus.chunks_m1024_t1 SET (fillfactor=90)",
        "partition helper (partition_)": "SELECT nexus.partition_leaf_for('t')",
        "index builder (pci_)": "SELECT pci_build()",
        "table with a foreign key into nexus.chunks": "DROP TABLE nexus.catalog_document_chunks",
    }.items():
        assert any(name in v for v in _violations(_continue_cs(body))), name
    # No CONTINUE precondition: out of scope (an ordinary changeset is counted as schema work).
    plain = _cs('<changeSet id="p" author="t"><sql>CREATE INDEX x ON nexus.chunks (id)</sql></changeSet>')
    assert _violations(plain) == []
    # HALT / MARK_RAN preconditions: out of scope.
    halt = _cs(
        '<changeSet id="h" author="t"><preConditions onFail="HALT"><sqlCheck expectedResult="0">SELECT 0'
        "</sqlCheck></preConditions><sql>CREATE INDEX x ON nexus.chunks (id)</sql></changeSet>"
    )
    assert _violations(halt) == []
    # Prose and rollback are not executed at boot.
    prose = _cs(
        f'<changeSet id="c" author="t">{_PRE}<comment>mentions nexus.chunks and pci_</comment>'
        "<sql>SELECT 1</sql><rollback><sql>SELECT nexus.chunks_m1</sql></rollback></changeSet>"
    )
    assert _violations(prose) == []


def test_chunks_is_caught_however_it_is_written() -> None:
    """The shapes the first version missed: quoting, no schema, case, whitespace around the dot."""
    for body in (
        "CREATE INDEX x ON nexus.chunks (id)",
        'CREATE INDEX x ON "nexus"."chunks" (id)',
        'CREATE INDEX x ON nexus."chunks" (id)',
        'CREATE INDEX x ON "nexus".chunks (id)',
        "SET search_path = nexus; CREATE INDEX x ON chunks (id)",
        "CREATE INDEX x ON Nexus.ChUnks (id)",
        "CREATE INDEX x ON NEXUS.CHUNKS (id)",
        "CREATE INDEX x ON nexus . chunks (id)",
        'CREATE INDEX x ON "NEXUS"\n   .\n   "Chunks" (id)',
        "DROP TABLE nexus.catalog_document_chunks",
        'DROP TABLE "nexus"."catalog_document_chunks"',
        "ALTER TABLE nexus.topic_assignments DROP CONSTRAINT topic_assignments_chunk_fk",
        "DROP TABLE IF EXISTS chunk_orphaned_at",
        'SELECT "nexus"."partition_leaf_for"(\'t\')',
        "SELECT partition_leaf_for('t')",
        'ALTER TABLE "nexus"."chunks_m1024_t1" SET (fillfactor=90)',
        'SELECT "nexus"."pci_build"()',
    ):
        v = _violations(_continue_cs(body))
        assert v, f"not caught: {body!r}"
    # Words that merely contain the names are not names.
    for body in ("SELECT 'chunks_of_text'", "SELECT precip_total, mypci_x FROM nexus.aspect_extraction_queue"):
        assert _violations(_continue_cs(body)) == [], body
    # An attribute-carried name is read too (the serialised element, attributes included).
    attr = _cs(f'<changeSet id="a" author="t">{_PRE}<sql dbms="nexus.chunks">SELECT 1</sql></changeSet>')
    assert _violations(attr)


def test_a_continue_changeset_that_hides_its_body_fails() -> None:
    for el in (
        '<sqlFile path="x.sql"/>',
        '<customChange class="dev.Foo"/>',
        '<executeCommand executable="psql"/>',
    ):
        cs = _cs(f'<changeSet id="o" author="t">{_PRE}{el}</changeSet>')
        v = _violations(cs)
        assert any("cannot read into" in x for x in v), el
    # Out of scope without the CONTINUE precondition.
    plain = _cs('<changeSet id="o" author="t"><sqlFile path="x.sql"/></changeSet>')
    assert _violations(plain) == []


def test_a_changeset_that_can_stay_unrecorded_must_say_on_fail_continue() -> None:
    # onError=CONTINUE with onFail HALT (explicit), MARK_RAN, or absent (the default is HALT).
    for pre in (
        '<preConditions onFail="HALT" onError="CONTINUE"><sqlCheck expectedResult="0">SELECT 0</sqlCheck></preConditions>',
        '<preConditions onFail="MARK_RAN" onError="CONTINUE"><sqlCheck expectedResult="0">SELECT 0</sqlCheck></preConditions>',
        '<preConditions onError="CONTINUE"><sqlCheck expectedResult="0">SELECT 0</sqlCheck></preConditions>',
    ):
        v = _violations(_cs(f'<changeSet id="e" author="t">{pre}<sql>SELECT 1</sql></changeSet>'))
        assert len(v) == 1 and "can stay unrecorded" in v[0], pre
    # failOnError=false with no CONTINUE precondition (none, or HALT).
    for pre in ("", '<preConditions onFail="HALT"><sqlCheck expectedResult="0">SELECT 0</sqlCheck></preConditions>'):
        v = _violations(_cs(f'<changeSet id="f" author="t" failOnError="false">{pre}<sql>SELECT 1</sql></changeSet>'))
        assert len(v) == 1 and "can stay unrecorded" in v[0], repr(pre)
    # The shape aspects-005-2 has is the allowed one: failOnError=false WITH onFail=CONTINUE (its body is then linted).
    ok = _cs(
        '<changeSet id="g" author="t" failOnError="false"><preConditions onFail="CONTINUE" onError="CONTINUE">'
        '<sqlCheck expectedResult="0">SELECT 0</sqlCheck></preConditions><sql>SELECT 1</sql></changeSet>'
    )
    assert _violations(ok) == []
    # An ordinary changeset is none of these.
    assert _violations(_cs('<changeSet id="h" author="t"><sql>SELECT 1</sql></changeSet>')) == []


def test_a_concurrent_index_build_on_a_foreign_key_table_is_the_one_exempt_shape() -> None:
    """P3.3c's exemption: CREATE INDEX CONCURRENTLY on a table with a foreign key into chunks passes the foreign-key-table rule."""
    for body in (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_x ON nexus.chunk_orphaned_at (tenant_id, chash)",
        "create index concurrently idx_x on chunk_orphaned_at(tenant_id, chash)",
        'CREATE INDEX CONCURRENTLY idx_x ON "nexus"."topic_assignments" (tenant_id, doc_id)',
    ):
        assert _violations(_continue_cs(body)) == [], body


def test_the_exemption_is_only_that_statement_shape_on_a_foreign_key_table() -> None:
    """Falsification: the same table under any other statement, the same shape on chunks itself, and extra mentions all still fail."""
    fk = "table with a foreign key into nexus.chunks"
    for body in (
        "ALTER TABLE nexus.chunk_orphaned_at ADD COLUMN x int",
        "DROP TABLE nexus.catalog_document_chunks",
        "CREATE INDEX idx_x ON nexus.chunk_orphaned_at (tenant_id, chash)",   # not CONCURRENTLY: a SHARE lock on the table
        "CREATE INDEX CONCURRENTLY idx_x ON nexus.chunk_orphaned_at (tenant_id, chash); ALTER TABLE nexus.chunk_orphaned_at DROP COLUMN y",
        "CREATE INDEX CONCURRENTLY idx_x ON nexus.chunk_orphaned_at (tenant_id, chash) WHERE chash IN (SELECT chash FROM nexus.topic_assignments)",
    ):
        assert any(fk in v for v in _violations(_continue_cs(body))), body
    # on nexus.chunks itself the exemption does not apply: that is the chunks rule
    assert any("nexus.chunks" in v for v in _violations(_continue_cs("CREATE INDEX CONCURRENTLY idx_x ON nexus.chunks (id)")))
    assert any("chunk leaf" in v for v in _violations(_continue_cs("CREATE INDEX CONCURRENTLY idx_x ON nexus.chunks_m1024_t1 (id)")))
