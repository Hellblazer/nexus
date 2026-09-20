# SPDX-License-Identifier: AGPL-3.0-or-later
"""Planner for the aspect ``source_uri`` backfill (the 481-row repair).

The defect this repairs: ``_build_record_from_entry`` — the BATCH aspect
builder — constructed its ``AspectRecord`` without ``source_uri``, while the
two single-doc builders minted one via ``uri_for``. ``document_aspects.source_uri``
is the key the ``aspect_sql`` operators re-derive and look a row up by, so a
row stored without one matches nothing, and ``operator_filter`` reports that
miss as ``"does not match"`` — a content verdict rather than an error.

Measured on this box 2026-09-20: 481 of 658 rows across the 24 knowledge
collections carried an empty ``source_uri``.
"""
from __future__ import annotations

from nexus.aspect_uri_repair import plan_source_uri_backfill
from nexus.db.t2.records import AspectRecord


def _row(
    *,
    collection: str = "knowledge__delos__voyage-context-3__v1",
    source_path: str = "a" * 64,
    source_uri: str = "",
    confidence: float | None = 0.9,
    doc_id: str = "1.2.3",
) -> AspectRecord:
    return AspectRecord(
        collection=collection,
        source_path=source_path,
        problem_formulation="P",
        proposed_method="M",
        experimental_datasets=[],
        experimental_baselines=[],
        experimental_results=None,
        extras={},
        confidence=confidence,
        extracted_at="2026-09-20T00:00:00+00:00",
        model_version="m",
        extractor_name="e",
        source_uri=source_uri,
        doc_id=doc_id,
    )


class TestPlanSourceUriBackfill:
    def test_a_row_with_no_uri_is_repaired_to_what_uri_for_mints(self) -> None:
        """The repair value is ``uri_for``'s, never a hand-built string.

        The writer and every lookup derive the key from that one function; a
        backfill that composed the URI itself would be a fourth implementation
        of the contract and could drift from the other three silently.
        """
        from nexus.aspect_readers import uri_for

        row = _row()
        plan = plan_source_uri_backfill([row])

        assert len(plan.updates) == 1
        repaired, new_uri = plan.updates[0]
        assert new_uri == uri_for(row.collection, row.source_path)
        assert repaired.source_uri == new_uri
        assert not plan.refusals

    def test_the_repaired_record_changes_nothing_but_the_uri(self) -> None:
        """Upsert is a whole-row overwrite, so every other field must survive."""
        row = _row()
        repaired, _ = plan_source_uri_backfill([row]).updates[0]

        for f in (
            "collection", "source_path", "problem_formulation", "proposed_method",
            "experimental_datasets", "experimental_baselines", "experimental_results",
            "extras", "confidence", "extracted_at", "model_version",
            "extractor_name", "doc_id",
        ):
            assert getattr(repaired, f) == getattr(row, f), f"{f} was altered"

    def test_a_row_that_already_has_a_uri_is_left_alone(self) -> None:
        """Idempotency: a second run must be a no-op, not a rewrite."""
        row = _row(source_uri="chroma://knowledge__delos__voyage-context-3__v1/" + "a" * 64)
        plan = plan_source_uri_backfill([row])
        assert not plan.updates
        assert plan.already_attributed == 1

    def test_an_empty_source_path_is_refused_never_guessed(self) -> None:
        """No identity in, no identity out.

        ``uri_for`` returns None for an empty source_path. Minting something
        anyway would collapse every such row under one key, which is worse
        than leaving them unattributed.
        """
        plan = plan_source_uri_backfill([_row(source_path="")])
        assert not plan.updates
        assert len(plan.refusals) == 1
        assert "source_path" in plan.refusals[0][1]

    def test_a_relative_path_in_a_file_routed_collection_is_refused(self) -> None:
        """``uri_for`` returns None rather than anchoring on the process CWD
        (nexus-yg70j). The backfill inherits that refusal instead of supplying
        a repo_root it cannot know at repair time.
        """
        plan = plan_source_uri_backfill([
            _row(collection="docs__nexus__voyage-context-3__v1", source_path="docs/rdr/x.md"),
        ])
        assert not plan.updates
        assert len(plan.refusals) == 1

    def test_a_row_below_the_confidence_gate_is_refused_with_its_reason(self) -> None:
        """``upsert`` silently returns False under confidence 0.3.

        Planning these as updates would report a repair count the store never
        performed. They are refused by name so the number stays honest.
        """
        plan = plan_source_uri_backfill([_row(confidence=0.1)])
        assert not plan.updates
        assert len(plan.refusals) == 1
        assert "confidence" in plan.refusals[0][1]

    def test_an_absent_confidence_is_refused_like_a_low_one(self) -> None:
        """The engine coerces a null confidence to -1.0 and fails the same
        comparison (``AspectRepository.upsertAspect``, nexus-j0nec), so None
        must not slip through as repairable."""
        plan = plan_source_uri_backfill([_row(confidence=None)])
        assert not plan.updates
        assert len(plan.refusals) == 1
        assert "confidence" in plan.refusals[0][1]

    def test_a_row_with_no_doc_id_is_refused(self) -> None:
        """``upsert`` raises ValueError on a blank doc_id (hygiene-001). The
        planner refuses it rather than letting --apply die mid-run."""
        plan = plan_source_uri_backfill([_row(doc_id="")])
        assert not plan.updates
        assert len(plan.refusals) == 1
        assert "doc_id" in plan.refusals[0][1]

    def test_the_census_partitions_every_input_row_exactly(self) -> None:
        """Non-vacuity: the three buckets must account for all N rows.

        A planner that silently dropped a row would otherwise report a clean
        small number and leave the rest unrepaired with nothing saying so.
        """
        rows = [
            _row(source_path="a" * 64),
            _row(source_path="b" * 64),
            _row(source_path="c" * 64, source_uri="chroma://x/y"),
            _row(source_path=""),
            _row(source_path="d" * 64, confidence=0.1),
        ]
        plan = plan_source_uri_backfill(rows)
        assert len(plan.updates) + plan.already_attributed + len(plan.refusals) == len(rows)
        assert plan.total == len(rows)
