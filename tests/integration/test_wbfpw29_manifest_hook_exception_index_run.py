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


def _git_commit_all(repo: Path, msg: str) -> None:
    for cmd in (["git", "add", "."], ["git", "commit", "-m", msg]):
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


def _code_fixture_lines(n_functions: int) -> str:
    """A code file whose chunk count reliably exceeds a cap of 1 (and
    stays well above the default 150-line/chunk window, so it chunks
    into several pieces even under normal caps)."""
    return "\n".join(
        f"def fn_{i}(x):\n"
        f"    \"\"\"Function {i} of the wbfpw29 channel-3 fixture.\"\"\"\n"
        f"    return x + {i}\n"
        for i in range(n_functions)
    )


def _channel3_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, name: str) -> tuple[Path, Path]:
    """Force ``ChunkBatcher.add()`` to REJECT a fixture file, routing it
    through ``code_indexer.py``'s legacy per-file fallback -- the ONE call
    site where the real, unpatched ``manifest_write_batch_hook`` fires
    unconditionally for `nx index repo` (see the acceptance test's own
    docstring for the full reachability argument). Returns (repo,
    fixture_path)."""
    import nexus.db.http_vector_client as http_vector_client

    # per_collection_chunk_cap() re-reads this module global on every
    # call (it is NOT captured at import time by any caller) -- see its
    # own onnx-local branch, `return _ONNX_LOCAL_UPSERT_CHUNK_CAP`.
    monkeypatch.setattr(http_vector_client, "_ONNX_LOCAL_UPSERT_CHUNK_CAP", 1)

    repo = tmp_path / name
    repo.mkdir()
    fixture_path = repo / "big.py"
    fixture_path.write_text(_code_fixture_lines(80))
    _git_init(repo, "Initial commit")
    return repo, fixture_path


def test_manifest_hook_exception_fails_run_names_doc_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bead's acceptance scenario, driven through the REAL production
    dispatch site the manifest hook actually occupies for `nx index repo`
    -- channel 3, code_indexer.py's legacy per-file fallback.

    ROUND-2 REWRITE (critic Critical): the original version of this test
    replaced ``mcp_infra.manifest_write_batch_hook`` wholesale with a bare
    function. That double has no ``batch_grain`` attribute, so
    ``HookRegistry``'s classification (``getattr(hook, "batch_grain",
    "file")``) silently defaulted it to "file" -- a bucket
    (``indexer.py``'s per-file ``_fire_deferred_hooks``) the REAL,
    correctly-classified hook (``batch_grain = "flush"``) never occupies
    for a ChunkBatcher-ACCEPTED file. The test passed, but for a dispatch
    the real hook cannot reach.

    This version instead forces ``ChunkBatcher.add()`` to REJECT the
    fixture file (monkeypatching the onnx-local chunk cap down to 1, so
    the file's several chunks exceed it), which routes the file through
    ``code_indexer.py``'s legacy per-file fallback
    (``ctx.hooks.fire_batch(ids, ctx.corpus, ..., catalog_doc_id=...)``,
    NO ``grain=`` override, NO ``skip_hooks=``) -- the ONE call site where
    the real, unpatched ``manifest_write_batch_hook`` fires unconditionally
    for `nx index repo`. Confirmed the dominant channel under onnx-local
    (the default local/dev mode): ``per_collection_chunk_cap``'s onnx-local
    branch applies its cap to EVERY prefix including code (nexus-33hpq), so
    any ordinary source file whose chunk count exceeds the (default 16, here
    forced to 1) cap takes this path -- not a rare/legacy corner.

    Fault injection targets the hook's OWN dependency
    (``nexus.mcp_infra.get_catalog``, one of the exact seams round 1's
    unit tests already used against the real, unpatched function) instead
    of replacing the hook object, so the hook keeps its real identity and
    classification throughout. A call-log on the faulted ``get_catalog``
    is the dispatch-trace proof this test needs: ``get_catalog()`` is
    called from exactly one place during ordinary `nx index repo` traffic
    for a code file -- inside ``manifest_write_batch_hook`` itself -- so a
    non-empty log is direct proof the REAL hook executed, not a
    plausible-looking double standing in for it.

    ROUND-3 REWRITE (critic Critical): ``nx index repo``'s own SAME-RUN
    manifest self-heal pass (indexer.py, nexus-c21fk) reads the catalog
    via its OWN ``make_catalog_reader()`` call -- a DIFFERENT import path
    than ``mcp_infra.get_catalog()``, which is all this test faults -- so
    self-heal is HEALTHY here and genuinely repairs the exact gap the
    faulted hook just left, in the SAME run. Before round 3's fix, the
    run still failed non-zero with a stale "run nx catalog reconcile"
    remedy for a document that no longer needed it. Now: the run exits 0,
    with an INFORMATIONAL "restored by self-heal" line naming the doc --
    not a failure. The companion test below
    (``test_manifest_hook_exception_when_self_heal_is_also_faulted``)
    covers the case where self-heal genuinely cannot repair the gap.
    """
    from click.testing import CliRunner

    import nexus.mcp_infra as mcp_infra
    from nexus.cli import main
    from tests._catalog_fixture_ops import only_document

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)

    repo, fixture_path = _channel3_repo(tmp_path, monkeypatch, name="channel3-repo")

    call_log: list[str] = []
    original_get_catalog = mcp_infra.get_catalog

    def faulting_get_catalog():
        call_log.append("get_catalog")
        raise RuntimeError("nexus-wbfpw.29 fault injection (channel 3)")

    runner = CliRunner()
    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        monkeypatch.setattr(mcp_infra, "get_catalog", faulting_get_catalog)
        first = runner.invoke(main, ["index", "repo", str(repo)])

    # DISPATCH TRACE: proves the REAL manifest_write_batch_hook executed
    # (its body is the only caller of get_catalog() on this run's path),
    # not merely that fire_batch's generic exception handling works.
    assert call_log, (
        "get_catalog() was never called -- the real manifest_write_batch_hook "
        "never fired, so channel 3 was not actually reached this run "
        "(check the chunk-cap monkeypatch and the fixture's chunk count)"
    )

    doc = only_document()

    # nexus-wbfpw.29 round 3: self-heal is healthy on this path (it never
    # calls the faulted mcp_infra.get_catalog), so it repairs the gap in
    # THIS SAME run -- the run must exit 0 with an informational note,
    # never the "run nx catalog reconcile" failure this bead's round-1/2
    # fix used to print unconditionally for a document that no longer
    # needs it.
    assert first.exit_code == 0, first.output
    assert "restored by self-heal in this same run" in first.output
    assert str(doc.tumbler) in first.output, (
        f"expected the restored document's tumbler {doc.tumbler!r} to be "
        f"named in the run's output:\n{first.output}"
    )
    assert "run 'nx catalog reconcile'" not in first.output.lower(), (
        "the manifest was already restored by self-heal -- the run must "
        "not point the operator at a no-op remedy"
    )
    assert doc.chunk_count > 0

    # Sanity: an unfaulted, genuinely-changed re-index still succeeds
    # normally afterward.
    monkeypatch.setattr(mcp_infra, "get_catalog", original_get_catalog)
    fixture_path.write_text(_code_fixture_lines(80) + "\ndef fn_extra(x):\n    return x - 1\n")
    _git_commit_all(repo, "modify big.py")

    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        second = runner.invoke(main, ["index", "repo", str(repo)])

    assert second.exit_code == 0, second.output


def test_manifest_hook_exception_when_self_heal_is_also_faulted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-wbfpw.29 round 3 (critic Critical), the OTHER half: when the
    SAME run's self-heal pass cannot repair the gap either (here: its
    core, ``heal_manifest_gaps``, is faulted separately from the manifest
    hook -- a genuinely independent failure, not the common case, but the
    one this bead's fail-loud check exists for), the run must still exit
    non-zero, name the document, and print the ORIGINAL "run nx catalog
    reconcile" remedy -- because that remedy is now actually true. The
    remedy is proven to actually work by the separate
    ``test_reconcile_is_the_remedy_the_warning_actually_names`` test.
    """
    from click.testing import CliRunner

    import nexus.catalog.manifest_heal as manifest_heal
    import nexus.mcp_infra as mcp_infra
    from nexus.catalog.manifest_heal import ManifestHealResult
    from nexus.cli import main
    from tests._catalog_fixture_ops import only_document

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)

    repo, _fixture_path = _channel3_repo(tmp_path, monkeypatch, name="channel3-repo-noheal")

    def faulting_get_catalog():
        raise RuntimeError("nexus-wbfpw.29 fault injection (channel 3)")

    def noop_heal(*args, **kwargs):
        # Genuinely finds/repairs nothing -- the gap survives.
        return ManifestHealResult()

    runner = CliRunner()
    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        monkeypatch.setattr(mcp_infra, "get_catalog", faulting_get_catalog)
        monkeypatch.setattr(manifest_heal, "heal_manifest_gaps", noop_heal)
        result = runner.invoke(main, ["index", "repo", str(repo)])

    doc = only_document()

    assert result.exit_code != 0, result.output
    assert "catalog manifest write failed for 1 document(s)" in result.output
    assert str(doc.tumbler) in result.output, (
        f"expected the still-failing document's tumbler {doc.tumbler!r} "
        f"to be named in the run's output:\n{result.output}"
    )
    assert "run 'nx catalog reconcile'" in result.output.lower(), (
        "the gap genuinely was not repaired this run -- the remedy must "
        "still be printed"
    )
    assert "restored by self-heal" not in result.output


def test_heal_manifest_gaps_reports_which_documents_it_reconciled(
    tmp_path: Path,
) -> None:
    """nexus-wbfpw.29 round 3: ``ManifestHealResult.reconciled`` was a
    bare COUNT with no way to identify which documents it covered --
    closing the self-heal/exit-check coordination gap needed the actual
    doc_ids. Proves ``reconciled_doc_ids`` is populated (and agrees with
    the count) against the same directly-seeded gap shape
    ``test_reconcile_is_the_remedy_the_warning_actually_names`` uses,
    calling ``heal_manifest_gaps`` directly rather than through the CLI.
    """
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.catalog.manifest_heal import heal_manifest_gaps
    from nexus.db import make_t3
    from nexus.db.http_vector_client import HttpVectorClient

    collection = "docs__wbfpw29-heal-ids-gate__bge-base-en-v15-768__v1"
    content_hash = "cc" * 32
    chash = "dd" * 32

    with HttpCatalogClient() as cat:
        owner = cat.register_owner(
            "wbfpw29-heal-ids-owner", "repo", repo_hash="wbfpw29-heal-ids-hash",
        )
        doc_id = str(cat.register(
            owner, "Heal Ids Gate Doc",
            content_type="pdf", physical_collection=collection,
            chunk_count=1, meta={"content_hash": content_hash},
        ))

    HttpVectorClient().upsert_chunks_with_embeddings(
        collection_name=collection,
        ids=[chash],
        documents=["nexus-wbfpw29 heal-ids gate content"],
        embeddings=[[]],
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

    assert result.reconciled == 1
    assert result.reconciled_doc_ids == [doc_id]


def _prose_fixture_text(n_paragraphs: int) -> str:
    """A markdown file whose chunk count reliably exceeds a cap of 1."""
    return "\n\n".join(
        f"## Section {i}\n\nParagraph {i} of the wbfpw29 channel-3 prose "
        f"fixture, with enough distinct content to force its own chunk "
        f"under the semantic markdown chunker."
        for i in range(n_paragraphs)
    )


def test_prose_indexer_channel3_manifest_hook_exception_fails_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """critic Minor (round 3): mirrors the code_indexer channel-3
    acceptance test for ``prose_indexer.py::index_prose_file``'s
    structurally-identical legacy per-file fallback (same
    ``ctx.hooks.fire_batch(...)`` call shape, no ``grain=``, no
    ``skip_hooks=``, confirmed by direct code comparison in round 3's
    review) -- proving the real dispatch site end to end for prose, not
    just inferring it from the shared mechanism. Self-heal is healthy
    here too (same reasoning as the code test), so the correct outcome is
    exit 0 with the restored-by-self-heal note.
    """
    from click.testing import CliRunner

    import nexus.mcp_infra as mcp_infra
    from nexus.cli import main
    from tests._catalog_fixture_ops import only_document

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)

    repo, _fixture_path = _channel3_repo(tmp_path, monkeypatch, name="channel3-prose-repo")
    # Remove the code fixture from the shared helper's repo -- this test
    # wants exactly ONE prose file, not a code+prose mix.
    (repo / "big.py").unlink()
    (repo / "big.md").write_text(_prose_fixture_text(60))
    _git_commit_all(repo, "swap fixture for prose")

    call_log: list[str] = []

    def faulting_get_catalog():
        call_log.append("get_catalog")
        raise RuntimeError("nexus-wbfpw.29 fault injection (prose channel 3)")

    runner = CliRunner()
    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        monkeypatch.setattr(mcp_infra, "get_catalog", faulting_get_catalog)
        result = runner.invoke(main, ["index", "repo", str(repo)])

    assert call_log, (
        "get_catalog() was never called -- the real manifest_write_batch_hook "
        "never fired for the prose file; channel 3 was not reached "
        "(check the fixture's chunk count under the forced cap)"
    )

    doc = only_document()
    assert result.exit_code == 0, result.output
    assert "restored by self-heal in this same run" in result.output
    assert str(doc.tumbler) in result.output


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
    from nexus.db.http_vector_client import HttpVectorClient

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
    HttpVectorClient().upsert_chunks_with_embeddings(
        collection_name=collection,
        ids=[chash],
        documents=["nexus-wbfpw29 reconcile gate content"],
        embeddings=[[]],  # server-embeds
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


def test_manifest_hook_exception_via_doc_indexer_pdf_channel_runfence_already_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """critic round-2 (code-review Important): covers the OTHER real
    dispatch site the manifest hook occupies unconditionally --
    doc_indexer.py's small-document PDF path (`index_pdf`,
    streaming="never"): ``hooks.fire_batch(...)`` there carries no
    ``grain=`` override at all, so it fires every hook including the real
    ``manifest_write_batch_hook`` regardless of its own ``batch_grain``
    classification (unlike channel 3's caller, whose grain filtering is
    exactly what round 1's test-artifact bug hinged on).

    Demonstrates why THIS bead's own collector-based signal is not what
    surfaces a failure on this channel: RUNFENCE's own, INDEPENDENT,
    PRE-EXISTING ``_fence_complete`` call (nexus-5xn3k, predates
    nexus-wbfpw.29 entirely) already raises ``IndexRunVerifyRefused`` the
    instant the manifest write comes back with zero referenced rows for a
    claimed non-zero chunk count -- a different, older, and LOUDER failure
    mode that fires before this bead's exit-code check is ever consulted
    on this channel. Fault injection is the SAME seam as channel 3
    (``mcp_infra.get_catalog`` raising), driving the REAL, unpatched hook.
    """
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import _register_or_lookup_doc_id, index_pdf
    from nexus.errors import IndexRunVerifyRefused

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

    t3 = HttpVectorClient()
    with patch("nexus.doc_indexer.PDFExtractor") as ME, \
         patch("nexus.doc_indexer.PDFChunker") as MC:
        ME.return_value.extract.side_effect = _fake_pdf_extract_side_effect(
            _fake_pdf_extraction_result()
        )
        MC.return_value.chunk.return_value = _fake_pdf_chunks()

        with pytest.raises(IndexRunVerifyRefused) as excinfo:
            index_pdf(
                pdf_path, "wbfpw29-channel4-gate", t3=t3,
                collection_name=collection, streaming="never",
            )

    # RUNFENCE's own counts prove the manifest was genuinely never
    # written this run (referenced=0) despite one real chunk landing --
    # exactly the shape this bead's collectors describe, caught here by
    # an entirely separate, pre-existing mechanism.
    assert excinfo.value.doc_id == doc_id
    assert excinfo.value.referenced == 0
    assert excinfo.value.chunk_count == 1


def test_index_markdown_manifest_hook_exception_runfence_already_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """critic Significant: ``index_markdown`` (doc_indexer.py ~3498-3681
    -- backing ``nx dt index``'s markdown records, ``nx collection
    reindex``, and standalone RDR indexing) was never traced by either
    prior round. Grepping its OWN function body for
    ``fire_batch``/``manifest_write_batch_hook`` finds nothing because
    ``index_markdown`` does not fire hooks itself at all -- it delegates
    its entire body to ``_index_document`` (doc_indexer.py:1591), the
    SAME shared pipeline function ``index_pdf``'s OTHER (non-small-doc)
    paths use. ``_index_document`` fires ``hooks.fire_batch(...)`` with
    NO ``grain=`` override (real hook fires unconditionally, same as the
    channel-4 PDF test above) and then calls
    ``_fence_complete(_catalog_doc_id_for_batch, content_hash,
    len(prepared))`` -- byte-for-byte the same RUNFENCE backstop. A
    manifest-hook failure during markdown/RDR/dt-markdown indexing is
    therefore ALREADY loud via the identical pre-existing mechanism, not
    an unresolved or silent gap.
    """
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import _register_or_lookup_doc_id, index_markdown
    from nexus.errors import IndexRunVerifyRefused

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

    t3 = HttpVectorClient()
    with pytest.raises(IndexRunVerifyRefused) as excinfo:
        index_markdown(
            md_path, "wbfpw29-channel-md-gate", t3=t3, collection_name=collection,
        )

    assert excinfo.value.doc_id == doc_id
    assert excinfo.value.referenced == 0
    assert excinfo.value.chunk_count == 1


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
