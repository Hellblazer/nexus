# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 254-P2-S4: ``nx catalog owners --execute move``.

Driven through ``CliRunner`` against the engine substrate (the gate-jar engine carries the move routes and
``catalog_move_supported``). The documents are registered the way ``nx index pdf`` registers one
(``nexus.doc_indexer._register_or_lookup_doc_id`` plus the file-hash stamp), so the GH #1571 state is the real one:
a file indexed, moved, and indexed again from its new path.
"""
from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main

pytestmark = pytest.mark.t2_tenant

_COLLECTION = "knowledge__move-cmd__test-embed-768__v1"
_BYTES = b"%PDF-1.4\nsame bytes at every path\n%%EOF\n"
_SHA = hashlib.sha256(_BYTES).hexdigest()
_SEQ = [0]


@pytest.fixture(autouse=True)
def _env(monkeypatch, t2_service_env):
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


def _corpus() -> str:
    _SEQ[0] += 1
    return f"move-cmd-{_SEQ[0]}"


def _pdf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_BYTES)
    return path


def _register(path: Path, corpus: str) -> tuple[str, bool]:
    """The pre-flight ``index_pdf`` runs, then the file-hash stamp the PDF writer adds: ``(doc_id, created)``."""
    from nexus.catalog.multi_batch_write import stamp_registered_content_hash
    from nexus.doc_indexer import _register_or_lookup_doc_id
    from tests._catalog_fixture_ops import ActiveCatalog

    doc_id, created = _register_or_lookup_doc_id(
        path, corpus, content_type="paper", physical_collection=_COLLECTION, with_created=True)
    assert doc_id and stamp_registered_content_hash(ActiveCatalog(), doc_id, _SHA) is True
    return doc_id, bool(created)


def _run(*args: str):
    return CliRunner().invoke(main, ["catalog", "owners", *args])


def _counts(output: str) -> dict[str, int]:
    """The ``  label: N`` rows of a report, by label."""
    return {m.group(1): int(m.group(2)) for m in re.finditer(r"^  ([a-z][a-z ()']+?): (\d+)$", output, re.M)}


@pytest.fixture
def moved_pdf(tmp_path):
    """GH #1571: /a/x.pdf indexed (OLD), another document linked to it, the file moved to /b/x.pdf and indexed
    again (NEW, a second live document), with a link onto the new row too."""
    cat = HttpCatalogClient()
    corpus = _corpus()
    old = _pdf(tmp_path / "a" / "x.pdf")
    old_id, _ = _register(old, corpus)
    other_id, _ = _register(_pdf(tmp_path / "other" / "o.pdf"), corpus)
    new = tmp_path / "b" / "x.pdf"
    new.parent.mkdir(parents=True)
    shutil.move(str(old), str(new))
    new_id, created = _register(new, corpus)
    assert created is True and new_id != old_id
    cat.link(other_id, old_id, "cites", "test")
    cat.link(other_id, new_id, "relates", "test")
    yield {"cat": cat, "old": old_id, "new": new_id, "other": other_id, "new_path": new, "corpus": corpus}
    cat.close()


@pytest.fixture
def move_routes_spy(monkeypatch):
    """Records every request the catalog client posts, so a test can say a move route was never reached."""
    paths: list[str] = []
    real = HttpCatalogClient._post

    def spy(self, path, body=None, **kw):
        paths.append(path)
        return real(self, path, body, **kw)

    monkeypatch.setattr(HttpCatalogClient, "_post", spy)
    return paths


def _move(old_id: str, new_path: Path | str, *flags: str):
    return _run("--execute", "move", "--document", old_id, "--new-path", str(new_path), *flags)


# ── a document ──────────────────────────────────────────────────────────────


def test_document_move_dry_run_writes_nothing_and_counts_match_confirmed_run(moved_pdf) -> None:
    cat, old, new, path = moved_pdf["cat"], moved_pdf["old"], moved_pdf["new"], moved_pdf["new_path"]
    before = (cat.resolve(old).to_dict(), cat.resolve(new).to_dict())

    dry = _move(old, path)
    assert dry.exit_code == 0, dry.output
    assert "(dry run)" in dry.output and "no catalog writes performed" in dry.output
    assert (cat.resolve(old).to_dict(), cat.resolve(new).to_dict()) == before, "a dry run writes nothing"
    assert str(cat.by_source_uri("file://" + str(path)).tumbler) == new

    confirmed = _move(old, path, "--no-dry-run", "--confirm")
    assert confirmed.exit_code == 0, confirmed.output
    assert "(write)" in confirmed.output and "Done." in confirmed.output
    assert _counts(dry.output) == _counts(confirmed.output), "the dry run reports the counts the write reports"
    assert _counts(dry.output)["absorbed (trashed)"] == 1 and _counts(dry.output)["repointed"] == 1


def test_no_dry_run_without_confirm_is_report_only(moved_pdf) -> None:
    cat, old, path = moved_pdf["cat"], moved_pdf["old"], moved_pdf["new_path"]
    before = cat.resolve(old).file_path
    result = _move(old, path, "--no-dry-run")
    assert result.exit_code == 0 and "Add --confirm" in result.output and "(dry run)" in result.output
    assert cat.resolve(old).file_path == before


def test_dry_run_report_ends_with_write_form(moved_pdf, tmp_path) -> None:
    """The last line is the exact command that writes, its paths shell-quoted; running it does the move."""
    cat, old = moved_pdf["cat"], moved_pdf["old"]
    spaced = tmp_path / "my docs" / "it's x.pdf"
    spaced.parent.mkdir(parents=True)
    shutil.move(str(moved_pdf["new_path"]), str(spaced))

    dry = _move(old, spaced)
    assert dry.exit_code == 0, dry.output
    last = dry.output.rstrip("\n").splitlines()[-1]
    assert last == (f"nx catalog owners --execute move --document {shlex.quote(old)} "
                    f"--new-path {shlex.quote(str(spaced))} --no-dry-run --confirm")
    argv = shlex.split(last)
    assert argv[:3] == ["nx", "catalog", "owners"] and "--no-dry-run" not in dry.output.replace(last, "")

    ran = CliRunner().invoke(main, ["catalog", *argv[2:]])
    assert ran.exit_code == 0, ran.output
    assert cat.resolve(old).file_path == str(spaced.resolve())


def test_document_move_absorbs_new_row_keeps_old_tumbler_and_link(moved_pdf) -> None:
    """GH #1571: after the move one live document holds the new path, it is the OLD tumbler, and the link that
    pointed at the old tumbler is still there; the link that pointed at the new row now points at the old one."""
    cat, old, new, other, path = (moved_pdf[k] for k in ("cat", "old", "new", "other", "new_path"))
    result = _move(old, path, "--no-dry-run", "--confirm")
    assert result.exit_code == 0, result.output

    row = cat.resolve(old)
    assert row.file_path == str(path.resolve()) and row.source_uri == "file://" + str(path.resolve())
    assert str(cat.by_source_uri("file://" + str(path.resolve())).tumbler) == old
    assert getattr(cat.resolve(new, follow_alias=False), "source_uri", "") == "", "the absorbed row is cleared"
    targets = {(str(link.to_tumbler), link.link_type) for link in cat.links_from(other)}
    assert (old, "cites") in targets, "the link to the old tumbler is kept"
    assert (old, "relates") in targets and not any(t == new for t, _ in targets), "the new row's link was remapped"
    assert (row.meta or {}).get("content_hash") == _SHA


def test_new_path_spelling_equals_curator_registration(moved_pdf, tmp_path) -> None:
    """A path with a space under a symlinked parent: the row the move writes is the row a fresh curator
    registration of that file finds (created False), so a later ``nx index pdf`` registers nothing new. The index
    commands resolve the path before they register (``path.resolve()``), so the registration is made from it."""
    cat, old, new = moved_pdf["cat"], moved_pdf["old"], moved_pdf["new"]
    real = tmp_path / "real dir"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    typed = link / "my x.pdf"
    shutil.move(str(moved_pdf["new_path"]), str(real / "my x.pdf"))
    assert typed.is_file() and str(typed) != str(typed.resolve())

    assert _move(old, typed, "--no-dry-run", "--confirm").exit_code == 0
    row = cat.resolve(old)
    assert row.file_path == str(typed.resolve()) and row.source_uri == "file://" + str(typed.resolve())
    assert " " in row.source_uri and "%20" not in row.source_uri, "raw file://<abspath>, never Path.as_uri()"

    again, created = _register(typed.resolve(), moved_pdf["corpus"])
    assert (again, created) == (old, False)
    assert new != again


def test_refuses_absent_new_path(moved_pdf, tmp_path, move_routes_spy) -> None:
    result = _move(moved_pdf["old"], tmp_path / "nowhere" / "x.pdf", "--no-dry-run", "--confirm")
    assert result.exit_code != 0 and "not an existing file" in result.output and "Nothing was written" in result.output
    assert not [p for p in move_routes_spy if p.endswith("/move")]
    assert moved_pdf["cat"].resolve(moved_pdf["old"]).file_path != str(tmp_path / "nowhere" / "x.pdf")


def test_refuses_on_engine_without_flag(moved_pdf, move_routes_spy, monkeypatch) -> None:
    """The old-engine refusal against a flag-less /version: nothing written, no move route reached, and the message
    is require_catalog_move's."""
    monkeypatch.setattr(HttpCatalogClient, "_version_flags", lambda self: {"release_version": "0.1.150"})
    before = moved_pdf["cat"].resolve(moved_pdf["old"]).file_path
    result = _move(moved_pdf["old"], moved_pdf["new_path"], "--no-dry-run", "--confirm")
    assert result.exit_code != 0
    assert "does not support catalog moves" in result.output and "0.1.150" in result.output
    assert "not yet released" in result.output and "Nothing was written" in result.output
    assert not [p for p in move_routes_spy if p.endswith("/move")], "no move route may be called before the probe says yes"
    assert moved_pdf["cat"].resolve(moved_pdf["old"]).file_path == before


def test_refuses_when_the_probe_cannot_confirm(moved_pdf, move_routes_spy, monkeypatch) -> None:
    def raising(self):
        raise OSError("engine unreachable")

    monkeypatch.setattr(HttpCatalogClient, "_version_flags", raising)
    result = _move(moved_pdf["old"], moved_pdf["new_path"])
    assert result.exit_code != 0 and "could not confirm" in result.output
    assert not [p for p in move_routes_spy if p.endswith("/move")]


def test_an_engine_refusal_is_reported_as_a_refusal(moved_pdf, tmp_path) -> None:
    """A tumbler that names no document is a 409 from the engine: the command says so and exits non-zero."""
    result = _move("999.9.9", moved_pdf["new_path"])
    assert result.exit_code != 0 and "The engine refused the move" in result.output
    assert "Nothing was written" in result.output


def test_prints_absorbed_tumblers(moved_pdf) -> None:
    result = _move(moved_pdf["old"], moved_pdf["new_path"])
    assert result.exit_code == 0, result.output
    assert re.search(rf"absorbed documents \(1, now trashed\):\n    {re.escape(moved_pdf['new'])}\n", result.output), result.output


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
def test_a_malformed_invocation_is_a_usage_error_before_any_network_call(args, message, move_routes_spy) -> None:
    result = _run(*args)
    assert result.exit_code != 0 and message in result.output, result.output
    assert move_routes_spy == []


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
def renamed_repo(tmp_path):
    """Owner A registered at /old with two documents; the repository has since moved to a new directory that a
    second owner B (a fresh `nx index repo` of the new directory) already holds, with the same two paths."""
    cat = HttpCatalogClient()
    new = _repo(tmp_path / "new-home")
    new_root = _git(new, "rev-parse", "--show-toplevel")
    new_hash = _hash8(new_root)
    old_root = str(tmp_path / "old-home")
    a = cat.register_owner("move-owner-a", "repo", repo_hash="0ldh0me0", repo_root=old_root)
    b = cat.register_owner("move-owner-b", "repo", repo_hash=new_hash, repo_root=new_root)
    docs = {}
    for owner, tag in ((a, "a"), (b, "b")):
        for rel in ("src/one.py", "src/two.py"):
            docs[(tag, rel)] = str(cat.register(
                owner, rel, content_type="code", file_path=rel, physical_collection=f"code__move{tag}__test-embed-768__v1"))
    yield {"cat": cat, "a": str(a), "b": str(b), "new": new, "new_root": new_root, "new_hash": new_hash,
           "old_root": old_root, "docs": docs, "tmp": tmp_path}
    cat.close()


def _owner_row(cat: HttpCatalogClient, prefix: str) -> dict:
    return next(o for o in cat.list_owners(include_deactivated=True) if o["tumbler_prefix"] == prefix)


def test_owner_move_dry_run_then_write_fold_the_new_owner_into_the_old_one(renamed_repo) -> None:
    cat, a, b, docs = renamed_repo["cat"], renamed_repo["a"], renamed_repo["b"], renamed_repo["docs"]
    flags = ("--owner", a, "--new-root", str(renamed_repo["new"]))
    before = (_owner_row(cat, a), _owner_row(cat, b))

    dry = _run("--execute", "move", *flags)
    assert dry.exit_code == 0, dry.output
    assert (_owner_row(cat, a), _owner_row(cat, b)) == before, "a dry run writes nothing"
    last = dry.output.rstrip("\n").splitlines()[-1]
    assert last == (f"nx catalog owners --execute move --owner {shlex.quote(a)} "
                    f"--new-root {shlex.quote(str(renamed_repo['new']))} --no-dry-run --confirm")
    assert _counts(dry.output)["owners folded"] == 1 and _counts(dry.output)["absorbed (trashed)"] == 2

    done = CliRunner().invoke(main, ["catalog", *shlex.split(last)[2:]])
    assert done.exit_code == 0, done.output
    assert _counts(done.output) == _counts(dry.output)
    row = _owner_row(cat, a)
    assert row["repo_root"] == renamed_repo["new_root"] and row["repo_hash"] == renamed_repo["new_hash"]
    assert _owner_row(cat, b).get("deactivated_at"), "the folded owner is deactivated"
    one = cat.resolve(docs[("a", "src/one.py")])
    assert one.source_uri == "file://" + renamed_repo["new_root"] + "/src/one.py"


def test_prints_every_absorbed_tumbler_of_an_owner_fold(renamed_repo) -> None:
    result = _run("--execute", "move", "--owner", renamed_repo["a"], "--new-root", str(renamed_repo["new"]))
    assert result.exit_code == 0, result.output
    absorbed = {renamed_repo["docs"][("b", "src/one.py")], renamed_repo["docs"][("b", "src/two.py")]}
    listed = re.findall(r"^    (\d+\.\d+\.\d+)$", result.output, re.M)
    assert set(listed) == absorbed and len(listed) == 2
    assert "absorbed documents (2, now trashed):" in result.output


def test_owner_move_from_worktree_sends_main_repo_root_and_hash(renamed_repo) -> None:
    """The new root is a linked worktree of the repository: the engine is sent the MAIN repository's path and its
    hash, the pair `nx index repo` registers, never the worktree's."""
    tmp = renamed_repo["tmp"]
    worktree = tmp / "a-worktree"
    _git(renamed_repo["new"], "worktree", "add", "--quiet", "-b", "wt", str(worktree))
    assert (worktree / ".git").is_file()

    result = _run("--execute", "move", "--owner", renamed_repo["a"], "--new-root", str(worktree), "--no-dry-run", "--confirm")
    assert result.exit_code == 0, result.output
    row = _owner_row(renamed_repo["cat"], renamed_repo["a"])
    assert row["repo_root"] == renamed_repo["new_root"] != str(worktree)
    assert row["repo_hash"] == _hash8(renamed_repo["new_root"])
    assert f"hash {_hash8(renamed_repo['new_root'])}" in result.output


def test_owner_refuses_non_git_root(renamed_repo, move_routes_spy) -> None:
    plain = renamed_repo["tmp"] / "plain-dir"
    plain.mkdir()
    (plain / "f.txt").write_text("x")
    result = _run("--execute", "move", "--owner", renamed_repo["a"], "--new-root", str(plain), "--no-dry-run", "--confirm")
    assert result.exit_code != 0 and "not inside a git work tree" in result.output and "Nothing was written" in result.output
    assert not [p for p in move_routes_spy if p.endswith("/move")]
    assert _owner_row(renamed_repo["cat"], renamed_repo["a"])["repo_root"] == renamed_repo["old_root"]


def test_owner_refuses_absent_new_root(renamed_repo, move_routes_spy) -> None:
    result = _run("--execute", "move", "--owner", renamed_repo["a"], "--new-root", str(renamed_repo["tmp"] / "gone"))
    assert result.exit_code != 0 and "not an existing directory" in result.output
    assert not [p for p in move_routes_spy if p.endswith("/move")]


def test_owner_move_refuses_on_engine_without_flag(renamed_repo, move_routes_spy, monkeypatch) -> None:
    monkeypatch.setattr(HttpCatalogClient, "_version_flags", lambda self: {"release_version": "0.1.150"})
    result = _run("--execute", "move", "--owner", renamed_repo["a"], "--new-root", str(renamed_repo["new"]),
                  "--no-dry-run", "--confirm")
    assert result.exit_code != 0 and "does not support catalog moves" in result.output
    assert not [p for p in move_routes_spy if p.endswith("/move")]
    assert _owner_row(renamed_repo["cat"], renamed_repo["a"])["repo_root"] == renamed_repo["old_root"]
