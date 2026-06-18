# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-159 P0.T (nexus-ue6g7.1) — the detection-classifier matrix.

The guided upgrade engine's first step (RDR-159 §Approach P0) classifies a
user's Chroma footprint per collection along TWO orthogonal axes before any
data moves:

* **source leg** — local ``PersistentClient`` vs ChromaCloud REST;
* **embedding model** — parsed from the conformant collection name segment,
  resolved to a *support* class against the service's wired embedders.

The support resolution is a PURE function of deployment mode (plan-audit
condition, 2026-06-13): P0 runs PRE-provisioning, so it cannot query a live
``EmbedderRouter``. It mirrors ``EmbedderRouter``'s wiring
(``service/.../vectors/EmbedderRouter.java``): the service's ONNX model
(``bge-base-en-v15-768`` since RDR-160) is wired in EVERY mode; the voyage
models (``voyage-code-3`` / ``voyage-context-3`` / ``voyage-3``) are wired iff
``NX_VOYAGE_API_KEY`` is present; anything else (e.g. a legacy
``minilm-l6-v2-384`` collection — a KNOWN dim in ``vector_etl._MODEL_DIMS`` but
wired by NO service embedder post-RDR-160) is UNSUPPORTED and must be detected +
flagged with the re-index diagnostic, never silently treated as migratable just
because its dim is known.

These tests are the contract for the classifier implemented in P0.I
(nexus-ue6g7.2). They use a small in-memory client double rather than a real
``EphemeralClient``: the matrix needs TWO independent legs at once (local AND
cloud), which the shared-process EphemeralClient backend cannot model, and a
deliberately-corrupt store for the fail-loud leg. The real Chroma read
substrate's iteration/paging behaviour is integration-pinned separately in
``test_chroma_read.py``; here we pin only the classification logic the
classifier layers on top of ``list_collections()`` + ``Collection.count()``.

Counts are asserted EXACTLY (``== N``), never ``>=`` — an inequality is how a
silent-undercount detection bug passes
(``feedback_exact_assertions_for_fixture_regression``).
"""
from __future__ import annotations

import pytest

from nexus.migration.detection import (
    CollectionClassification,
    DetectionReport,
    build_dry_run_preview,
    classify_collections,
    classify_model_support,
    cross_model_remappable,
    render_dry_run_preview,
    voyage_key_available,
    wired_models,
)

# ---------------------------------------------------------------------------
# Conformant collection names for the matrix: <content_type>__<owner>__<model>__v<n>
# ---------------------------------------------------------------------------
ONNX_384 = "knowledge__acme__minilm-l6-v2-384__v1"
VOYAGE_CTX_1024 = "knowledge__acme__voyage-context-3__v1"
VOYAGE_CODE_1024 = "code__acme__voyage-code-3__v1"
BGE_768 = "knowledge__acme__bge-base-en-v15-768__v1"
NON_CONFORMANT = "legacy_two_segment_name"


# ---------------------------------------------------------------------------
# In-memory client double — mimics the chromadb client surface the classifier
# touches: list_collections() -> objects with .name; get_collection(name) ->
# object with .count(). Deterministic counts, models two legs independently.
# ---------------------------------------------------------------------------
class _FakeCollection:
    def __init__(self, name: str, count: int) -> None:
        self.name = name
        self._count = count

    def count(self) -> int:
        return self._count


class _FakeChromaClient:
    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = dict(counts)

    def list_collections(self) -> list[_FakeCollection]:
        return [_FakeCollection(n, c) for n, c in self._counts.items()]

    def get_collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(name, self._counts[name])


class _ListExplodingClient:
    """A store too corrupt to even enumerate — list_collections() raises."""

    def list_collections(self):  # noqa: ANN201
        raise RuntimeError("corrupt sqlite header: not a database")


class _BrokenCountCollection:
    """A listed collection whose count probe raises (corrupt store row)."""

    def __init__(self, name: str) -> None:
        self.name = name

    def count(self) -> int:
        raise RuntimeError(f"collection {self.name} is unreadable: malformed segment")


class _CountExplodingClient:
    """Enumerable, but one collection cannot be probed (count raises)."""

    def list_collections(self) -> list[_BrokenCountCollection]:
        return [_BrokenCountCollection(ONNX_384)]


def _by_name(report: DetectionReport) -> dict[str, CollectionClassification]:
    return {c.collection: c for c in report.classifications}


# ---------------------------------------------------------------------------
# wired_models — the deployment-mode pure function (mirrors EmbedderRouter)
# ---------------------------------------------------------------------------
class TestWiredModels:
    def test_local_mode_wires_only_onnx(self) -> None:
        assert wired_models(voyage_key_present=False) == frozenset(
            {"bge-base-en-v15-768"}
        )

    def test_cloud_mode_wires_onnx_and_all_voyage(self) -> None:
        assert wired_models(voyage_key_present=True) == frozenset(
            {
                "bge-base-en-v15-768",
                "voyage-code-3",
                "voyage-context-3",
                "voyage-3",
            }
        )

    def test_minilm_is_wired_in_no_mode(self) -> None:
        # RDR-160 retired MiniLM-384 from the service; it is wired by no embedder
        # in any mode now (bge-768 is the service's ONNX model).
        assert "minilm-l6-v2-384" not in wired_models(voyage_key_present=True)
        assert "minilm-l6-v2-384" not in wired_models(voyage_key_present=False)


# ---------------------------------------------------------------------------
# classify_model_support — pure (model_token, voyage_key_present) -> support
# ---------------------------------------------------------------------------
class TestClassifyModelSupport:
    def test_onnx_supported_regardless_of_key(self) -> None:
        for key in (True, False):
            support, reason = classify_model_support(
                "bge-base-en-v15-768", voyage_key_present=key
            )
            assert support == "supported-onnx"
            assert reason == ""

    @pytest.mark.parametrize(
        "model", ["voyage-context-3", "voyage-code-3", "voyage-3"]
    )
    def test_voyage_supported_with_key(self, model: str) -> None:
        support, reason = classify_model_support(model, voyage_key_present=True)
        assert support == "supported-voyage-1024"
        assert reason == ""

    @pytest.mark.parametrize(
        "model", ["voyage-context-3", "voyage-code-3", "voyage-3"]
    )
    def test_voyage_without_key_is_unsupported_with_key_diagnostic(
        self, model: str
    ) -> None:
        support, reason = classify_model_support(model, voyage_key_present=False)
        assert support == "unsupported"
        # The diagnostic must point at the cheap fix (add the key), distinct
        # from the expensive re-index diagnostic bge gets.
        assert "NX_VOYAGE_API_KEY" in reason

    @pytest.mark.parametrize("key", [True, False])
    def test_minilm_is_unsupported_with_reindex_diagnostic(self, key: bool) -> None:
        support, reason = classify_model_support(
            "minilm-l6-v2-384", voyage_key_present=key
        )
        assert support == "unsupported"
        # Distinct from the voyage-key case: post-RDR-160 minilm-384 has no wired
        # service embedder in any mode, so the fix is a re-index, NOT a credential.
        assert "NX_VOYAGE_API_KEY" not in reason
        assert "re-index" in reason.lower()

    def test_unknown_model_is_unsupported(self) -> None:
        support, reason = classify_model_support(
            "some-future-model-512", voyage_key_present=True
        )
        assert support == "unsupported"
        assert reason

    def test_none_model_non_conformant_is_unsupported(self) -> None:
        support, reason = classify_model_support(None, voyage_key_present=True)
        assert support == "unsupported"
        assert "conformant" in reason.lower()


# ---------------------------------------------------------------------------
# classify_collections — the matrix: {local, cloud} x {onnx, voyage, bge} x
# {empty, has-data}
# ---------------------------------------------------------------------------
class TestClassifyCollectionsMatrix:
    def test_fresh_user_no_clients_is_empty_noop(self) -> None:
        report = classify_collections(
            local_client=None, cloud_client=None, voyage_key_present=True
        )
        assert report.classifications == ()
        assert report.legs_with_data == frozenset()
        assert report.unsupported == ()

    def test_reports_both_axes_per_collection(self) -> None:
        local = _FakeChromaClient({BGE_768: 7})
        report = classify_collections(
            local_client=local, cloud_client=None, voyage_key_present=True
        )
        (c,) = report.classifications
        # BOTH axes present: source leg AND embedding model.
        assert c.leg == "local"
        assert c.model == "bge-base-en-v15-768"
        assert c.dim == 768
        assert c.support == "supported-onnx"
        assert c.source_count == 7
        assert c.has_data is True

    def test_leg_attribution_across_both_legs(self) -> None:
        local = _FakeChromaClient({ONNX_384: 3})
        cloud = _FakeChromaClient({VOYAGE_CTX_1024: 5})
        report = classify_collections(
            local_client=local, cloud_client=cloud, voyage_key_present=True
        )
        by = _by_name(report)
        assert by[ONNX_384].leg == "local"
        assert by[VOYAGE_CTX_1024].leg == "cloud"
        assert by[ONNX_384].source_count == 3
        assert by[VOYAGE_CTX_1024].source_count == 5
        assert report.legs_with_data == frozenset({"local", "cloud"})

    def test_empty_collection_distinguished_from_has_data(self) -> None:
        local = _FakeChromaClient({ONNX_384: 0, VOYAGE_CODE_1024: 4})
        report = classify_collections(
            local_client=local, cloud_client=None, voyage_key_present=True
        )
        by = _by_name(report)
        # Configured-but-empty: present in the report, source_count == 0,
        # has_data False — NOT dropped (the operator must see empty legs).
        assert by[ONNX_384].source_count == 0
        assert by[ONNX_384].has_data is False
        assert by[VOYAGE_CODE_1024].source_count == 4
        assert by[VOYAGE_CODE_1024].has_data is True
        # Only the non-empty collection's leg counts as a data-bearing leg.
        assert report.legs_with_data == frozenset({"local"})

    def test_all_empty_leg_is_not_data_bearing(self) -> None:
        # A leg whose every collection is empty must NOT appear in
        # legs_with_data — load-bearing for P2's "refuse partial-leg success"
        # (an empty leg is nothing to migrate, not a half-migration).
        local = _FakeChromaClient({ONNX_384: 0, VOYAGE_CODE_1024: 0})
        report = classify_collections(
            local_client=local, cloud_client=None, voyage_key_present=True
        )
        assert len(report.classifications) == 2
        assert all(c.has_data is False for c in report.classifications)
        assert report.legs_with_data == frozenset()

    def test_minilm_flagged_unsupported_not_silently_known_dim(self) -> None:
        local = _FakeChromaClient({ONNX_384: 9})
        report = classify_collections(
            local_client=local, cloud_client=None, voyage_key_present=True
        )
        (c,) = report.classifications
        assert c.support == "unsupported"
        # The dim IS known (384 in _MODEL_DIMS) — but known-dim != supported;
        # post-RDR-160 the service has no minilm-384 embedder.
        assert c.dim == 384
        assert "re-index" in c.reason.lower()
        assert report.unsupported == (c,)

    def test_voyage_without_key_is_unsupported(self) -> None:
        cloud = _FakeChromaClient({VOYAGE_CTX_1024: 6})
        report = classify_collections(
            local_client=None, cloud_client=cloud, voyage_key_present=False
        )
        (c,) = report.classifications
        assert c.support == "unsupported"
        assert c.dim == 1024
        assert "NX_VOYAGE_API_KEY" in c.reason
        assert report.unsupported == (c,)

    def test_full_mixed_footprint(self) -> None:
        # Local leg: bge (data, supported) + minilm (data, unsupported legacy);
        # cloud leg: voyage ctx (data) + voyage code (empty). Voyage key present.
        local = _FakeChromaClient({BGE_768: 12, ONNX_384: 3})
        cloud = _FakeChromaClient({VOYAGE_CTX_1024: 20, VOYAGE_CODE_1024: 0})
        report = classify_collections(
            local_client=local, cloud_client=cloud, voyage_key_present=True
        )
        by = _by_name(report)
        assert len(report.classifications) == 4
        assert by[BGE_768].support == "supported-onnx"
        assert by[BGE_768].source_count == 12
        assert by[ONNX_384].support == "unsupported"
        assert by[ONNX_384].source_count == 3
        assert by[VOYAGE_CTX_1024].support == "supported-voyage-1024"
        assert by[VOYAGE_CTX_1024].source_count == 20
        assert by[VOYAGE_CODE_1024].support == "supported-voyage-1024"
        assert by[VOYAGE_CODE_1024].source_count == 0
        assert by[VOYAGE_CODE_1024].has_data is False
        # Both legs hold data; the empty cloud code collection does not negate
        # the data-bearing ctx collection on the same leg.
        assert report.legs_with_data == frozenset({"local", "cloud"})
        assert report.unsupported == (by[ONNX_384],)

    def test_non_conformant_name_is_unsupported(self) -> None:
        local = _FakeChromaClient({NON_CONFORMANT: 2})
        report = classify_collections(
            local_client=local, cloud_client=None, voyage_key_present=True
        )
        (c,) = report.classifications
        assert c.support == "unsupported"
        assert c.model is None
        assert c.dim is None
        assert "conformant" in c.reason.lower()


# ---------------------------------------------------------------------------
# Fail-loud: a malformed / unreadable store is a LOUD error, never a silent
# skip (no-silent-fallback rule).
# ---------------------------------------------------------------------------
class TestClassifyCollectionsFailLoud:
    def test_unenumerable_store_raises(self) -> None:
        with pytest.raises(RuntimeError, match="corrupt"):
            classify_collections(
                local_client=_ListExplodingClient(),
                cloud_client=None,
                voyage_key_present=True,
            )

    def test_unreadable_collection_probe_raises(self) -> None:
        # The store enumerates but a per-collection probe fails — must NOT be
        # silently skipped (a skipped collection is a silent half-migration).
        with pytest.raises(RuntimeError, match="unreadable"):
            classify_collections(
                local_client=_CountExplodingClient(),
                cloud_client=None,
                voyage_key_present=True,
            )


# ---------------------------------------------------------------------------
# voyage_key_available — deployment-mode signal (env / credential chain)
# ---------------------------------------------------------------------------
class TestVoyageKeyAvailable:
    def test_nx_voyage_api_key_present(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_VOYAGE_API_KEY", "vk-123")
        assert voyage_key_available() is True

    def test_absent_everywhere(self, monkeypatch) -> None:
        monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
        monkeypatch.setattr(
            "nexus.config.get_credential", lambda key: "", raising=True
        )
        assert voyage_key_available() is False

    def test_falls_back_to_credential(self, monkeypatch) -> None:
        monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
        monkeypatch.setattr(
            "nexus.config.get_credential",
            lambda key: "vk-cfg" if key == "voyage_api_key" else "",
            raising=True,
        )
        assert voyage_key_available() is True


# ---------------------------------------------------------------------------
# build_dry_run_preview / render_dry_run_preview — the P0 preview surface
# ---------------------------------------------------------------------------
class TestDryRunPreview:
    def test_empty_report_is_clean_noop_preview(self) -> None:
        report = classify_collections(
            local_client=None, cloud_client=None, voyage_key_present=True
        )
        preview = build_dry_run_preview(report)
        assert preview.groups == ()
        assert preview.unsupported == ()
        assert preview.migratable_chunks == 0
        assert preview.total_est_tokens == 0
        text = render_dry_run_preview(preview)
        assert "nothing to migrate" in text.lower()

    def test_supported_group_estimates_tokens_and_time(self) -> None:
        local = _FakeChromaClient({BGE_768: 100})
        report = classify_collections(
            local_client=local, cloud_client=None, voyage_key_present=True
        )
        preview = build_dry_run_preview(report)
        (g,) = preview.groups
        assert g.leg == "local"
        assert g.model == "bge-base-en-v15-768"
        assert g.support == "supported-onnx"
        assert g.collection_count == 1
        assert g.chunk_count == 100
        # 100 chunks x 512 tokens/chunk (exact constant)
        assert g.est_tokens == 100 * 512
        # onnx throughput 100 chunks/sec -> exactly 1.0s
        assert g.est_seconds == pytest.approx(1.0)
        assert preview.migratable_chunks == 100
        assert preview.total_est_tokens == 100 * 512

    def test_voyage_group_uses_voyage_throughput(self) -> None:
        cloud = _FakeChromaClient({VOYAGE_CTX_1024: 400})
        report = classify_collections(
            local_client=None, cloud_client=cloud, voyage_key_present=True
        )
        preview = build_dry_run_preview(report)
        (g,) = preview.groups
        assert g.support == "supported-voyage-1024"
        # 400 chunks / 200 voyage-chunks-per-sec -> exactly 2.0s
        assert g.est_seconds == pytest.approx(2.0)
        assert g.est_tokens == 400 * 512

    def test_unsupported_excluded_from_migratable_totals(self) -> None:
        local = _FakeChromaClient({BGE_768: 10, ONNX_384: 99})
        report = classify_collections(
            local_client=local, cloud_client=None, voyage_key_present=True
        )
        preview = build_dry_run_preview(report)
        # the legacy minilm-384 collection contributes NOTHING to migratable
        # totals — it would be blocked (needs re-index to bge-768 first).
        assert preview.migratable_chunks == 10
        assert preview.total_est_tokens == 10 * 512
        assert len(preview.unsupported) == 1
        assert preview.unsupported[0].collection == ONNX_384
        text = render_dry_run_preview(preview)
        assert "BLOCKED" in text
        assert ONNX_384 in text
        assert "re-index" in text.lower()
        # The minilm chunk count is NOT in the migratable line.
        assert "DRY RUN" in text

    def test_all_unsupported_render_has_no_dangling_migrate_section(self) -> None:
        # Every collection blocked → the "Would migrate (per leg / model):"
        # header must NOT appear with an empty body beneath it.
        local = _FakeChromaClient({ONNX_384: 4})
        report = classify_collections(
            local_client=local, cloud_client=None, voyage_key_present=True
        )
        text = render_dry_run_preview(build_dry_run_preview(report))
        assert "Would migrate (per leg / model):" not in text
        assert "nothing — every detected collection is blocked" in text
        assert "BLOCKED" in text

    def test_render_lists_both_legs(self) -> None:
        local = _FakeChromaClient({ONNX_384: 3})
        cloud = _FakeChromaClient({VOYAGE_CTX_1024: 5})
        report = classify_collections(
            local_client=local, cloud_client=cloud, voyage_key_present=True
        )
        text = render_dry_run_preview(build_dry_run_preview(report))
        assert "[local]" in text
        assert "[cloud]" in text
        assert "rough estimate" in text.lower()


# ---------------------------------------------------------------------------
# nx migrate-to-service CLI (P0: --dry-run only)
# ---------------------------------------------------------------------------
class TestMigrateToServiceCli:
    def _run(self, monkeypatch, args, local=None, cloud=None, voyage=True):
        from click.testing import CliRunner

        from nexus.commands import migrate_cmd

        monkeypatch.setattr(
            migrate_cmd, "open_read_legs", lambda *a, **k: (local, cloud)
        )
        monkeypatch.setattr(
            migrate_cmd, "voyage_key_available", lambda: voyage
        )
        return CliRunner().invoke(migrate_cmd.migrate_to_service_cmd, args)

    def test_dry_run_fresh_user_noop(self, monkeypatch) -> None:
        result = self._run(monkeypatch, ["--dry-run"], local=None, cloud=None)
        assert result.exit_code == 0
        assert "nothing to migrate" in result.output.lower()

    def test_dry_run_previews_supported(self, monkeypatch) -> None:
        local = _FakeChromaClient({BGE_768: 8})
        result = self._run(monkeypatch, ["--dry-run"], local=local)
        assert result.exit_code == 0
        assert "[local]" in result.output
        assert "bge-base-en-v15-768" in result.output

    def test_dry_run_with_unsupported_exits_nonzero(self, monkeypatch) -> None:
        local = _FakeChromaClient({ONNX_384: 2})
        result = self._run(monkeypatch, ["--dry-run"], local=local)
        # Unsupported collections would block a real run — dry-run gates non-zero.
        assert result.exit_code == 1
        assert "BLOCKED" in result.output
        # No dangling migratable section when everything is blocked.
        assert "Would migrate (per leg / model):" not in result.output

    def test_dry_run_passes_local_path_to_opener(self, monkeypatch) -> None:
        from click.testing import CliRunner

        from nexus.commands import migrate_cmd

        seen: dict[str, object] = {}

        def _fake_open(local_path=None):
            seen["local_path"] = local_path
            return None, None

        monkeypatch.setattr(migrate_cmd, "open_read_legs", _fake_open)
        monkeypatch.setattr(migrate_cmd, "voyage_key_available", lambda: True)
        result = CliRunner().invoke(
            migrate_cmd.migrate_to_service_cmd,
            ["--dry-run", "--local-path", "/tmp/custom-chroma"],
        )
        assert result.exit_code == 0
        assert seen["local_path"] == "/tmp/custom-chroma"

    def test_dry_run_surfaces_classify_error_not_namerror(self, monkeypatch) -> None:
        # A corrupt store mid-classify must surface its own error, never a
        # NameError from the post-block unsupported check (the clients still
        # close via finally).
        result = self._run(
            monkeypatch, ["--dry-run"], local=_ListExplodingClient()
        )
        assert result.exit_code != 0
        assert "NameError" not in result.output
        assert "corrupt" in str(result.exception or result.output).lower()


# ---------------------------------------------------------------------------
# nx migrate-to-service CLI — the non-dry-run guided run (RDR-159 P4,
# nexus-ue6g7.24). The command is a THIN renderer over run_guided_upgrade; these
# pin the wiring (clients/paths built, engine called) + the result rendering.
# ---------------------------------------------------------------------------
class TestMigrateToServiceRun:
    def _result(self, *, phase, ok, validation):
        from nexus.migration.detection import DetectionReport
        from nexus.migration.driver import GuidedUpgradeResult
        from nexus.migration.sequencer import SequenceOutcome

        cls = CollectionClassification(
            collection="code__o__bge-base-en-v15-768__v1",
            leg="local",
            model="bge-base-en-v15-768",
            dim=768,
            support="supported-onnx",
            source_count=10,
            has_data=True,
        )
        seq = SequenceOutcome(
            ok=ok,
            phase=phase,
            collections_total=1,
            collections_done=1 if ok else 0,
            t2_total_failed=0,
            legs_attempted=("local",),
            legs_ok=("local",) if ok else (),
            blocked_reason=None if ok else "dirty T2: total_failed=2",
            t2_report=None,
        )
        return GuidedUpgradeResult(
            detection=DetectionReport(classifications=(cls,)),
            sequence=seq,
            validation=validation,
            ok=ok and (validation is None or validation.unlocked),
        )

    def _validation(self, *, unlocked):
        from nexus.migration.validation import ValidationOutcome

        return ValidationOutcome(
            unlocked=unlocked,
            verdict="verified" if unlocked else "blocked",
            blocking_reasons=() if unlocked else ("counts: 1 collection mismatch",),
            taxonomy_orphans=(),
            count_mismatches=() if unlocked else ("code__o__bge-base-en-v15-768__v1",),
            count_indeterminate=False,
            manifest_orphan_count=0,
            manifest_vacuous=False,
            stale_aspects=0,
            advisory_notes=(),
            rollback_available=not unlocked,
        )

    def _run(self, monkeypatch, tmp_path, *, result, token="tok"):
        from click.testing import CliRunner

        from nexus.catalog import factory
        from nexus.commands import migrate_cmd
        from nexus.db import http_vector_client
        from nexus.migration import driver

        captured: dict = {}

        def _fake_run_guided_upgrade(**kwargs):
            captured.update(kwargs)
            return result

        monkeypatch.setattr(http_vector_client, "_resolve_endpoint", lambda: ("u", "t"))
        monkeypatch.setattr(http_vector_client, "HttpVectorClient", lambda *a, **k: object())
        monkeypatch.setattr(
            factory, "make_catalog_client_for_migration", lambda **k: object()
        )
        monkeypatch.setattr(driver, "run_guided_upgrade", _fake_run_guided_upgrade)
        if token is None:
            monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
        else:
            monkeypatch.setenv("NX_SERVICE_TOKEN", token)

        db = tmp_path / "memory.db"
        cat = tmp_path / ".catalog.db"
        db.write_text("")
        cat.write_text("")
        cli = CliRunner().invoke(
            migrate_cmd.migrate_to_service_cmd,
            ["--db", str(db), "--catalog-db", str(cat)],
        )
        return cli, captured

    def test_clean_run_reports_verified(self, monkeypatch, tmp_path) -> None:
        result = self._result(
            phase="migrated", ok=True, validation=self._validation(unlocked=True)
        )
        cli, captured = self._run(monkeypatch, tmp_path, result=result)
        assert cli.exit_code == 0, cli.output
        assert "VERIFIED" in cli.output
        # The engine was driven with the resolved sources/paths.
        assert captured["t2_db_path"].name == "memory.db"
        assert captured["sources"].catalog_db_path.name == ".catalog.db"

    def test_fresh_user_noop_reported(self, monkeypatch, tmp_path) -> None:
        result = self._result(phase="not-migrating", ok=True, validation=None)
        cli, _ = self._run(monkeypatch, tmp_path, result=result)
        assert cli.exit_code == 0, cli.output
        assert "nothing to migrate" in cli.output.lower()

    def test_sequence_block_exits_nonzero(self, monkeypatch, tmp_path) -> None:
        result = self._result(phase="migrated-failed", ok=False, validation=None)
        cli, _ = self._run(monkeypatch, tmp_path, result=result)
        assert cli.exit_code == 1
        assert "BLOCKED before completion" in cli.output
        assert "dirty T2" in cli.output

    def test_validation_block_offers_rollback(self, monkeypatch, tmp_path) -> None:
        result = self._result(
            phase="migrated", ok=False, validation=self._validation(unlocked=False)
        )
        cli, _ = self._run(monkeypatch, tmp_path, result=result)
        assert cli.exit_code == 1
        assert "FAILED validation" in cli.output
        assert "nx storage migrate vectors --rollback" in cli.output

    def test_missing_service_token_errors_early(self, monkeypatch, tmp_path) -> None:
        result = self._result(
            phase="migrated", ok=True, validation=self._validation(unlocked=True)
        )
        cli, captured = self._run(monkeypatch, tmp_path, result=result, token=None)
        assert cli.exit_code != 0
        assert "NX_SERVICE_TOKEN is required" in cli.output
        # The engine was never reached.
        assert captured == {}


class TestCrossModelRemappable:
    """RDR-162 P2: which unsupported collections the orchestrator auto-migrates
    via stored-text re-embed (cross-model remap to bge-768) vs leaves blocked."""

    def _cls(self, collection, model, *, support, has_data=True, dim=None):
        return CollectionClassification(
            collection=collection, leg="local", model=model, dim=dim,
            support=support, source_count=1 if has_data else 0, has_data=has_data,
        )

    def test_legacy_minilm_is_remappable(self) -> None:
        c = self._cls(ONNX_384, "minilm-l6-v2-384", support="unsupported", dim=384)
        assert cross_model_remappable(c) is True

    def test_supported_bge_is_not_remappable(self) -> None:
        # Already servable — migrates byte-for-byte, never remapped.
        c = self._cls(BGE_768, "bge-base-en-v15-768", support="supported-onnx", dim=768)
        assert cross_model_remappable(c) is False

    def test_voyage_unsupported_is_not_remappable(self) -> None:
        # Credential case (add the key), not a model switch — stays blocked.
        c = self._cls(VOYAGE_CTX_1024, "voyage-context-3", support="unsupported")
        assert cross_model_remappable(c) is False

    def test_non_conformant_name_is_not_remappable(self) -> None:
        c = self._cls(NON_CONFORMANT, None, support="unsupported")
        assert cross_model_remappable(c) is False

    def test_empty_collection_is_not_remappable(self) -> None:
        c = self._cls(ONNX_384, "minilm-l6-v2-384", support="unsupported",
                      has_data=False)
        assert cross_model_remappable(c) is False
