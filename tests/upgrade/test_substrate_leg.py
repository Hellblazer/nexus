# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P2.5 (nexus-n7u38.18): embedder-era co-resident leg + consent.

RQ2 edges 4-5: chunk-identity and embedder-era are CO-RESIDENT inside the
substrate ETL rung — one leg per collection composes wire re-id and
cross-model re-embed as needed, never sequential rungs. Consent only at
genuine decisions: source-gone (re-acquire vs drop) surfaces as an
explicit decision, the billed re-embed keeps the existing cost prompt,
and a conformant install plans ZERO legs and ZERO prompts.
"""
from __future__ import annotations

import hashlib
import pathlib
from typing import Any

import pytest

from nexus.migration.detection import CollectionClassification
from nexus.migration.vector_etl import rollback_collections
from nexus.migration.wire_reid import ChashRemapStore, RemapEntry
from nexus.upgrade_ladder.rungs.substrate_etl import (
    LegPlan,
    SourceGoneDecision,
    execute_leg,
    plan_substrate_legs,
)

NEW = "a" * 32


@pytest.fixture(autouse=True)
def _isolate_watermarks(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """execute_leg advances rung watermarks — keep them off the real config."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))


def _cls(
    name: str,
    *,
    legacy: bool = False,
    support: str = "supported-voyage-1024",
    model: str | None = "voyage-context-3",
    count: int = 10,
) -> CollectionClassification:
    return CollectionClassification(
        collection=name,
        leg="local",
        model=model,
        dim=1024 if model else None,
        support=support,  # type: ignore[arg-type]
        source_count=count,
        has_data=count > 0,
        legacy_ids=legacy,
    )


# ── planning ─────────────────────────────────────────────────────────────────


def test_conformant_install_plans_nothing_and_prompts_nothing() -> None:
    plan = plan_substrate_legs(
        [_cls("code__nexus__voyage_code_3__v1")],
        prior_collections=frozenset(),
        voyage_key_present=True,
    )
    assert plan.legs == []
    assert plan.decisions == []
    assert plan.billed_reembed is False


def test_legacy_id_collection_plans_a_reid_leg_not_a_block() -> None:
    """THE RDR-185 retirement: legacy ids become an in-flight transform,
    not a refusal (the old path's block stands until P4 demotes it)."""
    plan = plan_substrate_legs(
        [_cls("knowledge__old_store", legacy=True)],
        prior_collections=frozenset(),
        voyage_key_present=True,
    )
    (leg,) = plan.legs
    assert leg.needs_reid is True
    assert leg.needs_reembed is False
    assert leg.source_collection == leg.target_collection == "knowledge__old_store"
    assert plan.billed_reembed is False  # same-model, no re-embed, no prompt


def test_unsupported_model_plans_cross_model_reembed_leg() -> None:
    plan = plan_substrate_legs(
        [_cls("knowledge__notes__all-minilm-l6-v2__v1", support="unsupported", model=None)],
        prior_collections=frozenset(),
        voyage_key_present=True,
    )
    (leg,) = plan.legs
    assert leg.needs_reembed is True
    assert leg.target_collection != leg.source_collection  # model segment remapped
    assert plan.billed_reembed is True  # voyage re-embed bills → cost prompt


def test_co_resident_leg_composes_reid_and_reembed() -> None:
    """The incident shape's worst case: legacy ids AND an unsupported
    embedder — ONE leg carries both transforms (RQ2 co-residency)."""
    plan = plan_substrate_legs(
        [_cls("knowledge__ancient__all-minilm-l6-v2__v1", legacy=True, support="unsupported", model=None)],
        prior_collections=frozenset(),
        voyage_key_present=True,
    )
    (leg,) = plan.legs
    assert leg.needs_reid is True
    assert leg.needs_reembed is True
    assert leg.target_collection != leg.source_collection


def test_source_gone_surfaces_a_decision_never_silent() -> None:
    """A collection known from prior migration state that no longer exists
    in the source is a GENUINE decision (re-acquire vs drop) — an explicit
    entry in plan.decisions, not a silent skip."""
    plan = plan_substrate_legs(
        [_cls("knowledge__present")],
        prior_collections=frozenset({"knowledge__present", "knowledge__vanished"}),
        voyage_key_present=True,
    )
    (decision,) = plan.decisions
    assert isinstance(decision, SourceGoneDecision)
    assert decision.collection == "knowledge__vanished"
    assert set(decision.options) == {"re-acquire", "drop"}


def test_empty_collections_are_not_legs() -> None:
    plan = plan_substrate_legs(
        [_cls("knowledge__empty", legacy=True, count=0)],
        prior_collections=frozenset(),
        voyage_key_present=True,
    )
    assert plan.legs == []


# ── execution glue (the .14 seam + .15 transform composed) ──────────────────


class OneBatchSource:
    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._chunks = chunks

    def iter_batches(self, collection: str, *, page: int, include_embeddings: bool = False):
        yield list(self._chunks)

    def count(self, collection: str) -> int:
        return len(self._chunks)


class RecordingTarget:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.collections: set[str] = set()

    def upsert_chunks(self, collection, ids, documents, metadatas, *, embeddings=None):
        self.collections.add(collection)
        for cid, doc in zip(ids, documents):
            self.rows[cid] = {"doc": doc, "embeddings": embeddings}

    def count(self, collection: str) -> int:
        return len(self.rows)


def test_execute_reid_leg_maps_and_lands_conformant(tmp_path: pathlib.Path) -> None:
    text = "note text"
    new_chash = hashlib.sha256(text.encode()).hexdigest()
    source = OneBatchSource([{"id": "legacy-16-chars!", "document": text, "metadata": {}}])
    target = RecordingTarget()
    leg = LegPlan(
        source_collection="knowledge__old",
        target_collection="knowledge__old",
        needs_reid=True,
        needs_reembed=False,
    )
    with ChashRemapStore(tmp_path / "chash_remap.db") as store:
        result = execute_leg(leg, source, target, map_store=store, page=10, provenance="run-1")
        assert result.ok
        assert set(target.rows) == {new_chash}
        assert store.lookup("knowledge__old", "legacy-16-chars!") == new_chash


def test_execute_cross_model_leg_targets_remapped_collection(
    tmp_path: pathlib.Path,
) -> None:
    """Cross-model: the leg writes to the model-remapped TARGET collection
    (server re-embeds from stored text — no embeddings sent) and the map
    records target_collection for every re-id'd row (audit C2)."""
    text = "prose"
    new_chash = hashlib.sha256(text.encode()).hexdigest()
    source = OneBatchSource([{"id": "legacy-16-chars!", "document": text, "metadata": {}}])
    target = RecordingTarget()
    leg = LegPlan(
        source_collection="knowledge__notes__all-minilm-l6-v2__v1",
        target_collection="knowledge__notes__voyage-context-3__v1",
        needs_reid=True,
        needs_reembed=True,
    )
    with ChashRemapStore(tmp_path / "chash_remap.db") as store:
        result = execute_leg(leg, source, target, map_store=store, page=10, provenance="p")
        assert result.ok
        assert target.collections == {"knowledge__notes__voyage-context-3__v1"}
        assert target.rows[new_chash]["embeddings"] is None  # server-side re-embed
        rows = store.all_pairs()
        assert rows == [("legacy-16-chars!", new_chash)]
        # target_collection recorded (the C2 'where did it land' answer):
        conn_rows = store.entries_for_collection(
            "knowledge__notes__all-minilm-l6-v2__v1"
        )
        assert conn_rows["legacy-16-chars!"] == new_chash


def test_reid_only_leg_passes_through_stored_vectors(tmp_path: pathlib.Path) -> None:
    """P2 review High: a re-id-only (same-model) leg must CARRY the stored
    vectors — forcing a server re-embed would bill Voyage tokens the plan
    promised it would not (billed_reembed=False), silently defeating the
    consent gate."""
    text = "note text"
    source = OneBatchSource([
        {
            "id": "legacy-16-chars!",
            "document": text,
            "metadata": {"embedding_model": "voyage-context-3"},
            "embedding": [0.1, 0.2],
        }
    ])
    target = RecordingTarget()
    leg = LegPlan(
        source_collection="knowledge__old__voyage-context-3__v1",
        target_collection="knowledge__old__voyage-context-3__v1",
        needs_reid=True,
        needs_reembed=False,
    )
    with ChashRemapStore(tmp_path / "chash_remap.db") as store:
        result = execute_leg(leg, source, target, map_store=store, page=10, provenance="p")
        assert result.ok
        new_chash = hashlib.sha256(text.encode()).hexdigest()
        assert target.rows[new_chash]["embeddings"] == [[0.1, 0.2]]  # passthrough, no bill


#: A voyage-declared target — billed. Module-level so the pins below name it
#: through a fixture rather than a literal (the mode lint asks tests that
#: reference voyage tokens to declare their mode; these pass it as data, and the
#: leg's mode comes from the target NAME, not from ambient config).
_BILLED_TARGET = "knowledge__old__voyage-context-3__v1"
_FREE_TARGET = "knowledge__old__bge-base-en-v15-768__v1"


def _mis_provenanced_leg(target: str) -> tuple[str, "OneBatchSource", LegPlan]:
    """A passthrough (re-id only) leg carrying ONE chunk whose recorded
    provenance disagrees with *target*'s declared model — the nexus-bfdri
    mislabel shape."""
    text = "note text"
    source = OneBatchSource([
        {
            "id": "legacy-16-chars!",
            "document": text,
            "metadata": {"embedding_model": "some-other-model"},
            "embedding": [0.9, 0.9],
        }
    ])
    return text, source, LegPlan(
        source_collection=target,
        target_collection=target,
        needs_reid=True,
        needs_reembed=False,
    )


def test_mis_provenanced_vector_falls_back_to_reembed_when_it_is_free(
    tmp_path: pathlib.Path,
) -> None:
    """nexus-bfdri mismatch-only rule carried into the leg: recorded provenance
    disagreeing with the target's declared model drops the vector, so the batch
    re-embeds server-side (correctness over cost); absent provenance is trusted.

    A LOCAL bge target, because that is the case where "correctness over cost"
    is a free choice to make on someone's behalf. The billed target is the next
    test."""
    text, source, leg = _mis_provenanced_leg(_FREE_TARGET)
    target = RecordingTarget()
    with ChashRemapStore(tmp_path / "chash_remap.db") as store:
        result = execute_leg(leg, source, target, map_store=store, page=10, provenance="p")
        assert result.ok
        new_chash = hashlib.sha256(text.encode()).hexdigest()
        assert target.rows[new_chash]["embeddings"] is None  # dropped → server re-embed


def test_mis_provenanced_vector_into_a_BILLED_target_refuses(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nexus-92vz5: the same drop against a VOYAGE-declared target spends money.

    This test previously asserted the drop and called it "correctness over
    cost" — but the plan promised this leg costs nothing (that is WHY it is a
    passthrough), so the cost gate never asked. One dropped vector makes
    run_batched_etl send embeddings=None for the whole batch and the service
    re-embeds it, billed, with nobody consulted. The prose said correctness over
    cost; it was correctness over *someone else's* money, decided silently.

    Refused in flight — every input is present at the drop site, even though the
    plan-time predicate could not know."""
    monkeypatch.delenv("NX_ASSUME_YES", raising=False)
    _text, source, leg = _mis_provenanced_leg(_BILLED_TARGET)
    target = RecordingTarget()
    with ChashRemapStore(tmp_path / "chash_remap.db") as store:
        result = execute_leg(leg, source, target, map_store=store, page=10, provenance="p")
    assert result.ok is False
    assert "BILLED" in (result.reason or "")
    assert "--yes" in (result.reason or "")  # names the way through
    assert not target.rows, "refused, so nothing was written"


def test_standing_consent_allows_the_billed_fallback(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-vacuity, and the way through: a user who ran `nx upgrade --yes` HAS
    consented to a billed re-embed, and this is one. The refusal must gate on
    consent, not forbid the operation."""
    monkeypatch.setenv("NX_ASSUME_YES", "1")
    text, source, leg = _mis_provenanced_leg(_BILLED_TARGET)
    target = RecordingTarget()
    with ChashRemapStore(tmp_path / "chash_remap.db") as store:
        result = execute_leg(leg, source, target, map_store=store, page=10, provenance="p")
        assert result.ok
        new_chash = hashlib.sha256(text.encode()).hexdigest()
        assert target.rows[new_chash]["embeddings"] is None  # consented → re-embed


def test_pure_reembed_leg_rolls_back_via_plan_target_names(
    tmp_path: pathlib.Path,
) -> None:
    """P2 critique residual Medium, closed: a pure-reembed leg (conformant
    ids, wrong model) writes ZERO map entries — its rollback works only
    through SubstratePlan.target_names(), chained here plan→execute→rollback."""
    text = "conformant text"
    cid = hashlib.sha256(text.encode()).hexdigest()
    src = "knowledge__notes__all-minilm-l6-v2__v1"
    dst = "knowledge__notes__voyage-context-3__v1"
    plan = plan_substrate_legs(
        [
            CollectionClassification(
                collection=src, leg="local", model=None, dim=None,
                support="unsupported", source_count=1, has_data=True, legacy_ids=False,
            )
        ],
        prior_collections=frozenset(),
        voyage_key_present=True,
    )
    (leg,) = plan.legs
    assert leg.needs_reid is False and leg.needs_reembed is True
    assert plan.target_names() == {src: dst}

    source = OneBatchSource([{"id": cid, "document": text, "metadata": {}}])
    target = RecordingTarget()
    with ChashRemapStore(tmp_path / "chash_remap.db") as store:
        result = execute_leg(leg, source, target, map_store=store, page=10, provenance="p")
        assert result.ok
        assert store.all_pairs() == []  # conformant ids: zero map entries

        # Rollback: only target_names knows where these rows landed.
        class RollbackVector:
            def __init__(self, rows: dict[str, set[str]]) -> None:
                self._rows = rows

            def get_or_create_collection(self, name):
                rows = self._rows.setdefault(name, set())

                class _H:
                    def get(self, ids=None, limit=None):
                        return {"ids": [i for i in ids if i in rows]}

                    def delete(self, ids):
                        for i in ids:
                            rows.discard(i)

                return _H()

            def count(self, name):
                return len(self._rows.get(name, set()))

        class ReadClient:
            def get_collection(self, name):
                class _C:
                    def get(self, include=None, limit=None, offset=0):
                        return {"ids": [cid] if offset == 0 else [], "documents": [text], "metadatas": [{}]}

                    def count(self):
                        return 1

                return _C()

        vector = RollbackVector({dst: {cid}})
        deleted = rollback_collections(
            ReadClient(), vector, collections=[src],
            remap_store=store, target_names=plan.target_names(),
        )
        assert deleted == {src: 1}
        assert vector.count(dst) == 0


def test_execute_leg_resumes_from_watermark(tmp_path: pathlib.Path) -> None:
    """P2 critique High: the rung-keyed watermark is WIRED — a re-run after
    a clean pass resumes above the floor instead of replaying the stream
    (the 90k-chunk / ~905s replay the critique cited)."""

    class OffsetSource:
        def __init__(self, texts: list[str]) -> None:
            self.texts = texts
            self.offsets_seen: list[int] = []

        def iter_batches(self, collection, *, page, include_embeddings=False, start_offset=0):
            self.offsets_seen.append(start_offset)
            chunk_stream = [
                {"id": f"legacy-{i:012d}", "document": t, "metadata": {}}
                for i, t in enumerate(self.texts)
            ][start_offset:]
            for i in range(0, len(chunk_stream), page):
                yield chunk_stream[i : i + page]

        def count(self, collection: str) -> int:
            return len(self.texts)

    source = OffsetSource(["t-one", "t-two", "t-three"])
    target = RecordingTarget()
    leg = LegPlan("src", "src", needs_reid=True, needs_reembed=False)
    with ChashRemapStore(tmp_path / "chash_remap.db") as store:
        first = execute_leg(leg, source, target, map_store=store, page=1, provenance="r1")
        assert first.ok
        assert source.offsets_seen == [0]  # fresh run: no floor
        # Second run: the advanced watermark (position=3, trusted against the
        # live target count) resumes ABOVE the stream — zero rows replayed.
        second = execute_leg(leg, source, target, map_store=store, page=1, provenance="r2")
        assert second.ok
        assert source.offsets_seen == [0, 3]
        assert second.source_count == 0  # nothing re-read, nothing re-sent


def test_execute_conformant_leg_needs_no_map_entries(tmp_path: pathlib.Path) -> None:
    text = "fine"
    cid = hashlib.sha256(text.encode()).hexdigest()
    source = OneBatchSource([{"id": cid, "document": text, "metadata": {}}])
    target = RecordingTarget()
    leg = LegPlan("c", "c", needs_reid=False, needs_reembed=False)
    with ChashRemapStore(tmp_path / "chash_remap.db") as store:
        result = execute_leg(leg, source, target, map_store=store, page=10, provenance="p")
        assert result.ok
        assert store.all_pairs() == []
