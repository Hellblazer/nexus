# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 254-P3-S4: ``nx catalog owners --census`` reports ``docs_outside_root`` and the owner move lines.

Beside ``tests/test_catalog_owners_census.py`` (which sits at the test-file size cap) and in the same style: the CLI
against a catalog handle, here a real ``HttpCatalogClient`` on the mock engine of ``tests/_move_hint_engine.py``, so the
one ``/list`` page a row costs, and what it carries on the wire, is on record.

The flag is information BESIDE the root class: it is not a census class, it is not an eligibility input, and a flagged
owner is never deactivate-eligible because of it. A flagged owner's row carries the owner move command with its own
root; each ``path_vanished`` row carries the command with a ``<new root>`` placeholder, beside the deactivate arm it
has today."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from nexus.catalog.engine_writes import hints
from nexus.cli import main
from nexus.commands.catalog_cmds import owners as owners_mod
from tests._move_hint_engine import HintEngine

_OLD = "/nonexistent-old-checkout"


@pytest.fixture
def healthy_root(tmp_path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "f.py").write_text("x = 1\n")
    return root


def _census(engine: HintEngine, *flags: str):
    with patch("nexus.commands.catalog._get_catalog", return_value=engine.client()):
        return CliRunner().invoke(main, ["catalog", "owners", "--census", *flags])


def _json(engine: HintEngine) -> dict:
    result = _census(engine, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _outside(engine: HintEngine, prefix: str, root: str) -> None:
    """Two documents whose source_uri name an old root (the same-name move after a re-registration re-rooted the owner)."""
    for i in range(2):
        engine.add_doc(prefix, f"src/f{i}.py", f"file://{_OLD}/src/f{i}.py")


def _inside(engine: HintEngine, prefix: str, root: str) -> None:
    for i in range(2):
        engine.add_doc(prefix, f"src/f{i}.py", f"file://{root}/src/f{i}.py")


def test_docs_outside_root_flag_reported_beside_class(healthy_root, tmp_path):
    engine = HintEngine()
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    (other / "g.py").write_text("y = 2\n")
    engine.add_owner("1.2", str(healthy_root), "h-1.2")
    _outside(engine, "1.2", str(healthy_root))
    engine.add_owner("1.3", str(other), "h-1.3")
    _inside(engine, "1.3", str(other))

    data = _json(engine)
    rows = {r["tumbler"]: r for r in data["healthy"]}
    assert rows["1.2"]["docs_outside_root"] is True and rows["1.3"]["docs_outside_root"] is False
    assert "docs_outside_root" not in data and data["path_vanished"] == [], "a flag on a row, never a class of its own"

    human = _census(engine)
    assert human.exit_code == 0, human.output
    assert "Documents outside their root (1):" in human.output
    assert "1.3" not in human.output.split("Documents outside their root")[1].split("dead owner")[0]


def test_flag_never_makes_owner_deactivate_eligible(healthy_root, tmp_path):
    assert owners_mod._CENSUS_CLASSES == ("healthy", "path_vanished", "path_exists_empty", "not_a_repository", "unreadable")
    assert owners_mod._ELIGIBLE_CLASSES == ("path_vanished", "not_a_repository")

    engine = HintEngine()
    engine.add_owner("1.2", str(healthy_root), "h-1.2")
    _outside(engine, "1.2", str(healthy_root))
    engine.add_owner("1.5", f"{_OLD}/gone", "h-1.5")  # vanished, no documents: eligible, as before
    engine.add_owner("1.6", f"{_OLD}/gone2", "h-1.6")  # vanished, documents outside its (gone) root: excluded as before
    _outside(engine, "1.6", f"{_OLD}/gone2")

    data = _json(engine)
    assert [r["tumbler"] for r in data["mutation_eligible"]] == ["1.5"]
    assert [r["tumbler"] for r in data["mutation_excluded"]] == ["1.6"]
    assert "1.2" not in {r["tumbler"] for r in data["mutation_eligible"] + data["mutation_excluded"]}
    assert data["dead_owner_count"] == 2


def test_flagged_owner_row_carries_owner_move_line(healthy_root):
    engine = HintEngine()
    engine.add_owner("1.2", str(healthy_root), "h-1.2")
    _outside(engine, "1.2", str(healthy_root))

    (row,) = _json(engine)["healthy"]
    expected = hints.owner_move_line("1.2", str(healthy_root))
    assert row["move_command"] == expected
    assert "--no-dry-run" not in expected and "--confirm" not in expected
    assert expected in _census(engine).output


def test_path_vanished_row_carries_move_line_with_new_root_placeholder(tmp_path):
    engine = HintEngine()
    engine.add_owner("1.5", f"{_OLD}/gone", "h-1.5")

    data = _json(engine)
    (row,) = data["path_vanished"]
    assert row["move_command"] == "nx catalog owners --execute move --owner 1.5 --new-root <new root>"

    human = _census(engine)
    assert row["move_command"] in human.output
    assert "To deactivate the 1 eligible owner(s):" in human.output, "the move line sits beside the deactivate arm"
    assert row["docs_outside_root"] is False


def test_no_move_command_on_an_engine_without_the_flag_but_the_flag_is_still_reported(healthy_root):
    engine = HintEngine(move_supported=None)
    engine.add_owner("1.2", str(healthy_root), "h-1.2")
    _outside(engine, "1.2", str(healthy_root))
    engine.add_owner("1.5", f"{_OLD}/gone", "h-1.5")

    data = _json(engine)
    assert data["healthy"][0]["docs_outside_root"] is True
    assert all("move_command" not in r for r in data["healthy"] + data["path_vanished"])
    assert "--execute move" not in _census(engine).output


def test_flag_reads_first_page_only(healthy_root):
    """One ``/list`` request per owner, with the first-page limit and no offset: a document outside the root that sits
    past the first page is not seen, and no second page is ever requested."""
    engine = HintEngine()
    engine.add_owner("1.2", str(healthy_root), "h-1.2")
    for i in range(hints.OWNER_FIRST_PAGE):
        engine.add_doc("1.2", f"src/f{i}.py", f"file://{healthy_root}/src/f{i}.py")
    engine.add_doc("1.2", "src/late.py", f"file://{_OLD}/src/late.py")  # document 201: past the page

    data = _json(engine)
    assert data["healthy"][0]["docs_outside_root"] is False, "the one outside document is not on the first page"
    pages = [q for m, p, q, _b in engine.requests if p == "/v1/catalog/list" and q.get("owner") == "1.2"]
    assert pages == [{"owner": "1.2", "limit": str(hints.OWNER_FIRST_PAGE)}], pages


def test_an_unreadable_page_leaves_the_flag_null_and_the_census_running(healthy_root, tmp_path):
    engine = HintEngine()
    engine.add_owner("1.2", str(healthy_root), "h-1.2")
    _outside(engine, "1.2", str(healthy_root))

    class _Broken(type(engine.client())):
        def owner_documents_page(self, owner, limit):
            raise RuntimeError("the page read failed")

    client = _Broken(base_url="http://mock.test", _token="t")
    client._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(engine.handle))
    with patch("nexus.commands.catalog._get_catalog", return_value=client):
        result = CliRunner().invoke(main, ["catalog", "owners", "--census", "--json"])
    assert result.exit_code == 0, result.output
    (row,) = json.loads(result.stdout)["healthy"]
    assert row["docs_outside_root"] is None and "move_command" not in row


def test_flagged_owner_whose_root_the_move_would_refuse_keeps_the_flag_and_gets_no_line(tmp_path):
    """``--execute move --owner`` accepts a new root only inside a git work tree, so a flagged owner whose root is
    not_a_repository or path_exists_empty is reported with the flag and no command the move would refuse."""
    not_repo = tmp_path / "plain"
    not_repo.mkdir()
    (not_repo / "f.txt").write_text("x")
    empty = tmp_path / "empty"
    empty.mkdir()
    engine = HintEngine()
    engine.add_owner("1.2", str(not_repo), "h-1.2")
    _outside(engine, "1.2", str(not_repo))
    engine.add_owner("1.3", str(empty), "h-1.3")
    _outside(engine, "1.3", str(empty))

    data = _json(engine)
    (plain,), (hollow,) = data["not_a_repository"], data["path_exists_empty"]
    assert plain["docs_outside_root"] is True and hollow["docs_outside_root"] is True
    assert "move_command" not in plain and "move_command" not in hollow
    human = _census(engine)
    assert "Documents outside their root (2):" in human.output
    assert "--execute move" not in human.output


def test_a_malformed_catalog_entry_does_not_fail_the_census(healthy_root):
    """A non-string source_uri on one document: the entry is skipped, the census exits 0, and the other documents of
    the owner still decide its flag."""
    engine = HintEngine()
    engine.add_owner("1.2", str(healthy_root), "h-1.2")
    _outside(engine, "1.2", str(healthy_root))
    engine.docs["1.2"][0]["source_uri"] = 12345

    result = _census(engine, "--json")
    assert result.exit_code == 0, result.output
    (row,) = json.loads(result.stdout)["healthy"]
    assert row["docs_outside_root"] is True and row["move_command"].startswith("nx catalog owners --execute move")

    engine.docs["1.2"][1]["source_uri"] = ["not", "a", "string"]
    result = _census(engine, "--json")
    assert result.exit_code == 0, result.output
    (row,) = json.loads(result.stdout)["healthy"]
    assert row["docs_outside_root"] is None and "move_command" not in row, "nothing on the page was readable: unknown"
