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
_FILE = _ROOT / "db/changelog/vectors-078-rdr229-p35b-readers-continue.xml"
_LIST = _ROOT / "db/rdr229/p35b-continue-changesets.txt"
_GATE = re.compile(r'<preConditions onFail="CONTINUE" onError="CONTINUE">\s*<sqlCheck expectedResult="0">[^<]*rdr229-stamp-%')


def _listed() -> list[str]:
    return [ln.strip() for ln in _LIST.read_text().splitlines() if ln.strip() and not ln.startswith("#")]


def _changesets() -> dict[str, str]:
    text = re.sub(r"<!--.*?-->", "", _FILE.read_text(), flags=re.S)
    return {m.group(1): m.group(0) for m in re.finditer(r'(?s)<changeSet id="([^"]+)".*?</changeSet>', text)}


def test_the_list_names_exactly_the_files_changesets() -> None:
    listed = _listed()
    assert listed, f"{_LIST} is empty: P3.7 refuses an empty list, and P3.5b has CONTINUE changesets"
    assert len(listed) == len(set(listed)), f"duplicate ids in {_LIST}"
    assert set(listed) == set(_changesets()), (
        f"{_LIST.name} and {_FILE.name} differ: only in the list {sorted(set(listed) - set(_changesets()))}, "
        f"only in the file {sorted(set(_changesets()) - set(listed))}")


def test_every_listed_changeset_carries_the_stamp_gate() -> None:
    sets = _changesets()
    ungated = [cid for cid in _listed() if cid in sets and not _GATE.search(sets[cid])]
    assert not ungated, f"these listed changesets lack the CONTINUE gate on the rdr229-stamp-* moves: {ungated}"
