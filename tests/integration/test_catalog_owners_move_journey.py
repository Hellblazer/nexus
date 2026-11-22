# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 254-P2-S4: ``nx catalog owners --execute move`` end to end on the engine substrate (the gate-jar engine
carries the move routes and ``catalog_move_supported``).

Two journeys, one tenant each, because what they pin is what the ENGINE does with the command's requests: a document
move that absorbs the duplicate a re-index made, and an owner fold from a worktree path. The command's own behaviour
(arguments, refusals, spelling, report text, the write form) is tenant-free in tests/commands/test_catalog_owners_move.py
and tests/commands/test_owner_move_failures.py, on a mock engine. The documents are registered the way ``nx index pdf``
registers one (``nexus.doc_indexer._register_or_lookup_doc_id`` plus the file-hash stamp), so the GH #1571 state is the
real one: a file indexed, moved, and indexed again from its new path.

An integration-marked module is not counted by the python_unit tenant ratchet (tests/AGENTS.md: a new engine-backed
test goes straight to tests/integration/); CI's ``pytest (integration, affected)`` job runs it on the push that adds it."""
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
from tests._catalog_fixture_ops import register_seed_collections

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]

_COLLECTION = "knowledge__move-cmd__test-embed-768__v1"
_BYTES = b"%PDF-1.4\nsame bytes at every path\n%%EOF\n"
_SHA = hashlib.sha256(_BYTES).hexdigest()
_SEQ = [0]


@pytest.fixture(autouse=True)
def _env(monkeypatch, t2_service_env):
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


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


def _write_form(dry_output: str) -> list[str]:
    """The argv after ``nx catalog`` of the dry run's last line."""
    return shlex.split(dry_output.rstrip("\n").splitlines()[-1])[2:]


def test_a_document_move_absorbs_the_new_row_keeps_the_old_tumbler_and_link(tmp_path) -> None:
    """GH #1571: /a/x.pdf indexed (OLD), another document linked to it, the file moved to a path with a space under a
    symlinked parent and indexed again (NEW, a second live document), with a link onto the new row too. The dry run
    writes nothing and reports the counts the write then reports; after the write ONE live document holds the new path,
    it is the OLD tumbler, the link that pointed at the old tumbler is still there, the link that pointed at the new
    row now points at the old one, and a fresh registration of the file finds that row (created False)."""
    cat = HttpCatalogClient()
    try:
        _SEQ[0] += 1
        corpus = f"move-cmd-{_SEQ[0]}"
        old_path = _pdf(tmp_path / "a" / "x.pdf")
        old_id, _ = _register(old_path, corpus)
        other_id, _ = _register(_pdf(tmp_path / "other" / "o.pdf"), corpus)
        real = tmp_path / "real dir"
        real.mkdir()
        linked = tmp_path / "linked"
        linked.symlink_to(real, target_is_directory=True)
        typed = linked / "my x.pdf"
        shutil.move(str(old_path), str(real / "my x.pdf"))
        assert typed.is_file() and str(typed) != str(typed.resolve())
        new_id, created = _register(typed.resolve(), corpus)
        assert created is True and new_id != old_id
        cat.link(other_id, old_id, "cites", "test")
        cat.link(other_id, new_id, "relates", "test")
        resolved = typed.resolve()
        uri = "file://" + str(resolved)
        before = (cat.resolve(old_id).to_dict(), cat.resolve(new_id).to_dict())

        dry = _run("--execute", "move", "--document", old_id, "--new-path", str(typed))
        assert dry.exit_code == 0, dry.output
        assert "(dry run)" in dry.output and "no catalog writes performed" in dry.output
        assert (cat.resolve(old_id).to_dict(), cat.resolve(new_id).to_dict()) == before, "a dry run writes nothing"
        assert str(cat.by_source_uri(uri).tumbler) == new_id
        assert re.search(rf"absorbed documents \(1, would be trashed\):\n    {re.escape(new_id)}\n", dry.output), dry.output

        done = CliRunner().invoke(main, ["catalog", *_write_form(dry.output)])
        assert done.exit_code == 0, done.output
        assert "(write)" in done.output and "Done." in done.output
        assert _counts(done.output) == _counts(dry.output), "the dry run reports the counts the write reports"
        assert _counts(dry.output)["absorbed (trashed)"] == 1 and _counts(dry.output)["repointed"] == 1

        row = cat.resolve(old_id)
        assert row.file_path == str(resolved) and row.source_uri == uri
        assert " " in row.source_uri and "%20" not in row.source_uri, "raw file://<abspath>, never Path.as_uri()"
        assert str(cat.by_source_uri(uri).tumbler) == old_id
        assert getattr(cat.resolve(new_id, follow_alias=False), "source_uri", "") == "", "the absorbed row is cleared"
        targets = {(str(link.to_tumbler), link.link_type) for link in cat.links_from(other_id)}
        assert (old_id, "cites") in targets, "the link to the old tumbler is kept"
        assert (old_id, "relates") in targets and not any(t == new_id for t, _ in targets), "the new row's link was remapped"
        assert (row.meta or {}).get("content_hash") == _SHA
        assert _register(resolved, corpus) == (old_id, False), "a later index of the file registers nothing new"
    finally:
        cat.close()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _hash8(root: str) -> str:
    return hashlib.sha256(root.encode()).hexdigest()[:8]


def _owner_row(cat: HttpCatalogClient, prefix: str) -> dict:
    return next(o for o in cat.list_owners(include_deactivated=True) if o["tumbler_prefix"] == prefix)


def test_an_owner_move_from_a_worktree_folds_the_new_owner_into_the_old_one(tmp_path) -> None:
    """Owner A registered at /old with two documents; the repository has since moved to a directory that a second
    owner B (a fresh `nx index repo` of it) already holds, with the same two paths. The new root named is a linked
    WORKTREE of that repository. The dry run writes nothing and ends with the write form naming the MAIN repository;
    the write re-roots A there, folds B into it (B deactivated, its two rows absorbed), and A's documents carry
    URIs under the new root."""
    cat = HttpCatalogClient()
    try:
        main_repo = tmp_path / "new-home"
        main_repo.mkdir()
        _git(main_repo, "init", "--quiet")
        _git(main_repo, "commit", "--quiet", "--allow-empty", "--message", "x")
        new_root = _git(main_repo, "rev-parse", "--show-toplevel")
        worktree = tmp_path / "a-worktree"
        _git(main_repo, "worktree", "add", "--quiet", "-b", "wt", str(worktree))
        assert (worktree / ".git").is_file()
        old_root = str(tmp_path / "old-home")
        a = cat.register_owner("move-owner-a", "repo", repo_hash="0ldh0me0", repo_root=old_root)
        b = cat.register_owner("move-owner-b", "repo", repo_hash=_hash8(new_root), repo_root=new_root)
        docs = {}
        # RDR-229 P3.1: the engine refuses a document under an unregistered collection.
        register_seed_collections("code__movea__test-embed-768__v1", "code__moveb__test-embed-768__v1")
        for owner, tag in ((a, "a"), (b, "b")):
            for rel in ("src/one.py", "src/two.py"):
                docs[(tag, rel)] = str(cat.register(
                    owner, rel, content_type="code", file_path=rel, physical_collection=f"code__move{tag}__test-embed-768__v1"))
        before = (_owner_row(cat, str(a)), _owner_row(cat, str(b)))

        dry = _run("--execute", "move", "--owner", str(a), "--new-root", str(worktree))
        assert dry.exit_code == 0, dry.output
        assert (_owner_row(cat, str(a)), _owner_row(cat, str(b))) == before, "a dry run writes nothing"
        form = _write_form(dry.output)
        assert form[form.index("--new-root") + 1] == new_root != str(worktree)
        assert _counts(dry.output)["owners folded"] == 1 and _counts(dry.output)["absorbed (trashed)"] == 2
        absorbed = {docs[("b", "src/one.py")], docs[("b", "src/two.py")]}
        assert set(re.findall(r"^    (\d+\.\d+\.\d+)$", dry.output, re.M)) == absorbed

        done = CliRunner().invoke(main, ["catalog", *form])
        assert done.exit_code == 0, done.output
        assert _counts(done.output) == _counts(dry.output)
        row = _owner_row(cat, str(a))
        assert row["repo_root"] == new_root and row["repo_hash"] == _hash8(new_root)
        assert _owner_row(cat, str(b)).get("deactivated_at"), "the folded owner is deactivated"
        assert cat.resolve(docs[("a", "src/one.py")]).source_uri == "file://" + new_root + "/src/one.py"
    finally:
        cat.close()
