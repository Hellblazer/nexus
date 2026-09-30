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


def test_manifest_hook_exception_self_heals_same_run_then_recovers(
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
    manifest self-heal pass (indexer.py, nexus-c21fk) reaches the catalog
    via its OWN ``make_catalog_reader()`` call site, not through
    ``mcp_infra.get_catalog()`` -- the ONE call this test faults. This is
    a different call SITE, not a different catalog: ``get_catalog()`` is
    itself a one-line wrapper around ``make_catalog_reader()`` (round 4
    critique review), so both resolve to the identical service-backed
    handle over the same engine -- there is no separate cache or snapshot
    for the two to disagree about. What this test actually proves is
    narrower: a fault confined to the ONE round trip inside
    ``get_catalog()``'s own call (a transient failure, or -- as here -- a
    fault injected specifically there) leaves a separate, later round trip
    through the same factory free to succeed moments later, so self-heal
    is HEALTHY here and genuinely repairs the exact gap the faulted hook
    just left, in the SAME run. Before round 3's fix, the run still
    failed non-zero with a stale "run nx catalog reconcile" remedy for a
    document that no longer needed it. Now: the run exits 0, with an
    INFORMATIONAL "restored by self-heal" line naming the doc -- not a
    failure. The companion test below
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


def test_manifest_hook_exception_with_partial_t3_write_fails_with_reconcile_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-wbfpw.29 round 6 (round-5's own documented gap, T2
    critique-wbfpw29-r4 CRITICAL): channel 3's manifest-hook EXCEPTION
    leaves the document's catalog row at ``chunk_count == 0`` (only a
    SUCCESSFUL hook write ever bumps it) -- so round 5's confirmation
    check (``len(rebuilt_chunks) >= entry.chunk_count``) compared against
    zero and trivially passed for ANY rebuild, including one built from a
    T3 write that itself only PARTIALLY landed this same run. This test
    reproduces exactly that compound fault: the manifest hook raises
    (the same ``get_catalog`` fault channel 3's sibling test above uses),
    AND the file's own chunk upload silently drops its LAST chunk before
    it ever reaches T3 (a truncating wrapper around
    ``HttpVectorClient.upsert_chunks_with_embeddings``, independent of
    the hook fault) -- so self-heal's own T3 fetch by content_hash finds
    only the truncated subset and rebuilds a manifest that is genuinely
    short of what this run's own manifest-write attempt was trying to
    record. The run must fail with the reconcile remedy, not report a
    false "restored by self-heal".

    FAILS AT HEAD 0b1022eec (round 5): the run exits 0, because
    ``ManifestHealResult.confirmed_doc_ids``'s ``len(chunks) >=
    entry.chunk_count`` reads ``entry.chunk_count == 0`` for this exact
    document shape and confirms unconditionally.
    """
    from click.testing import CliRunner

    import nexus.db.http_vector_client as http_vector_client
    import nexus.mcp_infra as mcp_infra
    from nexus.cli import main
    from tests._catalog_fixture_ops import only_document

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)

    repo, _fixture_path = _channel3_repo(tmp_path, monkeypatch, name="channel3-partial-t3")

    real_upsert = http_vector_client.HttpVectorClient.upsert_chunks_with_embeddings

    def truncating_upsert(
        self, collection_name, ids, documents, embeddings,
        metadatas=None, **kwargs,
    ):
        # ``**kwargs`` forwards whatever the real signature grows (nexus-w94eo
        # added ``delete_keys``); a pinned signature here raised TypeError
        # inside the index run and the hook under test never fired.
        # Drop the LAST chunk before it ever reaches T3 -- genuinely
        # short, not a duplicate-content collapse (RDR-108's OTHER benign
        # explanation for a manifest shortfall).
        if collection_name.startswith("code__") and len(ids) > 1:
            keep = len(ids) - 1
            ids = ids[:keep]
            documents = documents[:keep]
            embeddings = embeddings[:keep]
            metadatas = (metadatas or [])[:keep]
        return real_upsert(
            self, collection_name, ids, documents, embeddings,
            metadatas=metadatas, **kwargs,
        )

    call_log: list[str] = []

    def faulting_get_catalog():
        call_log.append("get_catalog")
        raise RuntimeError("nexus-wbfpw.29 fault injection (channel 3, partial T3)")

    runner = CliRunner()
    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        monkeypatch.setattr(mcp_infra, "get_catalog", faulting_get_catalog)
        monkeypatch.setattr(
            http_vector_client.HttpVectorClient,
            "upsert_chunks_with_embeddings",
            truncating_upsert,
        )
        result = runner.invoke(main, ["index", "repo", str(repo)])

    assert call_log, (
        "get_catalog() was never called -- the real manifest_write_batch_hook "
        "never fired this run (check the chunk-cap monkeypatch and the "
        "fixture's chunk count)"
    )

    doc = only_document()

    assert result.exit_code != 0, result.output
    assert "catalog manifest write failed for 1 document(s)" in result.output
    assert str(doc.tumbler) in result.output, (
        f"expected the still-failing document's tumbler {doc.tumbler!r} "
        f"to be named in the run's output:\n{result.output}"
    )
    assert "run 'nx catalog reconcile'" in result.output.lower(), (
        "self-heal's own rebuild was genuinely short this run (one chunk "
        "never reached T3 at all) -- the reconcile remedy must still be "
        "printed, not a false restoration claim"
    )
    assert "restored by self-heal" not in result.output, (
        "the rebuilt manifest is missing at least one chash this run's "
        "own manifest-write attempt was trying to record -- it must "
        "never be reported as a completed restoration"
    )

    error_lines = [ln for ln in result.output.splitlines() if ln.startswith("Error:")]
    assert len(error_lines) == 1, result.output
    assert "nx catalog reconcile" in error_lines[0], error_lines[0]


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

    ROUND-5 REWRITE (critique Important): the previous version replaced
    ``manifest_heal.heal_manifest_gaps`` -- the function under test --
    wholesale with a no-op double, which proves only that the exit-code
    wiring reads whatever the function returns, not that a genuine
    internal self-heal failure produces that return. This version instead
    faults ``make_catalog_writer`` (``nexus.catalog.factory``), one of
    ``heal_manifest_gaps``'s own REAL dependencies (its lazy
    ``writer_factory`` param, indexer.py's self-heal call site ~6125-6151)
    -- letting the real function run its real gap detection and its real
    T3 chunk fetch (proving self-heal genuinely FOUND the gap and
    genuinely tried to rebuild it), and fail only at the write step, the
    same way a transient catalog-write outage would in production.

    The fault targets specifically self-heal's own lazy writer closure
    (``_tracked_writer``, indexer.py ~6136) by inspecting the IMMEDIATE
    caller's frame, rather than counting calls: `nx index repo` mints
    several OTHER catalog writers first (the registration phase's own,
    the unconditional migration-writer mint at ~4867), and their exact
    count/order is an implementation detail this test should not need to
    track -- a call-counting version broke the very first time an
    intervening writer call was added elsewhere in the pipeline. Scoping
    by caller name is exact regardless of how many other writers this run
    mints before or after self-heal's own.
    """
    import sys

    from click.testing import CliRunner

    import nexus.catalog.factory as catalog_factory
    import nexus.mcp_infra as mcp_infra
    from nexus.cli import main
    from tests._catalog_fixture_ops import only_document

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)

    repo, _fixture_path = _channel3_repo(tmp_path, monkeypatch, name="channel3-repo-noheal")

    def faulting_get_catalog():
        raise RuntimeError("nexus-wbfpw.29 fault injection (channel 3)")

    real_make_catalog_writer = catalog_factory.make_catalog_writer
    self_heal_writer_faulted = [False]

    def faulting_make_catalog_writer(*args, **kwargs):
        caller_name = sys._getframe(1).f_code.co_name
        if caller_name == "_tracked_writer":
            # self-heal's own lazy writer closure, and only that one.
            self_heal_writer_faulted[0] = True
            raise RuntimeError(
                "nexus-wbfpw.29 fault injection (self-heal catalog writer)"
            )
        return real_make_catalog_writer(*args, **kwargs)

    runner = CliRunner()
    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        monkeypatch.setattr(mcp_infra, "get_catalog", faulting_get_catalog)
        monkeypatch.setattr(
            catalog_factory, "make_catalog_writer", faulting_make_catalog_writer,
        )
        # nexus-0ntxj: the run's exit handler fail-stamps the unfinished
        # document, and _fence_fail now heals it too. This test is about
        # the run where NO repair lands, so that second heal path is
        # faulted as well.
        monkeypatch.setattr(
            "nexus.doc_indexer._heal_failed_document", lambda doc_id: None,
        )
        result = runner.invoke(main, ["index", "repo", str(repo)])

    assert self_heal_writer_faulted[0], (
        "self-heal never reached its own writer-construction call -- "
        "either the gap was never detected or the fault never fired "
        "(check indexer.py's self-heal closure is still named "
        "_tracked_writer)"
    )

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

    # The fail-loud Error line must name the SAME remedy as the WARNING
    # above it. Before this fix it said "re-index with --force", a costly
    # re-embed, while the WARNING (and
    # test_reconcile_is_the_remedy_the_warning_actually_names) establish
    # `nx catalog reconcile` as the repair, rebuilt from T3 directly.
    error_lines = [ln for ln in result.output.splitlines() if ln.startswith("Error:")]
    assert len(error_lines) == 1, result.output
    assert "nx catalog reconcile" in error_lines[0], error_lines[0]
    assert "--force" not in error_lines[0], error_lines[0]


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
    from nexus.db.http_vector_client import HttpVectorClient

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

    HttpVectorClient().upsert_chunks_with_embeddings(
        collection_name=collection,
        ids=[chash],
        documents=["nexus-wbfpw29 short-heal gate content -- only one real chunk"],
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
    from nexus.db.http_vector_client import HttpVectorClient
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

    HttpVectorClient().upsert_chunks_with_embeddings(
        collection_name=collection,
        ids=[chash],
        documents=["nexus-wbfpw29 short-heal e2e gate content -- one real chunk"],
        embeddings=[[]],
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


def _prose_fixture_text(n_paragraphs: int) -> str:
    """A markdown file whose chunk count reliably exceeds a cap of 1."""
    return "\n\n".join(
        f"## Section {i}\n\nParagraph {i} of the wbfpw29 channel-3 prose "
        f"fixture, with enough distinct content to force its own chunk "
        f"under the semantic markdown chunker."
        for i in range(n_paragraphs)
    )


def test_prose_indexer_channel3_manifest_hook_exception_self_heals_same_run(
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
    """
    from nexus.catalog.factory import make_catalog_reader
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

    t3 = HttpVectorClient()
    n = index_markdown(
        md_path, "wbfpw29-channel-md-gate", t3=t3, collection_name=collection,
    )

    assert n == 1
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
