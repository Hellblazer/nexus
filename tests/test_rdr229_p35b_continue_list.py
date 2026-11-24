# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.5b (nexus-p2m1x.24): db/rdr229/p35b-continue-changesets.txt names exactly the CONTINUE-gated changesets.

P3.7's one-row rename waits until every P3.5b changeset is in databasechangelog, and the completion check asks for a restart
while one is missing (RDR item 6, "Readers during the stamp"). Both read the list, so a changeset added to vectors-078 and not
to the list would let the rename run on a reader still keyed on the name, and a listed id with no changeset would hold the
rename refused forever. The list must equal the file's changesets, each of which carries the CONTINUE gate, and must not be empty.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_ROOT = Path(__file__).resolve().parents[1] / "service/src/main/resources"
_FILES = (_ROOT / "db/changelog/vectors-078-rdr229-p35b-readers-continue.xml", _ROOT / "db/changelog/vectors-084-rdr229-ann-query-by-id.xml")
_LIST = _ROOT / "db/rdr229/p35b-continue-changesets.txt"
_GATE = re.compile(r'<preConditions onFail="CONTINUE" onError="CONTINUE">\s*<sqlCheck expectedResult="0">[^<]*rdr229-stamp-%')


def _listed() -> list[str]:
    return [ln.strip() for ln in _LIST.read_text().splitlines() if ln.strip() and not ln.startswith("#")]


def _changesets() -> dict[str, str]:
    """Every changeset of the files, 078 (the readers) and 084 (the by-id ANN overloads), by id."""
    sets: dict[str, str] = {}
    for f in _FILES:
        text = re.sub(r"<!--.*?-->", "", f.read_text(), flags=re.S)
        sets.update({m.group(1): m.group(0) for m in re.finditer(r'(?s)<changeSet id="([^"]+)".*?</changeSet>', text)})
    return sets


def test_the_list_names_exactly_the_files_changesets() -> None:
    listed = _listed()
    assert listed, f"{_LIST} is empty: P3.7 refuses an empty list, and P3.5b has CONTINUE changesets"
    assert len(listed) == len(set(listed)), f"duplicate ids in {_LIST}"
    assert set(listed) == set(_changesets()), (
        f"{_LIST.name} and {[f.name for f in _FILES]} differ: only in the list {sorted(set(listed) - set(_changesets()))}, "
        f"only in the file {sorted(set(_changesets()) - set(listed))}")


def test_every_listed_changeset_carries_the_stamp_gate() -> None:
    sets = _changesets()
    ungated = [cid for cid in _listed() if cid in sets and not _GATE.search(sets[cid])]
    assert not ungated, f"these listed changesets lack the CONTINUE gate on the rdr229-stamp-* moves: {ungated}"


_CHECKS = _ROOT / "db/rdr229/p31-checks.tsv"
_SQLCHECK = re.compile(r'<preConditions onFail="CONTINUE" onError="CONTINUE">\s*<sqlCheck expectedResult="0">(.*?)</sqlCheck>', re.S)


def _gates() -> dict[str, str]:
    return {cid: m.group(1) for cid, body in _changesets().items() if (m := _SQLCHECK.search(body))}


def _listed_checks() -> list[str]:
    """Every CHECK constraint of p31-checks.tsv (the HISTORY rows have none), in file order."""
    rows = [ln.split("\t") for ln in _CHECKS.read_text().splitlines() if ln and not ln.startswith("#")]
    return [r[3] for r in rows if r[0] != "table" and r[3] != "-"]


def test_the_gate_is_one_sqlcheck_repeated_verbatim_on_every_changeset() -> None:
    gates = _gates()
    assert set(gates) == set(_listed()), f"changesets without a gate: {sorted(set(_listed()) - set(gates))}"
    assert len(set(gates.values())) == 1, "the gate differs between changesets: it must be the same text on all fifteen"


def test_the_gate_names_every_listed_check_and_no_other() -> None:
    """The gate is CompletionCheck's rule for a stamp label: every CHECK of the list present and validated, so the list is its input."""
    gate = next(iter(_gates().values()))
    named = re.findall(r"\('([a-z_0-9]+)'\)", gate.split("AS e(conname)")[0])
    listed = _listed_checks()
    assert len(listed) == 20, f"the list changed ({len(listed)} CHECKs): re-read the gate and the 078 header"
    assert sorted(named) == sorted(listed), (
        f"the gate's CHECK list and p31-checks.tsv differ: only in the gate {sorted(set(named) - set(listed))}, "
        f"only in the list {sorted(set(listed) - set(named))}")
    assert "catalog_collections_superseded_by_id_check" in named
    assert "convalidated" in gate, "a CHECK that is present but NOT VALID must hold the gate"


def test_the_gate_reads_pg_catalog_and_the_move_registry_only() -> None:
    """The CONTINUE lint's rule (no chunk-family name in a precondition) and linda's: nothing but pg_catalog and nexus.data_moves."""
    gate = next(iter(_gates().values()))
    assert set(re.findall(r"\bnexus\.([a-z_]+)", gate)) == {"data_moves", "aspect_extraction_queue"}, (
        "only nexus.data_moves is read, and the retired queue is named only as a to_regclass argument")
    assert "FROM nexus.aspect_extraction_queue" not in gate and "JOIN nexus.aspect_extraction_queue" not in gate
    assert "rdr229-stamp-%" in gate
