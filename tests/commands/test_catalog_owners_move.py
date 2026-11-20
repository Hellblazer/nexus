# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 254-P2-S4: ``nx catalog owners --execute move``, the command and the client under it.

No engine and no tenant: the catalog handles the command asks for are real ``HttpCatalogClient`` objects on an
``httpx.MockTransport`` engine (``_Engine``), so the argument checks, the capability refusal, the path spelling, the
main-repository resolution, the report text and the write form are driven through the command AND the client's wire
methods, and each request the command sent is on record. What the engine DOES with a move (the absorb, the kept
tumbler and link, the owner fold, a dry run that writes nothing and reports the counts the write reports) is pinned
Java-side (CatalogDocumentMoveTest, CatalogOwnerMoveTest) and end to end against the substrate in
tests/integration/test_catalog_owners_move_journey.py. The failures of a route call are in
tests/commands/test_owner_move_failures.py."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from nexus.catalog.engine_writes import floors
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from nexus.commands import catalog as _cat_cmd

_REPORT = {
    "dry_run": True, "repointed": 1, "absorbed": 1, "trashed": 0, "skipped": 0, "unchanged": 0,
    "aspect_rows_rewritten": 2, "aspect_rows_deleted": 0, "highlight_rows_rewritten": 0, "highlight_rows_deleted": 0,
    "links_remapped": 1, "links_collapsed": 0, "links_dropped": 0, "absorbed_tumblers": ["1.2.9"],
}
_OWNER_REPORT = {**_REPORT, "absorbed": 2, "repointed": 2, "owners_folded": 1, "absorbed_tumblers": ["1.3.1", "1.3.2"]}


class _Engine:
    """A mock engine: ``GET /version`` answers ``version``, a move route answers the report for its ``dry_run`` (a
    document that is not ``1.2.3`` is a 409, as an engine answers a tumbler that names no document). Every request is
    in ``requests`` as ``(method, path, body)``, so a test can say a route was never reached."""

    def __init__(self, version=None, version_error: Exception | None = None) -> None:
        self.version = {"catalog_move_supported": True, "release_version": "0.1.199"} if version is None else version
        self.version_error = version_error
        self.requests: list[tuple[str, str, dict]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body))
        if request.url.path == "/version":
            if self.version_error is not None:
                raise self.version_error
            return httpx.Response(200, json=self.version)
        if request.url.path == "/v1/catalog/documents/move":
            if body["tumbler"] != "1.2.3":
                return httpx.Response(409, json={"error": f"no document {body['tumbler']}"})
            return httpx.Response(200, json={**_REPORT, "dry_run": body["dry_run"]})
        if request.url.path == "/v1/catalog/owners/move":
            return httpx.Response(200, json={**_OWNER_REPORT, "dry_run": body["dry_run"]})
        return httpx.Response(404, json={"error": request.url.path})

    def client(self) -> HttpCatalogClient:
        cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
        cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(self.handle))
        return cat

    @property
    def moves(self) -> list[tuple[str, dict]]:
        """The move-route posts, ``(path, body)``."""
        return [(p, b) for _m, p, b in self.requests if p.endswith("/move")]


@pytest.fixture
def engine(monkeypatch) -> _Engine:
    fake = _Engine()
    monkeypatch.setattr(_cat_cmd, "_get_catalog", fake.client)
    monkeypatch.setattr(_cat_cmd, "_get_catalog_writer", fake.client)
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")
    return fake


def _run(*args: str):
    return CliRunner().invoke(main, ["catalog", "owners", *args])


def _counts(output: str) -> dict[str, int]:
    """The ``  label: N`` rows of a report, by label."""
    return {m.group(1): int(m.group(2)) for m in re.finditer(r"^  ([a-z][a-z ()']+?): (\d+)$", output, re.M)}


@pytest.fixture
def pdf(tmp_path) -> Path:
    path = tmp_path / "b" / "x.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF-1.4\nsame bytes at every path\n%%EOF\n")
    return path


def _move(path: Path | str, *flags: str, document: str = "1.2.3"):
    return _run("--execute", "move", "--document", document, "--new-path", str(path), *flags)


# ── a document ──────────────────────────────────────────────────────────────


def test_document_move_dry_run_asks_for_a_rehearsal_and_its_counts_match_the_confirmed_run(engine, pdf) -> None:
    """The dry run sends dry_run=true (the engine rolls it back: the row-snapshot proof is in the journey), the write
    sends dry_run=false, and both print the counts the engine answered, which are the same rows."""
    dry = _move(pdf)
    assert dry.exit_code == 0, dry.output
    assert "(dry run)" in dry.output and "no catalog writes performed" in dry.output
    assert [b["dry_run"] for _p, b in engine.moves] == [True]

    confirmed = _move(pdf, "--no-dry-run", "--confirm")
    assert confirmed.exit_code == 0, confirmed.output
    assert "(write)" in confirmed.output and "Done." in confirmed.output
    assert [b["dry_run"] for _p, b in engine.moves] == [True, False]
    assert _counts(dry.output) == _counts(confirmed.output), "the dry run reports the counts the write reports"
    assert _counts(dry.output)["absorbed (trashed)"] == 1 and _counts(dry.output)["repointed"] == 1


def test_no_dry_run_without_confirm_is_report_only(engine, pdf) -> None:
    result = _move(pdf, "--no-dry-run")
    assert result.exit_code == 0 and "Add --confirm" in result.output and "(dry run)" in result.output
    assert [b["dry_run"] for _p, b in engine.moves] == [True], "no write was asked for"


def test_dry_run_report_ends_with_write_form(engine, tmp_path) -> None:
    """The last line is the exact command that writes, its paths shell-quoted; running it asks for the move."""
    spaced = tmp_path / "my docs" / "it's x.pdf"
    spaced.parent.mkdir(parents=True)
    spaced.write_bytes(b"%PDF-1.4\n")

    dry = _move(spaced)
    assert dry.exit_code == 0, dry.output
    last = dry.output.rstrip("\n").splitlines()[-1]
    assert last == ("nx catalog owners --execute move --document 1.2.3 "
                    f"--new-path {shlex.quote(str(spaced.resolve()))} --no-dry-run --confirm")
    argv = shlex.split(last)
    assert argv[:3] == ["nx", "catalog", "owners"] and "--no-dry-run" not in dry.output.replace(last, "")

    ran = CliRunner().invoke(main, ["catalog", *argv[2:]])
    assert ran.exit_code == 0, ran.output
    path, body = engine.moves[-1]
    assert body["dry_run"] is False and body["new_file_path"] == str(spaced.resolve()) and body["tumbler"] == "1.2.3"


def test_new_path_spelling_equals_curator_registration(engine, tmp_path) -> None:
    """A path with a space under a symlinked parent: what the command sends is what a fresh curator registration of
    that file computes (``_curator_identity``: the resolved path and the raw ``file://<abspath>``), so a later
    ``nx index pdf`` finds the row the move wrote (the journey registers it again and finds it)."""
    from nexus.doc_indexer import _curator_identity

    real = tmp_path / "real dir"
    real.mkdir()
    (real / "my x.pdf").write_bytes(b"%PDF-1.4\n")
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    typed = link / "my x.pdf"
    assert typed.is_file() and str(typed) != str(typed.resolve())

    assert _move(typed, "--no-dry-run", "--confirm").exit_code == 0
    _path, body = engine.moves[-1]
    file_path, _legacy, source_uri = _curator_identity(typed.resolve(), typed.resolve().parent)
    assert body["new_file_path"] == file_path == str(typed.resolve())
    assert body["new_source_uri"] == source_uri == "file://" + str(typed.resolve())
    assert " " in body["new_source_uri"] and "%20" not in body["new_source_uri"], "raw file://<abspath>, never Path.as_uri()"


def test_refuses_absent_new_path(engine, tmp_path) -> None:
    result = _move(tmp_path / "nowhere" / "x.pdf", "--no-dry-run", "--confirm")
    assert result.exit_code != 0 and "not an existing file" in result.output and "Nothing was written" in result.output
    assert engine.requests == [], "the path is read from disk first: not even the capability probe was sent"


def test_refuses_on_engine_without_flag(engine, pdf, monkeypatch) -> None:
    """The old-engine refusal against a flag-less /version: no move route reached, and the message is
    require_catalog_move's. The floor is pinned to None so filling it at 254-P5-S1 does not change the text."""
    monkeypatch.setattr(floors, "CATALOG_MOVE_MIN_ENGINE_VERSION", None)
    engine.version = {"release_version": "0.1.150"}
    result = _move(pdf, "--no-dry-run", "--confirm")
    assert result.exit_code != 0
    assert "does not support catalog moves" in result.output and "0.1.150" in result.output
    assert "not yet released" in result.output and "Nothing was written" in result.output
    assert engine.moves == [], "no move route may be called before the probe says yes"
    assert [p for _m, p, _b in engine.requests] == ["/version"]


def test_refuses_when_the_probe_cannot_confirm(engine, pdf) -> None:
    engine.version_error = httpx.ConnectError("engine unreachable")
    result = _move(pdf)
    assert result.exit_code != 0 and "could not confirm" in result.output
    assert engine.moves == []


def test_an_engine_refusal_is_reported_as_a_refusal(engine, pdf) -> None:
    """A tumbler that names no document is a 409 from the engine: the command says so and exits non-zero."""
    result = _move(pdf, document="999.9.9")
    assert result.exit_code != 0 and "The engine refused the move" in result.output
    assert "no document 999.9.9" in result.output and "Nothing was written" in result.output


def test_prints_absorbed_tumblers(engine, pdf) -> None:
    result = _move(pdf)
    assert result.exit_code == 0, result.output
    assert re.search(r"absorbed documents \(1, would be trashed\):\n    1\.2\.9\n", result.output), result.output


@pytest.mark.parametrize("args, message", [
    (["--execute", "move"], "exactly one of --document"),
    (["--execute", "move", "--document", "1.2.3"], "needs --new-path"),
    (["--execute", "move", "--owner", "1.2"], "needs --new-root"),
    (["--execute", "move", "--document", "1.2.3", "--owner", "1.2", "--new-path", "x"], "exactly one of --document"),
    (["--execute", "move", "--document", "1.2.3", "--new-path", "x", "--new-root", "y"], "no --new-root"),
    (["--execute", "move", "--owner", "1.2", "--new-root", "y", "--new-path", "x"], "no --new-path"),
    (["--execute", "move", "--census", "--document", "1.2.3", "--new-path", "x"], "does not take --census"),
    (["--execute", "reactivate", "--owner", "1.2", "--new-root", "y"], "only apply to --execute move"),
    (["--document", "1.2.3"], "only apply to --execute move"),
])
def test_a_malformed_invocation_is_a_usage_error_before_any_network_call(args, message, engine) -> None:
    result = _run(*args)
    assert result.exit_code != 0 and message in result.output, result.output
    assert engine.requests == []


# ── an owner ────────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "--quiet")
    _git(path, "commit", "--quiet", "--allow-empty", "--message", "x")
    return path


def _hash8(root: str) -> str:
    return hashlib.sha256(root.encode()).hexdigest()[:8]


@pytest.fixture
def new_repo(tmp_path) -> dict:
    path = _repo(tmp_path / "new-home")
    return {"path": path, "root": _git(path, "rev-parse", "--show-toplevel"), "tmp": tmp_path}


def test_owner_move_dry_run_then_write_ask_for_the_same_fold(engine, new_repo) -> None:
    flags = ("--owner", "1.5", "--new-root", str(new_repo["path"]))

    dry = _run("--execute", "move", *flags)
    assert dry.exit_code == 0, dry.output
    last = dry.output.rstrip("\n").splitlines()[-1]
    assert last == ("nx catalog owners --execute move --owner 1.5 "
                    f"--new-root {shlex.quote(new_repo['root'])} --no-dry-run --confirm")
    assert _counts(dry.output)["owners folded"] == 1 and _counts(dry.output)["absorbed (trashed)"] == 2
    assert [b["dry_run"] for _p, b in engine.moves] == [True]

    done = CliRunner().invoke(main, ["catalog", *shlex.split(last)[2:]])
    assert done.exit_code == 0, done.output
    assert _counts(done.output) == _counts(dry.output)
    (_, dry_body), (path, body) = engine.moves
    assert path == "/v1/catalog/owners/move" and body["dry_run"] is False
    assert {k: v for k, v in body.items() if k != "dry_run"} == {k: v for k, v in dry_body.items() if k != "dry_run"}
    assert body["owner"] == "1.5" and body["new_root"] == new_repo["root"] and body["new_repo_hash"] == _hash8(new_repo["root"])


def test_prints_every_absorbed_tumbler_of_an_owner_fold(engine, new_repo) -> None:
    result = _run("--execute", "move", "--owner", "1.5", "--new-root", str(new_repo["path"]))
    assert result.exit_code == 0, result.output
    listed = re.findall(r"^    (\d+\.\d+\.\d+)$", result.output, re.M)
    assert listed == ["1.3.1", "1.3.2"]
    assert "absorbed documents (2, would be trashed):" in result.output


def test_owner_move_from_worktree_sends_main_repo_root_and_hash(engine, new_repo) -> None:
    """The new root is a linked worktree of the repository: the engine is sent the MAIN repository's path and its
    hash, the pair `nx index repo` registers, never the worktree's."""
    worktree = new_repo["tmp"] / "a-worktree"
    _git(new_repo["path"], "worktree", "add", "--quiet", "-b", "wt", str(worktree))
    assert (worktree / ".git").is_file()

    result = _run("--execute", "move", "--owner", "1.5", "--new-root", str(worktree), "--no-dry-run", "--confirm")
    assert result.exit_code == 0, result.output
    _path, body = engine.moves[-1]
    assert body["new_root"] == new_repo["root"] != str(worktree)
    assert body["new_repo_hash"] == _hash8(new_repo["root"])
    assert f"hash {_hash8(new_repo['root'])}" in result.output
    heading = next(line for line in result.output.splitlines() if line.startswith("Move owner"))
    assert str(worktree) not in heading and new_repo["root"] in heading


def test_owner_refuses_non_git_root(engine, tmp_path) -> None:
    plain = tmp_path / "plain-dir"
    plain.mkdir()
    (plain / "f.txt").write_text("x")
    result = _run("--execute", "move", "--owner", "1.5", "--new-root", str(plain), "--no-dry-run", "--confirm")
    assert result.exit_code != 0 and "not inside a git work tree" in result.output and "Nothing was written" in result.output
    assert engine.requests == []


def test_owner_refuses_absent_new_root(engine, tmp_path) -> None:
    result = _run("--execute", "move", "--owner", "1.5", "--new-root", str(tmp_path / "gone"))
    assert result.exit_code != 0 and "not an existing directory" in result.output
    assert engine.requests == []


def test_owner_move_refuses_on_engine_without_flag(engine, new_repo, monkeypatch) -> None:
    monkeypatch.setattr(floors, "CATALOG_MOVE_MIN_ENGINE_VERSION", None)
    engine.version = {"release_version": "0.1.150"}
    result = _run("--execute", "move", "--owner", "1.5", "--new-root", str(new_repo["path"]), "--no-dry-run", "--confirm")
    assert result.exit_code != 0 and "does not support catalog moves" in result.output
    assert engine.moves == []
