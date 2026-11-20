# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 254-P3-S3: the repository hints (Gaps 2 and 3) at the end of ``nx index repo``.

CLI through ``nx index repo`` (an engine-backed test, so it lives in tests/integration) with the indexer and the registry stubbed out (the run's own work is not under test) and
the catalog a mock engine (``tests/_move_hint_engine.py``): the hints read durable catalog state, so what the mock holds
is exactly what decides a line. Three shapes:

* Gap 3, documents outside their root: the owner is already at the repository's root and hash, but its documents'
  ``source_uri`` still name the old root. One line, on the re-rooting run and on every run after it.
* Gap 2, a renamed repository: the owner at the new root holds documents whose ``meta.content_hash`` also sits on live
  rows under an owner whose root is gone. The line names that owner and the new root.
* neither: nothing is printed.

Every line is the dry-run form. Nothing a hint asks writes: the mock knows only the read routes, and a test lists the
routes a run used."""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.catalog.engine_writes import hints
from nexus.cli import main
from nexus.repo_identity import _repo_identity_with_main
from tests._move_hint_engine import READ_ROUTES, HintEngine

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]  # index_repo_cmd reaches storage; the catalog is the mock

_OLD_ROOT = "/nonexistent-old-checkout/proj"


@pytest.fixture(autouse=True)
def _tenant(t2_service_env):
    """The run's own storage touches (the taxonomy marker path) need a tenant; the catalog the hints read is the mock."""


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _identity(repo: Path) -> tuple[str, str]:
    _name, repo_hash, main_repo = _repo_identity_with_main(repo)
    return repo_hash, str(main_repo)


def _run(engine: HintEngine, repo: Path):
    reg = MagicMock()
    reg.get.return_value = {"collection": "code__proj"}
    with patch("nexus.commands.index._registry", return_value=reg), \
            patch("nexus.indexer.index_repository", return_value={}), \
            patch("nexus.catalog.factory.make_catalog_reader", return_value=engine.client()):
        return CliRunner().invoke(main, ["index", "repo", str(repo)], env={"NX_NO_TELEMETRY": "1"})


def _hint_lines(result) -> list[str]:
    return [ln for ln in result.output.splitlines() if ln.startswith("nx catalog owners")]


def _new_owner(engine: HintEngine, repo: Path, prefix: str = "1.2", *, hashes: list[str | None] | None = None,
               stale_root: str | None = None) -> str:
    """The repository's own owner with one document per entry of *hashes* (None: no content hash). A document's
    ``source_uri`` is under the repository's root, or under *stale_root* when given (the Gap 3 shape)."""
    repo_hash, root = _identity(repo)
    engine.add_owner(prefix, root, repo_hash)
    for i, h in enumerate(hashes if hashes is not None else [None]):
        base = stale_root or root
        engine.add_doc(prefix, f"src/f{i}.py", f"file://{base}/src/f{i}.py", content_hash=h)
    return root


def test_same_name_move_prints_hint_on_rerooting_run_and_next_run(repo):
    engine = HintEngine()
    root = _new_owner(engine, repo, stale_root=_OLD_ROOT)
    expected = hints.owner_move_line("1.2", root)

    first = _run(engine, repo)
    assert first.exit_code == 0, first.output
    assert _hint_lines(first) == [expected]
    assert first.output.rstrip("\n").splitlines()[-1] == "Done."

    again = _run(engine, repo)  # durable catalog state: it repeats until the move is made
    assert again.exit_code == 0 and _hint_lines(again) == [expected]
    assert "--no-dry-run" not in again.output and "--confirm" not in again.output


def test_healthy_unmoved_repo_prints_none(repo):
    engine = HintEngine()
    _new_owner(engine, repo, hashes=["h1", "h2"])
    result = _run(engine, repo)
    assert result.exit_code == 0, result.output
    assert _hint_lines(result) == []
    assert engine.asked_by_meta() == [], "no vanished owner: nothing to compare against, no metadata query"


def _vanished(engine: HintEngine, prefix: str, hashes: list[str], root: str | None = None) -> None:
    engine.add_owner(prefix, root or f"/nonexistent-old/{prefix}", f"hash-{prefix}")
    for i, h in enumerate(hashes):
        engine.add_doc(prefix, f"src/f{i}.py", f"file:///nonexistent-old/{prefix}/src/f{i}.py", content_hash=h)


def test_renamed_repo_prints_owner_move_with_old_prefix_and_new_root(repo):
    engine = HintEngine()
    root = _new_owner(engine, repo, prefix="1.2", hashes=["h1", "h2", "h3", "h4"])
    _vanished(engine, "1.7", ["h1", "h2", "h3", "h4"])
    result = _run(engine, repo)
    assert result.exit_code == 0, result.output
    assert _hint_lines(result) == [hints.owner_move_line("1.7", root)]
    assert shlex.split(_hint_lines(result)[0])[-4:] == ["--owner", "1.7", "--new-root", root]


def test_two_vanished_owners_qualifying_print_highest_first(repo):
    """V1 holds all four hashes, V2 two (exactly half: it qualifies), V3 one (below half: it does not)."""
    engine = HintEngine()
    root = _new_owner(engine, repo, prefix="1.2", hashes=["h1", "h2", "h3", "h4"])
    _vanished(engine, "1.5", ["h1", "h2"])
    _vanished(engine, "1.6", ["h1"])
    _vanished(engine, "1.9", ["h1", "h2", "h3", "h4"])
    result = _run(engine, repo)
    assert result.exit_code == 0, result.output
    assert _hint_lines(result) == [hints.owner_move_line("1.9", root), hints.owner_move_line("1.5", root)]


def test_a_vanished_owner_that_is_deactivated_or_whose_root_exists_is_not_named(repo, tmp_path):
    engine = HintEngine()
    _new_owner(engine, repo, prefix="1.2", hashes=["h1", "h2"])
    _vanished(engine, "1.5", ["h1", "h2"], root=str(tmp_path))  # its root is still there: not vanished
    _vanished(engine, "1.6", ["h1", "h2"])
    engine.owners["1.6"]["deactivated_at"] = "2026-02-02T00:00:00Z"  # already handled
    result = _run(engine, repo)
    assert result.exit_code == 0, result.output
    assert _hint_lines(result) == []


def test_repo_without_content_hash_rows_prints_none_and_no_error(repo):
    engine = HintEngine()
    _new_owner(engine, repo, prefix="1.2", hashes=[None, None])
    _vanished(engine, "1.7", ["h1", "h2"])
    result = _run(engine, repo)
    assert result.exit_code == 0, result.output
    assert _hint_lines(result) == []
    assert engine.asked_by_meta() == [], "rows without a hash are not counted, and an owner with none asks nothing"


def test_hints_write_no_row(repo):
    """No route a hint used writes: every request of a run that prints BOTH hints is a read route, so the catalog
    rows before and after are the same ones."""
    engine = HintEngine()
    _new_owner(engine, repo, prefix="1.2", hashes=["h1", "h2"], stale_root=_OLD_ROOT)
    _vanished(engine, "1.7", ["h1", "h2"])
    before = {p: [dict(d) for d in rows] for p, rows in engine.docs.items()}
    owners_before = {p: dict(o) for p, o in engine.owners.items()}
    result = _run(engine, repo)
    assert result.exit_code == 0 and len(_hint_lines(result)) == 2, result.output
    assert set(engine.routes()) <= READ_ROUTES, set(engine.routes()) - READ_ROUTES
    assert engine.docs == before and engine.owners == owners_before
    assert not any(m != "GET" and p != "/v1/catalog/owners/by_type" for m, p in engine.routes())


def test_failing_metadata_query_leaves_exit_code_unchanged(repo):
    engine = HintEngine(by_meta_status=500)
    root = _new_owner(engine, repo, prefix="1.2", hashes=["h1", "h2"], stale_root=_OLD_ROOT)
    _vanished(engine, "1.7", ["h1", "h2"])
    result = _run(engine, repo)
    assert result.exit_code == 0, result.output
    assert "Done." in result.output
    assert _hint_lines(result) == [hints.owner_move_line("1.2", root)], "the outside-root line needs no metadata query"
    assert engine.asked_by_meta(), "non-vacuity: the failing query was made"


def test_old_engine_prints_none(repo):
    engine = HintEngine(move_supported=None)  # /version has no catalog_move_supported
    _new_owner(engine, repo, prefix="1.2", hashes=["h1", "h2"], stale_root=_OLD_ROOT)
    _vanished(engine, "1.7", ["h1", "h2"])
    result = _run(engine, repo)
    assert result.exit_code == 0, result.output
    assert _hint_lines(result) == []
    assert set(engine.routes()) == {("GET", "/version")}, "a client never prints a command its engine refuses"


def test_a_malformed_catalog_entry_does_not_change_the_exit_code(repo):
    """The hint computes over what the catalog returned; one entry of an unexpected shape (a non-string source_uri)
    must cost the hint, never the run. The well-formed entry still produces its line."""
    engine = HintEngine()
    root = _new_owner(engine, repo, prefix="1.2", hashes=["h1", "h2"], stale_root=_OLD_ROOT)
    engine.docs["1.2"][0]["source_uri"] = 12345
    result = _run(engine, repo)
    assert result.exit_code == 0, result.output
    assert "Done." in result.output
    assert _hint_lines(result) == [hints.owner_move_line("1.2", root)], "the entry that could be read still counts"


def test_a_hint_computation_that_raises_does_not_change_the_exit_code(repo, monkeypatch):
    """Whatever the hint code does with the data, the call site in the command is fail-open: nothing printed, the run
    ends as it would have."""
    engine = HintEngine()
    _new_owner(engine, repo, prefix="1.2", hashes=["h1", "h2"], stale_root=_OLD_ROOT)

    def explode(*args, **kwargs):
        raise TypeError("unexpected shape")

    monkeypatch.setattr("nexus.commands.index._repo_hints", explode)
    result = _run(engine, repo)
    assert result.exit_code == 0, result.output
    assert "Done." in result.output and _hint_lines(result) == []
