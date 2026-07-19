# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-166 Gap 4 (nexus-cewad) — the estimate-and-confirm Voyage cost guardrail.

The guardrail must GATE the billed `run_guided_upgrade` call, not merely echo a
warning beside it: a declined confirmation aborts before any re-embed runs, and
`--yes` is the only way to proceed non-interactively. A migration that bills
nothing (no cross-model→voyage re-embed) is never prompted.
"""
from __future__ import annotations

import click
import pytest

from nexus.commands.migrate_cmd import _confirm_voyage_cost
from nexus.db.http_vector_client import reset_http_vector_client_for_tests
from nexus.db.managed_endpoint import ManagedCapabilities, ManagedServiceIncompatible
from nexus.migration.detection import DryRunPreview


@pytest.fixture(autouse=True)
def _reset_vector_client_singleton():
    """nexus-b6qlf Fix 1: _run_migration now routes construction through
    get_http_vector_client(), a process-local singleton + probe cache --
    reset it around every test in this file so a mocked construction from
    one test never leaks into another (chromadb-ephemeral-shared-state
    class of bug)."""
    reset_http_vector_client_for_tests()
    yield
    reset_http_vector_client_for_tests()


def _caps() -> ManagedCapabilities:
    return ManagedCapabilities(
        base_url="https://api.conexus-nexus.com",
        app_version="1.0-SNAPSHOT",
        release_version="0.1.99",
        embedding_mode="voyage",
        embedding_models=["voyage-context-3"],
        schema_latest_id="latest",
        schema_changeset_count=42,
    )


def _preview(*, cost: float, tokens: int) -> DryRunPreview:
    return DryRunPreview(
        groups=(),
        unsupported=(),
        legs_with_data=frozenset(),
        migratable_chunks=0,
        total_est_tokens=tokens,
        est_seconds=0.0,
        billed_voyage_tokens=tokens,
        est_voyage_cost_usd=cost,
    )


class TestConfirmVoyageCost:
    def test_free_migration_proceeds_without_prompting(self) -> None:
        calls: list[bool] = []
        proceed = _confirm_voyage_cost(
            _preview(cost=0.0, tokens=0),
            assume_yes=False,
            confirm=lambda msg: calls.append(True) or True,
        )
        assert proceed is True
        assert calls == []  # never prompted — nothing is billed

    def test_assume_yes_bypasses_prompt_on_billed_run(self) -> None:
        calls: list[bool] = []
        proceed = _confirm_voyage_cost(
            _preview(cost=5.0, tokens=1000),
            assume_yes=True,
            confirm=lambda msg: calls.append(True) or True,
        )
        assert proceed is True
        assert calls == []  # --yes skips the prompt

    def test_billed_run_prompts_and_proceeds_on_yes(self) -> None:
        seen: list[str] = []
        proceed = _confirm_voyage_cost(
            _preview(cost=5.0, tokens=1000),
            assume_yes=False,
            confirm=lambda msg: seen.append(msg) or True,
        )
        assert proceed is True
        assert len(seen) == 1  # prompted exactly once

    def test_billed_run_aborts_on_decline(self) -> None:
        proceed = _confirm_voyage_cost(
            _preview(cost=5.0, tokens=1000),
            assume_yes=False,
            confirm=lambda msg: False,
        )
        assert proceed is False  # declined → caller must not run the billed leg


class TestRunMigrationGate:
    """The gate must precede the billed run_guided_upgrade in _run_migration."""

    def _wire(self, monkeypatch, *, cost: float, tokens: int = 1000):
        import nexus.commands.migrate_cmd as mc

        ran: list[str] = []
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        monkeypatch.setattr(mc, "open_read_legs", lambda p: (None, None))
        monkeypatch.setattr(mc, "classify_collections", lambda **k: object())
        monkeypatch.setattr(mc, "voyage_key_available", lambda: True)
        monkeypatch.setattr(
            mc, "build_dry_run_preview", lambda r: _preview(cost=cost, tokens=tokens)
        )
        monkeypatch.setattr(mc, "_close_quietly", lambda c: None)
        # endpoint preflight + client construction
        monkeypatch.setattr(
            "nexus.db.http_vector_client._resolve_endpoint",
            lambda: ("https://api.conexus-nexus.com:443", "tok"),
        )
        # nexus-b6qlf Fix 1: _run_migration now calls get_http_vector_client(),
        # which probes engine-version compatibility in cloud mode. Force
        # local mode for these cost-guardrail-sequencing tests (they care
        # about prompt ordering, not the floor gate) so no probe fires;
        # TestRunMigrationEngineFloorGate below exercises the gate itself.
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr(
            "nexus.db.http_vector_client.HttpVectorClient",
            lambda **k: object(),
        )
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_client_for_migration",
            lambda **k: object(),
        )
        monkeypatch.setattr(
            "nexus.migration.driver.run_guided_upgrade",
            lambda **k: ran.append("billed") or _stub_result(),
        )
        monkeypatch.setattr(mc, "_resolve_db_path", lambda p: _existing_path())
        monkeypatch.setattr(mc, "_resolve_catalog_db_path", lambda p: _existing_path())
        monkeypatch.setattr(mc, "_render_result", lambda r: None)
        return mc, ran

    def test_declined_cost_aborts_before_billed_run(self, monkeypatch) -> None:
        mc, ran = self._wire(monkeypatch, cost=5.0)
        monkeypatch.setattr("click.confirm", lambda *a, **k: False)
        with pytest.raises(click.Abort):
            mc._run_migration(None, None, None, None, assume_yes=False)
        assert ran == []  # billed run never reached

    def test_assume_yes_reaches_billed_run(self, monkeypatch) -> None:
        mc, ran = self._wire(monkeypatch, cost=5.0)
        mc._run_migration(None, None, None, None, assume_yes=True)
        assert ran == ["billed"]

    def test_free_migration_reaches_billed_run_without_prompt(self, monkeypatch) -> None:
        mc, ran = self._wire(monkeypatch, cost=0.0, tokens=0)
        # No confirm patched: a free migration must not call click.confirm.
        monkeypatch.setattr(
            "click.confirm",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompted on free run")),
        )
        mc._run_migration(None, None, None, None, assume_yes=False)
        assert ran == ["billed"]


class TestRunMigrationEngineFloorGate:
    """nexus-b6qlf Fix 1 (CRITICAL): _run_migration constructed a bare
    HttpVectorClient() directly (src/nexus/commands/migrate_cmd.py:241),
    completely bypassing the fail-loud engine-version-floor probe --
    exactly the highest-stakes cloud operation (a data migration) for an
    engine-version mismatch to matter. Must now route through
    get_http_vector_client() and fail loud BEFORE run_guided_upgrade (the
    billed T2/T3 ETL) ever runs."""

    def _wire_cloud(self, monkeypatch, *, cost: float = 0.0, tokens: int = 0):
        import nexus.commands.migrate_cmd as mc

        ran: list[str] = []
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        monkeypatch.setattr(mc, "open_read_legs", lambda p: (None, None))
        monkeypatch.setattr(mc, "classify_collections", lambda **k: object())
        monkeypatch.setattr(mc, "voyage_key_available", lambda: True)
        monkeypatch.setattr(
            mc, "build_dry_run_preview", lambda r: _preview(cost=cost, tokens=tokens)
        )
        monkeypatch.setattr(mc, "_close_quietly", lambda c: None)
        monkeypatch.setattr(
            "nexus.db.http_vector_client._resolve_endpoint",
            lambda: ("https://api.conexus-nexus.com:443", "tok"),
        )
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_client_for_migration",
            lambda **k: object(),
        )
        monkeypatch.setattr(
            "nexus.migration.driver.run_guided_upgrade",
            lambda **k: ran.append("billed") or _stub_result(),
        )
        monkeypatch.setattr(mc, "_resolve_db_path", lambda p: _existing_path())
        monkeypatch.setattr(mc, "_resolve_catalog_db_path", lambda p: _existing_path())
        monkeypatch.setattr(mc, "_render_result", lambda r: None)
        return mc, ran

    def test_incompatible_engine_fails_loud_before_billed_run(self, monkeypatch) -> None:
        mc, ran = self._wire_cloud(monkeypatch)
        monkeypatch.setattr(
            "nexus.db.managed_endpoint.probe_managed_service",
            lambda: (_ for _ in ()).throw(
                ManagedServiceIncompatible(
                    "managed nexus service at https://api.conexus-nexus.com is "
                    "release_version '0.1.8', below the minimum this client "
                    "supports (v0.1.41).",
                    deployed_version="0.1.8",
                    required_version="0.1.41",
                )
            ),
        )
        with pytest.raises(click.ClickException) as exc_info:
            mc._run_migration(None, None, None, None, assume_yes=True)
        # the billed guided-upgrade engine must NEVER run against a bad engine
        assert ran == []
        message = str(exc_info.value)
        assert "0.1.8" in message
        assert "0.1.41" in message

    def test_compatible_engine_still_reaches_billed_run(self, monkeypatch) -> None:
        mc, ran = self._wire_cloud(monkeypatch)
        monkeypatch.setattr(
            "nexus.db.managed_endpoint.probe_managed_service", lambda: _caps()
        )
        mc._run_migration(None, None, None, None, assume_yes=True)
        assert ran == ["billed"]


class TestRunMigrationCollisionGuard:
    """nexus-5b9v0 Fix 1: TargetNameCollisionBlocked used to propagate raw out
    of `_run_migration`'s bare try/finally (no except clause existed) — a
    real collision dumped an unhandled Python traceback at the operator.
    It must now render as a clean click.ClickException, mirroring the
    _resolve_endpoint / get_http_vector_client wrapping convention already in
    this same call site."""

    def _wire(self, monkeypatch, *, cost: float = 0.0, tokens: int = 0):
        import nexus.commands.migrate_cmd as mc

        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        monkeypatch.setattr(mc, "open_read_legs", lambda p: (None, None))
        monkeypatch.setattr(mc, "classify_collections", lambda **k: object())
        monkeypatch.setattr(mc, "voyage_key_available", lambda: True)
        monkeypatch.setattr(
            mc, "build_dry_run_preview", lambda r: _preview(cost=cost, tokens=tokens)
        )
        monkeypatch.setattr(mc, "_close_quietly", lambda c: None)
        monkeypatch.setattr(
            "nexus.db.http_vector_client._resolve_endpoint",
            lambda: ("https://api.conexus-nexus.com:443", "tok"),
        )
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        monkeypatch.setattr(
            "nexus.db.http_vector_client.HttpVectorClient",
            lambda **k: object(),
        )
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_client_for_migration",
            lambda **k: object(),
        )
        monkeypatch.setattr(mc, "_resolve_db_path", lambda p: _existing_path())
        monkeypatch.setattr(mc, "_resolve_catalog_db_path", lambda p: _existing_path())
        return mc

    def test_target_name_collision_renders_as_click_exception(
        self, monkeypatch
    ) -> None:
        from nexus.migration.detection import CollectionClassification
        from nexus.migration.driver import TargetNameCollisionBlocked

        mc = self._wire(monkeypatch)

        misnamed = CollectionClassification(
            collection="code__1-3__voyage-code-3__v1",
            leg="local",
            model="voyage-code-3",
            dim=1024,
            support="unsupported",
            source_count=10,
            has_data=True,
            reason="no NX_VOYAGE_API_KEY configured",
            measured_dim=768,
        )
        honest = CollectionClassification(
            collection="code__1-3__bge-base-en-v15-768__v1",
            leg="local",
            model="bge-base-en-v15-768",
            dim=768,
            support="supported-onnx",
            source_count=10,
            has_data=True,
        )

        def _boom(**_kwargs):
            raise TargetNameCollisionBlocked(
                {"code__1-3__bge-base-en-v15-768__v1": [misnamed, honest]}
            )

        monkeypatch.setattr("nexus.migration.driver.run_guided_upgrade", _boom)

        with pytest.raises(click.ClickException) as exc_info:
            mc._run_migration(None, None, None, None, assume_yes=True)
        # Never a raw TargetNameCollisionBlocked / bare traceback reaching
        # the CLI boundary — always the clean, wrapped exception.
        assert not isinstance(exc_info.value, TargetNameCollisionBlocked)
        message = str(exc_info.value)
        assert "code__1-3__voyage-code-3__v1" in message
        assert "code__1-3__bge-base-en-v15-768__v1" in message

    def test_other_runtime_error_from_guided_upgrade_also_wrapped(
        self, monkeypatch
    ) -> None:
        """nexus-5b9v0 Fix B (round-2): the except clause was narrowed to
        `except TargetNameCollisionBlocked` by name — every OTHER RuntimeError
        `run_guided_upgrade` can raise (e.g. driver.py's validation-setup-
        failure path: `mark_failed(reason); raise`, ~90 lines below the
        collision guard) still escaped as a raw, unwrapped exception. Widened
        to `except RuntimeError` to match the three sibling call sites
        (`_resolve_endpoint`, `get_http_vector_client`,
        `make_catalog_client_for_migration`) literally — this generic
        RuntimeError (standing in for the validation-setup-failure raw
        re-raise) must ALSO surface as click.ClickException now."""
        mc = self._wire(monkeypatch)

        def _boom(**_kwargs):
            raise RuntimeError("validation could not be performed: gate exploded")

        monkeypatch.setattr("nexus.migration.driver.run_guided_upgrade", _boom)

        with pytest.raises(click.ClickException) as exc_info:
            mc._run_migration(None, None, None, None, assume_yes=True)
        assert "gate exploded" in str(exc_info.value)

    def test_validation_setup_filenotfound_wrapped_end_to_end(
        self, monkeypatch, tmp_path
    ) -> None:
        """nexus-5b9v0 round-3 Fix D (bead nexus-rndvq, CRITICAL). Unlike
        ``test_other_runtime_error_from_guided_upgrade_also_wrapped`` above
        (which monkeypatches ``run_guided_upgrade`` itself to raise a
        synthetic ``RuntimeError``, bypassing the actual ``reopen_leg` ->
        ``open_local_read_client`` code path entirely — the round-3
        remediation's test was VACUOUS w.r.t. this claim), this test drives
        the REAL ``driver.run_guided_upgrade`` all the way to the real
        validation-setup except-block: only ``open_read_legs``,
        ``classify_collections``, and ``run_sequenced_migration`` are
        stubbed (the detect/sequence steps, exactly like
        ``tests/migration/test_driver.py``'s ``_patch_engine``); the
        validation-reopen step is left REAL, and ``--local-path`` points at
        a directory that does not exist, so the real
        ``chroma_read.open_local_read_client`` raises a genuine
        ``FileNotFoundError`` — the exact production scenario (the local
        Chroma store vanishing between the ETL write and the validation
        reopen). This proves the end-to-end claim that motivated widening
        the CLI's except clause, which the synthetic-RuntimeError test above
        could not."""
        from nexus.migration import driver
        from nexus.migration.detection import (
            CollectionClassification,
            DetectionReport,
        )
        from nexus.migration.sequencer import SequenceOutcome

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "config"))
        mc = self._wire(monkeypatch)

        detection = DetectionReport(
            classifications=(
                CollectionClassification(
                    collection="code__o__bge-base-en-v15-768__v1",
                    leg="local",
                    model="bge-base-en-v15-768",
                    dim=768,
                    support="supported-onnx",
                    source_count=10,
                    has_data=True,
                    reason="",
                ),
            )
        )
        sequence = SequenceOutcome(
            ok=True,
            phase="migrated",
            collections_total=1,
            collections_done=1,
            t2_total_failed=0,
            legs_attempted=("local",),
            legs_ok=("local",),
            blocked_reason=None,
            t2_report={"summary": {"total_failed": 0}},
        )

        monkeypatch.setattr(
            driver, "open_read_legs", lambda local_path=None: (object(), object())
        )
        monkeypatch.setattr(driver, "classify_collections", lambda **_k: detection)
        # nexus-jxizy.10.7: the guided driver now runs the land-then-transform
        # sequencer; the wrap-behavior under test (FileNotFoundError -> loud
        # ClickException) fires in the reopen-for-landing step before this
        # fake would matter.
        monkeypatch.setattr(
            driver, "run_land_then_transform_migration", lambda *_a, **_k: sequence
        )
        monkeypatch.setattr(driver, "voyage_key_available", lambda: True)

        nonexistent_local = tmp_path / "chroma_that_does_not_exist"

        with pytest.raises(click.ClickException) as exc_info:
            mc._run_migration(
                str(nonexistent_local), None, None, None, assume_yes=True
            )
        # Must never be the raw FileNotFoundError escaping unwrapped.
        assert not isinstance(exc_info.value, FileNotFoundError)
        message = str(exc_info.value)
        assert "local Chroma store not found" in message
        assert str(nonexistent_local) in message


def _stub_result():
    class _Seq:
        phase = "migrated"

    class _R:
        sequence = _Seq()

    return _R()


class _ExistingPath:
    def exists(self) -> bool:
        return True


def _existing_path() -> _ExistingPath:
    return _ExistingPath()
