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
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from tests.test_changelog_rls_lint import CHANGELOG_DIR

_NS = "{http://www.liquibase.org/xml/ns/dbchangelog}"

_FORBIDDEN = {
    "nexus.chunks": re.compile(r"nexus\.chunks", re.IGNORECASE),
    "chunk leaf (chunks_m)": re.compile(r"chunks_m", re.IGNORECASE),
    "partition helper (nexus.partition_)": re.compile(r"nexus\.partition_", re.IGNORECASE),
    "index builder (pci_)": re.compile(r"\bpci_", re.IGNORECASE),
}


def _is_continue(cs: ET.Element) -> bool:
    """True when any preConditions container of the changeset (nested ones included) has onFail="CONTINUE"."""
    return any(
        (pre.get("onFail") or "").strip().upper() == "CONTINUE" for pre in cs.iter(f"{_NS}preConditions")
    )


def _executable_text(cs: ET.Element) -> str:
    """Everything the changeset runs at boot: itertext() of every child but comment and rollback."""
    parts: list[str] = []
    for child in cs:
        if child.tag in (f"{_NS}comment", f"{_NS}rollback"):
            continue
        parts.append(ET.tostring(child, encoding="unicode"))
    return "\n".join(parts)


def _violations(cs: ET.Element) -> list[str]:
    """THE RULE. One implementation, shared by the corpus test and the fixture test."""
    if not _is_continue(cs):
        return []
    text = _executable_text(cs)
    return [
        f"{cs.get('id', '?')}: onFail=CONTINUE body names {label}"
        for label, pat in _FORBIDDEN.items()
        if pat.search(text)
    ]


def _changesets(changelog_dir: Path) -> list[tuple[str, ET.Element]]:
    out = []
    for path in sorted(changelog_dir.glob("*.xml")):
        # No `except ParseError: continue`: an unparseable changelog IS the finding.
        for cs in ET.parse(path).getroot().iter(f"{_NS}changeSet"):
            out.append((path.name, cs))
    return out


def _census(changelog_dir: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """(continue changesets as (file, id), violations)."""
    changesets = _changesets(changelog_dir)
    assert len(changesets) > 100, (
        f"only {len(changesets)} changesets parsed from {changelog_dir}: empty, misconfigured, "
        "or the dbchangelog namespace moved"
    )
    cont = [(f, cs.get("id", "?")) for f, cs in changesets if _is_continue(cs)]
    found = [f"{f}:{v}" for f, cs in changesets for v in _violations(cs)]
    return cont, found


def test_no_continue_precondition_changeset_touches_chunks_or_the_builder(capsys) -> None:
    cont, found = _census(CHANGELOG_DIR)

    # NON-VACUITY: aspects-005-2 is the one such changeset today; the migrator's exemption is written for it.
    assert ("aspects-005-retire-aspect-queue-table.xml", "aspects-005-2") in cont, (
        f"aspects-005-2 is no longer a CONTINUE changeset (found {cont}): the detector stopped seeing it, "
        "or the migrator's retrying exemption (RetryingChangesets.isSelfRetrying) lost its only case"
    )
    with capsys.disabled():
        sys.stderr.write(f"\ncontinue-precondition census: {len(cont)} changeset(s): {cont}\n")
    assert found == [], (
        "a changeset skipped with onFail=CONTINUE is not counted as schema work by SchemaMigrator, so a restart "
        "keeps the index builder through it; its body must not touch the chunk tables or the builder "
        f"(nexus-f3yqd.3). Violations: {found}"
    )


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
    cont, found = _census(tmp_path)
    assert ("zz-fixture.xml", "fixture-bad") in cont
    assert found == ["zz-fixture.xml:fixture-bad: onFail=CONTINUE body names nexus.chunks"]


def _cs(xml: str) -> ET.Element:
    return ET.fromstring(xml.replace("<changeSet", f"<changeSet xmlns='{_NS[1:-1]}'", 1))


def test_each_forbidden_name_is_caught_and_only_under_continue() -> None:
    pre = '<preConditions onFail="CONTINUE"><sqlCheck expectedResult="0">SELECT 0</sqlCheck></preConditions>'
    for name, body in {
        "chunk leaf (chunks_m)": "ALTER TABLE nexus.chunks_m1024_t1 SET (fillfactor=90)",
        "partition helper (nexus.partition_)": "SELECT nexus.partition_leaf_for('t')",
        "index builder (pci_)": "SELECT pci_build()",
    }.items():
        bad = _cs(f'<changeSet id="b" author="t">{pre}<sql>{body}</sql></changeSet>')
        assert any(name in v for v in _violations(bad)), name
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
        f'<changeSet id="c" author="t">{pre}<comment>mentions nexus.chunks and pci_</comment>'
        "<sql>SELECT 1</sql><rollback><sql>SELECT nexus.chunks_m1</sql></rollback></changeSet>"
    )
    assert _violations(prose) == []
