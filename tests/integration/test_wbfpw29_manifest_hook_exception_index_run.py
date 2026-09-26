# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wbfpw.29 (RDR-192 Step 3b, client) acceptance test: a
manifest-hook EXCEPTION during ``nx index repo`` is recorded per document
and fails the run.

Drives the PRODUCTION entry point (``nx index repo`` via the real Click
CLI, ``nexus.hook_registry.HookRegistry.fire_batch`` unmocked) against
the shared engine substrate every test in this suite already boots
(``tests/conftest.py``'s autouse ``_pin_t2_substrate``) — same shape as
``tests/test_indexer_e2e.py::test_cli_index_repo``. Fault injection is at
the one seam the bead names: ``nexus.mcp_infra.manifest_write_batch_hook``
itself is replaced with a callable that raises, so the failure reaches
``HookRegistry.fire_batch``'s generic except block exactly the way a real
bug in the manifest hook would -- never mocking ``fire_batch`` or the
exit-check machinery under test.

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


def test_manifest_hook_exception_fails_run_names_doc_then_recovers(
    one_file_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bead's own acceptance scenario, verbatim: fault-inject the
    manifest hook for the run's one document; the run exits non-zero and
    names that document; a re-run with the fault removed writes the
    manifest and exits 0."""
    from click.testing import CliRunner

    from nexus.cli import main
    from tests._catalog_fixture_ops import only_document

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)

    import nexus.mcp_infra as mcp_infra

    original_hook = mcp_infra.manifest_write_batch_hook

    def faulty_manifest_hook(*args, **kwargs):
        raise RuntimeError("nexus-wbfpw.29 fault injection")

    runner = CliRunner()
    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        monkeypatch.setattr(mcp_infra, "manifest_write_batch_hook", faulty_manifest_hook)
        first = runner.invoke(main, ["index", "repo", str(one_file_repo)])

        # nexus-7lw6a: the run must fail loud, not report "Done." at
        # rc=0 while the chunks landed with no manifest row.
        assert first.exit_code != 0, first.output
        assert "catalog manifest write failed for 1 document(s)" in first.output

        # The chunks DID get written and the file WAS registered in the
        # catalog (over-work-never-under-work: only the manifest LINKAGE
        # failed) -- so the document's real tumbler is discoverable, and
        # the bead's own wording ("names the documents to re-index")
        # requires it actually appear in the failure output, not just a
        # bare count.
        doc = only_document()
        assert str(doc.tumbler) in first.output, (
            f"expected the failing document's tumbler {doc.tumbler!r} to "
            f"be named in the run's output:\n{first.output}"
        )

        # Remove the fault and force a genuine re-index (content change,
        # not relying on the separate self-heal pass alone) so this
        # run's OWN manifest hook call -- the thing this bead fixes the
        # failure-routing for -- is what proves the recovery. Restore
        # ONLY the patched hook attribute (not monkeypatch.undo(), which
        # would also unwind the autouse engine-substrate env patches
        # sharing this same function-scoped monkeypatch instance).
        monkeypatch.setattr(mcp_infra, "manifest_write_batch_hook", original_hook)
        (one_file_repo / "only.py").write_text(
            "def greet(name):\n"
            "    return f'hello again {name}'\n"
        )
        _git_commit_all(one_file_repo, "modify only.py")

        second = runner.invoke(main, ["index", "repo", str(one_file_repo)])

    assert second.exit_code == 0, second.output
    doc = only_document()
    assert doc.chunk_count > 0


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
