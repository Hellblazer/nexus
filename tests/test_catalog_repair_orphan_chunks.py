# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the RDR-101 Phase 2 PR ε ``repair-orphan-chunks`` verb.

Coverage:
- Verb fails when catalog is not initialized or events.jsonl is empty.
- Default mode lists orphan ChunkIndexed events.
- ``--list`` filters by ``--collection``.
- ``--assign CHUNK_ID:DOC_ID`` appends a corrective event with the right
  doc_id and synthesized_orphan=False.
- Multiple ``--assign`` pairs in one invocation.
- ``--assign`` for a non-orphan chunk is skipped (the orphan was already
  resolved or the chunk_id is unknown), surfaced in the skipped report.
- ``--assign`` malformed pair → usage error.
- Last-event-wins: replay of the log after assignment shows the chunk
  resolved (the original orphan event stays in the log; the projector
  dispatches on the latest event per chunk_id).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.catalog import events as ev
from nexus.catalog.catalog import Catalog
from nexus.catalog.event_log import EventLog
from nexus.commands.catalog import repair_orphan_chunks_cmd


@pytest.fixture()
def isolated_nexus(tmp_path: Path) -> Path:
    return tmp_path / "test-catalog"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _seed(catalog_dir: Path, events: list[ev.Event]) -> None:
    catalog_dir.mkdir(parents=True, exist_ok=True)
    Catalog.init(catalog_dir)
    log = EventLog(catalog_dir)
    log.append_many(events)


def _chunk(
    chunk_id: str, doc_id: str, coll_id: str,
    *, orphan: bool = False, chash: str = "h",
) -> ev.Event:
    return ev.Event(
        type=ev.TYPE_CHUNK_INDEXED, v=0,
        payload=ev.ChunkIndexedPayload(
            chunk_id=chunk_id, chash=chash, doc_id=doc_id,
            coll_id=coll_id, position=0,
            synthesized_orphan=orphan,
        ),
        ts="2026-04-30T00:00:00Z",
    )


# ── Usage ────────────────────────────────────────────────────────────────


class TestUsage:
    def test_missing_catalog(self, isolated_nexus, runner):
        result = runner.invoke(repair_orphan_chunks_cmd, [])
        assert result.exit_code != 0
        assert "not initialized" in result.output.lower()

    def test_empty_log(self, isolated_nexus, runner):
        Catalog.init(isolated_nexus)
        result = runner.invoke(repair_orphan_chunks_cmd, [])
        assert result.exit_code != 0
        assert "empty" in result.output.lower()

    def test_list_and_assign_together(self, isolated_nexus, runner):
        _seed(isolated_nexus, [_chunk("ch1", "", "code__test", orphan=True)])
        result = runner.invoke(
            repair_orphan_chunks_cmd,
            ["--list", "--assign", "ch1:doc-7"],
        )
        assert result.exit_code != 0
        assert "not both" in (result.output + (result.stderr or ""))

    def test_assign_malformed_pair(self, isolated_nexus, runner):
        _seed(isolated_nexus, [_chunk("ch1", "", "code__test", orphan=True)])
        for bad in ["ch1", ":doc-7", "ch1:", ""]:
            result = runner.invoke(
                repair_orphan_chunks_cmd, ["--assign", bad],
            )
            assert result.exit_code != 0


# ── List mode ────────────────────────────────────────────────────────────


class TestListMode:
    def test_default_lists_orphans(self, isolated_nexus, runner):
        _seed(isolated_nexus, [
            _chunk("ch1", "uuid7-A", "code__test"),  # not orphan
            _chunk("orph1", "", "code__test", orphan=True),
            _chunk("orph2", "", "knowledge__paper", orphan=True),
        ])
        result = runner.invoke(
            repair_orphan_chunks_cmd, ["--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["mode"] == "list"
        assert payload["orphans_count"] == 2
        chunk_ids = {o["chunk_id"] for o in payload["orphans"]}
        assert chunk_ids == {"orph1", "orph2"}

    def test_collection_filter(self, isolated_nexus, runner):
        _seed(isolated_nexus, [
            _chunk("orph1", "", "code__a", orphan=True),
            _chunk("orph2", "", "code__b", orphan=True),
        ])
        result = runner.invoke(
            repair_orphan_chunks_cmd,
            ["--list", "--collection", "code__a", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        chunk_ids = {o["chunk_id"] for o in payload["orphans"]}
        assert chunk_ids == {"orph1"}


# ── Assign mode ──────────────────────────────────────────────────────────


class TestAssignMode:
    def test_assign_appends_corrective_event(self, isolated_nexus, runner):
        _seed(isolated_nexus, [
            _chunk("orph1", "", "code__test", orphan=True, chash="ch1hash"),
        ])
        result = runner.invoke(
            repair_orphan_chunks_cmd,
            ["--assign", "orph1:doc-uuid7-A", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["repairs_count"] == 1
        assert payload["skipped_count"] == 0
        assert payload["remaining_orphans"] == 0

        # Replay events.jsonl: the latest event per (coll, chunk_id) must
        # carry doc_id="doc-uuid7-A" and synthesized_orphan=False, AND
        # the chash from the original orphan must survive.
        log = EventLog(isolated_nexus)
        events = list(log.replay())
        last_for_chunk = events[-1]
        assert last_for_chunk.type == ev.TYPE_CHUNK_INDEXED
        assert last_for_chunk.payload.chunk_id == "orph1"
        assert last_for_chunk.payload.doc_id == "doc-uuid7-A"
        assert last_for_chunk.payload.synthesized_orphan is False
        assert last_for_chunk.payload.chash == "ch1hash"

    def test_assign_multiple_pairs(self, isolated_nexus, runner):
        _seed(isolated_nexus, [
            _chunk("orph1", "", "code__test", orphan=True),
            _chunk("orph2", "", "code__test", orphan=True),
        ])
        result = runner.invoke(
            repair_orphan_chunks_cmd,
            ["--assign", "orph1:doc-1", "--assign", "orph2:doc-2", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["repairs_count"] == 2
        assert payload["remaining_orphans"] == 0

    def test_assign_unknown_chunk_is_skipped(
        self, isolated_nexus, runner,
    ):
        _seed(isolated_nexus, [
            _chunk("orph1", "", "code__test", orphan=True),
        ])
        result = runner.invoke(
            repair_orphan_chunks_cmd,
            ["--assign", "ch-not-orphan:doc-1", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["repairs_count"] == 0
        assert payload["skipped_count"] == 1
        assert payload["skipped"][0]["chunk_id"] == "ch-not-orphan"

    def test_assign_already_resolved_chunk_is_skipped(
        self, isolated_nexus, runner,
    ):
        # Chunk was orphan, then resolved by an earlier corrective event.
        _seed(isolated_nexus, [
            _chunk("ch1", "", "code__test", orphan=True),
            _chunk("ch1", "doc-1", "code__test"),  # corrective from earlier run
        ])
        result = runner.invoke(
            repair_orphan_chunks_cmd,
            ["--assign", "ch1:doc-2", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        # ch1 is no longer an orphan after the earlier corrective event,
        # so a second --assign on it is a no-op.
        assert payload["repairs_count"] == 0
        assert payload["skipped_count"] == 1
        assert "not currently an orphan" in payload["skipped"][0]["reason"]


# ── List mode shows zero orphans after a complete repair ─────────────────


class TestListAfterRepair:
    def test_list_reports_zero_after_repair_assign(self, isolated_nexus, runner):
        _seed(isolated_nexus, [
            _chunk("orph1", "", "code__test", orphan=True),
        ])
        # Repair.
        runner.invoke(
            repair_orphan_chunks_cmd,
            ["--assign", "orph1:doc-1"],
        )
        # List → 0.
        result = runner.invoke(
            repair_orphan_chunks_cmd, ["--list", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["orphans_count"] == 0
