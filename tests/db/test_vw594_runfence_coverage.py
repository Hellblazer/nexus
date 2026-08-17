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
    """Production test #3 (nexus-vw594 F1), UPDATED by nexus-bhlfy: a
    repo-index flush failure now stamps ``index_state == 'failed'``
    instead of stranding the row at ``'indexing'`` forever.

    ORIGINAL (pre-bhlfy) behaviour: this class's kill injector (a raise
    from the batcher flush's catalog write) landed between
    ``_fence_begin_many`` and the completion stamp with NO paired fail
    arm — ``ChunkBatcher``'s ``on_file_failed`` callback
    (``indexer.py``'s ``_batched_file_failed``) fired but only logged,
    never called ``_fence_fail``. The row was stranded at ``'indexing'``
    with only the 6h doctor sweep (``_check_stale_indexing_runs``, which
    fires ONLY for ``state == 'indexing'``) as signal — exactly the
    nexus-bhlfy bug (nexus-tjf7l investigation: 13 ART docs stranded this
    way by a real 504 run).

    nexus-bhlfy wires ``_fence_fail`` into ``_batched_file_failed`` (the
    per-file failure callback ``ChunkBatcher`` already fires once per
    PERMANENTLY-failed file — no new ChunkBatcher callback needed). This
    test now asserts the CORRECTED outcome: the row reaches ``'failed'``
    with a non-empty ``index_started_at`` (proving begin genuinely fired
    before the failure), and — load-bearing — the doctor's stale-
    'indexing' WARN does NOT fire for it any more, because the failure
    was already reported at run time (``index_batch_upload_failures`` in
    the CLI output) and 'failed' is deliberately excluded from that
    specific check (``health.py``'s ``if state != "indexing": continue``)
    — restating an already-known failure 6h later would be noise, not
    signal. ``doc_indexer.py``'s freshness three-way still treats
    'failed' identically to 'indexing' (both "definitely stale, always
    re-index"), so this is a reporting change only, never a correctness
    regression.

    KILL CONTROL: this test's assertion that ``index_state == 'failed'``
    is its kill control against BOTH a regression to the pre-F1 fully-
    unfenced behaviour (a NULL row would fail the ``state == "failed"``
    assertion outright) AND a regression to the pre-bhlfy stranded-
    'indexing' behaviour (reverting the ``_fence_fail`` wiring in
    ``_batched_file_failed`` reproduces ``state == "indexing"`` instead —
    verified manually 2026-08-17 by commenting out that call and
    re-running this test alone).

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
    fail LOUDLY (fence reads 'complete' instead of 'failed') rather
    than silently passing vacuous, per the F1 kill-control discipline —
    grep ``_batch_flush`` in ``indexer.py`` for the current upload call
    before assuming this patch target is still correct.
    """

    def test_repo_index_killed_midflush_stamps_failed(
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
        # nexus-bhlfy: the fence now stamps 'failed', not 'indexing' — the
        # missing fail arm this bead closes. A regression to the pre-bhlfy
        # gap (or the pre-F1 fully-unfenced state) would read "indexing"
        # or None here respectively.
        assert state == "failed", (
            f"expected the fence to be stamped 'failed' after a kill "
            f"mid-flush (nexus-bhlfy fail-arm wiring), got {state!r} — "
            f"either the begin never fired (coverage regression), the "
            f"flush somehow completed despite the injected raise, or the "
            f"fail arm regressed back to the pre-bhlfy stranded-'indexing' "
            f"gap"
        )
        assert run_id != "", "index_run_id empty — begin never fired"
        assert started_at != ""

        # Load-bearing: a CLEANLY-REPORTED failure (state == 'failed', not
        # 'indexing') must NOT trip doctor's stale-'indexing' WARN — that
        # check exists for a run that died silently with no report at all
        # (health.py's `_check_stale_indexing_runs`: `if state !=
        # "indexing": continue`). Restating an already-known failure 6h
        # later would be noise, not signal — this is the actual payoff of
        # the fail-arm fix (T1 scratch nexus-tjf7l investigation).
        import nexus.health as health

        monkeypatch.setattr(health, "_STALE_INDEXING_THRESHOLD_HOURS", 0.0)
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda *a, **k: cat, raising=False,
        )
        results = health._check_stale_indexing_runs()
        warns = [r for r in results if r.warn and r.ok is False]
        assert not warns, (
            f"a cleanly fence-failed document ('failed', not 'indexing') "
            f"must not trip the stale-'indexing' WARN — the failure was "
            f"already reported at run time: {warns}"
        )


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


class TestBatcherBisectedBatchFailsExactlyOnePoisonedFile:
    """nexus-bhlfy production test: a batch of TWO identity-having files
    that fails as a whole flush (both share the initial combined
    ``write_manifest_many`` POST) must, after ``ChunkBatcher`` bisects the
    failure to file granularity, report the fence fail EXACTLY ONCE for
    the genuinely poisoned file and never for its healthy batch-mate — no
    double-report, no leak.

    Distinct from ``TestRepoIndexOrphanSeamFailureDoesNotStrandBatchmateFence``
    above: that class kills the OTHER upload seam (the orphan
    ``upsert_chunks_with_embeddings`` call for a file with NO catalog
    identity) and proves isolation from an unrelated batch-mate. This
    class kills the SAME seam ``TestRepoIndexKilledMidflushLeavesIndexing``
    does (``HttpCatalogClient.write_manifest_many``, the combined-write
    seam every identity-having file goes through) but SELECTIVELY — only
    for the batch containing the poisoned file's chunk text — so both
    files start in the SAME initial flush and the poisoned one survives
    bisection down to its own single-file retry.

    KILL CONTROL: reverting the ``_fence_fail`` wiring in ``indexer.py``'s
    ``_batched_file_failed`` reproduces ``bad.py``'s fence stranded at
    'indexing' instead of 'failed' (the pre-bhlfy gap) while leaving this
    test's ``fail_reports`` assertion trivially satisfied (zero reports
    for either file) — verified manually 2026-08-17 by commenting out the
    ``_fence_fail`` call and re-running this test alone.
    """

    def test_bisected_batch_reports_poisoned_file_exactly_once(
        self, t2_service_env, tmp_path, monkeypatch,
    ) -> None:
        from nexus.catalog.factory import make_catalog_reader, reset_shared_service_catalog_client_for_tests
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.db.http_vector_client import reset_http_vector_client_for_tests
        from nexus.mcp_infra import reset_singletons

        reset_http_vector_client_for_tests()
        reset_singletons()
        reset_shared_service_catalog_client_for_tests()

        poison = "NEXUS_BHLFY_BISECT_POISON_MARKER"
        repo = _git_repo(tmp_path, {
            "good.py": "def good():\n    return 1\n",
            "bad.py": f'def bad():\n    """{poison}"""\n    return 2\n',
        })

        real_write_manifest_many = HttpCatalogClient.write_manifest_many

        def _selective_boom(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003 — test-local kill injector
            chunks = kw.get("chunks") or []
            if any(poison in c.get("text", "") for c in chunks):
                raise RuntimeError("nexus-bhlfy kill-control: injected selective bisect failure")
            return real_write_manifest_many(self, *a, **kw)

        monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", _selective_boom)

        # Record every _fence_fail call (module-level patch target every
        # producer resolves at call time) while still calling through, so
        # the real engine catalog is genuinely stamped.
        import nexus.doc_indexer as doc_indexer_mod

        real_fence_fail = doc_indexer_mod._fence_fail
        fail_reports: list[tuple[str, str]] = []

        def _wrapped_fail(doc_id: str, error: str) -> None:
            fail_reports.append((doc_id, error))
            real_fence_fail(doc_id, error)

        monkeypatch.setattr(doc_indexer_mod, "_fence_fail", _wrapped_fail)

        from click.testing import CliRunner

        from nexus.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["index", "repo", str(repo)])
        assert result.exit_code != 0 or "fail" in result.output.lower() or "error" in result.output.lower(), result.output

        monkeypatch.setattr(HttpCatalogClient, "write_manifest_many", real_write_manifest_many)

        cat = make_catalog_reader()
        assert cat is not None
        good_entries = [
            e for e in cat.all_documents(limit=0)
            if str(getattr(e, "file_path", "")).endswith("good.py")
        ]
        bad_entries = [
            e for e in cat.all_documents(limit=0)
            if str(getattr(e, "file_path", "")).endswith("bad.py")
        ]
        assert len(good_entries) == 1, good_entries
        assert len(bad_entries) == 1, bad_entries

        good_state, good_run, _ = _fence_fields(good_entries[0])
        bad_state, bad_run, _ = _fence_fields(bad_entries[0])
        assert good_state == "complete", (
            f"good.py must reach 'complete' despite bad.py's poisoned "
            f"content sharing the initial flush, got {good_state!r}"
        )
        assert bad_state == "failed", (
            f"bad.py must reach 'failed' (nexus-bhlfy fix), got {bad_state!r}"
        )
        assert good_run != "" and bad_run != ""

        good_doc_id = str(good_entries[0].tumbler)
        bad_doc_id = str(bad_entries[0].tumbler)
        bad_reports = [r for r in fail_reports if r[0] == bad_doc_id]
        good_reports = [r for r in fail_reports if r[0] == good_doc_id]
        assert len(bad_reports) == 1, (
            f"expected EXACTLY ONE fence-fail report for bad.py (no "
            f"double-report across bisect retries), got {bad_reports} "
            f"(all reports: {fail_reports})"
        )
        assert not good_reports, (
            f"good.py must never receive a fence-fail report: {fail_reports}"
        )
