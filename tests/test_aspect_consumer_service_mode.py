# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Service-mode guard tests for aspect consumer modules.

Covers nexus-gmiaf.35 (aspect_promotion.py), nexus-gmiaf.36
(operators/aspect_sql.py), and nexus-gmiaf.37 (commands/aspects.py
gc-fixtures) — the three remaining direct-SQL aspect consumers that
must behave correctly when NX_STORAGE_BACKEND_DOCUMENT_ASPECTS=service.

Disposition per module:
  .35 aspect_promotion.py — CLI-only; guard raises error (clean CLI exit).
  .36 operators/aspect_sql.py — RUNTIME; auto mode → LLM fallback (None),
                                 aspects mode → stub result (no crash).
  .37 commands/aspects.py gc-fixtures — CLI-only; guard → clean exit code 2.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands.aspects import aspects_group
from nexus.operators import aspect_sql


# ── Shared fixture: patch document_aspects to service mode ────────────────────


@pytest.fixture()
def service_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set NX_STORAGE_BACKEND_DOCUMENT_ASPECTS=service for the test."""
    monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")


# ── .35: aspect_promotion.py — CLI-only guard ─────────────────────────────────


class TestAspectPromotionServiceModeGuard:
    """promote_extras_field and list_promotions must fail-loud with a clear
    message in service mode. The CLI wrapper (enrich.py) must catch the error
    and exit cleanly (code != 0) rather than showing an unhandled traceback."""

    def test_promote_extras_field_raises_in_service_mode(
        self, service_mode: None,
    ) -> None:
        """promote_extras_field raises with a message referencing the bead."""
        from nexus.aspect_promotion import promote_extras_field

        class FakeDB:
            pass

        with pytest.raises(NotImplementedError, match="nexus-gmiaf.35"):
            promote_extras_field(FakeDB(), "my_field")

    def test_list_promotions_raises_in_service_mode(
        self, service_mode: None,
    ) -> None:
        """list_promotions raises with a message referencing the bead."""
        from nexus.aspect_promotion import list_promotions

        class FakeDB:
            pass

        with pytest.raises(NotImplementedError, match="nexus-gmiaf.35"):
            list_promotions(FakeDB())

    def test_cli_promote_field_fails_cleanly_in_service_mode(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        service_mode: None,
    ) -> None:
        """CLI 'nx enrich aspects-promote-field' exits with non-zero code and a
        clear message in service mode — no unhandled exception traceback."""
        from nexus.commands.enrich import enrich

        # Provide a real db path so T2Database can open (the guard fires
        # before any real SQL).
        db_path = tmp_path / "memory.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)

        runner = CliRunner()
        result = runner.invoke(enrich, ["aspects-promote-field", "my_field"])

        assert result.exit_code != 0, (
            "CLI must exit non-zero in service mode; "
            f"got 0 with output: {result.output}"
        )
        # The error message must mention the service backend or the bead.
        combined = (result.output or "") + (result.exception.__str__() if result.exception else "")
        assert "service" in combined.lower() or "nexus-gmiaf.35" in combined, (
            f"Expected 'service' or bead reference in output; got: {result.output!r}, "
            f"exception: {result.exception!r}"
        )
        # Must NOT show an unhandled exception traceback.
        assert "Traceback" not in (result.output or ""), (
            f"Got unhandled exception traceback: {result.output!r}"
        )

    def test_cli_history_fails_cleanly_in_service_mode(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        service_mode: None,
    ) -> None:
        """CLI '--history' flag also fails cleanly in service mode."""
        from nexus.commands.enrich import enrich

        db_path = tmp_path / "memory.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)

        runner = CliRunner()
        result = runner.invoke(enrich, ["aspects-promote-field", "--history", "_x"])

        assert result.exit_code != 0, (
            f"CLI --history must exit non-zero in service mode; output: {result.output!r}"
        )
        assert "Traceback" not in (result.output or ""), (
            f"Got unhandled exception traceback: {result.output!r}"
        )


# ── .36: operators/aspect_sql.py — LLM fallback in service mode ───────────────


def _items_json(*paths: str, collection: str = "knowledge__delos") -> str:
    return json.dumps([
        {"id": p, "collection": collection, "source_path": p}
        for p in paths
    ])


def _groups_json(key_value: str, *paths: str) -> str:
    return json.dumps([{
        "key_value": key_value,
        "items": [
            {"id": p, "collection": "knowledge__delos", "source_path": p}
            for p in paths
        ],
    }])


class TestAspectSqlServiceModeFallback:
    """In service mode, the SQL fast-path operators must not crash.

    source='auto' (default) must return None → operator falls back to LLM.
    source='aspects' must return a stub result with an explanatory note.
    source='llm' is already a no-op (handled before the SQL path).

    These tests verify the guard fires BEFORE any SQL attempt, so no real
    T2 database is needed. The test is purely about routing."""

    # --- try_filter ---

    def test_filter_auto_returns_none_in_service_mode(
        self, service_mode: None,
    ) -> None:
        """source='auto' + service mode → None (LLM fallback, no crash)."""
        items = _items_json("/papers/paxos.pdf")
        result = aspect_sql.try_filter(
            items,
            "paxos",
            source="auto",
            aspect_field="proposed_method",
        )
        assert result is None, (
            f"Expected None (LLM fallback) in service mode auto, got: {result!r}"
        )

    def test_filter_aspects_mode_returns_stub_in_service_mode(
        self, service_mode: None,
    ) -> None:
        """source='aspects' + service mode → stub result (no crash)."""
        items = _items_json("/papers/paxos.pdf")
        result = aspect_sql.try_filter(
            items,
            "paxos",
            source="aspects",
            aspect_field="proposed_method",
        )
        assert result is not None, (
            "Expected stub dict (not None) in aspects mode + service mode"
        )
        # Must have the operator's schema shape.
        assert "items" in result
        assert "rationale" in result
        # Stub items must be empty (no SQL ran).
        assert result["items"] == []

    def test_filter_llm_mode_unaffected_by_service_mode(
        self, service_mode: None,
    ) -> None:
        """source='llm' → None unconditionally (service mode irrelevant)."""
        items = _items_json("/papers/paxos.pdf")
        result = aspect_sql.try_filter(
            items,
            "paxos",
            source="llm",
            aspect_field="proposed_method",
        )
        assert result is None

    # --- try_groupby ---

    def test_groupby_auto_returns_none_in_service_mode(
        self, service_mode: None,
    ) -> None:
        """source='auto' + service mode → None (LLM fallback, no crash)."""
        items = _items_json("/papers/paxos.pdf")
        result = aspect_sql.try_groupby(
            items,
            "venue",
            source="auto",
            aspect_field="extras.venue",
        )
        assert result is None, (
            f"Expected None (LLM fallback) in service mode auto, got: {result!r}"
        )

    def test_groupby_aspects_mode_returns_stub_in_service_mode(
        self, service_mode: None,
    ) -> None:
        """source='aspects' + service mode → stub result (no crash)."""
        items = _items_json("/papers/paxos.pdf")
        result = aspect_sql.try_groupby(
            items,
            "venue",
            source="aspects",
            aspect_field="extras.venue",
        )
        assert result is not None, (
            "Expected stub dict (not None) in aspects mode + service mode"
        )
        assert "groups" in result

    # --- try_aggregate ---

    def test_aggregate_auto_returns_none_in_service_mode(
        self, service_mode: None,
    ) -> None:
        """source='auto' + service mode → None (LLM fallback, no crash)."""
        groups = _groups_json("VLDB", "/papers/paxos.pdf")
        result = aspect_sql.try_aggregate(
            groups,
            "avg confidence",
            source="auto",
            aspect_field="confidence",
        )
        assert result is None, (
            f"Expected None (LLM fallback) in service mode auto, got: {result!r}"
        )

    def test_aggregate_count_unaffected_by_service_mode(
        self, service_mode: None,
    ) -> None:
        """'count' reducer doesn't touch T2 SQL at all; service mode must not
        block it (count is computed purely from items)."""
        groups = _groups_json("VLDB", "/papers/paxos.pdf", "/papers/raft.pdf")
        result = aspect_sql.try_aggregate(
            groups,
            "count",
            source="auto",
            aspect_field="",
        )
        # count uses len(items) — no SQL dispatch — must work in service mode.
        assert result is not None
        assert result["aggregates"][0]["summary"] == "2 item(s)"

    def test_aggregate_count_distinct_unaffected_by_service_mode(
        self, service_mode: None,
    ) -> None:
        """'count distinct' deduplicates by id/identity — no SQL dispatch."""
        groups = _groups_json("VLDB", "/papers/paxos.pdf", "/papers/raft.pdf")
        result = aspect_sql.try_aggregate(
            groups,
            "count distinct",
            source="auto",
            aspect_field="",
        )
        assert result is not None
        assert "2 distinct" in result["aggregates"][0]["summary"]

    def test_aggregate_mixed_reducer_auto_returns_none(
        self, service_mode: None,
    ) -> None:
        """Mixed groups list (count group first, confidence group second) with
        source='auto' and a confidence reducer must return None cleanly without
        partial aggregation state.

        Regression guard for the guard-inside-loop bug (nexus-gmiaf.36):
        previously, try_aggregate appended the count group's result to
        `aggregates` before hitting `return None` on the confidence group,
        silently discarding the count state. The guard is now hoisted before
        the loop so the whole call returns None consistently."""
        groups = json.dumps([
            {
                "key_value": "VLDB",
                "items": [
                    {"id": "/papers/paxos.pdf", "collection": "knowledge__delos",
                     "source_path": "/papers/paxos.pdf"},
                    {"id": "/papers/raft.pdf", "collection": "knowledge__delos",
                     "source_path": "/papers/raft.pdf"},
                ],
            },
            {
                "key_value": "SOSP",
                "items": [
                    {"id": "/papers/bigtable.pdf", "collection": "knowledge__delos",
                     "source_path": "/papers/bigtable.pdf"},
                ],
            },
        ])
        result = aspect_sql.try_aggregate(
            groups,
            "avg confidence",
            source="auto",
            aspect_field="confidence",
        )
        assert result is None, (
            "Mixed-group confidence reducer in auto+service mode must return "
            f"None (LLM fallback), not partial state. Got: {result!r}"
        )

    def test_aggregate_aspects_mode_confidence_returns_stub(
        self, service_mode: None,
    ) -> None:
        """aspects mode + confidence reducer → stub (no crash)."""
        groups = _groups_json("VLDB", "/papers/paxos.pdf")
        result = aspect_sql.try_aggregate(
            groups,
            "avg confidence",
            source="aspects",
            aspect_field="confidence",
        )
        assert result is not None
        assert "aggregates" in result
        # Stub must reference the service backend and tracking bead so the
        # operator caller knows why the fast-path was bypassed.
        summary = result["aggregates"][0]["summary"]
        assert "service" in summary or "nexus-gmiaf.36" in summary, (
            f"Expected 'service' or 'nexus-gmiaf.36' in stub summary; got: {summary!r}"
        )


# ── .37: commands/aspects.py gc-fixtures — CLI guard ──────────────────────────


class TestGcFixturesServiceModeGuard:
    """gc-fixtures must exit with a non-zero code and a clear message when
    document_aspects is in service mode. It must NOT show an unhandled
    exception traceback."""

    def test_gc_fixtures_dry_run_fails_cleanly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        service_mode: None,
    ) -> None:
        """Dry-run (no --yes): service mode → exit code 2, clear message."""
        mem_path = tmp_path / "memory.db"
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path",
            lambda: mem_path,
        )
        # Create the db so the 'missing db' guard doesn't short-circuit.
        mem_path.touch()

        runner = CliRunner()
        result = runner.invoke(aspects_group, ["gc-fixtures"])

        assert result.exit_code == 2, (
            f"Expected exit code 2 (click.UsageError) in service mode; "
            f"got {result.exit_code}. Output: {result.output!r}"
        )
        output = (result.output or "")
        assert "service" in output.lower() or "gc-fixtures" in output.lower() or "nexus-gmiaf.37" in output, (
            f"Expected 'service' or bead reference in output; got: {output!r}"
        )
        assert "Traceback" not in output, (
            f"Got unhandled exception traceback: {output!r}"
        )

    def test_gc_fixtures_apply_fails_cleanly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        service_mode: None,
    ) -> None:
        """--yes flag: service mode → exit code 2, clear message."""
        mem_path = tmp_path / "memory.db"
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path",
            lambda: mem_path,
        )
        mem_path.touch()

        runner = CliRunner()
        result = runner.invoke(aspects_group, ["gc-fixtures", "--yes"])

        assert result.exit_code == 2, (
            f"Expected exit code 2 (click.UsageError) in service mode; "
            f"got {result.exit_code}. Output: {result.output!r}"
        )
        assert "Traceback" not in (result.output or ""), (
            f"Got unhandled exception traceback: {result.output!r}"
        )

    def test_missing_db_still_exits_zero_in_service_mode(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        service_mode: None,
    ) -> None:
        """A missing T2 database exits 0 (nothing to clean) regardless of
        service mode — the db-absent guard fires before the service guard."""
        mem_path = tmp_path / "does_not_exist.db"
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path",
            lambda: mem_path,
        )
        runner = CliRunner()
        result = runner.invoke(aspects_group, ["gc-fixtures", "--yes"])
        assert result.exit_code == 0, (
            f"Missing db must still exit 0; got: {result.exit_code}, output: {result.output!r}"
        )
        assert "nothing to do" in (result.output or "")
