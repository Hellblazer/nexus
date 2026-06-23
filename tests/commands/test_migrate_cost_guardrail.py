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
from nexus.migration.detection import DryRunPreview


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
