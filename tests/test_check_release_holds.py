# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/check_release_holds.py (nexus-3wh8d.28): an engine tag whose tree carries a held changeset is refused."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_release_holds.py"

_spec = importlib.util.spec_from_file_location("check_release_holds", SCRIPT)
assert _spec and _spec.loader
crh = importlib.util.module_from_spec(_spec)
sys.modules["check_release_holds"] = crh
_spec.loader.exec_module(crh)


def _tree(tmp_path: Path, holds: str, changelogs: dict[str, str]) -> Path:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "release-holds.txt").write_text(holds)
    d = tmp_path / "service" / "src" / "main" / "resources" / "db" / "changelog"
    d.mkdir(parents=True)
    for name, body in changelogs.items():
        (d / name).write_text(body)
    return tmp_path


_CL = '<databaseChangeLog>\n  <changeSet author="x" id="{id}">\n  </changeSet>\n</databaseChangeLog>\n'


def test_a_held_changeset_in_the_tree_is_reported(tmp_path: Path) -> None:
    root = _tree(tmp_path, "# comment\nvectors-030-1 nexus-3wh8d.28 one-way walk\n",
                 {"vectors-030.xml": _CL.format(id="vectors-030-1"), "a.xml": _CL.format(id="other-1")})
    held = crh.check(root)
    assert [(h.changeset, h.bead) for h in held] == [("vectors-030-1", "nexus-3wh8d.28")]
    assert held[0].reason == "one-way walk"
    assert held[0].file.endswith("vectors-030.xml")


def test_no_holds_or_an_empty_file_passes(tmp_path: Path) -> None:
    root = _tree(tmp_path, "# nothing held\n\n", {"a.xml": _CL.format(id="vectors-030-1")})
    assert crh.check(root) == []


def test_a_hold_naming_no_changeset_is_an_error_not_a_pass(tmp_path: Path) -> None:
    # A typo in the holds file must not silently release the changeset it meant to hold.
    root = _tree(tmp_path, "vectors-03O-1 nexus-3wh8d.28 typo\n", {"a.xml": _CL.format(id="vectors-030-1")})
    with pytest.raises(crh.HoldsError, match="vectors-03O-1"):
        crh.check(root)


def test_a_malformed_line_is_an_error(tmp_path: Path) -> None:
    root = _tree(tmp_path, "vectors-030-1\n", {"a.xml": _CL.format(id="vectors-030-1")})
    with pytest.raises(crh.HoldsError, match="line 1"):
        crh.check(root)


def test_a_missing_holds_file_is_an_error(tmp_path: Path) -> None:
    root = _tree(tmp_path, "", {})
    (root / "scripts" / "release-holds.txt").unlink()
    with pytest.raises(crh.HoldsError, match="release-holds.txt"):
        crh.check(root)


def test_the_id_is_matched_whatever_the_attribute_order(tmp_path: Path) -> None:
    root = _tree(tmp_path, "c-1 b-1 r\n", {"a.xml": '<changeSet id="c-1" author="a" runAlways="true">'})
    assert [h.changeset for h in crh.check(root)] == ["c-1"]


def test_cli_exits_1_naming_the_hold_and_0_when_clear(tmp_path: Path) -> None:
    (tmp_path / "h").mkdir()
    held = _tree(tmp_path / "h", "c-1 nexus-x.1 why\n", {"a.xml": _CL.format(id="c-1")})
    r = subprocess.run([sys.executable, str(SCRIPT), "--root", str(held)], capture_output=True, text=True)
    assert r.returncode == 1
    assert "RELEASE_HOLD c-1" in r.stdout and "nexus-x.1" in r.stdout
    (tmp_path / "c").mkdir()
    clear = _tree(tmp_path / "c", "", {"a.xml": _CL.format(id="c-1")})
    r = subprocess.run([sys.executable, str(SCRIPT), "--root", str(clear)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_this_repo_holds_vectors_030_1_until_rdr_225_is_released() -> None:
    # The hold this script exists for. Deleting its line in scripts/release-holds.txt is the release decision
    # (nexus-3wh8d.28); this test is deleted in the same commit.
    assert [h.changeset for h in crh.check(REPO)] == ["vectors-030-1"]
