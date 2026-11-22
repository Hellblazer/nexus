# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``catalog_collections_superseded_by_id_check`` changeset waits on the data D2's repair move could still violate.

WHY THIS EXISTS (RDR-229 P3.1, nexus-p2m1x.17, linda's ruling 2026-11-21, item (e)). The CHECK
``(superseded_by = '') = (superseded_by_id IS NULL)`` is added ``NOT VALID``, and a NOT VALID CHECK is enforced on every
UPDATE of a row. D2's repair move (``repair-catalog-collection-model-version``) UPDATEs ``catalog_collections`` rows that
carry a blank ``model_version``. On a skip-upgrade install (from before 0.1.200 straight to the Phase 3 release) a
tombstone (``superseded_by <> ''``) with a blank ``model_version`` and no ``superseded_by_id`` yet would make that UPDATE
fail with 23514, and the move would fail permanently.

So the changeset that adds the CHECK takes an RDR-246 ``onFail="CONTINUE"`` precondition on the DATA, not on D2's state
(M1: a precondition naming D2's label would skip the CHECK forever if D2 failed): it waits while any ``catalog_collections``
row has ``superseded_by <> ''`` AND ``superseded_by_id IS NULL`` AND ``model_version = ''``. Cleared by D2 completing or by
P3.1's own ``superseded_by`` stamp filling the id.

This lint is the guard that keeps the precondition there: ANY changeset in the changelog that adds the constraint, in any
file under any id, must carry it. It is structural (the shape of the precondition); the behaviour is
``Rdr229P31SupersededByCheckWaitTest`` against a real PostgreSQL.

Scanned: preconditions and body. Not scanned: ``<comment>`` (prose) and ``<rollback>`` (a rollback drops the constraint).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from tests.test_changelog_rls_lint import CHANGELOG_DIR

_NS = "{http://www.liquibase.org/xml/ns/dbchangelog}"

CONSTRAINT = "catalog_collections_superseded_by_id_check"

# What the sqlCheck must name: the table, the two columns of the CHECK, and the column D2's UPDATE is blind to.
_REQUIRED_IN_CHECK = ("catalog_collections", "superseded_by", "superseded_by_id", "model_version")

# A VALIDATE CONSTRAINT names the constraint without adding it (vectors-068-19 validates its own add, the stamp move's on_complete
# validates it again): the text is normalised to single spaces, so a fixed-width lookbehind drops it.
_ADDS = re.compile(r"(?:add\s+constraint\s+|(?<!validate )\bconstraint\s+)" + CONSTRAINT + r"\b")


def _normalise(text: str) -> str:
    """Lowercase, drop double quotes, collapse whitespace: ``"Nexus" . "Catalog_Collections"`` becomes ``nexus.catalog_collections``."""
    text = text.replace('"', "").lower()
    text = re.sub(r"\s*\.\s*", ".", text)
    return re.sub(r"\s+", " ", text)


def _executable_children(cs: ET.Element) -> list[ET.Element]:
    return [c for c in cs if c.tag not in (f"{_NS}comment", f"{_NS}rollback", f"{_NS}preConditions")]


def _body_text(cs: ET.Element) -> str:
    return _normalise("\n".join(ET.tostring(child, encoding="unicode") for child in _executable_children(cs)))


def adds_the_constraint(cs: ET.Element) -> bool:
    return _ADDS.search(_body_text(cs)) is not None


def precondition_problems(cs: ET.Element) -> list[str]:
    """Why the changeset's precondition is not the one the ruling requires; empty when it is."""
    cid = cs.get("id", "?")
    containers = cs.findall(f"{_NS}preConditions")
    continuing = [p for p in containers if (p.get("onFail") or "").strip().upper() == "CONTINUE"]
    if not continuing:
        return [f"{cid}: adds {CONSTRAINT} with no preConditions onFail=\"CONTINUE\" (the 068-2 data precondition, item e)"]
    checks = [c for p in continuing for c in p.iter(f"{_NS}sqlCheck")]
    if not checks:
        return [f"{cid}: the onFail=CONTINUE precondition has no sqlCheck on the data"]
    ok = False
    for check in checks:
        text = _normalise("".join(check.itertext()))
        if (check.get("expectedResult") or "").strip() == "0" and all(name in text for name in _REQUIRED_IN_CHECK):
            ok = True
    if not ok:
        return [
            f"{cid}: no sqlCheck with expectedResult=\"0\" names {', '.join(_REQUIRED_IN_CHECK)}: the precondition must "
            "count the tombstones (superseded_by <> '', superseded_by_id NULL) whose model_version is blank"
        ]
    return []


def _changesets(changelog_dir: Path) -> list[tuple[str, ET.Element]]:
    out = []
    for path in sorted(changelog_dir.glob("*.xml")):
        # No `except ParseError: continue`: an unparseable changelog IS the finding.
        for cs in ET.parse(path).getroot().iter(f"{_NS}changeSet"):
            out.append((path.name, cs))
    return out


def _census(changelog_dir: Path) -> tuple[int, list[tuple[str, str]], list[str]]:
    """(changesets examined, the (file, id) of each that adds the constraint, the problems found)."""
    changesets = _changesets(changelog_dir)
    adders = [(f, cs.get("id", "?")) for f, cs in changesets if adds_the_constraint(cs)]
    problems = [f"{f}:{p}" for f, cs in changesets if adds_the_constraint(cs) for p in precondition_problems(cs)]
    return len(changesets), adders, problems


def test_every_changeset_that_adds_the_constraint_carries_the_data_precondition() -> None:
    examined, adders, problems = _census(CHANGELOG_DIR)
    assert examined > 100, f"only {examined} changesets parsed from {CHANGELOG_DIR}: empty or the namespace moved"
    # Non-vacuity: the constraint is added somewhere, so the guard examined something.
    assert adders, f"no changeset adds {CONSTRAINT}: the scan stopped seeing it (examined {examined} changesets)"
    assert problems == [], problems


# ---- mutation cases: the same rules on fixtures, so a rule that goes silent is caught ----------------------------------


def _cs(xml: str) -> ET.Element:
    return ET.fromstring(xml.replace("<changeSet", f"<changeSet xmlns='{_NS[1:-1]}'", 1))


_GOOD_CHECK = (
    "SELECT count(*) FROM nexus.catalog_collections WHERE superseded_by &lt;&gt; '' "
    "AND (to_jsonb(catalog_collections)-&gt;&gt;'superseded_by_id') IS NULL AND model_version = ''"
)
_PRE = f'<preConditions onFail="CONTINUE" onError="CONTINUE"><sqlCheck expectedResult="0">{_GOOD_CHECK}</sqlCheck></preConditions>'
_ADD = (
    f"<sql>ALTER TABLE nexus.catalog_collections ADD CONSTRAINT {CONSTRAINT} "
    "CHECK ((superseded_by = '') = (superseded_by_id IS NULL)) NOT VALID</sql>"
)


def _adder(pre: str = _PRE, body: str = _ADD, extra: str = "") -> ET.Element:
    return _cs(f'<changeSet id="x" author="t">{pre}{extra}{body}</changeSet>')


def test_a_correct_changeset_passes() -> None:
    cs = _adder()
    assert adds_the_constraint(cs)
    assert precondition_problems(cs) == []


def test_mutation_no_precondition_fails() -> None:
    cs = _adder(pre="")
    assert adds_the_constraint(cs)
    assert any("no preConditions" in p for p in precondition_problems(cs))


def test_mutation_a_precondition_that_halts_fails() -> None:
    for fail in ("HALT", "MARK_RAN", "WARN"):
        pre = _PRE.replace('onFail="CONTINUE"', f'onFail="{fail}"')
        assert precondition_problems(_adder(pre=pre)), fail
    # onFail absent: the Liquibase default is HALT.
    pre = _PRE.replace('onFail="CONTINUE" ', "")
    assert precondition_problems(_adder(pre=pre))


def test_mutation_a_precondition_on_something_else_fails() -> None:
    # It waits on D2's state by label: M1 rejects that (a permanently failed D2 would skip the CHECK forever).
    by_label = (
        '<preConditions onFail="CONTINUE"><sqlCheck expectedResult="0">SELECT count(*) FROM nexus.data_moves '
        "WHERE label = 'repair-catalog-collection-model-version' AND completed_at IS NULL</sqlCheck></preConditions>"
    )
    assert precondition_problems(_adder(pre=by_label))
    # A trivially true check names none of the data.
    trivial = '<preConditions onFail="CONTINUE"><sqlCheck expectedResult="0">SELECT 0</sqlCheck></preConditions>'
    assert precondition_problems(_adder(pre=trivial))
    # Drops one named column (the model_version conjunct that scopes it to the rows D2's UPDATE could violate).
    no_model = _PRE.replace("AND model_version = ''", "")
    assert precondition_problems(_adder(pre=no_model))
    # A wrong expected result (the check counts offenders: zero means none).
    assert precondition_problems(_adder(pre=_PRE.replace('expectedResult="0"', 'expectedResult="1"')))
    # No sqlCheck at all.
    assert precondition_problems(_adder(pre='<preConditions onFail="CONTINUE"><tableExists tableName="x"/></preConditions>'))


def test_mutation_the_wording_of_the_add_does_not_hide_it() -> None:
    for body in (
        f'<sql>ALTER TABLE "nexus"."catalog_collections" ADD CONSTRAINT "{CONSTRAINT.upper()}" CHECK (true) NOT VALID</sql>',
        f"<sql>ALTER TABLE nexus.catalog_collections\n  ADD   CONSTRAINT {CONSTRAINT}\n CHECK (true) NOT VALID</sql>",
        f"<sql>CREATE TABLE nexus.t (superseded_by text, CONSTRAINT {CONSTRAINT} CHECK (true))</sql>",
    ):
        cs = _adder(pre="", body=body)
        assert adds_the_constraint(cs), body
        assert precondition_problems(cs), body


def test_a_comment_a_rollback_or_a_drop_is_not_an_add() -> None:
    prose = _adder(body="<sql>SELECT 1</sql>", extra=f"<comment>adds {CONSTRAINT} later</comment>")
    assert not adds_the_constraint(prose)
    rollback = _cs(
        '<changeSet id="r" author="t"><sql>SELECT 1</sql><rollback><sql>'
        f"ALTER TABLE nexus.catalog_collections DROP CONSTRAINT IF EXISTS {CONSTRAINT}</sql></rollback></changeSet>"
    )
    assert not adds_the_constraint(rollback)
    drop = _cs(
        f'<changeSet id="d" author="t"><sql>ALTER TABLE nexus.catalog_collections DROP CONSTRAINT IF EXISTS {CONSTRAINT}</sql></changeSet>'
    )
    assert not adds_the_constraint(drop)


def test_a_validate_of_the_constraint_is_not_an_add() -> None:
    """The stamp move's on_complete (vectors-070-1) and the DO block of vectors-068-19 name it after VALIDATE CONSTRAINT."""
    on_complete = _cs(
        '<changeSet id="v" author="t"><sql>INSERT INTO nexus.data_moves (label, on_complete) VALUES (\'x\', \'DO $v$ BEGIN '
        f"ALTER TABLE nexus.catalog_collections VALIDATE CONSTRAINT {CONSTRAINT}; END $v$')</sql></changeSet>"
    )
    assert not adds_the_constraint(on_complete)
    both = _adder(body=_ADD + f"<sql>ALTER TABLE nexus.catalog_collections VALIDATE   CONSTRAINT {CONSTRAINT}</sql>")
    assert adds_the_constraint(both), "the add is still found beside a validate"
    assert precondition_problems(both) == []


def test_a_new_file_that_adds_the_constraint_without_the_precondition_is_found(tmp_path: Path) -> None:
    """Falsification on the whole corpus: copy the real changelog, append a second adder with no precondition."""
    for p in CHANGELOG_DIR.glob("*.xml"):
        (tmp_path / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "zz-fixture.xml").write_text(
        f'<databaseChangeLog xmlns="{_NS[1:-1]}"><changeSet id="fixture-bad" author="t">{_ADD}</changeSet></databaseChangeLog>',
        encoding="utf-8",
    )
    _, adders, problems = _census(tmp_path)
    assert ("zz-fixture.xml", "fixture-bad") in adders
    assert [p for p in problems if p.startswith("zz-fixture.xml:fixture-bad:")], problems
