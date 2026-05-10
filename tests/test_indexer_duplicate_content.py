# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Indexer fixtures with realistic-duplicate chunk content (nexus-xdny).

After nexus-kmb6 (RDR-108 D1) retargeted the indexer write path to use
``chunk_text_hash[:32]`` as the Chroma natural id, identical chunk text
within a batch -> identical id -> ``DuplicateIDError``. nexus-1ljk
patched ``T3Database._write_batch`` to dedupe; this file locks the
end-to-end contract by exercising the indexer with fixtures real
corpora generate naturally:

1. Two byte-identical ``.py`` files in the same ``code__`` collection
   (duplicated utility module pattern). Cross-file scenario: each file
   is its own write batch, so the T3 row collapse rides on
   ``coll.upsert``'s overwrite semantic; locks the manifest
   cross-doc-shared-chash contract.
2. Two ``.md`` files sharing a paragraph block under the same heading
   (license blurb / stock disclaimer pattern). Cross-file scenario,
   same shape as (1) but exercising the markdown chunker.
3. One ``.md`` file whose chunker emits the same paragraph block twice
   under separate identical headings (long manual / spec pattern).
   Within-batch scenario: this is the case nexus-1ljk fixed; reverting
   the dedup makes this test fail with ``DuplicateIDError``.
4. A PDF whose two pages produce duplicate chunk text (mocked at the
   chunker boundary so the test stays deterministic and fast).
   Within-document scenario through the PDF write path; also fails
   with ``DuplicateIDError`` if the dedup is reverted.

Each scenario asserts:
* the indexer succeeds (no ``DuplicateIDError``);
* T3 row count collapses to one row per unique chunk text;
* every contributing catalog ``Document`` has a manifest, and the
  shared chash appears in each one (cross-doc) or appears at multiple
  positions in one (within-doc).

The original nexus-kmb6 fixture suite used distinct chunk content per
file, so the within-batch duplicate scenario was never executed; this
file closes that gap.
"""
from __future__ import annotations

import hashlib as _hl
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.catalog.catalog import Catalog
from nexus.db.t3 import T3Database
from nexus.registry import RepoRegistry


# ── shared fixtures ──────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in [
        ("GIT_AUTHOR_NAME", "Test"),
        ("GIT_AUTHOR_EMAIL", "test@test.invalid"),
        ("GIT_COMMITTER_NAME", "Test"),
        ("GIT_COMMITTER_EMAIL", "test@test.invalid"),
    ]:
        monkeypatch.setenv(k, v)


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    Catalog.init(catalog_dir)
    return catalog_dir


@pytest.fixture(autouse=True)
def mock_voyage_client():
    """Local-mode test: voyage AI is never reached, but the
    ``voyageai.Client`` constructor may still be invoked. Returns a
    stub whose ``.embed`` and ``.contextualized_embed`` produce
    DefaultEmbeddingFunction-shaped responses keyed off the chunk text
    so identical inputs yield identical embeddings (mirrors prod).
    """
    ef = DefaultEmbeddingFunction()
    mock_client = MagicMock()

    def fake_embed(texts, model, input_type="document"):
        r = MagicMock()
        r.embeddings = ef(texts)
        return r

    def fake_contextualized_embed(inputs, model, input_type="document"):
        r = MagicMock()
        br = MagicMock()
        br.embeddings = ef(inputs[0])
        r.results = [br]
        return r

    mock_client.embed.side_effect = fake_embed
    mock_client.contextualized_embed.side_effect = fake_contextualized_embed
    with patch("voyageai.Client", return_value=mock_client):
        yield mock_client


def _do_index(
    repo: Path, registry: RepoRegistry, t3: T3Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.indexer import index_repository

    monkeypatch.setenv("NX_LOCAL", "1")
    with patch("nexus.db.make_t3", return_value=t3), patch(
        "nexus.config.get_credential", side_effect=lambda k: "test-key"
    ):
        index_repository(repo, registry, force=False)


def _chash32(text: str) -> str:
    """Mirror RDR-108 D1: Chroma natural id is sha256(text)[:32]."""
    return _hl.sha256(text.encode()).hexdigest()[:32]


# ── (1) code__: two .py files with identical content (copied module) ───────


# A small utility module that two project subdirectories carry verbatim.
# Real-world pattern: someone copy-pasted ``utils.py`` from one tool into
# another and kept it byte-identical. Tree-sitter / CodeSplitter emit a
# single AST chunk per file (small file, below CodeSplitter's internal
# split threshold), so both files share the exact same chunk text and
# therefore the same ``chunk_text_hash[:32]``.
_SHARED_MODULE = (
    '"""Shared utility module duplicated across tools."""\n'
    "\n"
    "def _apply_license_header(path):\n"
    '    """Insert the standard license header at the top of *path*."""\n'
    "    text = path.read_text(encoding='utf-8')\n"
    "    if text.startswith('# SPDX-License-Identifier'):\n"
    "        return\n"
    "    header = '# SPDX-License-Identifier: MIT\\n'\n"
    "    path.write_text(header + text, encoding='utf-8')\n"
    "\n"
    "\n"
    "def main():\n"
    '    """Stamp every .py file under cwd."""\n'
    "    from pathlib import Path\n"
    "    for f in Path('.').rglob('*.py'):\n"
    "        _apply_license_header(f)\n"
)


@pytest.fixture
def duplicate_code_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "dup-code-repo"
    repo.mkdir()
    # Two files with byte-identical content but different basenames so
    # the catalog (which dedupes by ``head_hash + title``) registers
    # both as distinct Documents. Title is the basename, so the two
    # filenames produce two Documents; chunk text is content-only and
    # therefore byte-identical -> shared ``chunk_text_hash[:32]``.
    (repo / "utils_alpha.py").write_text(_SHARED_MODULE, encoding="utf-8")
    (repo / "utils_beta.py").write_text(_SHARED_MODULE, encoding="utf-8")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@nexus")
    _git(repo, "config", "user.name", "Nexus Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial commit")
    return repo


def test_code_indexer_collapses_duplicate_module_across_files(
    duplicate_code_repo: Path,
    local_t3: T3Database,
    catalog_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two .py files with byte-identical content (a duplicated utility
    module, common in monorepos and copy-pasted tooling) must:

    * index without ``DuplicateIDError`` (nexus-1ljk regression),
    * collapse to one T3 row per unique chunk text (RDR-108 D1
      content-derived identity),
    * each get a catalog ``Document`` whose manifest references the
      shared chash (manifest preserves per-doc identity even when
      content collapses across docs).
    """
    registry = RepoRegistry(tmp_path / "repos.json")
    registry.add(duplicate_code_repo)

    _do_index(duplicate_code_repo, registry, local_t3, monkeypatch)

    info = registry.get(duplicate_code_repo)
    assert info is not None
    code_collection = info.get("code_collection") or info["collection"]
    assert code_collection

    code_col = local_t3.get_collection(code_collection)
    result = code_col.get(include=["metadatas", "documents"])
    chunk_ids = result["ids"]
    docs = result["documents"]
    assert chunk_ids, "expected the indexer to write at least one chunk"
    assert len(chunk_ids) == len(set(chunk_ids)), (
        "T3 collection must not surface duplicate chroma ids; "
        "got duplicates: "
        f"{[i for i in chunk_ids if chunk_ids.count(i) > 1]}"
    )

    # The shared module text appears at least once in T3.
    shared_chunk_ids = {
        cid for cid, d in zip(chunk_ids, docs)
        if "_apply_license_header" in (d or "")
    }
    assert shared_chunk_ids, (
        "expected at least one T3 chunk to contain the shared module; "
        f"chunks={[d[:80] for d in docs]!r}"
    )

    # Each contributing file's chunks collapse to the same set of T3
    # rows; cross-file duplicates do not multiply the row count.
    cat = Catalog(catalog_env, catalog_env / ".catalog.db")
    documents = cat._db.execute(
        "SELECT tumbler, file_path FROM documents "
        "WHERE physical_collection = ?",
        (code_collection,),
    ).fetchall()
    by_path = {Path(row[1]).name: row[0] for row in documents if row[1]}
    assert {"utils_alpha.py", "utils_beta.py"} <= by_path.keys(), (
        f"expected catalog Documents for both copies, got {by_path!r}"
    )

    # Both per-file manifests must reference the same chash set
    # (proves the manifest write hook bound chunks to each Document
    # despite T3's row-level collapse).
    manifest_chashes = {}
    for path_key in ("utils_alpha.py", "utils_beta.py"):
        rows = cat.get_manifest(by_path[path_key])
        assert rows, f"expected manifest rows for {path_key}"
        manifest_chashes[path_key] = {r.chash[:32] for r in rows}
    assert manifest_chashes["utils_alpha.py"] == manifest_chashes["utils_beta.py"], (
        "byte-identical files must produce identical manifest chash sets; "
        f"alpha={manifest_chashes['utils_alpha.py']!r} "
        f"beta={manifest_chashes['utils_beta.py']!r}"
    )
    assert manifest_chashes["utils_alpha.py"] <= set(chunk_ids), (
        "every manifest chash must resolve to a T3 row; "
        f"manifest={manifest_chashes['utils_alpha.py']!r} "
        f"t3_ids={set(chunk_ids)!r}"
    )


# ── (2) docs__: two .md files share a paragraph under the same heading ─────


_SHARED_PARAGRAPH = (
    "All contributions to this project are licensed under the MIT "
    "license. By submitting a pull request you agree that your "
    "contribution may be redistributed under those terms.\n"
)


@pytest.fixture
def duplicate_docs_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "dup-docs-repo"
    repo.mkdir()
    (repo / "GUIDE_A.md").write_text(
        "# Guide A\n\n"
        "## Licensing\n\n"
        f"{_SHARED_PARAGRAPH}"
        "\n"
        "## A-specific notes\n\n"
        "Some prose unique to guide A so the file has more than one chunk.\n",
        encoding="utf-8",
    )
    (repo / "GUIDE_B.md").write_text(
        "# Guide B\n\n"
        "## Licensing\n\n"
        f"{_SHARED_PARAGRAPH}"
        "\n"
        "## B-specific notes\n\n"
        "Different prose unique to guide B so the file has more than one chunk.\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@nexus")
    _git(repo, "config", "user.name", "Nexus Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial commit")
    return repo


def test_prose_indexer_collapses_shared_paragraph_across_files(
    duplicate_docs_repo: Path,
    local_t3: T3Database,
    catalog_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two .md files sharing a Licensing paragraph block must index
    without ``DuplicateIDError`` and collapse to one T3 row for the
    shared chunk while keeping both per-file manifests intact.
    """
    registry = RepoRegistry(tmp_path / "repos.json")
    registry.add(duplicate_docs_repo)

    _do_index(duplicate_docs_repo, registry, local_t3, monkeypatch)

    info = registry.get(duplicate_docs_repo)
    assert info is not None
    docs_collection = info.get("docs_collection")
    assert docs_collection

    docs_col = local_t3.get_collection(docs_collection)
    result = docs_col.get(include=["metadatas", "documents"])
    chunk_ids = result["ids"]
    docs = result["documents"]
    assert chunk_ids, "expected the indexer to write at least one chunk"
    assert len(chunk_ids) == len(set(chunk_ids)), (
        "T3 collection must not surface duplicate chroma ids; "
        "got duplicates: "
        f"{[i for i in chunk_ids if chunk_ids.count(i) > 1]}"
    )

    # Find the chunk whose stored text contains the shared paragraph.
    shared_substr = _SHARED_PARAGRAPH.split("\n", 1)[0][:60]
    matching = [(cid, d) for cid, d in zip(chunk_ids, docs) if shared_substr in (d or "")]
    assert matching, (
        "expected at least one T3 chunk to contain the shared paragraph; "
        f"chunks={[d[:80] for d in docs]!r}"
    )
    # Within each file the paragraph appears once, so across two files
    # we expect the chunk text to collapse to a single T3 row.
    matching_ids = {cid for cid, _ in matching}
    assert len(matching_ids) == 1, (
        "shared paragraph must collapse to one T3 chroma id across files; "
        f"got {len(matching_ids)} distinct ids: {matching_ids!r}"
    )
    shared_chroma_id = next(iter(matching_ids))

    # Both per-file manifests must reference the shared chash.
    cat = Catalog(catalog_env, catalog_env / ".catalog.db")
    documents = cat._db.execute(
        "SELECT tumbler, file_path FROM documents "
        "WHERE physical_collection = ?",
        (docs_collection,),
    ).fetchall()
    by_path = {Path(row[1]).name: row[0] for row in documents if row[1]}
    assert {"GUIDE_A.md", "GUIDE_B.md"} <= by_path.keys(), (
        f"expected catalog Documents for both files, got {by_path!r}"
    )
    for filename in ("GUIDE_A.md", "GUIDE_B.md"):
        rows = cat.get_manifest(by_path[filename])
        assert rows, f"expected manifest rows for {filename}"
        assert any(r.chash[:32] == shared_chroma_id for r in rows), (
            f"shared paragraph chash missing from manifest of {filename}; "
            f"manifest chashes: {[r.chash[:32] for r in rows]!r}"
        )


# ── (2b) docs__: within-file duplicate paragraph (single batch) ────────────


@pytest.fixture
def repeated_paragraph_repo(tmp_path: Path) -> Path:
    """Single .md file with the same paragraph block repeated twice
    under separate (identical) headings. Realistic prod case: a long
    spec / user guide that legitimately restates a notice in different
    sections. The semantic markdown chunker emits the repeated block
    as two distinct chunks with byte-identical text -> identical
    ``chunk_text_hash[:32]`` -> ``DuplicateIDError`` pre-nexus-1ljk.
    """
    repo = tmp_path / "repeated-md-repo"
    repo.mkdir()
    notice = (
        "STANDARD NOTICE. Use of this software is governed by the "
        "applicable license agreement. By proceeding you accept those "
        "terms.\n"
    )
    (repo / "manual.md").write_text(
        "# Manual\n\n"
        "## Notice\n\n"
        f"{notice}"
        "\n"
        "## Setup\n\n"
        "Install the dependencies before continuing.\n"
        "\n"
        "## Notice\n\n"
        f"{notice}"
        "\n"
        "## Conclusion\n\n"
        "Restated for emphasis at the end of the manual.\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@nexus")
    _git(repo, "config", "user.name", "Nexus Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial commit")
    return repo


def test_prose_indexer_handles_within_file_duplicate_paragraph(
    repeated_paragraph_repo: Path,
    local_t3: T3Database,
    catalog_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single .md file whose chunker emits two byte-identical chunks
    must index without ``DuplicateIDError`` (this is the within-batch
    case that nexus-1ljk fixed). T3 collapses to one row at the shared
    chash[:32]; the manifest preserves both positions pointing at it.
    """
    registry = RepoRegistry(tmp_path / "repos.json")
    registry.add(repeated_paragraph_repo)

    _do_index(repeated_paragraph_repo, registry, local_t3, monkeypatch)

    info = registry.get(repeated_paragraph_repo)
    assert info is not None
    docs_collection = info.get("docs_collection")
    assert docs_collection

    docs_col = local_t3.get_collection(docs_collection)
    result = docs_col.get(include=["documents"])
    chunk_ids = result["ids"]
    docs = result["documents"]
    assert chunk_ids, "expected the indexer to write at least one chunk"
    assert len(chunk_ids) == len(set(chunk_ids)), (
        "T3 collection must not surface duplicate chroma ids; "
        "got duplicates: "
        f"{[i for i in chunk_ids if chunk_ids.count(i) > 1]}"
    )

    # Exactly one T3 row carries the repeated notice text.
    notice_marker = "STANDARD NOTICE."
    notice_rows = [(cid, d) for cid, d in zip(chunk_ids, docs) if notice_marker in (d or "")]
    assert len(notice_rows) == 1, (
        "within-file duplicate paragraph must collapse to a single T3 row; "
        f"got {len(notice_rows)} rows: {[d[:60] for _, d in notice_rows]!r}"
    )
    notice_chroma_id = notice_rows[0][0]

    # Manifest must carry TWO positions for this single Document, both
    # pointing at the shared chash. This is the load-bearing contract:
    # T3 rows collapse, but per-doc manifest positions do not.
    cat = Catalog(catalog_env, catalog_env / ".catalog.db")
    documents = cat._db.execute(
        "SELECT tumbler, file_path FROM documents "
        "WHERE physical_collection = ?",
        (docs_collection,),
    ).fetchall()
    assert len(documents) == 1, (
        f"expected exactly one catalog Document; got {documents!r}"
    )
    manifest = cat.get_manifest(documents[0][0])
    notice_positions = [r for r in manifest if r.chash[:32] == notice_chroma_id]
    assert len(notice_positions) == 2, (
        "manifest must record both positions of the duplicate paragraph; "
        f"got positions={[r.position for r in notice_positions]!r}"
    )


# ── (3) PDF: within-document duplicate chunks (overlapping page text) ───────


def test_pdf_indexer_handles_duplicate_chunks_within_document(
    tmp_path: Path,
    local_t3: T3Database,
    catalog_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PDF whose two pages produce identical chunk text must index
    without ``DuplicateIDError`` and collapse to a single T3 row;
    the catalog manifest should preserve both positions pointing at
    the shared chash.

    The PDFExtractor + PDFChunker boundary is patched so the test runs
    in milliseconds without exercising Docling/MinerU. The dedup path
    being verified lives downstream of the chunker (in
    ``T3Database._write_batch`` and the manifest write hook), so a
    deterministic chunker stub is sufficient, and avoids depending on
    a specific real PDF byte sequence to produce duplicates.
    """
    from nexus.doc_indexer import index_pdf
    from nexus.pdf_chunker import TextChunk

    pdf_path = tmp_path / "with-duplicate-pages.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 placeholder; chunker is mocked\n")

    monkeypatch.setenv("NX_LOCAL", "1")

    duplicate_text = (
        "STANDARD DISCLAIMER. The contents of this document are "
        "provided as is without warranty of any kind. The author "
        "disclaims all liability arising from its use."
    )

    fake_extracted_text = duplicate_text + "\n\n" + duplicate_text
    fake_extraction_metadata = {
        "extraction_method": "docling",
        "page_count": 2,
        "format": "markdown",
        "page_boundaries": [0, len(duplicate_text) + 2],
    }

    chunk_one = TextChunk(
        text=duplicate_text,
        chunk_index=0,
        metadata={
            "chunk_index": 0,
            "chunk_start_char": 0,
            "chunk_end_char": len(duplicate_text),
            "page_number": 1,
            "chunk_type": "text",
            "section_title": "",
            "section_type": "",
        },
    )
    chunk_two = TextChunk(
        text=duplicate_text,
        chunk_index=1,
        metadata={
            "chunk_index": 1,
            "chunk_start_char": len(duplicate_text) + 2,
            "chunk_end_char": 2 * len(duplicate_text) + 2,
            "page_number": 2,
            "chunk_type": "text",
            "section_title": "",
            "section_type": "",
        },
    )

    ef = DefaultEmbeddingFunction()

    def _local_embed(
        texts: list[str], target_model: str,
    ) -> tuple[list[list[float]], str]:
        # ``EmbedFn`` contract from doc_indexer: ``(texts, target_model)
        # -> (embeddings, actual_model)``. Mirror ``_make_local_embed_fn``:
        # normalise numpy float32 rows to native ``list[float]`` because
        # ChromaDB's upsert validator rejects ``list[list[np.float32]]``.
        vectors: list[list[float]] = []
        for v in ef(texts):
            if hasattr(v, "tolist"):
                vectors.append(v.tolist())
            else:
                vectors.append([float(x) for x in v])
        return vectors, target_model

    extractor_result = MagicMock()
    extractor_result.text = fake_extracted_text
    extractor_result.metadata = fake_extraction_metadata

    # Pin the destination collection name so the test does not have to
    # discover it via ``list_collections`` (the EphemeralClient shares
    # process state across fixtures, so ``list_collections`` returns
    # collections from earlier tests in the same session).
    pinned_collection = "docs__dupdocs__voyage-context-3__v1"

    with patch("nexus.doc_indexer.PDFExtractor") as ext_cls, patch(
        "nexus.doc_indexer.PDFChunker"
    ) as chunker_cls:
        ext_cls.return_value.extract.return_value = extractor_result
        chunker_cls.return_value.chunk.return_value = [chunk_one, chunk_two]
        result = index_pdf(
            pdf_path,
            corpus="dupdocs",
            t3=local_t3,
            collection_name=pinned_collection,
            embed_fn=_local_embed,
            force=True,
        )

    assert result == 2, (
        "index_pdf reports the pre-dedup chunk count (caller-visible "
        "manifest stays at 2); got "
        f"{result!r}"
    )

    # T3 must collapse to one row at the shared chash[:32].
    expected_chash = _hl.sha256(duplicate_text.encode()).hexdigest()
    expected_id = expected_chash[:32]
    docs_col = local_t3.get_collection(pinned_collection)
    res = docs_col.get(include=["metadatas"])
    assert res["ids"] == [expected_id], (
        "expected exactly one T3 row at the shared chash[:32]; "
        f"got ids={res['ids']!r}"
    )

    # Manifest contract: the catalog Document for this PDF must carry
    # two positions, both pointing at the shared chash.
    cat = Catalog(catalog_env, catalog_env / ".catalog.db")
    rows = cat._db.execute(
        "SELECT tumbler FROM documents WHERE physical_collection = ?",
        (pinned_collection,),
    ).fetchall()
    assert rows, "expected a catalog Document for the indexed PDF"
    doc_id = rows[0][0]
    manifest = cat.get_manifest(doc_id)
    assert len(manifest) == 2, (
        "manifest must preserve both positions even though T3 collapsed; "
        f"got {[(r.position, r.chash[:8]) for r in manifest]!r}"
    )
    assert {r.chash[:32] for r in manifest} == {expected_id}, (
        "both manifest rows must point at the shared chash; "
        f"got {[r.chash[:32] for r in manifest]!r}"
    )
