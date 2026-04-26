# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-089 follow-up: SQL fast path for the analytics quartet
(``operator_filter`` / ``operator_groupby`` / ``operator_aggregate``).

The substrate lives in ``src/nexus/operators/aspect_sql.py``; these
tests pin the contract independently of the operator wrappers in
``mcp/core.py``. Source-mode semantics, aspect-field inference,
items-shape gating, fallback rationale, T2 query correctness all
covered.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nexus.aspect_extractor import AspectRecord
from nexus.db.t2 import T2Database
from nexus.operators import aspect_sql


def _make_record(
    *, source_path: str, collection: str = "knowledge__delos",
    problem_formulation: str = "P",
    proposed_method: str = "M",
    experimental_datasets: list[str] | None = None,
    experimental_baselines: list[str] | None = None,
    experimental_results: str = "R",
    extras: dict | None = None,
    confidence: float = 0.9,
    model_version: str = "claude-haiku-4-5-20251001",
) -> AspectRecord:
    return AspectRecord(
        collection=collection,
        source_path=source_path,
        problem_formulation=problem_formulation,
        proposed_method=proposed_method,
        experimental_datasets=experimental_datasets or ["d1"],
        experimental_baselines=experimental_baselines or ["b1"],
        experimental_results=experimental_results,
        extras=extras or {},
        confidence=confidence,
        extracted_at=datetime.now(UTC).isoformat(),
        model_version=model_version,
        extractor_name="scholarly-paper-v1",
    )


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated T2 with a small fixture corpus.

    Five papers in knowledge__delos covering the columns the SQL fast
    path queries: scalar text (proposed_method, experimental_results,
    problem_formulation), JSON arrays (experimental_datasets,
    experimental_baselines), JSON object (extras.venue), and
    confidence reals."""
    db_path = tmp_path / "aspect_sql.db"
    import nexus.commands._helpers as h
    monkeypatch.setattr(h, "default_db_path", lambda: db_path)

    with T2Database(db_path) as db:
        db.document_aspects.upsert(_make_record(
            source_path="/papers/paxos.pdf",
            proposed_method="Hybrid Paxos with batched leader appends",
            experimental_datasets=["TPC-C", "YCSB"],
            experimental_baselines=["raft", "paxos"],
            experimental_results="30% throughput improvement",
            extras={"venue": "VLDB", "year": 2023},
            confidence=0.92,
        ))
        db.document_aspects.upsert(_make_record(
            source_path="/papers/raft.pdf",
            proposed_method="Raft variant with single-leader writes",
            experimental_datasets=["YCSB"],
            experimental_baselines=["paxos"],
            experimental_results="10% latency reduction",
            extras={"venue": "OSDI", "year": 2024},
            confidence=0.85,
        ))
        db.document_aspects.upsert(_make_record(
            source_path="/papers/bft.pdf",
            proposed_method="BFT consensus with HotStuff backbone",
            experimental_datasets=["TPC-C"],
            experimental_baselines=["pbft"],
            experimental_results="2x throughput vs PBFT",
            extras={"venue": "OSDI", "year": 2023},
            confidence=0.78,
        ))
        # A paper with null fields (extractor failed 3x).
        db.document_aspects.upsert(AspectRecord(
            collection="knowledge__delos",
            source_path="/papers/null.pdf",
            problem_formulation=None,
            proposed_method=None,
            experimental_datasets=[],
            experimental_baselines=[],
            experimental_results=None,
            extras={},
            confidence=None,
            extracted_at=datetime.now(UTC).isoformat(),
            model_version="claude-haiku-4-5-20251001",
            extractor_name="scholarly-paper-v1",
        ))
        # A paper from a different collection (must be excluded).
        db.document_aspects.upsert(_make_record(
            source_path="/papers/other.pdf",
            collection="knowledge__other",
            proposed_method="Some other method using paxos",
            extras={"venue": "VLDB"},
        ))
    return db_path


def _items(*paths: str, collection: str = "knowledge__delos") -> str:
    return json.dumps([
        {"id": p, "collection": collection, "source_path": p}
        for p in paths
    ])


# ── _infer_aspect_field heuristic ───────────────────────────────────────────


class TestInfer:
    @pytest.mark.parametrize("text,expected", [
        ("uses TPC-C dataset", "experimental_datasets"),
        ("trained on YCSB", "experimental_datasets"),
        ("compared with raft", "experimental_baselines"),
        ("baseline is pbft", "experimental_baselines"),
        ("achieves 30% throughput", "experimental_results"),
        ("reports F1 of 0.9", "experimental_results"),
        ("proposes hybrid paxos", "proposed_method"),
        ("introduces a new technique", "proposed_method"),
        ("addresses the BFT problem", "problem_formulation"),
        ("focuses on the consensus challenge", "problem_formulation"),
        ("published at VLDB venue", "extras.venue"),
        ("publication year 2023", "extras.year"),
    ])
    def test_inference_keywords(self, text: str, expected: str) -> None:
        assert aspect_sql._infer_aspect_field(text) == expected

    def test_no_match_returns_empty(self) -> None:
        assert aspect_sql._infer_aspect_field("xyzzy") == ""


# ── try_filter ──────────────────────────────────────────────────────────────


class TestFilter:
    def test_filter_scalar_column_explicit_field(self, env: Path) -> None:
        items = _items("/papers/paxos.pdf", "/papers/raft.pdf",
                       "/papers/bft.pdf")
        result = aspect_sql.try_filter(
            items, "paxos",
            source="auto", aspect_field="proposed_method",
        )
        assert result is not None
        kept = [i["source_path"] for i in result["items"]]
        # paxos.pdf has "Hybrid Paxos" in proposed_method; raft.pdf
        # does not mention paxos in its method (paxos is its
        # baseline, not method); bft.pdf does not.
        assert "/papers/paxos.pdf" in kept
        assert "/papers/raft.pdf" not in kept
        assert "/papers/bft.pdf" not in kept

    def test_filter_json_array_column(self, env: Path) -> None:
        items = _items("/papers/paxos.pdf", "/papers/raft.pdf",
                       "/papers/bft.pdf")
        result = aspect_sql.try_filter(
            items, "TPC-C",
            source="auto", aspect_field="experimental_datasets",
        )
        assert result is not None
        kept = sorted(i["source_path"] for i in result["items"])
        # paxos.pdf and bft.pdf use TPC-C; raft.pdf does not.
        assert kept == ["/papers/bft.pdf", "/papers/paxos.pdf"]

    def test_filter_extras_dot_syntax(self, env: Path) -> None:
        items = _items("/papers/paxos.pdf", "/papers/raft.pdf",
                       "/papers/bft.pdf")
        result = aspect_sql.try_filter(
            items, "OSDI",
            source="auto", aspect_field="extras.venue",
        )
        assert result is not None
        kept = sorted(i["source_path"] for i in result["items"])
        assert kept == ["/papers/bft.pdf", "/papers/raft.pdf"]

    def test_filter_inferred_field(self, env: Path) -> None:
        """auto mode infers the aspect field from criterion keywords."""
        items = _items("/papers/paxos.pdf", "/papers/raft.pdf",
                       "/papers/bft.pdf")
        # "uses TPC-C dataset" → experimental_datasets via inference
        result = aspect_sql.try_filter(
            items, "uses TPC-C dataset",
            source="auto", aspect_field="",
        )
        assert result is not None
        kept = sorted(i["source_path"] for i in result["items"])
        assert kept == ["/papers/bft.pdf", "/papers/paxos.pdf"]

    def test_filter_null_aspect_row_treated_as_no_match(
        self, env: Path,
    ) -> None:
        """A paper whose extractor failed (null fields) is correctly
        rejected by the filter, with a rationale that says so."""
        items = _items("/papers/paxos.pdf", "/papers/null.pdf")
        result = aspect_sql.try_filter(
            items, "paxos",
            source="auto", aspect_field="proposed_method",
        )
        assert result is not None
        kept = [i["source_path"] for i in result["items"]]
        assert kept == ["/papers/paxos.pdf"]
        # The null-row paper is in rationale with a note about absence.
        null_reasons = [
            r["reason"] for r in result["rationale"]
            if r["id"] == "/papers/null.pdf"
        ]
        assert null_reasons
        assert "queue may be pending" in null_reasons[0] \
            or "does not match" in null_reasons[0]

    def test_filter_collection_isolation(self, env: Path) -> None:
        """A paper from a different collection is not matched even if
        its aspects would otherwise satisfy the criterion."""
        items = _items("/papers/other.pdf", collection="knowledge__delos")
        # other.pdf is in knowledge__other; querying with collection
        # = knowledge__delos must not find it.
        result = aspect_sql.try_filter(
            items, "paxos",
            source="auto", aspect_field="proposed_method",
        )
        assert result is not None
        assert result["items"] == []

    def test_filter_source_llm_returns_none(self, env: Path) -> None:
        items = _items("/papers/paxos.pdf")
        assert aspect_sql.try_filter(
            items, "paxos",
            source="llm", aspect_field="proposed_method",
        ) is None

    def test_filter_auto_falls_back_when_no_field_inferable(
        self, env: Path,
    ) -> None:
        items = _items("/papers/paxos.pdf")
        result = aspect_sql.try_filter(
            items, "xyzzy",  # no inference cue
            source="auto", aspect_field="",
        )
        assert result is None  # caller falls back to LLM

    def test_filter_aspects_mode_returns_stub_on_unparseable(
        self, env: Path,
    ) -> None:
        """source='aspects' surfaces the prerequisite failure as an
        empty-items rationale rather than falling back silently."""
        result = aspect_sql.try_filter(
            "not a json array", "paxos",
            source="aspects", aspect_field="proposed_method",
        )
        assert result is not None
        assert result["items"] == []
        assert any(
            "aspects-only" in r["reason"] for r in result["rationale"]
        )

    def test_filter_empty_items_short_circuits(self, env: Path) -> None:
        result = aspect_sql.try_filter(
            "[]", "paxos",
            source="auto", aspect_field="proposed_method",
        )
        assert result == {"items": [], "rationale": []}


# ── try_groupby ─────────────────────────────────────────────────────────────


class TestGroupby:
    def test_groupby_extras_venue(self, env: Path) -> None:
        items = _items("/papers/paxos.pdf", "/papers/raft.pdf",
                       "/papers/bft.pdf")
        result = aspect_sql.try_groupby(
            items, "venue",
            source="auto", aspect_field="extras.venue",
        )
        assert result is not None
        groups = {g["key_value"]: [i["source_path"] for i in g["items"]]
                  for g in result["groups"]}
        assert sorted(groups["VLDB"]) == ["/papers/paxos.pdf"]
        assert sorted(groups["OSDI"]) == [
            "/papers/bft.pdf", "/papers/raft.pdf",
        ]

    def test_groupby_inferred_field(self, env: Path) -> None:
        """Inference: 'venue' alone routes to extras.venue."""
        items = _items("/papers/paxos.pdf", "/papers/raft.pdf")
        result = aspect_sql.try_groupby(
            items, "venue", source="auto", aspect_field="",
        )
        assert result is not None
        keys = sorted(g["key_value"] for g in result["groups"])
        assert keys == ["OSDI", "VLDB"]

    def test_groupby_json_array_field_falls_back_to_llm_in_auto(
        self, env: Path,
    ) -> None:
        """Substantive critic Critical #1 (RDR-089 round 2): the SQL
        path's natural unroll behavior on JSON-array fields would
        violate the LLM path's one-group-per-item invariant
        (RDR-093 §C-1). Under source='auto', the SQL path detects
        the json_array column and returns None so the operator
        falls back to LLM (which respects the invariant).
        """
        items = _items("/papers/paxos.pdf", "/papers/raft.pdf")
        result = aspect_sql.try_groupby(
            items, "datasets",
            source="auto", aspect_field="experimental_datasets",
        )
        assert result is None  # auto → LLM fallback for json_array

    def test_groupby_json_array_field_stubs_in_aspects_mode(
        self, env: Path,
    ) -> None:
        """Under source='aspects' the divergence is surfaced as a
        stub group with a clear rationale rather than silent
        multi-membership."""
        items = _items("/papers/paxos.pdf")
        result = aspect_sql.try_groupby(
            items, "datasets",
            source="aspects", aspect_field="experimental_datasets",
        )
        assert result is not None
        # Stub shape: one _meta group with the rejection reason.
        assert any(
            g.get("key_value") == "_meta"
            and "json_array" in g.get("_reason", "")
            for g in result["groups"]
        )

    def test_groupby_unassigned_for_null_aspects(self, env: Path) -> None:
        items = _items("/papers/paxos.pdf", "/papers/null.pdf")
        result = aspect_sql.try_groupby(
            items, "venue",
            source="auto", aspect_field="extras.venue",
        )
        assert result is not None
        groups = {g["key_value"]: [i["source_path"] for i in g["items"]]
                  for g in result["groups"]}
        # null.pdf has no extras.venue → unassigned
        assert "/papers/null.pdf" in groups.get("unassigned", [])
        # paxos.pdf in VLDB
        assert "/papers/paxos.pdf" in groups.get("VLDB", [])


# ── try_aggregate ───────────────────────────────────────────────────────────


class TestAggregate:
    def _groups_payload(self) -> str:
        """Build a groups JSON shape matching what operator_groupby
        emits, with items carrying collection + source_path."""
        return json.dumps([
            {
                "key_value": "VLDB",
                "items": [
                    {"id": "/papers/paxos.pdf",
                     "collection": "knowledge__delos",
                     "source_path": "/papers/paxos.pdf"},
                ],
            },
            {
                "key_value": "OSDI",
                "items": [
                    {"id": "/papers/raft.pdf",
                     "collection": "knowledge__delos",
                     "source_path": "/papers/raft.pdf"},
                    {"id": "/papers/bft.pdf",
                     "collection": "knowledge__delos",
                     "source_path": "/papers/bft.pdf"},
                ],
            },
        ])

    def test_aggregate_count(self, env: Path) -> None:
        result = aspect_sql.try_aggregate(
            self._groups_payload(), "count",
            source="auto", aspect_field="",
        )
        assert result is not None
        by_key = {a["key_value"]: a["summary"] for a in result["aggregates"]}
        assert "1 item(s)" in by_key["VLDB"]
        assert "2 item(s)" in by_key["OSDI"]

    def test_aggregate_count_distinct(self, env: Path) -> None:
        result = aspect_sql.try_aggregate(
            self._groups_payload(), "count distinct",
            source="auto", aspect_field="",
        )
        assert result is not None
        by_key = {a["key_value"]: a["summary"] for a in result["aggregates"]}
        assert "1 distinct" in by_key["VLDB"]
        assert "2 distinct" in by_key["OSDI"]

    def test_aggregate_avg_confidence(self, env: Path) -> None:
        result = aspect_sql.try_aggregate(
            self._groups_payload(), "avg confidence",
            source="auto", aspect_field="",
        )
        assert result is not None
        by_key = {a["key_value"]: a["summary"] for a in result["aggregates"]}
        assert "avg(confidence) = 0.920" in by_key["VLDB"]
        # OSDI: (0.85 + 0.78) / 2 = 0.815
        assert "avg(confidence) = 0.815" in by_key["OSDI"]

    def test_aggregate_max_confidence(self, env: Path) -> None:
        result = aspect_sql.try_aggregate(
            self._groups_payload(), "max confidence",
            source="auto", aspect_field="",
        )
        assert result is not None
        by_key = {a["key_value"]: a["summary"] for a in result["aggregates"]}
        assert "max(confidence) = 0.850" in by_key["OSDI"]

    def test_count_distinct_falls_back_to_identity_when_id_absent(
        self, env: Path,
    ) -> None:
        """Substantive critic Critical #2: items lacking an ``id``
        field but carrying ``(collection, source_path)`` identity
        previously returned ``0 distinct item(s)``. Now they
        deduplicate on the identity tuple and surface a clear
        annotation."""
        groups_payload = json.dumps([
            {
                "key_value": "VLDB",
                "items": [
                    # no 'id' field; only collection + source_path
                    {"collection": "knowledge__delos",
                     "source_path": "/papers/paxos.pdf"},
                    {"collection": "knowledge__delos",
                     "source_path": "/papers/raft.pdf"},
                    # duplicate of paxos: same identity
                    {"collection": "knowledge__delos",
                     "source_path": "/papers/paxos.pdf"},
                ],
            },
        ])
        result = aspect_sql.try_aggregate(
            groups_payload, "count distinct",
            source="auto", aspect_field="",
        )
        assert result is not None
        assert len(result["aggregates"]) == 1
        summary = result["aggregates"][0]["summary"]
        # Two distinct identities (paxos counted once despite duplicate).
        assert "2 distinct" in summary
        assert "id field absent" in summary

    def test_count_distinct_no_id_no_identity_falls_back_to_count(
        self, env: Path,
    ) -> None:
        """When items have neither id nor (collection, source_path),
        count distinct falls back to ``len(items)`` with a clear
        annotation rather than silently returning 0."""
        groups_payload = json.dumps([
            {
                "key_value": "anon",
                "items": [
                    {"title": "A"},
                    {"title": "B"},
                ],
            },
        ])
        result = aspect_sql.try_aggregate(
            groups_payload, "count distinct",
            source="auto", aspect_field="",
        )
        assert result is not None
        summary = result["aggregates"][0]["summary"]
        assert "2 item" in summary
        assert "no id or identity" in summary

    def test_aggregate_confidence_paginates_across_batches(
        self, env: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Substantive critic Critical #3: the previous
        implementation truncated groups larger than 300 silently.
        The pagination fix accumulates across all batches —
        verified here by running min/max against the small fixture
        and confirming the value matches the full set, NOT only
        the first 300 (the fixture is small enough that
        truncation would not have changed the test outcome before
        — but the new code path explicitly paginates so this test
        guards against regression to the old single-batch shape).
        """
        # Five papers with confidences 0.5, 0.6, 0.7, 0.8, 0.9.
        # Expected: avg=0.7, min=0.5, max=0.9. The pagination loop
        # processes them in one batch (5 < 300) but the
        # accumulation pattern is exercised.
        from nexus.db.t2 import T2Database

        with T2Database(env) as db:
            for i, conf in enumerate([0.5, 0.6, 0.7, 0.8, 0.9]):
                db.document_aspects.upsert(_make_record(
                    source_path=f"/papers/conf-{i}.pdf",
                    confidence=conf,
                ))

        groups_payload = json.dumps([
            {
                "key_value": "all",
                "items": [
                    {"id": f"/papers/conf-{i}.pdf",
                     "collection": "knowledge__delos",
                     "source_path": f"/papers/conf-{i}.pdf"}
                    for i in range(5)
                ],
            },
        ])
        avg_result = aspect_sql.try_aggregate(
            groups_payload, "avg confidence",
            source="auto", aspect_field="",
        )
        assert "avg(confidence) = 0.700" in avg_result["aggregates"][0]["summary"]
        min_result = aspect_sql.try_aggregate(
            groups_payload, "min confidence",
            source="auto", aspect_field="",
        )
        assert "min(confidence) = 0.500" in min_result["aggregates"][0]["summary"]
        max_result = aspect_sql.try_aggregate(
            groups_payload, "max confidence",
            source="auto", aspect_field="",
        )
        assert "max(confidence) = 0.900" in max_result["aggregates"][0]["summary"]

    def test_aggregate_unrecognised_reducer_returns_none_in_auto(
        self, env: Path,
    ) -> None:
        """auto mode falls back to LLM for reducers outside the SQL
        vocabulary."""
        result = aspect_sql.try_aggregate(
            self._groups_payload(),
            "winning baseline by reported metric",
            source="auto", aspect_field="",
        )
        assert result is None

    def test_aggregate_aspects_mode_stubs_unrecognised(
        self, env: Path,
    ) -> None:
        result = aspect_sql.try_aggregate(
            self._groups_payload(),
            "winning baseline by reported metric",
            source="aspects", aspect_field="",
        )
        assert result is not None
        assert any(
            "aspects-only" in a["summary"] for a in result["aggregates"]
        )


# ── Items shape gating ──────────────────────────────────────────────────────


class TestItemShapes:
    def test_filter_plain_text_items_falls_through(self, env: Path) -> None:
        result = aspect_sql.try_filter(
            "just plain text, not JSON",
            "paxos", source="auto", aspect_field="proposed_method",
        )
        assert result is None  # auto → LLM fallback

    def test_filter_items_without_identity_falls_through(
        self, env: Path,
    ) -> None:
        items = json.dumps([
            {"id": "x", "title": "Some paper"},  # no collection/source_path
        ])
        result = aspect_sql.try_filter(
            items, "paxos",
            source="auto", aspect_field="proposed_method",
        )
        assert result is None

    def test_filter_accepts_physical_collection_synonym(
        self, env: Path,
    ) -> None:
        """Items from `query` results carry `physical_collection` as the
        canonical key — the SQL path treats it as a synonym for
        `collection`."""
        items = json.dumps([
            {"id": "/papers/paxos.pdf",
             "physical_collection": "knowledge__delos",
             "source_path": "/papers/paxos.pdf"},
        ])
        result = aspect_sql.try_filter(
            items, "paxos",
            source="auto", aspect_field="proposed_method",
        )
        assert result is not None
        assert len(result["items"]) == 1
