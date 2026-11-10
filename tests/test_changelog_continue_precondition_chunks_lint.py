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
}

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
        out += [
            f"{cid}: onFail=CONTINUE body names {label}" for label, pat in _FORBIDDEN.items() if pat.search(text)
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
