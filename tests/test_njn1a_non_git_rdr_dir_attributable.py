# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-njn1a (7.88.0 shakedown, finding 12): `nx index rdr <dir>` on a directory outside any git
repo gave `nx enrich aspects` nothing to attribute.

The documents registered under a curator owner, which carries no repo_root, with a path relative
to the directory and no source_uri. `nx enrich aspects` then logged
`aspect_uri_for_relative_without_root` and skipped every document as `unattributable_identity`.

The registration now carries the file's ABSOLUTE path, as `nx index md` / `nx index pdf` do, so the
engine derives the raw `file://<abspath>` source_uri itself (never a percent-encoded `as_uri()` form),
and two non-git directories with the same basename get distinct documents. A document an earlier
release registered with a relative path and no source_uri is adopted and backfilled on the next run.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._catalog_fixture_ops import ActiveCatalog

_COLLECTION = "rdr__1-1__test-embed-768__v1"
_SEQ = [0]
_REL = "docs/rdr/rdr-901-example.md"


def _next() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _non_git_rdr_dir(tmp_path: Path, name: str | None = None) -> tuple[Path, Path]:
    root = tmp_path / (name or f"plain-{_next()}")
    (root / "docs" / "rdr").mkdir(parents=True)
    f = root / _REL
    f.write_text("# RDR-901\n\nbody\n")
    return root, f


def _raw_uri(f: Path) -> str:
    """The engine's spelling: ``file://`` + the absolute path, no percent-encoding."""
    return "file://" + str(f.resolve())


def _register(f: Path, root: Path, corpus: str) -> str:
    from nexus.doc_indexer import _register_or_lookup_doc_id

    return _register_or_lookup_doc_id(
        f, corpus, content_type="rdr", physical_collection=_COLLECTION, base_path=root,
    )


@pytest.mark.t2_tenant
def test_a_document_in_a_non_git_rdr_dir_carries_its_absolute_path_and_raw_source_uri(tmp_path):
    root, f = _non_git_rdr_dir(tmp_path)
    cat = ActiveCatalog()
    doc_id = _register(f, root, f"plain-{_next()}")
    assert doc_id
    doc = cat.resolve(doc_id)
    assert doc.file_path == str(f.resolve()), doc.file_path
    assert doc.source_uri == _raw_uri(f), doc.source_uri


@pytest.mark.t2_tenant
def test_a_directory_with_a_space_and_a_non_ascii_name_gets_the_raw_uri_not_the_encoded_one(tmp_path):
    """Path.as_uri() would stamp file:///.../my%20docs%20%C3%A9/..., which the engine's own derivation
    and `_doc_id_for_path` (both raw) never match."""
    root, f = _non_git_rdr_dir(tmp_path, f"my docs é-{_next()}")
    cat = ActiveCatalog()
    doc_id = _register(f, root, f"plain-{_next()}")
    doc = cat.resolve(doc_id)
    assert " " in doc.source_uri and "é" in doc.source_uri and "%" not in doc.source_uri, doc.source_uri
    assert doc.source_uri == _raw_uri(f)
    from nexus.doc_indexer import _doc_id_for_path

    assert _doc_id_for_path(f) == doc_id, "the staleness gate's read-only lookup must find the document"


@pytest.mark.t2_tenant
def test_two_non_git_dirs_with_the_same_basename_get_distinct_documents(tmp_path):
    """The curator owner is keyed by the directory's basename (index_rdr_cmd passes corpus=basename),
    so a relative fp collided across the two; the absolute one does not."""
    root_a, fa = _non_git_rdr_dir(tmp_path / "one", "notes")
    root_b, fb = _non_git_rdr_dir(tmp_path / "two", "notes")
    assert root_a.name == root_b.name and fa != fb
    cat = ActiveCatalog()
    corpus = f"plain-{_next()}"
    a = _register(fa, root_a, corpus)
    b = _register(fb, root_b, corpus)
    assert a and b and a != b, (a, b)
    assert cat.resolve(a).source_uri == _raw_uri(fa)
    assert cat.resolve(b).source_uri == _raw_uri(fb)
    assert _register(fa, root_a, corpus) == a and _register(fb, root_b, corpus) == b


@pytest.mark.t2_tenant
def test_a_git_repo_rdr_dir_registers_as_before(tmp_path):
    """A file inside a git repo takes the repo owner's identity (a repo-relative path); this change
    touches the curator branch only."""
    import subprocess

    root, f = _non_git_rdr_dir(tmp_path, f"repo-{_next()}")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    cat = ActiveCatalog()
    doc_id = _register(f, root, f"repo-corpus-{_next()}")
    assert doc_id
    doc = cat.resolve(doc_id)
    assert doc.file_path == _REL, doc.file_path


@pytest.fixture
def aspects_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The isolated T2 db and fake LLM backend `nx enrich aspects` needs (the setup
    tests/test_enrich_aspects.py uses); extraction itself is faked at the extract_aspects boundary."""
    db_path = tmp_path / "enrich_aspects.db"
    monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)
    monkeypatch.setattr("nexus.mcp_infra.default_db_path", lambda: db_path)
    monkeypatch.setattr("nexus.operators.llm_backend.resolve_backend_name", lambda req: "cli")
    monkeypatch.setattr("nexus.commands.enrich._claude_on_path", lambda: True)
    return db_path


@pytest.mark.t2_tenant
def test_enrich_aspects_attributes_a_non_git_rdr_document(tmp_path, aspects_env, monkeypatch):
    """The user-visible half: after `nx index rdr` on a non-git directory, `nx enrich aspects`
    stores a row carrying the document's source_uri instead of skipping it as
    `unattributable_identity`. The fake extractor leaves source_uri empty, the shape the real one
    produces for a relative path with no repo_root (`aspect_uri_for_relative_without_root`)."""
    import dataclasses as dc
    from datetime import UTC, datetime

    from click.testing import CliRunner

    from nexus.aspect_extractor import AspectRecord
    from nexus.commands.enrich import enrich
    from nexus.db.t2 import T2Database

    root, f = _non_git_rdr_dir(tmp_path, f"asp dir é-{_next()}")
    doc_id = _register(f, root, f"plain-{_next()}")
    assert doc_id

    def fake_extract(content, source_path, collection, **_kw):
        rec = AspectRecord(
            collection=collection, source_path=source_path, problem_formulation="P", proposed_method="M",
            experimental_datasets=[], experimental_baselines=[], experimental_results="R", extras={},
            confidence=0.9, extracted_at=datetime.now(UTC).isoformat(), model_version="m",
            extractor_name="rdr-frontmatter-v1", source_uri="", doc_id=doc_id,
        )
        return dc.replace(rec, source_uri="")

    monkeypatch.setattr("nexus.aspect_extractor.extract_aspects", fake_extract)
    result = CliRunner().invoke(enrich, ["aspects", _COLLECTION, "--validate-sample", "0"])
    assert result.exit_code == 0, result.output
    assert "unattributable_identity" not in result.output, result.output
    assert "1 extracted" in result.output, result.output
    with T2Database(aspects_env) as db:
        rows = db.document_aspects.list_by_collection(_COLLECTION)
    assert len(rows) == 1
    assert rows[0].source_uri == _raw_uri(f), rows[0].source_uri


def _legacy_doc(corpus: str, *, source_uri: str = "") -> str:
    """A curator document as 7.88.0 registered it: a path relative to the directory, and (when the
    engine could not derive one) no source_uri."""
    cat = ActiveCatalog()
    owner = cat.register_owner(corpus, "curator")
    res = cat.register(
        owner=owner, title="legacy", content_type="rdr", file_path=_REL, corpus=corpus,
        physical_collection=_COLLECTION, chunk_count=0, source_uri=source_uri,
    )
    return str(res[0] if isinstance(res, tuple) else res)


@pytest.mark.t2_tenant
def test_a_rerun_adopts_and_backfills_a_document_registered_with_a_relative_path_and_no_source_uri(tmp_path):
    root, f = _non_git_rdr_dir(tmp_path)
    cat = ActiveCatalog()
    corpus = f"plain-{_next()}"
    legacy = _legacy_doc(corpus)
    assert cat.resolve(legacy).source_uri == ""
    again = _register(f, root, corpus)
    assert again == legacy, "a re-run must not mint a second document"
    assert cat.resolve(legacy).source_uri == _raw_uri(f)


@pytest.mark.t2_tenant
def test_the_markdown_hook_finds_the_preflight_document_and_backfills_a_legacy_one(tmp_path):
    """`_catalog_markdown_hook` runs after the pre-flight with the same base_path: it must look the
    document up by the same identity, or it mints a second one under the relative path."""
    from nexus.doc_indexer import _catalog_markdown_hook

    root, f = _non_git_rdr_dir(tmp_path)
    cat = ActiveCatalog()
    corpus = f"plain-{_next()}"
    fresh = _register(f, root, corpus)
    _catalog_markdown_hook(f, _COLLECTION, "rdr", corpus, 3, base_path=root)
    assert cat.resolve(fresh).chunk_count == 3, "the hook updated the pre-flight document"

    root2, f2 = _non_git_rdr_dir(tmp_path)
    corpus2 = f"plain-{_next()}"
    legacy = _legacy_doc(corpus2)
    _catalog_markdown_hook(f2, _COLLECTION, "rdr", corpus2, 2, base_path=root2)
    doc = cat.resolve(legacy)
    assert doc.chunk_count == 2 and doc.source_uri == _raw_uri(f2), (doc.chunk_count, doc.source_uri)


@pytest.mark.t2_tenant
def test_the_markdown_hook_alone_registers_the_absolute_path(tmp_path):
    from nexus.doc_indexer import _catalog_markdown_hook

    root, f = _non_git_rdr_dir(tmp_path, f"hook dir é-{_next()}")
    corpus = f"plain-{_next()}"
    _catalog_markdown_hook(f, _COLLECTION, "rdr", corpus, 1, base_path=root)
    cat = ActiveCatalog()
    doc = cat.by_source_uri(_raw_uri(f))
    assert doc is not None and doc.file_path == str(f.resolve()), doc


# --- fix round 3: a moved directory, a directory that becomes a repo, the hook's advisory stamp ---


def _move(root: Path, dest_parent: Path) -> Path:
    import shutil

    dest_parent.mkdir(parents=True)
    dest = dest_parent / root.name
    shutil.move(str(root), str(dest))
    return dest


@pytest.mark.t2_tenant
def test_a_legacy_document_adopted_before_the_directory_moved_is_reused_and_restamped(tmp_path):
    """A non-git document's identity is its location (as `nx index md`), but a LEGACY relative-path row
    is the one case the new location can still claim: the path it holds is gone, so nobody else is
    using the row. Minting a second document here left the first as an orphan."""
    root, f = _non_git_rdr_dir(tmp_path / "a", "notes")
    cat = ActiveCatalog()
    corpus = f"plain-{_next()}"
    legacy = _legacy_doc(corpus)
    assert _register(f, root, corpus) == legacy
    old_uri = cat.resolve(legacy).source_uri
    assert old_uri == _raw_uri(f)
    root2 = _move(root, tmp_path / "b")
    f2 = root2 / _REL
    assert _register(f2, root2, corpus) == legacy, "the moved directory minted a second document"
    assert cat.resolve(legacy).source_uri == _raw_uri(f2), "the row must follow the file to its new location"


@pytest.mark.t2_tenant
def test_a_legacy_document_stamped_with_a_path_that_still_exists_is_not_adopted(tmp_path):
    other_root, other_f = _non_git_rdr_dir(tmp_path / "other", "notes")
    root, f = _non_git_rdr_dir(tmp_path / "mine", "notes")
    cat = ActiveCatalog()
    corpus = f"plain-{_next()}"
    legacy = _legacy_doc(corpus, source_uri=_raw_uri(other_f))
    mine = _register(f, root, corpus)
    assert mine and mine != legacy
    assert cat.resolve(legacy).source_uri == _raw_uri(other_f)


@pytest.mark.t2_tenant
def test_an_absolute_registered_document_whose_directory_moved_becomes_a_new_document(tmp_path):
    """The documented rule: a non-git document registered by absolute path is identified by that
    path, as with `nx index md`. A move makes a new document at the new path and leaves the old one
    for `nx catalog reconcile-stale`."""
    root, f = _non_git_rdr_dir(tmp_path / "a", "notes")
    cat = ActiveCatalog()
    corpus = f"plain-{_next()}"
    first = _register(f, root, corpus)
    root2 = _move(root, tmp_path / "b")
    f2 = root2 / _REL
    second = _register(f2, root2, corpus)
    assert second and second != first
    assert cat.resolve(first).source_uri == _raw_uri(f), "the old document is left as it was"
    assert cat.resolve(second).source_uri == _raw_uri(f2)


@pytest.mark.t2_tenant
def test_a_non_git_directory_that_later_becomes_a_git_repo_converges_on_the_same_document(tmp_path):
    import subprocess

    root, f = _non_git_rdr_dir(tmp_path, f"later-{_next()}")
    ActiveCatalog()
    corpus = f"plain-{_next()}"
    first = _register(f, root, corpus)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    assert _register(f, root, corpus) == first


@pytest.mark.t2_tenant
def test_the_markdown_hook_still_updates_the_chunk_count_when_the_source_uri_stamp_collides(tmp_path):
    """The source_uri stamp is advisory: a second live document already holding that URI
    (catalog-016) must not stop chunk_count / indexed_at from being written."""
    from nexus.doc_indexer import _catalog_markdown_hook

    root, f = _non_git_rdr_dir(tmp_path)
    cat = ActiveCatalog()
    corpus = f"plain-{_next()}"
    legacy = _legacy_doc(corpus)
    squatter_corpus = f"squat-{_next()}"
    squatter_owner = cat.register_owner(squatter_corpus, "curator")
    cat.register(
        owner=squatter_owner, title="squatter", content_type="rdr", file_path="elsewhere/x.md",
        corpus=squatter_corpus, physical_collection=_COLLECTION, chunk_count=0, source_uri=_raw_uri(f),
    )
    _catalog_markdown_hook(f, _COLLECTION, "rdr", corpus, 4, base_path=root)
    doc = cat.resolve(legacy)
    assert doc.chunk_count == 4 and doc.indexed_at, (doc.chunk_count, doc.indexed_at)

