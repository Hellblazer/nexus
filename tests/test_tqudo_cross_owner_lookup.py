# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A curator-side reindex must not fork a repo-owner document (nexus-tqudo).

THE DEFECT. ``nx index repo`` registers under a REPO owner; the doc_indexer
family (``nx index rdr``, ``nx index md``, ``nx collection reindex``) resolves
a CURATOR owner. ``by_file_path`` is owner-scoped, so the curator-side lookup
can NEVER see the repo-owner row — structurally, not occasionally. The second
command therefore mints a SECOND catalog Document for one physical file.

WHAT THE ORIGINAL SAFETY ARGUMENT MISSED. ``_catalog_markdown_hook``'s
nexus-3lswy docstring reasons that no double-registration exists because those
commands "never also run ``_catalog_hook``'s batched pass". True WITHIN one
invocation; silent about a PREVIOUS one. Measured on this install 2026-08-27:
two live forks (rdr-167, rdr-182), each a complete repo-owner row shadowed by a
complete curator-owner row, four days without self-healing.

WHY DETECTION CANNOT SUBSTITUTE FOR THE LOOKUP. ``_check_document_fork`` is
overlap-based, runs AFTER the mint, and is advisory. Against a document with an
EMPTY manifest — the rdr-195 case, chunk_count=0 from a failed run — overlap is
structurally 0, so it finds nothing however it is wired or whether it refuses.
That is why this is fixed at the lookup and not at the check.

``--force`` is causally irrelevant and deliberately absent from these tests: it
reaches only the reindex decision inside ``_index_document``, which runs AFTER
registration. A non-forced run forks identically.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from tests._catalog_fixture_ops import ActiveCatalog

#: A CONFORMANT collection name (RDR-103 <content_type>__<owner>__<model>__v<n>)
#: that deliberately does NOT carry a voyage token. Nothing here concerns the
#: embedder — the name is an opaque label on the Document row — and a voyage
#: token would trip the RDR-109 mode-declaration lint, which exists to catch
#: tests asserting cloud behaviour without declaring cloud_mode. These do not.
#: Do not "restore" a realistic embedder name here.
_COLLECTION = "rdr__1-1__test-embed-768__v1"

_SEQ = [0]


def _next() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _repo_with_file(tmp_path: Path) -> tuple[Path, Path, str]:
    """A repo root, a file inside it, and the repo hash the probe will derive."""
    root = tmp_path / f"repo-{_next()}"
    (root / "docs").mkdir(parents=True)
    f = root / "docs" / "note.md"
    f.write_text("# note\n\nbody\n")
    repo_hash = hashlib.sha256(str(root).encode()).hexdigest()[:8]
    return root, f, repo_hash


def _pin_repo_probe(monkeypatch, root: Path, repo_hash: str) -> None:
    """Pin repo resolution instead of shelling to git.

    The probe calls ``repo_identity._repo_identity_with_main``; tmp_path is not
    a git repo, so unpinned it would fall back to whatever path it was handed
    and the test would be measuring the fallback, not the lookup.
    """
    import nexus.repo_identity as ri
    monkeypatch.setattr(
        ri, "_repo_identity_with_main",
        lambda p: (root.name, repo_hash, root),
    )


# ── the falsifier: a repo-owner row exists, and must be found ───────────────

def test_curator_reindex_converges_on_the_repo_owner_document(tmp_path, monkeypatch):
    """THE BEAD. Before this, the curator lookup missed and minted a fork."""
    from nexus.doc_indexer import _register_or_lookup_doc_id

    root, f, repo_hash = _repo_with_file(tmp_path)
    _pin_repo_probe(monkeypatch, root, repo_hash)

    cat = ActiveCatalog()
    collection = _COLLECTION
    repo_owner = cat.register_owner(
        f"repo-{_next()}", "repo", repo_hash=repo_hash, repo_root=root,
    )
    original = cat.register(
        repo_owner, "note.md",
        content_type="rdr",
        file_path="docs/note.md",
        physical_collection=collection,
    )

    doc_id = _register_or_lookup_doc_id(
        f, f"curator-{_next()}",
        content_type="rdr",
        physical_collection=collection,
        base_path=root,
    )

    assert doc_id == str(original), (
        "the curator-side registration minted a new Document instead of "
        "converging on the repo-owner row — this IS the tqudo fork"
    )
    same_path = [d for d in cat.all_documents() if d.file_path == "docs/note.md"]
    assert len(same_path) == 1, (
        f"one physical file must have one catalog Document; got {len(same_path)}"
    )


def test_the_convergence_goes_through_the_repo_owner_probe(tmp_path, monkeypatch):
    """Pin that the probe is what found it.

    If this ever passes with the probe returning None, the test above is
    passing for some other reason and is no longer evidence.
    """
    from nexus.doc_indexer import _repo_owner_document_for
    from nexus.catalog.factory import make_catalog_reader

    root, f, repo_hash = _repo_with_file(tmp_path)
    _pin_repo_probe(monkeypatch, root, repo_hash)

    cat = ActiveCatalog()
    repo_owner = cat.register_owner(
        f"repo-{_next()}", "repo", repo_hash=repo_hash, repo_root=root,
    )
    original = cat.register(
        repo_owner, "note.md", content_type="rdr",
        file_path="docs/note.md",
        physical_collection=_COLLECTION,
    )

    found = _repo_owner_document_for(make_catalog_reader(), f)
    assert found is not None, "the probe did not find the repo-owner row"
    assert str(found.tumbler) == str(original)


# ── the other direction: it must not over-reach ────────────────────────────

def test_no_repo_owner_row_still_registers_under_the_curator(tmp_path, monkeypatch):
    """Unchanged behaviour when there is genuinely nothing to converge on."""
    from nexus.doc_indexer import _register_or_lookup_doc_id

    root, f, repo_hash = _repo_with_file(tmp_path)
    _pin_repo_probe(monkeypatch, root, repo_hash)

    cat = ActiveCatalog()
    doc_id = _register_or_lookup_doc_id(
        f, f"curator-{_next()}",
        content_type="rdr",
        physical_collection=_COLLECTION,
        base_path=root,
    )
    assert doc_id, "a first-time registration must still produce a tumbler"
    assert any(str(d.tumbler) == doc_id for d in cat.all_documents())


def test_a_file_outside_the_resolved_repo_is_not_claimed(tmp_path, monkeypatch):
    """relative_to fails -> no cross-owner claim.

    Guards against the probe attaching a document to a repo owner that merely
    happens to be resolvable, which would be a worse bug than the fork.
    """
    from nexus.doc_indexer import _repo_owner_document_for
    from nexus.catalog.factory import make_catalog_reader

    root, f, repo_hash = _repo_with_file(tmp_path)
    elsewhere = tmp_path / "outside" / "note.md"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("# other\n")
    _pin_repo_probe(monkeypatch, root, repo_hash)

    cat = ActiveCatalog()
    repo_owner = cat.register_owner(
        f"repo-{_next()}", "repo", repo_hash=repo_hash, repo_root=root,
    )
    cat.register(
        repo_owner, "note.md", content_type="rdr", file_path="docs/note.md",
        physical_collection=_COLLECTION,
    )

    assert _repo_owner_document_for(make_catalog_reader(), elsewhere) is None


# ── best-effort contract: a broken probe must not break indexing ───────────

def test_a_raising_repo_probe_degrades_to_none(tmp_path, monkeypatch):
    """This is a lookup widening, not a new gate. It must never be able to
    fail an index run."""
    from nexus.doc_indexer import _repo_owner_document_for
    from nexus.catalog.factory import make_catalog_reader
    import nexus.repo_identity as ri

    _root, f, _h = _repo_with_file(tmp_path)

    def _boom(_p):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(ri, "_repo_identity_with_main", _boom)
    assert _repo_owner_document_for(make_catalog_reader(), f) is None


def test_a_raising_probe_does_not_stop_registration(tmp_path, monkeypatch):
    from nexus.doc_indexer import _register_or_lookup_doc_id
    import nexus.repo_identity as ri

    root, f, _h = _repo_with_file(tmp_path)
    monkeypatch.setattr(
        ri, "_repo_identity_with_main",
        lambda _p: (_ for _ in ()).throw(RuntimeError("git exploded")),
    )

    doc_id = _register_or_lookup_doc_id(
        f, f"curator-{_next()}",
        content_type="rdr",
        physical_collection=_COLLECTION,
        base_path=root,
    )
    assert doc_id, "a failing cross-owner probe broke the registration path"
