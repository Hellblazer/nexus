# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wbfpw.29 (RDR-192 Step 3b, client) acceptance test: a
manifest-hook EXCEPTION during ``nx index repo`` is recorded per document
and fails the run.

Drives the PRODUCTION entry point (``nx index repo`` via the real Click
CLI, ``nexus.hook_registry.HookRegistry.fire_batch`` unmocked) against
the shared engine substrate every test in this suite already boots
(``tests/conftest.py``'s autouse ``_pin_t2_substrate``) — same shape as
``tests/test_indexer_e2e.py::test_cli_index_repo``.

ROUND-2 (critic Critical): fault injection targets the REAL, unpatched
``nexus.mcp_infra.manifest_write_batch_hook``'s own internal dependencies
(``get_catalog``), never the hook object itself. Replacing the hook
object with a bare double loses its ``batch_grain = "flush"``
classification attribute, which silently changes WHICH of
``HookRegistry``'s dispatch buckets fires it -- the round-1 version of
this test passed by accident, exercising a dispatch site (per-file
grain="file") the real hook never occupies for a ChunkBatcher-accepted
file. This version forces the file through the ONE real dispatch site
that fires the real hook unconditionally for ``nx index repo`` --
``code_indexer.py``'s legacy per-file fallback, reached whenever
``ChunkBatcher.add()`` rejects a file (chunk count over the onnx-local
cap, forced down to 1 here) -- documented in each test's own docstring,
along with a "guard test" pin
(``tests/test_wbfpw29_manifest_hook_exception.py::
test_manifest_write_batch_hook_declares_flush_grain``) against this
exact class of test-artifact bug recurring.

RETIRED 2026-10-01 (nexus-z0o2p.35, closes nexus-9xsji): the four tests that forced a file through
the legacy per-file oversize fallback to reach channel 3 are gone. RDR-223 moved that fallback onto
the combined chunk-plus-owner writer, so the real ``manifest_write_batch_hook`` never fires there and
those tests could no longer reach what they named; the failed-document heal they leaned on
(``_heal_failed_document``) is retired for the same reason (a failed run no longer leaves an ownerless
chunk to heal). What stays are the tests of the end-of-run self-heal (``heal_manifest_gaps``) and of
the paths that no longer depend on the hook. The combined writer's failure path is covered by
``tests/integration/test_rdr223_index_repo_oversize_journey.py``.

Marked ``integration`` per this directory's convention (drives a full
repo-indexing round trip through the catalog); run explicitly with
``uv run pytest -m integration tests/integration/test_wbfpw29_manifest_hook_exception_index_run.py``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import fake_credentials
from tests._chunk_seed import seed_chunks_direct

pytestmark = [pytest.mark.integration]


def _git_init(repo: Path, msg: str = "Initial commit") -> None:
    for cmd in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@nexus"],
        ["git", "config", "user.name", "Nexus Test"],
        ["git", "add", "."],
        ["git", "commit", "-m", msg],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)


@pytest.fixture
def one_file_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A repo with exactly ONE source file -- so the batch this run's
    single manifest-hook failure applies to has exactly one document,
    matching the bead's own acceptance wording ("fault-inject the
    manifest hook for one document")."""
    repo = tmp_path_factory.mktemp("nexus-wbfpw29-repo")
    (repo / "only.py").write_text(
        "def greet(name):\n"
        "    return f'hello {name}'\n"
    )
    _git_init(repo, "Initial commit")
    return repo


@pytest.fixture(autouse=True)
def _clean_collectors():
    from nexus.commands._helpers import reset_identity_drop_collectors

    reset_identity_drop_collectors()
    yield
    reset_identity_drop_collectors()


def test_heal_manifest_gaps_genuinely_short_rebuild_is_reconciled(
    tmp_path: Path,
) -> None:
    """nexus-wbfpw.29: drives the REAL ``heal_manifest_gaps`` against a
    document registered with ``chunk_count=2`` while T3 holds only ONE
    unique-content chunk for it (a genuine shortfall, not RDR-108
    duplicate collapse). The partial manifest is still written and counted
    in ``reconciled``, and the shortfall is tracked in ``dup_collapsed``.
    Whether a write failure counts as repaired is decided elsewhere, by
    reading the manifest back after the run
    (``commands._helpers.resolve_confirmed_write_failure_doc_ids``).
    """
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.catalog.manifest_heal import heal_manifest_gaps
    from nexus.db import make_t3

    collection = "docs__wbfpw29-short-heal-gate__bge-base-en-v15-768__v1"
    content_hash = "ee" * 32
    chash = "ff" * 32

    with HttpCatalogClient() as cat:
        owner = cat.register_owner(
            "wbfpw29-short-heal-owner", "repo", repo_hash="wbfpw29-short-heal-hash",
        )
        # Registered chunk_count=2, but only ONE real, unique-content
        # chunk will ever land in T3 for this content_hash -- a genuine
        # shortfall, not RDR-108 duplicate-content collapse.
        doc_id = str(cat.register(
            owner, "Short Heal Gate Doc",
            content_type="pdf", physical_collection=collection,
            chunk_count=2, meta={"content_hash": content_hash},
        ))

    seed_chunks_direct(
        collection,
        ids=[chash],
        documents=["nexus-wbfpw29 short-heal gate content -- only one real chunk"],
        embed=True,
        metadatas=[{
            "content_hash": content_hash,
            "chunk_text_hash": chash,
            "chunk_start_char": 0, "chunk_end_char": 10,
            "line_start": 0, "line_end": 0,
        }],
    )

    reader = make_catalog_reader()
    entries = [e for e in reader.all_documents() if str(e.tumbler) == doc_id]
    assert len(entries) == 1

    result = heal_manifest_gaps(entries, reader, make_t3, make_catalog_writer)

    # The write happened -- self-heal did real, useful work.
    assert result.reconciled == 1
    # The shortfall against the registered chunk_count is tracked, not
    # hidden -- but this function no longer tries to render a verdict
    # ("confirmed" vs not) about it; that verdict is computed downstream
    # by reading the manifest back after the whole run.
    assert result.dup_collapsed == 1

    manifest_after = make_catalog_reader().get_manifest(doc_id)
    assert len(manifest_after) == 1, (
        f"the manifest write itself must still have happened: "
        f"{len(manifest_after)} row(s)"
    )


@pytest.mark.parametrize(
    "expected_extra, repaired",
    [
        pytest.param(["56" * 32], False, id="expected-chash-missing-fails"),
        pytest.param([], True, id="expected-chashes-all-present-restores"),
    ],
)
def test_manifest_write_failure_verdict_reads_the_manifest_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    expected_extra: list[str], repaired: bool,
) -> None:
    """nexus-wbfpw.29: drives ``nx index repo`` against a document under
    the run's own owner that is registered with ``chunk_count=2`` while T3
    holds one of its chunks, so the real owner-scoped self-heal rebuilds a
    one-row manifest. A write failure for it is recorded through
    ``_record_manifest_write_failure`` (the function every real producer
    calls), with the chashes that write was trying to put in the manifest,
    injected after ``index_repo_cmd``'s own per-run collector reset.

    The verdict comes from reading the manifest back after the run. When
    the recorded expectation holds a chash the rebuilt manifest lacks, the
    run fails with the reconcile remedy. When every expected chash is
    present, the same run reports the failure as restored and exits 0,
    which proves the comparison ran rather than an UNKNOWN expectation
    failing the run by default.
    """
    from click.testing import CliRunner

    from nexus.catalog.factory import make_catalog_reader
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.cli import main
    from nexus.repo_identity import _repo_identity

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)

    repo = tmp_path / "shortheal-owner-repo"
    repo.mkdir()
    (repo / "only.py").write_text("def greet(name):\n    return f'hello {name}'\n")
    _git_init(repo, "Initial commit")

    runner = CliRunner()
    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        first = runner.invoke(main, ["index", "repo", str(repo)])
    assert first.exit_code == 0, first.output

    _, repo_hash = _repo_identity(repo)
    collection = "docs__wbfpw29-shortheal-e2e__bge-base-en-v15-768__v1"
    content_hash = "12" * 32
    chash = "34" * 32

    with HttpCatalogClient() as cat:
        owner = cat.owner_for_repo(repo_hash)
        assert owner is not None, "pass 1 must have registered an owner for this repo"
        seed_doc_id = str(cat.register(
            owner, "Short Heal E2E Gate Doc",
            content_type="pdf", physical_collection=collection,
            chunk_count=2, meta={"content_hash": content_hash},
        ))

    seed_chunks_direct(
        collection,
        ids=[chash],
        documents=["nexus-wbfpw29 short-heal e2e gate content -- one real chunk"],
        embed=True,
        metadatas=[{
            "content_hash": content_hash,
            "chunk_text_hash": chash,
            "chunk_start_char": 0, "chunk_end_char": 10,
            "line_start": 0, "line_end": 0,
        }],
    )

    from nexus.commands import _helpers as helpers_mod
    from nexus.mcp_infra import _record_manifest_write_failure

    real_reset = helpers_mod.reset_identity_drop_collectors

    def reset_then_record_seed_failure():
        real_reset()
        # Injects the SAME record a real manifest-write hook makes for a
        # persistent failure -- here for a document this run's own file
        # set never touches, standing in for the compound "a genuinely
        # short pre-existing gap ALSO picks up a fresh write failure this
        # run" fault this fix targets.
        _record_manifest_write_failure(seed_doc_id, [chash, *expected_extra])

    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        monkeypatch.setattr(
            helpers_mod, "reset_identity_drop_collectors",
            reset_then_record_seed_failure,
        )
        result = runner.invoke(main, ["index", "repo", str(repo)])

    if repaired:
        assert result.exit_code == 0, result.output
        assert "restored by self-heal" in result.output, result.output
    else:
        assert result.exit_code != 0, result.output
        assert "catalog manifest write failed for 1 document(s)" in result.output
        assert seed_doc_id in result.output, result.output
        assert "restored by self-heal" not in result.output, result.output
        error_lines = [ln for ln in result.output.splitlines() if ln.startswith("Error:")]
        assert len(error_lines) == 1, result.output
        assert "nx catalog reconcile" in error_lines[0], error_lines[0]
        assert "--force" not in error_lines[0], error_lines[0]

    # Self-heal wrote the one-row manifest in both cases.
    manifest = make_catalog_reader().get_manifest(seed_doc_id)
    assert len(manifest) == 1, (
        f"expected the genuinely partial manifest write to have happened: "
        f"{len(manifest)} row(s)"
    )


def test_reconcile_is_the_remedy_the_warning_actually_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """critic Significant: the warning tells the operator to run
    'nx catalog reconcile' -- prove that command is what actually repairs
    the gap this bead's exit-code check reports, not merely a plausible-
    sounding pointer.

    DESIGN NOTE (investigated at length before landing this shape): the
    obvious approach -- trigger the gap by actually firing a faulty
    manifest_write_batch_hook through a live `nx index repo`/`nx index
    pdf` run -- turns out NOT to reach a genuine, persisting gap for
    either verb, for two DIFFERENT and unrelated reasons:
      * `nx index repo`'s default ChunkBatcher path writes chunks +
        manifest ATOMICALLY in one combined POST and explicitly EXCLUDES
        manifest_write_batch_hook from firing at all there (indexer.py's
        `_fire_flush_grain_hooks`, `skip_hooks={manifest_write_batch_hook}`)
        -- a hook failure on that path is a no-op, not a gap.
      * `nx index pdf`'s small-document path DOES fire the real hook, but
        RUNFENCE's own `_fence_complete` (nexus-5xn3k, predates this bead)
        already fails the run loudly with `IndexRunVerifyRefused` the
        instant the manifest comes back empty -- a real gap there never
        survives long enough to reconcile.
    Both are good news for RDR-192 (fewer live-reachable gap scenarios
    than assumed) but neither gives this test a real run to trigger from.
    So this test constructs the gap the bead's own collector describes
    DIRECTLY at the data layer -- a document registered with chunk_count
    > 0, its chunk genuinely present in T3 with matching content_hash
    metadata, and NO manifest row -- the exact shape
    `catalog/manifest_heal.py`'s own gap-detection (`len(manifest) <
    chunk_count`) is written against, and proves 'nx catalog reconcile'
    (the literal command the warning prints) is what repairs it, end to
    end through the real CLI.
    """
    from click.testing import CliRunner

    from nexus.catalog.factory import make_catalog_reader
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.cli import main

    collection = "docs__wbfpw29-reconcile-gate__bge-base-en-v15-768__v1"
    content_hash = "aa" * 32
    chash = "bb" * 32

    with HttpCatalogClient() as cat:
        owner = cat.register_owner(
            "wbfpw29-reconcile-owner", "repo", repo_hash="wbfpw29-reconcile-hash",
        )
        doc_id = str(cat.register(
            owner, "Reconcile Gate Doc",
            content_type="pdf", physical_collection=collection,
            chunk_count=1, meta={"content_hash": content_hash},
        ))

    # The chunk genuinely lands in T3 -- this is what a manifest-hook
    # failure alone would otherwise leave stranded: content present,
    # searchable, but with no document_chunks row linking it back.
    seed_chunks_direct(
        collection,
        ids=[chash],
        documents=["nexus-wbfpw29 reconcile gate content"],
        embed=True,
        metadatas=[{
            "content_hash": content_hash,
            "chunk_text_hash": chash,
            "chunk_start_char": 0, "chunk_end_char": 10,
            "line_start": 0, "line_end": 0,
        }],
    )

    reader = make_catalog_reader()
    manifest_before = reader.get_manifest(doc_id)
    assert manifest_before == [], (
        f"the manifest gap must genuinely exist for this test to prove "
        f"anything: got {len(manifest_before)} row(s)"
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        reconcile_result = runner.invoke(main, ["catalog", "reconcile"])

    assert reconcile_result.exit_code == 0, reconcile_result.output
    assert "Reconciled 1 document(s)" in reconcile_result.output, reconcile_result.output

    manifest_after = make_catalog_reader().get_manifest(doc_id)
    assert len(manifest_after) == 1, (
        f"expected 'nx catalog reconcile' to fully rebuild the manifest: "
        f"manifest rows after={len(manifest_after)}"
    )
    assert manifest_after[0].chash == chash


def _fake_pdf_extraction_result():
    from nexus.pdf_extractor import ExtractionResult

    text = "Page 0 nexus-wbfpw29 channel-4 content.\n"
    return ExtractionResult(
        text=text,
        metadata={
            "extraction_method": "docling", "page_count": 1,
            "page_boundaries": [
                {"page_number": 1, "start_char": 0, "page_text_length": len(text)}
            ],
            "table_regions": [], "format": "markdown",
        },
    )


def _fake_pdf_extract_side_effect(result):
    def extract(pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None, **kwargs):
        if on_page:
            on_page(0, "Page 0 nexus-wbfpw29 channel-4 content.", {"page_number": 1})
        return result
    return extract


def _fake_pdf_chunks():
    from nexus.pdf_chunker import TextChunk

    return [
        TextChunk(
            text="nexus-wbfpw29 channel-4 unique chunk 0",
            chunk_index=0, metadata={"page": 1},
        )
    ]


def test_index_pdf_small_document_no_longer_depends_on_the_manifest_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The small-document PDF path (``index_pdf``, ``streaming="never"``) used to be the OTHER
    dispatch site the manifest hook occupied unconditionally: ``hooks.fire_batch(...)`` carried no
    ``grain=`` override, so it fired the real ``manifest_write_batch_hook``, and a fault in the hook's
    catalog gate (``mcp_infra.get_catalog`` raising) was only caught, loudly, by RUNFENCE's completion
    refusal (``IndexRunVerifyRefused``, referenced=0). Since RDR-223 (nexus-z0o2p.15) the chunks and
    their owner rows are one request and the hook is dropped from this path, so the same fault cannot
    reach the write: the run succeeds, the manifest is whole and the document is stamped complete.
    (The markdown twin is ``test_index_markdown_no_longer_depends_on_the_manifest_hook``.)

    It FAILS if the hook stays on the path: the faulting ``get_catalog`` makes the hook record a
    manifest write failure for the document, so an empty collector is the proof the hook never ran.
    """
    from nexus.catalog.factory import make_catalog_reader
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import _register_or_lookup_doc_id, index_pdf

    import nexus.mcp_infra as mcp_infra

    collection = "docs__wbfpw29-channel4-gate__bge-base-en-v15-768__v1"
    pdf_path = tmp_path / "channel4.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 nexus-wbfpw29 fake content\n")

    doc_id = _register_or_lookup_doc_id(
        pdf_path, "wbfpw29-channel4-gate",
        content_type="pdf", physical_collection=collection,
    )
    assert doc_id, "catalog registration must succeed against the real service"

    def faulting_get_catalog():
        raise RuntimeError("nexus-wbfpw.29 fault injection (channel 4)")

    monkeypatch.setattr(mcp_infra, "get_catalog", faulting_get_catalog)
    # Arm the collectors the hook records into (they are no-ops until a run resets them).
    mcp_infra.reset_manifest_write_failures()
    mcp_infra.reset_manifest_identity_drops()

    manifest_writes: list[str] = []
    real_post = HttpCatalogClient._post

    def counting_post(self, path, body=None, **kw):
        if path in ("/manifest/write_many", "/manifest/append", "/manifest/replace"):
            manifest_writes.append(path)
        return real_post(self, path, body, **kw)

    monkeypatch.setattr(HttpCatalogClient, "_post", counting_post)

    t3 = HttpVectorClient()
    with patch("nexus.doc_indexer.PDFExtractor") as ME, \
         patch("nexus.doc_indexer.PDFChunker") as MC:
        ME.return_value.extract.side_effect = _fake_pdf_extract_side_effect(
            _fake_pdf_extraction_result()
        )
        MC.return_value.chunk.return_value = _fake_pdf_chunks()
        n = index_pdf(
            pdf_path, "wbfpw29-channel4-gate", t3=t3,
            collection_name=collection, streaming="never",
        )

    assert n == 1
    # The hook did not run: had it, its faulting catalog gate would have recorded this document.
    assert mcp_infra.get_manifest_write_failures() == []
    assert mcp_infra.get_manifest_identity_drops() == []
    assert manifest_writes == ["/manifest/write_many"], "the combined write is the only manifest write"
    reader = make_catalog_reader()
    assert reader is not None
    assert len(reader.get_manifest(doc_id)) == 1
    entry = reader.resolve(doc_id)
    assert entry is not None and entry.index_state == "complete"


def test_index_markdown_no_longer_depends_on_the_manifest_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``index_markdown`` (backing ``nx index md``, ``nx index rdr``,
    ``nx dt index``'s markdown records and ``nx collection reindex``) delegates
    to ``_index_document``. Until RDR-223 (nexus-z0o2p.13) that function wrote
    its chunks first and left the manifest to ``manifest_write_batch_hook``, so
    a fault in the hook's catalog gate was only caught, loudly, by the RUNFENCE
    completion refusal (``IndexRunVerifyRefused``, referenced=0). The chunks and
    their owner rows are now one request and the hook is dropped from this
    path, so the same fault (``get_catalog`` raising, which is the hook's own
    gate) cannot reach the write: the run succeeds, the manifest is whole and the
    document is stamped complete. This pins that the hook is off the path; the
    fence's refusal contract keeps its own coverage on the paths that still use
    the hook.

    It FAILS if the hook stays on the path: the faulting ``get_catalog`` makes the hook
    record a manifest write failure for the document (the collector the CLI exit code
    reads), so an empty collector is the proof the hook never ran. It also asserts the
    write itself was the only manifest write.
    """
    from nexus.catalog.factory import make_catalog_reader
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import _register_or_lookup_doc_id, index_markdown

    import nexus.mcp_infra as mcp_infra

    collection = "docs__wbfpw29-channel-md-gate__bge-base-en-v15-768__v1"
    md_path = tmp_path / "channel-md.md"
    md_path.write_text("# Title\n\nSome markdown content for nexus-wbfpw29.\n")

    doc_id = _register_or_lookup_doc_id(
        md_path, "wbfpw29-channel-md-gate",
        content_type="prose", physical_collection=collection,
    )
    assert doc_id, "catalog registration must succeed against the real service"

    def faulting_get_catalog():
        raise RuntimeError("nexus-wbfpw.29 fault injection (markdown channel)")

    monkeypatch.setattr(mcp_infra, "get_catalog", faulting_get_catalog)
    # Arm the collectors the hook records into (they are no-ops until a run resets them).
    mcp_infra.reset_manifest_write_failures()
    mcp_infra.reset_manifest_identity_drops()

    manifest_writes: list[str] = []
    real_post = HttpCatalogClient._post

    def counting_post(self, path, body=None, **kw):
        if path in ("/manifest/write_many", "/manifest/append", "/manifest/replace"):
            manifest_writes.append(path)
        return real_post(self, path, body, **kw)

    monkeypatch.setattr(HttpCatalogClient, "_post", counting_post)

    t3 = HttpVectorClient()
    n = index_markdown(
        md_path, "wbfpw29-channel-md-gate", t3=t3, collection_name=collection,
    )

    assert n == 1
    # The hook did not run: had it, its faulting catalog gate would have recorded this document.
    assert mcp_infra.get_manifest_write_failures() == []
    assert mcp_infra.get_manifest_identity_drops() == []
    assert manifest_writes == ["/manifest/write_many"], "the combined write is the only manifest write"
    reader = make_catalog_reader()
    assert reader is not None
    assert len(reader.get_manifest(doc_id)) == 1
    entry = reader.resolve(doc_id)
    assert entry is not None and entry.index_state == "complete"


def test_non_manifest_hook_exception_does_not_change_exit_code(
    one_file_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure in a DIFFERENT batch hook (taxonomy-assign) must stay
    best-effort exactly as before this bead -- it is unrelated to
    nexus-wbfpw.29's manifest-specific routing and must not change the
    run's exit code by way of it."""
    from click.testing import CliRunner

    from nexus.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)

    import nexus.mcp_infra as mcp_infra

    def faulty_taxonomy_hook(*args, **kwargs):
        raise RuntimeError("unrelated taxonomy-assign failure")

    monkeypatch.setattr(mcp_infra, "taxonomy_assign_batch_hook", faulty_taxonomy_hook)

    runner = CliRunner()
    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        result = runner.invoke(main, ["index", "repo", str(one_file_repo)])

    assert result.exit_code == 0, result.output
    assert "catalog manifest write failed" not in result.output
