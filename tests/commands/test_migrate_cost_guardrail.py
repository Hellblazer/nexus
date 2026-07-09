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
                    "supports (v0.1.34).",
                    deployed_version="0.1.8",
                    required_version="0.1.34",
                )
            ),
        )
        with pytest.raises(click.ClickException) as exc_info:
            mc._run_migration(None, None, None, None, assume_yes=True)
        # the billed guided-upgrade engine must NEVER run against a bad engine
        assert ran == []
        message = str(exc_info.value)
        assert "0.1.8" in message
        assert "0.1.34" in message

    def test_compatible_engine_still_reaches_billed_run(self, monkeypatch) -> None:
        mc, ran = self._wire_cloud(monkeypatch)
        monkeypatch.setattr(
            "nexus.db.managed_endpoint.probe_managed_service", lambda: _caps()
        )
        mc._run_migration(None, None, None, None, assume_yes=True)
        assert ran == ["billed"]


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
