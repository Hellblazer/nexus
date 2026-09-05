# SPDX-License-Identifier: AGPL-3.0-or-later
"""A repo file indexed by the doc_indexer family registers under its REPO owner (nexus-3o4lt).

THE DEFECT. ``nx index rdr`` / ``nx index md`` resolved a CURATOR owner by
corpus name and stored the path they were handed. A repo file indexed before
``nx index repo`` had walked it therefore got a curator row with an ABSOLUTE
path, typed prose; measured on the work box 2026-09-04: 231 such rows for
one repo, 198 of them RDRs, none visible to any owner-scoped reader, every
one re-registered ("199 new") by each later ``nx index repo`` run.
nexus-tqudo only converges on a repo-owner row that already exists; this
fix decides the identity before any lookup.

These tests run ``git init`` for real: the repo test is whether a genuine
git checkout is recognised, and a pinned identity would prove the pin.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus.errors import EphemeralPathRefusedError
from tests._catalog_fixture_ops import ActiveCatalog

_COLLECTION = "rdr__1-1__test-embed-768__v1"
_SEQ = [0]


def _next() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / f"repo-{_next()}"
    (root / "docs" / "rdr").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", root], check=True)
    return root


def _owner_of(cat, tumbler: str) -> dict:
    prefix = tumbler.rsplit(".", 1)[0]
    owners = {o["tumbler_prefix"]: o for o in cat.list_owners()}
    return owners[prefix]


def test_an_rdr_in_a_git_repo_registers_under_the_repo_owner_relative_and_typed_rdr(tmp_path):
    """THE BEAD: no owner row exists yet; the result is a repo owner, a
    repo-relative path and content_type rdr, never a curator row."""
    from nexus.doc_indexer import _register_or_lookup_doc_id
    from nexus.repo_identity import _repo_identity_with_main

    root = _git_repo(tmp_path)
    f = root / "docs" / "rdr" / "rdr-901-example.md"
    f.write_text("# RDR-901\n\nbody\n")
    cat = ActiveCatalog()

    doc_id = _register_or_lookup_doc_id(
        f, f"curator-{_next()}", content_type="prose", physical_collection=_COLLECTION,
    )
    assert doc_id
    doc = cat.resolve(doc_id)
    assert doc.file_path == "docs/rdr/rdr-901-example.md", doc.file_path
    assert doc.content_type == "rdr"
    owner = _owner_of(cat, doc_id)
    assert owner["owner_type"] == "repo", owner
    _name, repo_hash, _main = _repo_identity_with_main(root)
    assert str(cat.owner_for_repo(repo_hash)) == owner["tumbler_prefix"], (
        "the owner must be the one nx index repo would find by repo_hash"
    )


def test_a_later_repo_index_finds_the_same_row(tmp_path):
    """The point of using the repo identity: nx index repo's own lookup
    (owner by repo_hash, then by_file_path with the relative path) lands on
    the row the single-file command made. One file, one Document."""
    from nexus.doc_indexer import _register_or_lookup_doc_id
    from nexus.repo_identity import _repo_identity_with_main

    root = _git_repo(tmp_path)
    f = root / "docs" / "note.md"
    f.write_text("# note\n")
    cat = ActiveCatalog()
    doc_id = _register_or_lookup_doc_id(
        f, f"curator-{_next()}", content_type="prose", physical_collection=_COLLECTION,
    )
    _name, repo_hash, _main = _repo_identity_with_main(root)
    owner = cat.owner_for_repo(repo_hash)
    found = cat.by_file_path(owner, "docs/note.md")
    assert found is not None and str(found.tumbler) == doc_id
    again = _register_or_lookup_doc_id(
        f, f"curator-{_next()}", content_type="prose", physical_collection=_COLLECTION,
    )
    assert again == doc_id, "a second single-file run must not mint"
    assert [d for d in cat.all_documents() if d.file_path == "docs/note.md" and str(d.tumbler).startswith(str(owner) + ".")] .__len__() == 1


def test_a_non_rdr_markdown_keeps_the_callers_type(tmp_path):
    from nexus.doc_indexer import _register_or_lookup_doc_id

    root = _git_repo(tmp_path)
    f = root / "docs" / "guide.md"
    f.write_text("# guide\n")
    doc = ActiveCatalog().resolve(
        _register_or_lookup_doc_id(f, f"curator-{_next()}", content_type="prose", physical_collection=_COLLECTION)
    )
    assert doc.content_type == "prose" and doc.file_path == "docs/guide.md"


def test_a_file_outside_any_git_repo_still_registers_under_the_curator(tmp_path):
    """The corpus identity is unchanged where it belongs: standalone notes."""
    from nexus.doc_indexer import _register_or_lookup_doc_id

    loose = tmp_path / f"loose-{_next()}"
    loose.mkdir()
    f = loose / "note.md"
    f.write_text("# loose\n")
    cat = ActiveCatalog()
    corpus = f"curator-{_next()}"
    doc_id = _register_or_lookup_doc_id(f, corpus, content_type="prose", physical_collection=_COLLECTION)
    owner = _owner_of(cat, doc_id)
    assert owner["owner_type"] == "curator" and owner["name"] == corpus, owner


def test_a_pdf_inside_a_git_repo_keeps_the_corpus_identity(tmp_path):
    """A paper filed in a repo is a curated corpus document, not a repo document."""
    from nexus.doc_indexer import _register_or_lookup_doc_id

    root = _git_repo(tmp_path)
    f = root / "papers" / "p.pdf"
    f.parent.mkdir()
    f.write_bytes(b"%PDF-1.4\n")
    cat = ActiveCatalog()
    corpus = f"curator-{_next()}"
    doc_id = _register_or_lookup_doc_id(f, corpus, content_type="paper", physical_collection="docs__1-1__test-embed-768__v1")
    assert _owner_of(cat, doc_id)["owner_type"] == "curator"


def test_the_configured_rdr_paths_drive_the_type(tmp_path, monkeypatch):
    from nexus.doc_indexer import _repo_home_for
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer

    root = _git_repo(tmp_path)
    (root / ".nexus.yml").write_text("indexing:\n  rdr_paths:\n    - decisions\n")
    (root / "decisions").mkdir()
    f = root / "decisions" / "d-1.md"
    f.write_text("# d\n")
    home = _repo_home_for(make_catalog_reader(), make_catalog_writer(), f, "prose")
    assert home is not None
    _owner, rel, ct = home
    assert (rel, ct) == ("decisions/d-1.md", "rdr")
    g = root / "docs" / "rdr" / "not-configured.md"
    g.write_text("# x\n")
    assert _repo_home_for(make_catalog_reader(), make_catalog_writer(), g, "prose")[2] == "prose"


def test_a_file_in_a_nested_agent_worktree_registers_as_the_primary_file(tmp_path):
    """Review [24431] Critical: the worktree segment must never reach the
    stored path. A file the primary also holds registers as the primary's
    file; a worktree-only file is refused (the u8n4r guard), not minted."""
    from nexus.doc_indexer import _register_or_lookup_doc_id

    root = _git_repo(tmp_path)
    shared = root / "docs" / "rdr" / "rdr-902-shared.md"
    shared.write_text("# RDR-902\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@example.invalid", "-c", "user.name=T",
         "commit", "-q", "-m", "seed"], check=True,
    )
    wt = root / ".claude" / "worktrees" / "agent-abc123"
    wt.parent.mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", str(wt)], check=True)
    cat = ActiveCatalog()

    doc_id = _register_or_lookup_doc_id(
        wt / "docs" / "rdr" / "rdr-902-shared.md", f"curator-{_next()}",
        content_type="prose", physical_collection=_COLLECTION,
    )
    assert doc_id
    doc = cat.resolve(doc_id)
    assert doc.file_path == "docs/rdr/rdr-902-shared.md", doc.file_path
    assert ".claude/worktrees" not in doc.file_path
    assert _owner_of(cat, doc_id)["owner_type"] == "repo"

    only_here = wt / "docs" / "rdr" / "rdr-903-worktree-only.md"
    only_here.write_text("# RDR-903\n")
    with pytest.raises(EphemeralPathRefusedError, match="only in an agent worktree"):
        _register_or_lookup_doc_id(
            only_here, f"curator-{_next()}", content_type="prose", physical_collection=_COLLECTION,
        )
    assert not [d for d in cat.all_documents() if "rdr-903-worktree-only" in (d.file_path or "")]


def test_indexing_a_worktree_only_file_end_to_end_mints_nothing(tmp_path, monkeypatch):
    """Critique [24433] Critical: through index_markdown, a refused file used to
    be chunked anyway and the post-hook minted the curator row. The refusal
    must stop the run before any chunk or catalog write."""
    from nexus.doc_indexer import index_markdown

    root = _git_repo(tmp_path)
    (root / "README.md").write_text("# r\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@example.invalid", "-c", "user.name=T",
         "commit", "-q", "-m", "seed"], check=True,
    )
    wt = root / ".claude" / "worktrees" / "agent-def456"
    wt.parent.mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", str(wt)], check=True)
    only_here = wt / "docs" / "rdr" / "rdr-904-new-in-worktree.md"
    only_here.parent.mkdir(parents=True)
    only_here.write_text("# RDR-904\n\nA brand-new RDR drafted in a worktree.\n")
    cat = ActiveCatalog()
    before = len(cat.all_documents())

    embedded: list[int] = []

    def _no_embed(texts, *a, **k):
        embedded.append(len(texts))
        return [[0.0] * 8 for _ in texts]

    with pytest.raises(EphemeralPathRefusedError):
        index_markdown(only_here, corpus=f"curator-{_next()}", collection_name=_COLLECTION, embed_fn=_no_embed)
    assert embedded == [], "nothing may be embedded for a refused file"
    assert len(cat.all_documents()) == before
    assert not [d for d in cat.all_documents() if "rdr-904" in (d.file_path or "")]


def test_a_file_under_the_nexus_config_dir_is_never_a_repo_document(tmp_path, monkeypatch):
    """Critique [24433] Significant 2: ~/.config as a dotfiles checkout must
    not claim nx dt index's staged DEVONthink content."""
    from nexus.doc_indexer import _repo_home_for
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer

    cfg_repo = _git_repo(tmp_path)
    cfg = cfg_repo / "nexus"
    (cfg / "catalog" / ".dt-content").mkdir(parents=True)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    f = cfg / "catalog" / ".dt-content" / "abc.md"
    f.write_text("# dt record\n")
    assert _repo_home_for(make_catalog_reader(), make_catalog_writer(), f, "prose") is None
