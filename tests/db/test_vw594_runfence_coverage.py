# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-vw594 F1/F2 production-layer gate — the RUNFENCE coverage tests.

Companion to ``tests/db/test_5xn3k_runfence_gate.py`` (the phase-exit gate
for the original 5xn3k fence arc, which drove only the 4 already-fenced
PDF/md/dt producers). This file drives the producers the investigation
memo (T2 ``nx memory get -p nexus -t "vw594-investigation-2026-08-04"``)
found completely unfenced: the repo indexer (``nx index repo`` — code/
prose, both the ChunkBatcher flush-grain hot path and the legacy per-file
fallback) and MCP ``store_put``.

Substrate: rather than booting a dedicated hermetic PG + service JAR per
module (5xn3k's approach), this file uses the SUITE-WIDE session substrate
(``tests/_engine_substrate.py`` via the ``t2_service_env`` fixture) that
``tests/conftest.py``'s autouse ``_pin_t2_substrate`` already routes every
unit test's T2 (catalog) calls through. Since RDR-155 P4a.2, ``make_t3()``
returns the service-backed T3 client UNCONDITIONALLY (see
``nexus.db.http_vector_client.is_vector_service_mode``'s docstring), so
setting ``NX_SERVICE_URL``/``NX_SERVICE_TOKEN`` (which ``t2_service_env``
does) routes BOTH the catalog fence writes AND the T3 chunk writes at the
SAME engine instance — the dcv2k lesson 5xn3k's gate exists to enforce
("a fence read from a substrate different than where the chunks landed
rebuilds the exact vacuity this arc exists to kill") holds here too,
just via the shared substrate instead of a bespoke one.

Every scenario drives a REAL production entry point: the ``nx index repo``
CLI command (which is what an operator or CI actually runs — no hand-built
registry stub) and ``nexus.mcp.core.store_put`` directly.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.needs_stamped_jar]


def _git_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    return repo


def _fence_fields(entry) -> tuple[str | None, str, str]:
    return (
        getattr(entry, "index_state", None),
        getattr(entry, "index_run_id", "") or "",
        getattr(entry, "index_started_at", "") or "",
    )


class TestRepoIndexStampsFullFence:
    """Production test #1 (nexus-vw594 F1): every document ``nx index repo``
    writes carries a complete fence record — ``index_state == 'complete'``,
    ``index_run_id`` and ``index_started_at`` both non-empty.

    KILL CONTROL: temporarily removing the ``_fence_begin`` call from
    ``code_indexer.py``'s ``index_code_file`` (the legacy per-file path)
    and the ``on_batch_begin=_fire_flush_grain_begin`` wiring in
    ``indexer.py``'s ``ChunkBatcher(...)`` construction (the batcher hot
    path) independently reproduces ``index_run_id == ''`` reappearing on
    the affected documents — verified manually 2026-08-04 by reverting
    each wiring point in turn and re-running this test; both revert
    the SAME way the F4 AST gate's own kill control does (see
    ``tests/test_vw594_fence_coverage_gate.py``), so the two gates
    falsify independently: the AST gate proves the wiring EXISTS in the
    source, this test proves it actually STAMPS the engine.
    """

    def test_repo_index_stamps_full_fence(self, t2_service_env, tmp_path, monkeypatch) -> None:
        from click.testing import CliRunner

        from nexus.catalog.factory import make_catalog_reader, reset_shared_service_catalog_client_for_tests
        from nexus.cli import main
        from nexus.db.http_vector_client import reset_http_vector_client_for_tests
        from nexus.mcp_infra import reset_singletons

        reset_http_vector_client_for_tests()
        reset_singletons()
        reset_shared_service_catalog_client_for_tests()

        repo = _git_repo(tmp_path, {
            "a.py": "def foo():\n    return 1\n",
            "README.md": "# Title\n\nSome prose content for the docs collection.\n",
        })

        runner = CliRunner()
        result = runner.invoke(main, ["index", "repo", str(repo)])
        assert result.exit_code == 0, result.output

        cat = make_catalog_reader()
        assert cat is not None
        entries = [
            e for e in cat.all_documents(limit=0)
            if str(getattr(e, "file_path", "")).endswith((".py", ".md"))
        ]
        assert len(entries) >= 2, f"expected at least 2 registered docs, got {len(entries)}: {entries}"
        for entry in entries:
            state, run_id, started_at = _fence_fields(entry)
            assert state == "complete", (entry.file_path, state)
            assert run_id != "", (entry.file_path, "index_run_id empty")
            assert started_at != "", (entry.file_path, "index_started_at empty")


class TestStorePutStampsFence:
    """Production test #2 (nexus-vw594 F2): MCP ``store_put`` stamps the
    fence exactly like the repo indexer.

    KILL CONTROL: temporarily dropping the ``manifest_complete=`` kwarg
    from ``mcp/core.py``'s ``store_put`` ``_hooks.fire_batch(...)`` call
    reproduces ``index_state`` staying at ``'indexing'`` forever (the
    begin lands, the completion ride never fires) — verified manually
    2026-08-04 by removing the kwarg and re-running this test.
    """

    def test_store_put_stamps_fence(self, t2_service_env, monkeypatch) -> None:
        from nexus.catalog.factory import make_catalog_reader, reset_shared_service_catalog_client_for_tests
        from nexus.db.http_vector_client import reset_http_vector_client_for_tests
        from nexus.mcp.core import store_put
        from nexus.mcp_infra import reset_singletons

        reset_http_vector_client_for_tests()
        reset_singletons()
        reset_shared_service_catalog_client_for_tests()

        result = store_put(
            content="nexus-vw594 store_put fence coverage test content.",
            collection="knowledge",
            title="vw594-store-put-fence-test",
        )
        assert "Error" not in result, result

        cat = make_catalog_reader()
        assert cat is not None
        entries = [
            e for e in cat.all_documents(limit=0)
            if getattr(e, "title", "") == "vw594-store-put-fence-test"
        ]
        assert len(entries) == 1, entries
        entry = entries[0]
        state, run_id, started_at = _fence_fields(entry)
        assert state == "complete", state
        assert run_id != "", "index_run_id empty"
        assert started_at != "", "index_started_at empty"


class TestRepoIndexKilledMidflushLeavesIndexing:
    """Production test #3 (nexus-vw594 F1): a repo-index run killed AFTER
    begin but BEFORE the flush's upsert lands leaves ``index_state ==
    'indexing'`` with a non-empty ``index_started_at`` — and, load-bearing,
    ``health._check_stale_indexing_runs`` is actually CAPABLE of firing
    against that row (with the staleness threshold patched down to make
    the injected failure immediately "stale"). This is the first test in
    the repo proving the doctor check is capable of firing at all; every
    prior fence test only asserted the COLUMN value, never that the
    consuming health check could see it.

    KILL CONTROL: this test's own assertion that ``index_state ==
    'indexing'`` IS its kill control against a regression to the pre-F1
    behaviour (where this producer had no begin call and the column
    would read NULL/unreported instead) — a NULL row would fail the
    ``state == "indexing"`` assertion outright. Additionally verified
    manually 2026-08-04: with the ``_STALE_INDEXING_THRESHOLD_HOURS``
    patch removed, the doctor check does NOT fire (the row is real but
    not yet old enough), confirming the threshold patch — not test
    fixture leakage — is what makes the assertion meaningful.

    INJECTION-POINT RETARGET (2026-08-09, nexus-vw594 release-blocker
    follow-up): nexus-wxjr6/nexus-kl2z6 (this same release) replaced the
    ChunkBatcher flush's two-call shape (an ``upsert_chunks_with_
    embeddings`` POST here, then a SEPARATE ``write_many`` POST later
    from the flush-grain manifest hook) with ONE combined
    ``cat.write_manifest_many(..., chunks=...)`` call that carries chunk
    content AND manifest rows atomically per-doc, server-side (see
    ``indexer.py``'s ``_batch_flush``). ``upsert_chunks_with_embeddings``
    is no longer on this path for files that HAVE catalog identity (the
    normal case — every file `nx index repo` registers before indexing),
    so a raise injected there no longer intercepts anything: the flush
    genuinely succeeds and the fence genuinely reaches 'complete'. The
    kill injector below targets ``HttpCatalogClient.write_manifest_many``
    instead — the ACTUAL network call ``_batch_flush`` makes for this
    file, reached via ``get_catalog_writer()`` -> ``_ServiceCatalogWriter``
    -> ``_SharedServiceCatalogHandle`` -> the shared ``HttpCatalogClient``
    singleton (both proxies forward via plain ``getattr``, so patching
    the class method intercepts through both layers). This still fires
    AFTER ``_fence_begin_many`` (a distinct method, ``begin_index_run_
    many``, unaffected by this patch) and BEFORE the completion stamp,
    which rides IN the same ``write_manifest_many`` call via its
    ``complete=`` kwarg — so raising here still lands squarely between
    begin and completion. IF THIS SEAM CHANGES AGAIN: this test will
    fail LOUDLY (fence reads 'complete' instead of 'indexing') rather
    than silently passing vacuous, per the F1 kill-control discipline —
    grep ``_batch_flush`` in ``indexer.py`` for the current upload call
    before assuming this patch target is still correct.
    """

    def test_repo_index_killed_midflush_leaves_indexing(
        self, t2_service_env, tmp_path, monkeypatch,
    ) -> None:
        from nexus.catalog.factory import make_catalog_reader, reset_shared_service_catalog_client_for_tests
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.db.http_vector_client import reset_http_vector_client_for_tests
        from nexus.mcp_infra import reset_singletons

        reset_http_vector_client_for_tests()
        reset_singletons()
        reset_shared_service_catalog_client_for_tests()

        repo = _git_repo(tmp_path, {"a.py": "def foo():\n    return 1\n"})

        # Kill mid-flush: raise from inside the catalog client's combined
        # write call — the CURRENT upload seam for a file WITH catalog
        # identity (see class docstring "INJECTION-POINT RETARGET" above).
        # This fires AFTER _fence_begin_many has already stamped
        # 'indexing' (a separate begin_index_run_many round trip,
        # untouched by this patch — memo §3.5 T0 ordering) but before the
        # completion stamp, which rides inside this very call's
        # ``complete=`` kwarg and therefore never lands.
        real_write_manifest_many = HttpCatalogClient.write_manifest_many

        def _boom(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003 — test-local kill injector
            raise RuntimeError("nexus-vw594 kill-control: injected mid-flush failure")

        monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", _boom)

        from click.testing import CliRunner

        from nexus.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["index", "repo", str(repo)])
        # The CLI surfaces the upload failure; the run must not silently
        # report success.
        assert result.exit_code != 0 or "fail" in result.output.lower() or "error" in result.output.lower()

        monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", real_write_manifest_many)

        cat = make_catalog_reader()
        assert cat is not None
        entries = [
            e for e in cat.all_documents(limit=0)
            if str(getattr(e, "file_path", "")).endswith("a.py")
        ]
        assert len(entries) == 1, entries
        entry = entries[0]
        state, run_id, started_at = _fence_fields(entry)
        assert state == "indexing", (
            f"expected the fence to be stranded at 'indexing' after a "
            f"kill mid-flush, got {state!r} — either the begin never "
            f"fired (coverage regression) or the flush somehow completed "
            f"despite the injected raise"
        )
        assert run_id != ""
        assert started_at != ""

        # Load-bearing: prove the doctor check can actually FIRE against
        # this exact row, not just that the column holds the right value.
        import nexus.health as health

        monkeypatch.setattr(health, "_STALE_INDEXING_THRESHOLD_HOURS", 0.0)
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda *a, **k: cat, raising=False,
        )
        results = health._check_stale_indexing_runs()
        assert len(results) == 1
        r = results[0]
        assert r.ok is False and r.warn is True, (
            f"doctor's stale-run check did not fire against a real "
            f"stranded-indexing row: {r}"
        )
        assert "a.py" in r.detail or entry.tumbler and str(entry.tumbler) in r.detail


class TestRepoIndexOrphanSeamFailureDoesNotStrandBatchmateFence:
    """Production test #4 (nexus-vw594 F1 follow-up, task item 3, 2026-08-09):
    a kill on the OTHER upload seam that survives in ``_batch_flush`` —
    the legacy ``db.upsert_chunks_with_embeddings`` call for chunks
    belonging to a file with NO catalog identity (the "orphan" slice,
    code review Critical C1 2026-08-09 / nexus-3mwuo) — must NOT strand
    a co-batched, identity-HAVING file's fence.

    COVERAGE DECISION (task item 3), including a CORRECTED hypothesis:
    the task asked whether a kill on the orphan seam should ALSO be
    covered, since ``_batch_flush`` calls the orphan upsert BEFORE the
    combined write (``indexer.py`` — ``if orphan_ids: db.upsert_chunks_
    with_embeddings(...)`` precedes ``cat.write_manifest_many`` for
    ``full_docs``), so a naive reading suggests an orphan-slice failure
    could propagate out of the whole flush closure and prevent an
    unrelated batch-mate's combined write from ever being attempted —
    collateral stranding. EMPIRICALLY THIS DOES NOT HAPPEN: this test
    originally asserted collateral stranding and went RED against real
    production code (structlog showed ``chunk_batch_flush_bisect`` firing
    before the failure ever reached settlement). ``ChunkBatcher._flush_
    batch`` bisects ANY failed flush with >= 2 files, retrying each file
    as its OWN independent flush (``_split``, file-boundary halves,
    recursing to file granularity) — so a poisoned file's failure is
    ALWAYS isolated to itself once 2+ files are involved, and a lone
    orphan-only flush (1 file, no bisect possible) has no ``full_docs``
    at all (``_batch_flush``'s early-return), so there is no co-located
    identity-having document in that flush to strand either. There is no
    reachable production configuration where an orphan-seam failure
    strands a DIFFERENT file's fence — this test now asserts the
    ISOLATION property instead (the corrected, verified expectation),
    which is the real invariant worth protecting: a future change that
    weakens or removes the bisect-on-failure retry would silently
    reintroduce the collateral-stranding hazard, and this test would
    catch it (``a.py`` would stop reaching 'complete').

    Orphan chunks carry no catalog ``doc_id`` and therefore no
    ``index_state`` column to observe — this test asserts on the
    IDENTITY-HAVING batch-mate (``a.py``) only.

    KILL CONTROL: temporarily removing (or narrowing) the bisect branch
    in ``ChunkBatcher._flush_batch`` (the ``if len(pend.file_counts) >=
    2:`` split) would make this test fail — ``a.py`` would no longer
    reach 'complete' once its flush is no longer retried in isolation
    from ``b.py``'s failure. Not exercised via source mutation here
    (that block is shared production logic with its own dedicated
    ``chunk_batcher`` unit tests); the ORIGINAL version of this test
    (asserting stranding) already serves as the falsification record —
    it went red against the real bisect behavior, which is exactly how
    this corrected expectation was discovered.
    """

    def test_orphan_upsert_failure_isolated_from_batchmate_fence(
        self, t2_service_env, tmp_path, monkeypatch,
    ) -> None:
        from nexus.catalog.factory import make_catalog_reader, reset_shared_service_catalog_client_for_tests
        from nexus.db.http_vector_client import reset_http_vector_client_for_tests
        from nexus.mcp_infra import reset_singletons

        reset_http_vector_client_for_tests()
        reset_singletons()
        reset_shared_service_catalog_client_for_tests()

        repo = _git_repo(tmp_path, {
            "a.py": "def foo():\n    return 1\n",
            "b.py": "def bar():\n    return 2\n",
        })

        # Force b.py's flush-time catalog-identity resolution to "" —
        # simulates code_indexer.py's documented "stages the file into
        # the batcher regardless of whether resolution succeeded" case
        # (_build_combined_write_payload docstring) — so b.py's chunks
        # route through the orphan slice of _batch_flush while a.py
        # keeps its real catalog identity and goes through the combined
        # write. Both tiny single-chunk files land in the SAME initial
        # flush (well under the chunk cap) — verified via structlog:
        # ``chunk_batch_flush_bisect chunks=2 files=2`` fires before the
        # per-file retry.
        import nexus.indexer as indexer_mod

        real_build_resolver = indexer_mod.build_doc_id_resolver

        def _resolver_forcing_b_orphan(file_to_doc_id):
            real_resolver = real_build_resolver(file_to_doc_id)

            def _resolver(path):
                if path.name == "b.py":
                    return ""
                return real_resolver(path)

            return _resolver

        monkeypatch.setattr(indexer_mod, "build_doc_id_resolver", _resolver_forcing_b_orphan)

        # Kill ONLY the orphan seam: raise from db.upsert_chunks_with_
        # embeddings, the call _batch_flush makes FIRST (before the
        # combined write) for chunks belonging to identity-less files.
        # a.py's own combined write (cat.write_manifest_many) is left
        # untouched — this test is about whether b.py's failure reaches
        # a.py, not about killing a.py's own seam (class above covers
        # that).
        import nexus.db.http_vector_client as hvc

        real_upsert = hvc.HttpVectorClient.upsert_chunks_with_embeddings

        def _boom(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003 — test-local kill injector
            raise RuntimeError("nexus-vw594 kill-control: injected orphan-seam failure")

        monkeypatch.setattr(hvc.HttpVectorClient, "upsert_chunks_with_embeddings", _boom)

        from click.testing import CliRunner

        from nexus.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["index", "repo", str(repo)])
        # The CLI must surface b.py's upload failure — it must not
        # silently report full success while a file failed.
        assert result.exit_code != 0 or "fail" in result.output.lower() or "error" in result.output.lower(), result.output

        monkeypatch.setattr(hvc.HttpVectorClient, "upsert_chunks_with_embeddings", real_upsert)

        cat = make_catalog_reader()
        assert cat is not None
        entries = [
            e for e in cat.all_documents(limit=0)
            if str(getattr(e, "file_path", "")).endswith("a.py")
        ]
        assert len(entries) == 1, entries
        entry = entries[0]
        state, run_id, started_at = _fence_fields(entry)
        # ISOLATION, not stranding: bisect-on-failure retries a.py's
        # flush independently of b.py's poisoned one, so a.py's own
        # combined write DOES land and its fence reaches 'complete' —
        # b.py's failure must never leak into a.py's fence state.
        assert state == "complete", (
            f"expected a.py's fence to reach 'complete' — its combined "
            f"write is independent of the co-batched orphan (b.py) "
            f"failure once ChunkBatcher bisects the flush by file, got "
            f"{state!r} (bisect-on-failure isolation may have broken)"
        )
        assert run_id != ""
        assert started_at != ""
