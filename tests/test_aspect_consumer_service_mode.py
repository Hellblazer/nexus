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


# ── .36: operators/aspect_sql.py — service-mode routing (nexus-l9hd8) ─────────
#
# In service mode, the SQL fast-path operators now call the HTTP client instead
# of returning None/stub. These tests mock _get_http_aspects_client() to verify
# the right methods are invoked and that results are passed back correctly.


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


class _MockClient:
    """Minimal HTTP client stub for service-mode routing tests."""

    def __init__(
        self,
        *,
        filter_result: list[str] | None = None,
        groupby_result: dict | None = None,
        confidence_result: float | None = None,
    ) -> None:
        self._filter_result = filter_result or []
        self._groupby_result = groupby_result or {}
        self._confidence_result = confidence_result
        self.filter_calls: list[tuple] = []
        self.groupby_calls: list[tuple] = []
        self.confidence_calls: list[tuple] = []
        self.closed = False

    def operator_filter(self, source_uris: list, field: str, predicate: str) -> list[str]:
        self.filter_calls.append((source_uris, field, predicate))
        return self._filter_result

    def operator_groupby(self, source_uris: list, field: str) -> dict:
        self.groupby_calls.append((source_uris, field))
        return self._groupby_result

    def operator_confidence_aggregate(self, source_uris: list, reducer_kind: str) -> float | None:
        self.confidence_calls.append((source_uris, reducer_kind))
        return self._confidence_result

    def close(self) -> None:
        self.closed = True


class TestAspectSqlServiceModeRouting:
    """In service mode (nexus-l9hd8), the SQL fast-path operators route to the
    HTTP client.  These tests mock _get_http_aspects_client() to verify routing
    and result pass-through without requiring a live service.

    The old guard behavior (None/stub returns) was removed; the operators now
    produce real results from the service path in service mode."""

    # --- try_filter ---

    def test_filter_auto_calls_service_and_returns_matched_items(
        self, service_mode: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """service mode + source='auto': matched URIs from service → items returned."""
        from nexus.aspect_readers import uri_for
        path = "/papers/paxos.pdf"
        collection = "knowledge__delos"
        expected_uri = uri_for(collection, path) or f"file://{path}"

        mock = _MockClient(filter_result=[expected_uri])
        monkeypatch.setattr(aspect_sql, "_get_http_aspects_client", lambda: mock)

        items = _items_json(path, collection=collection)
        result = aspect_sql.try_filter(
            items,
            "paxos",
            source="auto",
            aspect_field="proposed_method",
        )
        assert result is not None, f"Expected a result dict, got None; mock.filter_calls={mock.filter_calls}"
        assert "items" in result
        assert len(result["items"]) == 1
        assert mock.filter_calls, "Expected HTTP client.operator_filter to be called"
        assert mock.closed, "Client must be closed after call"

    def test_filter_aspects_mode_calls_service_returns_schema(
        self, service_mode: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """source='aspects' + service mode: service call happens, result has schema."""
        from nexus.aspect_readers import uri_for
        path = "/papers/paxos.pdf"
        collection = "knowledge__delos"
        expected_uri = uri_for(collection, path) or f"file://{path}"

        mock = _MockClient(filter_result=[expected_uri])
        monkeypatch.setattr(aspect_sql, "_get_http_aspects_client", lambda: mock)

        items = _items_json(path, collection=collection)
        result = aspect_sql.try_filter(
            items,
            "paxos",
            source="aspects",
            aspect_field="proposed_method",
        )
        assert result is not None
        assert "items" in result
        assert "rationale" in result
        assert mock.filter_calls, "Expected HTTP client.operator_filter to be called"

    def test_filter_llm_mode_unaffected_by_service_mode(
        self, service_mode: None,
    ) -> None:
        """source='llm' → None unconditionally (service mode irrelevant, no client call)."""
        items = _items_json("/papers/paxos.pdf")
        result = aspect_sql.try_filter(
            items,
            "paxos",
            source="llm",
            aspect_field="proposed_method",
        )
        assert result is None

    def test_filter_no_match_from_service_returns_empty_items(
        self, service_mode: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Service returns empty list → no items in result (no crash, no LLM fallback)."""
        mock = _MockClient(filter_result=[])
        monkeypatch.setattr(aspect_sql, "_get_http_aspects_client", lambda: mock)

        items = _items_json("/papers/paxos.pdf")
        result = aspect_sql.try_filter(
            items,
            "paxos",
            source="aspects",
            aspect_field="proposed_method",
        )
        assert result is not None
        assert result["items"] == []

    # --- try_groupby ---

    def test_groupby_calls_service_returns_groups(
        self, service_mode: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """service mode: HTTP client called, grouped result returned."""
        from nexus.aspect_readers import uri_for
        path = "/papers/paxos.pdf"
        collection = "knowledge__delos"
        expected_uri = uri_for(collection, path) or f"file://{path}"

        mock = _MockClient(groupby_result={expected_uri: "VLDB"})
        monkeypatch.setattr(aspect_sql, "_get_http_aspects_client", lambda: mock)

        items = _items_json(path, collection=collection)
        result = aspect_sql.try_groupby(
            items,
            "venue",
            source="auto",
            aspect_field="extras.venue",
        )
        assert result is not None, f"Expected groups dict, got None; calls={mock.groupby_calls}"
        assert "groups" in result
        assert mock.groupby_calls, "Expected HTTP client.operator_groupby to be called"
        assert mock.closed

    def test_groupby_aspects_mode_calls_service(
        self, service_mode: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """source='aspects' + service mode: client called, groups returned."""
        from nexus.aspect_readers import uri_for
        path = "/papers/paxos.pdf"
        collection = "knowledge__delos"
        expected_uri = uri_for(collection, path) or f"file://{path}"

        mock = _MockClient(groupby_result={expected_uri: "SOSP"})
        monkeypatch.setattr(aspect_sql, "_get_http_aspects_client", lambda: mock)

        items = _items_json(path, collection=collection)
        result = aspect_sql.try_groupby(
            items,
            "venue",
            source="aspects",
            aspect_field="extras.venue",
        )
        assert result is not None
        assert "groups" in result
        assert mock.groupby_calls

    def test_groupby_absent_uri_lands_in_unassigned(
        self, service_mode: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """URIs absent from service response land in 'unassigned' group."""
        mock = _MockClient(groupby_result={})  # no matches
        monkeypatch.setattr(aspect_sql, "_get_http_aspects_client", lambda: mock)

        items = _items_json("/papers/paxos.pdf")
        result = aspect_sql.try_groupby(
            items,
            "venue",
            source="aspects",
            aspect_field="extras.venue",
        )
        assert result is not None
        groups = result["groups"]
        assert any(g["key_value"] == "unassigned" for g in groups), (
            f"Expected 'unassigned' group; got: {groups!r}"
        )

    # --- try_aggregate ---

    def test_aggregate_confidence_calls_service(
        self, service_mode: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """source='auto' + confidence reducer: HTTP client called, value returned."""
        mock = _MockClient(confidence_result=0.82)
        monkeypatch.setattr(aspect_sql, "_get_http_aspects_client", lambda: mock)

        groups = _groups_json("VLDB", "/papers/paxos.pdf")
        result = aspect_sql.try_aggregate(
            groups,
            "avg confidence",
            source="auto",
            aspect_field="confidence",
        )
        assert result is not None, f"Expected aggregates dict; got None. Calls: {mock.confidence_calls}"
        assert "aggregates" in result
        assert mock.confidence_calls, "Expected HTTP client.operator_confidence_aggregate called"
        assert mock.closed

    def test_aggregate_count_unaffected_by_service_mode(
        self, service_mode: None,
    ) -> None:
        """'count' reducer doesn't touch T2 SQL or HTTP; service mode must not
        block it (count is computed purely from items)."""
        groups = _groups_json("VLDB", "/papers/paxos.pdf", "/papers/raft.pdf")
        result = aspect_sql.try_aggregate(
            groups,
            "count",
            source="auto",
            aspect_field="",
        )
        # count uses len(items) — no SQL/HTTP dispatch — must work in service mode.
        assert result is not None
        assert result["aggregates"][0]["summary"] == "2 item(s)"

    def test_aggregate_count_distinct_unaffected_by_service_mode(
        self, service_mode: None,
    ) -> None:
        """'count distinct' deduplicates by id/identity — no SQL/HTTP dispatch."""
        groups = _groups_json("VLDB", "/papers/paxos.pdf", "/papers/raft.pdf")
        result = aspect_sql.try_aggregate(
            groups,
            "count distinct",
            source="auto",
            aspect_field="",
        )
        assert result is not None
        assert "2 distinct" in result["aggregates"][0]["summary"]

    def test_aggregate_mixed_groups_confidence_calls_service(
        self, service_mode: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Multi-group confidence reducer: HTTP client called once with all URIs,
        result applied across groups (nexus-l9hd8 parity with SQLite fold)."""
        mock = _MockClient(confidence_result=0.75)
        monkeypatch.setattr(aspect_sql, "_get_http_aspects_client", lambda: mock)

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
        assert result is not None, f"Expected aggregates dict in service mode; got None"
        assert "aggregates" in result
        assert mock.confidence_calls, "HTTP client must be called for confidence reducer"

    def test_aggregate_aspects_mode_confidence_calls_service(
        self, service_mode: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """aspects mode + confidence reducer: service call happens, result returned."""
        mock = _MockClient(confidence_result=0.9)
        monkeypatch.setattr(aspect_sql, "_get_http_aspects_client", lambda: mock)

        groups = _groups_json("VLDB", "/papers/paxos.pdf")
        result = aspect_sql.try_aggregate(
            groups,
            "avg confidence",
            source="aspects",
            aspect_field="confidence",
        )
        assert result is not None
        assert "aggregates" in result
        assert mock.confidence_calls


class TestServiceErrorFallback:
    """Service transport errors in source='auto' must trigger LLM fallback (return None),
    not propagate as exceptions. (nexus-l9hd8 Sig-2 fix)

    In source='aspects' mode service errors return a stub result (no LLM fallback).
    """

    class _RaisingClient:
        """Mock client that always raises a RuntimeError (simulates missing NX_SERVICE_PORT)."""

        def operator_filter(self, *_a, **_kw):
            raise RuntimeError("service port not configured")

        def operator_groupby(self, *_a, **_kw):
            raise RuntimeError("connection refused")

        def operator_confidence_aggregate(self, *_a, **_kw):
            raise RuntimeError("service unreachable")

        def close(self) -> None:
            pass

    @pytest.fixture(autouse=True)
    def _patch_client(self, monkeypatch: pytest.MonkeyPatch, service_mode: None) -> None:  # noqa: PT004
        """Inject the raising client and set service mode for all tests in this class."""
        monkeypatch.setattr(
            aspect_sql, "_get_http_aspects_client",
            lambda: self._RaisingClient(),
        )

    def test_filter_auto_service_error_falls_back_to_none(self) -> None:
        """source='auto': filter service error → None (triggers LLM fallback, no raise)."""
        items = _items_json("/papers/paxos.pdf")
        result = aspect_sql.try_filter(
            items, "paxos", source="auto", aspect_field="proposed_method",
        )
        assert result is None, (
            "Expected None (LLM fallback) on service error in source='auto'; "
            f"got: {result!r}"
        )

    def test_filter_aspects_service_error_returns_stub(self) -> None:
        """source='aspects': filter service error → stub result, not raise."""
        items = _items_json("/papers/paxos.pdf")
        result = aspect_sql.try_filter(
            items, "paxos", source="aspects", aspect_field="proposed_method",
        )
        assert result is not None, (
            "source='aspects' must return stub on error, not raise"
        )
        assert result["items"] == []

    def test_groupby_auto_service_error_falls_back_to_none(self) -> None:
        """source='auto': groupby service error → None (LLM fallback)."""
        items = _items_json("/papers/paxos.pdf")
        result = aspect_sql.try_groupby(
            items, "venue", source="auto", aspect_field="extras.venue",
        )
        assert result is None, (
            "Expected None (LLM fallback) on service error in source='auto'; "
            f"got: {result!r}"
        )

    def test_groupby_aspects_service_error_returns_stub(self) -> None:
        """source='aspects': groupby service error → stub result, not raise."""
        items = _items_json("/papers/paxos.pdf")
        result = aspect_sql.try_groupby(
            items, "venue", source="aspects", aspect_field="extras.venue",
        )
        assert result is not None
        assert "groups" in result

    def test_aggregate_confidence_auto_service_error_falls_back_to_none(self) -> None:
        """source='auto': confidence aggregate service error → None (LLM fallback)."""
        groups = _groups_json("VLDB", "/papers/paxos.pdf")
        result = aspect_sql.try_aggregate(
            groups, "avg confidence", source="auto", aspect_field="confidence",
        )
        assert result is None, (
            "Expected None (LLM fallback) on service error in source='auto'; "
            f"got: {result!r}"
        )

    def test_aggregate_confidence_aspects_service_error_returns_stub(self) -> None:
        """source='aspects': confidence aggregate service error → stub result, not raise."""
        groups = _groups_json("VLDB", "/papers/paxos.pdf")
        result = aspect_sql.try_aggregate(
            groups, "avg confidence", source="aspects", aspect_field="confidence",
        )
        assert result is not None
        assert "aggregates" in result


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
