# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit + CLI tests for nx taxonomy review --auto (nexus-6i01g, nexus-vfs67).

Design: nx memory get -p nexus -t design-taxonomy-review-auto.md (approved).
Reference rubric/results: nx memory get -p nexus -t taxonomy-auto-review-2026-07-14.

Two suites:

* ``TestReviewVerdictsDispatch`` — pure-function tests for
  ``_generate_review_verdicts_batch`` / ``_REVIEW_VERDICT_SCHEMA``, modeled
  directly on ``tests/test_rdr_085_glossary_labeler.py``'s
  ``patch("nexus.operators.dispatch.claude_dispatch", ...)`` pattern (the
  import is deferred-at-call-time inside the dispatcher, so patching must
  target that module path, not ``nexus.commands.taxonomy_cmd``).
* ``TestReviewAutoCLI`` — CLI-level tests (cases a-p from the bead task
  list, plus the stacked-review follow-up findings), modeled on
  ``tests/test_taxonomy.py::TestReviewCLI``'s ``_seed_topics`` /
  ``_t2_router`` / ``patch("nexus.commands.taxonomy_cmd._default_db_path", ...)``
  + ``patch.object(_mi, "t2_index_write", ...)`` conventions.

Verdicts are keyed by the REAL topic id (schema property ``"id"``), not a
positional ``idx`` — the review-gate fix for the wrong-topic-verdict class
(nexus-6i01g stacked-review finding 3).
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

import itertools

import nexus.mcp_infra as _mi
from nexus.commands.taxonomy_cmd import taxonomy
from nexus.db.t2 import T2Database

from tests.conftest import next_import_seed_id  # session-unique import ids (see conftest note)


@pytest.fixture(autouse=True)
def _engine_substrate(t2_service_env):
    """RDR-155 P4b P0a' representative batch: this module runs its T2
    against the engine-backed substrate (per-test minted tenant)."""

# ── Shared seeding + mocking helpers ────────────────────────────────────────


def _seed_topic(
    db_path: Path,
    label: str,
    *,
    collection: str = "proj",
    doc_count: int = 1,
    terms: list[str] | None = None,
    review_status: str = "pending",
    n_docs: int | None = None,
) -> int:
    """Insert one topic (+ topic_assignments) and return its id.

    RDR-155 P4b P0a': seeds via the taxonomy fidelity-import surface
    (import_topic / import_assignment write rows verbatim, preserving
    the supplied id) — the substrate-neutral replacement for the retired
    raw-conn INSERTs. Ids come from a process-wide counter; per-test
    minted tenants namespace them server-side anyway.
    """
    import json as _json

    with T2Database(db_path) as db:
        topic_id = db.taxonomy.import_topic(
            src_id=next_import_seed_id(),
            label=label,
            parent_id=None,
            collection=collection,
            centroid_hash=None,
            doc_count=doc_count,
            created_at="2026-01-01T00:00:00Z",
            review_status=review_status,
            terms=_json.dumps(terms or ["term-a", "term-b"]),
        )
        n = n_docs if n_docs is not None else doc_count
        for i in range(n):
            db.taxonomy.import_assignment(
                doc_id=f"{label}-doc-{i}.py",
                topic_id=topic_id,
                assigned_by="test-seed",
                similarity=None,
                assigned_at=None,
                source_collection=None,
            )
    return topic_id


def _t2_router(db_path: Path):
    """Return a t2_index_write stub that routes through db_path (RDR-151 P3)."""
    def _router(fn):
        with T2Database(db_path) as db:
            return fn(db)
    return _router


def _t2_router_delete_fails_for(db_path: Path, failing_id: int):
    """Like ``_t2_router`` but ``delete_topic(failing_id)`` raises.

    Used to test apply-loop resilience (stacked-review finding 2): one bad
    delete must not abort the remaining deletes/merges in the batch. Only
    ``delete_topic`` is monkeypatched; ``rename_topic``/``mark_topic_reviewed``/
    ``merge_topics`` are untouched.
    """
    def _router(fn):
        with T2Database(db_path) as db:
            orig_delete = db.taxonomy.delete_topic

            def _delete(topic_id, **kw):
                if topic_id == failing_id:
                    raise RuntimeError("boom-delete")
                return orig_delete(topic_id, **kw)

            db.taxonomy.delete_topic = _delete
            return fn(db)
    return _router


def _dispatch_by_topic_id(verdicts_by_id: dict[int, dict]):
    """Build a fake ``claude_dispatch`` keyed by REAL topic id.

    ``_generate_review_verdicts_batch`` embeds ``id=<topic_id>`` per prompt
    line; this parses those back out so tests can specify verdicts by real
    topic id and the fake naturally scopes its response to whichever ids
    are actually in the current batch's prompt (needed for multi-batch
    tests). Topics with no entry in ``verdicts_by_id`` are omitted from the
    response (fail-open: they land as None / stay pending).
    """
    async def _fake(prompt: str, schema: dict, **kw):  # noqa: ARG001
        ids_in_prompt = {int(m) for m in re.findall(r"id=(\d+)", prompt)}
        out = [
            {"id": tid, **v}
            for tid, v in verdicts_by_id.items()
            if tid in ids_in_prompt
        ]
        return {"verdicts": out}

    return _fake


# ── _generate_review_verdicts_batch / _REVIEW_VERDICT_SCHEMA ───────────────


class TestReviewVerdictsDispatch:

    @pytest.mark.asyncio
    async def test_accept_verdict_parses(self) -> None:
        from nexus.commands import taxonomy_cmd

        items = [(101, "old label", ["a", "b"], ["doc1.py"], "proj")]
        dispatched = AsyncMock(return_value={"verdicts": [{"id": 101, "action": "accept"}]})
        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_review_verdicts_batch(items)
        assert results == [{"action": "accept"}]

    @pytest.mark.asyncio
    async def test_rename_with_valid_label(self) -> None:
        from nexus.commands import taxonomy_cmd

        items = [(101, "old", [], [], "proj")]
        dispatched = AsyncMock(
            return_value={"verdicts": [{"id": 101, "action": "rename", "label": "New Label"}]}
        )
        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_review_verdicts_batch(items)
        assert results == [{"action": "rename", "label": "New Label"}]

    @pytest.mark.asyncio
    async def test_rename_with_invalid_label_length_becomes_none(self) -> None:
        from nexus.commands import taxonomy_cmd

        items = [(101, "old", [], [], "proj")]
        dispatched = AsyncMock(
            return_value={"verdicts": [{"id": 101, "action": "rename", "label": "x"}]}
        )
        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_review_verdicts_batch(items)
        assert results == [None]

    @pytest.mark.asyncio
    async def test_rename_label_stripped_before_length_check(self) -> None:
        """Finding 5: strip happens BEFORE the length check, not after.

        "  x  " strips to "x" (len 1) — must be rejected, not accepted on
        the strength of its padded length.
        """
        from nexus.commands import taxonomy_cmd

        items = [(101, "old", [], [], "proj")]
        dispatched = AsyncMock(
            return_value={"verdicts": [{"id": 101, "action": "rename", "label": "  x  "}]}
        )
        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_review_verdicts_batch(items)
        assert results == [None]

    @pytest.mark.asyncio
    async def test_rename_label_over_60_chars_rejected(self) -> None:
        from nexus.commands import taxonomy_cmd

        items = [(101, "old", [], [], "proj")]
        dispatched = AsyncMock(
            return_value={"verdicts": [{"id": 101, "action": "rename", "label": "Z" * 61}]}
        )
        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_review_verdicts_batch(items)
        assert results == [None]

    @pytest.mark.asyncio
    async def test_rename_label_stored_stripped_when_valid(self) -> None:
        from nexus.commands import taxonomy_cmd

        items = [(101, "old", [], [], "proj")]
        dispatched = AsyncMock(
            return_value={
                "verdicts": [{"id": 101, "action": "rename", "label": "  Good Label  "}],
            }
        )
        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_review_verdicts_batch(items)
        assert results == [{"action": "rename", "label": "Good Label"}]

    @pytest.mark.asyncio
    async def test_merge_with_target_id(self) -> None:
        from nexus.commands import taxonomy_cmd

        items = [(101, "old", [], [], "proj")]
        dispatched = AsyncMock(
            return_value={
                "verdicts": [{"id": 101, "action": "merge", "target_id": 202, "reason": "dup"}],
            }
        )
        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_review_verdicts_batch(items)
        assert results == [{"action": "merge", "target_id": 202, "reason": "dup"}]

    @pytest.mark.asyncio
    async def test_merge_without_target_id_becomes_none(self) -> None:
        from nexus.commands import taxonomy_cmd

        items = [(101, "old", [], [], "proj")]
        dispatched = AsyncMock(return_value={"verdicts": [{"id": 101, "action": "merge"}]})
        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_review_verdicts_batch(items)
        assert results == [None]

    @pytest.mark.asyncio
    async def test_delete_with_reason(self) -> None:
        from nexus.commands import taxonomy_cmd

        items = [(101, "old", [], [], "proj")]
        dispatched = AsyncMock(
            return_value={"verdicts": [{"id": 101, "action": "delete", "reason": "pollution"}]}
        )
        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_review_verdicts_batch(items)
        assert results == [{"action": "delete", "reason": "pollution"}]

    @pytest.mark.asyncio
    async def test_unknown_action_ignored(self) -> None:
        from nexus.commands import taxonomy_cmd

        items = [(101, "old", [], [], "proj")]
        dispatched = AsyncMock(return_value={"verdicts": [{"id": 101, "action": "explode"}]})
        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_review_verdicts_batch(items)
        assert results == [None]

    @pytest.mark.asyncio
    async def test_dispatch_raises_returns_all_none(self) -> None:
        from nexus.commands import taxonomy_cmd

        items = [(101, "old", [], [], "proj"), (102, "b", [], [], "proj")]
        with patch(
            "nexus.operators.dispatch.claude_dispatch",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            results = await taxonomy_cmd._generate_review_verdicts_batch(items)
        assert results == [None, None]

    @pytest.mark.asyncio
    async def test_missing_verdicts_key_returns_all_none(self) -> None:
        from nexus.commands import taxonomy_cmd

        items = [(101, "old", [], [], "proj")]
        dispatched = AsyncMock(return_value={"nope": []})
        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_review_verdicts_batch(items)
        assert results == [None]

    @pytest.mark.asyncio
    async def test_id_not_in_batch_ignored(self) -> None:
        """Finding 3: an entry keyed with an id NOT present in this batch's

        items is ignored entirely (not misapplied to some other slot by
        position) — the wrong-topic-verdict class this fix eliminates.
        """
        from nexus.commands import taxonomy_cmd

        items = [(101, "old", [], [], "proj")]
        dispatched = AsyncMock(return_value={"verdicts": [{"id": 9999, "action": "accept"}]})
        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_review_verdicts_batch(items)
        assert results == [None]

    @pytest.mark.asyncio
    async def test_empty_items_returns_empty_without_dispatch(self) -> None:
        from nexus.commands import taxonomy_cmd

        dispatched = AsyncMock()
        with patch("nexus.operators.dispatch.claude_dispatch", dispatched):
            results = await taxonomy_cmd._generate_review_verdicts_batch([])
        assert results == []
        assert not dispatched.called

    @pytest.mark.asyncio
    async def test_prompt_keys_by_id_not_position_and_merge_wording(self) -> None:
        """Finding 3 + 4: the prompt must not present a separate ordinal

        index alongside the id (the dual-numbering ambiguity this fix
        eliminates), and the merge instruction must allow same-collection
        targets not necessarily listed in this batch.
        """
        from nexus.commands import taxonomy_cmd

        captured: dict[str, str] = {}

        async def fake_dispatch(prompt: str, schema: dict, **kw):
            captured["prompt"] = prompt
            return {"verdicts": []}

        items = [(101, "old", ["term"], ["doc.py"], "proj")]
        with patch("nexus.operators.dispatch.claude_dispatch", fake_dispatch):
            await taxonomy_cmd._generate_review_verdicts_batch(items)

        prompt = captured["prompt"]
        assert "id=101" in prompt
        assert "same collection" in prompt
        assert "id= value" in prompt
        # No positional idx concept anywhere in the instructions.
        assert "idx" not in prompt.lower()


# ── CLI: nx taxonomy review --auto ──────────────────────────────────────────


class TestReviewAutoCLI:

    def test_accept_applies_immediately_no_confirm(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        tid = _seed_topic(db_path, "old label", doc_count=3)
        dispatch = AsyncMock(side_effect=_dispatch_by_topic_id({tid: {"action": "accept"}}))

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(taxonomy, ["review", "--auto", "--collection", "proj"])

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            status = db.taxonomy.get_topic_by_id(tid)["review_status"]
        assert status == "accepted"

    def test_rename_applies_immediately(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        tid = _seed_topic(db_path, "bad label", doc_count=1)
        dispatch = AsyncMock(
            side_effect=_dispatch_by_topic_id(
                {tid: {"action": "rename", "label": "Deep Learning"}}
            )
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(taxonomy, ["review", "--auto", "--collection", "proj"])

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            topic = db.taxonomy.get_topic_by_id(tid)
        assert topic["label"] == "Deep Learning"
        assert topic["review_status"] == "accepted"

    def test_delete_declined_by_default_leaves_pending(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        tid = _seed_topic(db_path, "junk", doc_count=1)
        dispatch = AsyncMock(
            side_effect=_dispatch_by_topic_id(
                {tid: {"action": "delete", "reason": "pollution"}}
            )
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(
                taxonomy, ["review", "--auto", "--collection", "proj"], input="n\n",
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            count = len(db.taxonomy.get_all_topics())
            status = db.taxonomy.get_topic_by_id(tid)["review_status"]
        assert count == 1
        assert status == "pending"
        # Finding 6: declined destructive items count toward the skipped
        # bucket in the summary line, not silently dropped from the tally.
        assert "0 accepted, 0 renamed, 0 deleted, 0 merged, 1 skipped, 0 failed." in result.output

    def test_delete_applied_with_yes(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        tid = _seed_topic(db_path, "junk", doc_count=1)
        dispatch = AsyncMock(
            side_effect=_dispatch_by_topic_id(
                {tid: {"action": "delete", "reason": "pollution"}}
            )
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(
                taxonomy, ["review", "--auto", "--collection", "proj", "--yes"],
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            count = len(db.taxonomy.get_all_topics())
        assert count == 0

    def test_merge_applied_with_confirm(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        source_id = _seed_topic(db_path, "source", doc_count=2, n_docs=2)
        target_id = _seed_topic(
            db_path, "target", doc_count=3, n_docs=3, review_status="accepted",
        )
        dispatch = AsyncMock(
            side_effect=_dispatch_by_topic_id(
                {source_id: {"action": "merge", "target_id": target_id, "reason": "dup"}}
            )
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(
                taxonomy, ["review", "--auto", "--collection", "proj"], input="y\n",
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            assert db.taxonomy.get_topic_by_id(source_id) is None
            target = db.taxonomy.get_topic_by_id(target_id)
        assert target["doc_count"] == 5

    def test_merge_nonexistent_target_stays_pending(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        source_id = _seed_topic(db_path, "source", doc_count=1)
        dispatch = AsyncMock(
            side_effect=_dispatch_by_topic_id(
                {source_id: {"action": "merge", "target_id": 999999, "reason": "dup"}}
            )
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(taxonomy, ["review", "--auto", "--collection", "proj"])

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            count = len(db.taxonomy.get_all_topics())
            status = db.taxonomy.get_topic_by_id(source_id)["review_status"]
        assert count == 1
        assert status == "pending"

    def test_merge_cross_collection_target_stays_pending(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        source_id = _seed_topic(db_path, "source", collection="proj", doc_count=1)
        target_id = _seed_topic(db_path, "target", collection="other", doc_count=1)
        dispatch = AsyncMock(
            side_effect=_dispatch_by_topic_id(
                {source_id: {"action": "merge", "target_id": target_id, "reason": "dup"}}
            )
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(taxonomy, ["review", "--auto", "--collection", "proj"])

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            status = db.taxonomy.get_topic_by_id(source_id)["review_status"]
        assert status == "pending"

    def test_merge_self_target_stays_pending_and_not_in_plan(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        source_id = _seed_topic(db_path, "source", doc_count=1)
        dispatch = AsyncMock(
            side_effect=_dispatch_by_topic_id(
                {source_id: {"action": "merge", "target_id": source_id, "reason": "dup"}}
            )
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(taxonomy, ["review", "--auto", "--collection", "proj"])

        assert result.exit_code == 0, result.output
        assert "Destructive" not in result.output
        with T2Database(db_path) as db:
            status = db.taxonomy.get_topic_by_id(source_id)["review_status"]
        assert status == "pending"

    def test_merge_target_also_deleted_merge_pending_delete_applies(
        self, tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "memory.db"
        a_id = _seed_topic(db_path, "topic-a", doc_count=1)
        b_id = _seed_topic(db_path, "topic-b", doc_count=1)
        dispatch = AsyncMock(
            side_effect=_dispatch_by_topic_id(
                {
                    a_id: {"action": "merge", "target_id": b_id, "reason": "dup"},
                    b_id: {"action": "delete", "reason": "pollution"},
                }
            )
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(
                taxonomy, ["review", "--auto", "--collection", "proj"], input="y\n",
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            a_status = db.taxonomy.get_topic_by_id(a_id)["review_status"]
            b_topic = db.taxonomy.get_topic_by_id(b_id)
        assert a_status == "pending"
        assert b_topic is None

    def test_merge_chain_validation_drops_target_that_is_also_a_source(
        self, tmp_path: Path,
    ) -> None:
        """Finding 1 (unit-level, dry-run): A->B, B->C in the same batch.

        The second-pass guard must drop A->B (B is itself a merge SOURCE
        this run) while leaving B->C valid — proven here purely at the
        validation layer via --dry-run so no apply-time mechanics are in
        play. Isolates the merge_source_ids guard from the
        data-integrity/apply-order concern covered by the sibling
        --yes test below.
        """
        db_path = tmp_path / "memory.db"
        a_id = _seed_topic(db_path, "topic-a", doc_count=1)
        b_id = _seed_topic(db_path, "topic-b", doc_count=1)
        c_id = _seed_topic(db_path, "topic-c", doc_count=1, review_status="accepted")
        dispatch = AsyncMock(
            side_effect=_dispatch_by_topic_id(
                {
                    a_id: {"action": "merge", "target_id": b_id, "reason": "dup"},
                    b_id: {"action": "merge", "target_id": c_id, "reason": "dup"},
                }
            )
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(
                taxonomy, ["review", "--auto", "--collection", "proj", "--dry-run"],
            )

        assert result.exit_code == 0, result.output
        # Only the B->C merge appears in the destructive plan; A->B was
        # dropped at validation (B is a merge source in this run).
        assert "topic-b" in result.output and "topic-c" in result.output
        assert "topic-a" not in result.output.split("Destructive actions pending:")[-1]
        assert (
            "0 would be accepted, 0 would be renamed, 0 would be deleted, "
            "1 would be merged, 1 skipped."
            in result.output
        )

    def test_merge_chain_same_run_applies_b_to_c_drops_a_to_b_data_intact(
        self, tmp_path: Path,
    ) -> None:
        """Finding 1 (CRITICAL, reproduction): A->B (target), B->C (source)

        in the same run. merge_topics has no target-existence check and T2
        SQLite runs with foreign keys off, so without the merge-source
        guard, applying A->B after B->C deletes B would silently orphan
        A's assignments. Proves: B->C applies (deterministic regardless of
        candidate order), A->B stays pending, A's own assignments are
        completely untouched (no data loss).
        """
        db_path = tmp_path / "memory.db"
        a_id = _seed_topic(db_path, "topic-a", doc_count=2, n_docs=2)
        b_id = _seed_topic(db_path, "topic-b", doc_count=2, n_docs=2)
        c_id = _seed_topic(
            db_path, "topic-c", doc_count=1, n_docs=1, review_status="accepted",
        )
        dispatch = AsyncMock(
            side_effect=_dispatch_by_topic_id(
                {
                    a_id: {"action": "merge", "target_id": b_id, "reason": "dup"},
                    b_id: {"action": "merge", "target_id": c_id, "reason": "dup"},
                }
            )
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(
                taxonomy, ["review", "--auto", "--collection", "proj"], input="y\n",
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            a_topic = db.taxonomy.get_topic_by_id(a_id)
            b_topic = db.taxonomy.get_topic_by_id(b_id)
            c_topic = db.taxonomy.get_topic_by_id(c_id)
            a_assignments = len(db.taxonomy.get_all_topic_doc_ids(a_id))

        # A stays pending, untouched — no merge was ever attempted against it.
        assert a_topic is not None
        assert a_topic["review_status"] == "pending"
        assert a_assignments == 2
        # B->C applied: B gone, C absorbed B's docs (1 own + 2 from B == 3).
        assert b_topic is None
        assert c_topic["doc_count"] == 3
        assert (
            "0 accepted, 0 renamed, 0 deleted, 1 merged, 1 skipped, 0 failed."
            in result.output
        )

    def test_dry_run_mixed_batch_zero_mutations(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        t_accept = _seed_topic(db_path, "accept-me", doc_count=1)
        t_rename = _seed_topic(db_path, "bad-label", doc_count=1)
        t_delete = _seed_topic(db_path, "pytest fixture scaffolding", doc_count=1)
        t_target = _seed_topic(
            db_path, "keep-me", doc_count=1, review_status="accepted",
        )
        t_merge = _seed_topic(db_path, "dup-of-keep-me", doc_count=1)
        dispatch = AsyncMock(
            side_effect=_dispatch_by_topic_id(
                {
                    t_accept: {"action": "accept"},
                    t_rename: {"action": "rename", "label": "Good New Label"},
                    t_delete: {"action": "delete", "reason": "pollution"},
                    t_merge: {"action": "merge", "target_id": t_target, "reason": "dup"},
                }
            )
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(
                taxonomy, ["review", "--auto", "--collection", "proj", "--dry-run"],
            )

        assert result.exit_code == 0, result.output
        assert "Destructive" in result.output
        with T2Database(db_path) as db:
            for tid, expected_label in [
                (t_accept, "accept-me"),
                (t_rename, "bad-label"),
                (t_delete, "pytest fixture scaffolding"),
                (t_target, "keep-me"),
                (t_merge, "dup-of-keep-me"),
            ]:
                topic = db.taxonomy.get_topic_by_id(tid)
                assert topic is not None
                assert topic["label"] == expected_label
            assert db.taxonomy.get_topic_by_id(t_accept)["review_status"] == "pending"
            assert db.taxonomy.get_topic_by_id(t_rename)["review_status"] == "pending"
            assert db.taxonomy.get_topic_by_id(t_delete)["review_status"] == "pending"
            assert db.taxonomy.get_topic_by_id(t_merge)["review_status"] == "pending"
            assert db.taxonomy.get_topic_by_id(t_target)["review_status"] == "accepted"

    def test_dispatch_raises_all_stay_pending_exit_zero(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        t1 = _seed_topic(db_path, "topic-1", doc_count=1)
        t2 = _seed_topic(db_path, "topic-2", doc_count=1)
        dispatch = AsyncMock(side_effect=RuntimeError("boom"))

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(taxonomy, ["review", "--auto", "--collection", "proj"])

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            assert db.taxonomy.get_topic_by_id(t1)["review_status"] == "pending"
            assert db.taxonomy.get_topic_by_id(t2)["review_status"] == "pending"
        assert "2 skipped" in result.output

    def test_multi_batch_second_batch_dispatch_raises_first_batch_applies(
        self, tmp_path: Path,
    ) -> None:
        """Finding 7: batch 1 succeeds, batch 2's dispatch call raises.

        Batch-1 verdicts must still be applied; batch-2 topics stay
        pending; exit code 0; exact skip count for batch 2's topics.
        """
        db_path = tmp_path / "memory.db"
        # doc_count DESC ordering controls batch membership: t1,t2 (highest)
        # land in batch 1; t3,t4 in batch 2, with --batch-size 2.
        t1 = _seed_topic(db_path, "topic-1", doc_count=4)
        t2 = _seed_topic(db_path, "topic-2", doc_count=3)
        t3 = _seed_topic(db_path, "topic-3", doc_count=2)
        t4 = _seed_topic(db_path, "topic-4", doc_count=1)

        calls = {"n": 0}

        async def _fake(prompt: str, schema: dict, **kw):  # noqa: ARG001
            calls["n"] += 1
            if calls["n"] == 1:
                ids = [int(m) for m in re.findall(r"id=(\d+)", prompt)]
                return {"verdicts": [{"id": tid, "action": "accept"} for tid in ids]}
            raise RuntimeError("batch2 boom")

        dispatch = AsyncMock(side_effect=_fake)

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(
                taxonomy,
                ["review", "--auto", "--collection", "proj", "--batch-size", "2"],
            )

        assert result.exit_code == 0, result.output
        assert calls["n"] == 2
        with T2Database(db_path) as db:
            assert db.taxonomy.get_topic_by_id(t1)["review_status"] == "accepted"
            assert db.taxonomy.get_topic_by_id(t2)["review_status"] == "accepted"
            assert db.taxonomy.get_topic_by_id(t3)["review_status"] == "pending"
            assert db.taxonomy.get_topic_by_id(t4)["review_status"] == "pending"
        assert (
            "2 accepted, 0 renamed, 0 deleted, 0 merged, 2 skipped, 0 failed." in result.output
        )

    def test_apply_loop_one_bad_delete_does_not_abort_remaining_actions(
        self, tmp_path: Path,
    ) -> None:
        """Finding 2: t2_index_write raising for one delete must not abort

        the remaining deletes/merges — it's counted as failed, the loop
        continues, exit code stays 0.
        """
        db_path = tmp_path / "memory.db"
        d1 = _seed_topic(db_path, "junk-1", doc_count=1, n_docs=1)
        d2 = _seed_topic(db_path, "junk-2", doc_count=1, n_docs=1)
        source_id = _seed_topic(db_path, "source", doc_count=1, n_docs=1)
        target_id = _seed_topic(
            db_path, "target", doc_count=1, n_docs=1, review_status="accepted",
        )
        dispatch = AsyncMock(
            side_effect=_dispatch_by_topic_id(
                {
                    d1: {"action": "delete", "reason": "pollution"},
                    d2: {"action": "delete", "reason": "pollution"},
                    source_id: {"action": "merge", "target_id": target_id, "reason": "dup"},
                }
            )
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router_delete_fails_for(db_path, d1)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(
                taxonomy, ["review", "--auto", "--collection", "proj", "--yes"],
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            d1_topic = db.taxonomy.get_topic_by_id(d1)
            d2_topic = db.taxonomy.get_topic_by_id(d2)
            source_topic = db.taxonomy.get_topic_by_id(source_id)
            target_topic = db.taxonomy.get_topic_by_id(target_id)
        # d1's delete failed: topic untouched, still pending.
        assert d1_topic is not None
        assert d1_topic["review_status"] == "pending"
        # d2's delete succeeded despite d1's failure.
        assert d2_topic is None
        # The merge (unrelated to the failing delete) still applied.
        assert source_topic is None
        assert target_topic["doc_count"] == 2
        assert (
            "0 accepted, 0 renamed, 1 deleted, 1 merged, 0 skipped, 1 failed." in result.output
        )

    def test_auto_no_limit_processes_all_20_via_5000_default(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        ids = [_seed_topic(db_path, f"topic-{i}", doc_count=1) for i in range(20)]
        dispatch = AsyncMock(
            side_effect=_dispatch_by_topic_id({tid: {"action": "accept"} for tid in ids})
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(taxonomy, ["review", "--auto", "--collection", "proj"])

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            accepted_count = sum(
                1 for t in db.taxonomy.get_all_topics()
                if t["review_status"] == "accepted"
            )
        assert accepted_count == 20

    def test_auto_limit_5_processes_exactly_5(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        ids = [_seed_topic(db_path, f"topic-{i}", doc_count=20 - i) for i in range(20)]
        dispatch = AsyncMock(
            side_effect=_dispatch_by_topic_id({tid: {"action": "accept"} for tid in ids})
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(
                taxonomy, ["review", "--auto", "--collection", "proj", "--limit", "5"],
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            statuses = [t["review_status"] for t in db.taxonomy.get_all_topics()]
            accepted_count = statuses.count("accepted")
            pending_count = statuses.count("pending")
        assert accepted_count == 5
        assert pending_count == 15

    def test_batch_size_2_dispatches_ceil_5_over_2(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        ids = [_seed_topic(db_path, f"topic-{i}", doc_count=1) for i in range(5)]
        dispatch = AsyncMock(
            side_effect=_dispatch_by_topic_id({tid: {"action": "accept"} for tid in ids})
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(
                taxonomy,
                ["review", "--auto", "--collection", "proj", "--batch-size", "2"],
            )

        assert result.exit_code == 0, result.output
        assert dispatch.call_count == 3

    def test_no_pending_topics_same_all_done_message(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        with T2Database(db_path):
            pass

        runner = CliRunner()
        with patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path):
            result = runner.invoke(taxonomy, ["review", "--auto", "--collection", "proj"])

        assert result.exit_code == 0, result.output
        assert (
            "no unreviewed topics" in result.output.lower()
            or "all done" in result.output.lower()
        )

    def test_mixed_outcome_summary_reports_exact_counts(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        t_accept = _seed_topic(db_path, "accept-me", doc_count=1)
        t_rename = _seed_topic(db_path, "bad-label", doc_count=1)
        t_delete = _seed_topic(db_path, "pytest fixture scaffolding", doc_count=1)
        t_target = _seed_topic(
            db_path, "keep-me", doc_count=1, review_status="accepted",
        )
        t_merge = _seed_topic(db_path, "dup-of-keep-me", doc_count=1)
        dispatch = AsyncMock(
            side_effect=_dispatch_by_topic_id(
                {
                    t_accept: {"action": "accept"},
                    t_rename: {"action": "rename", "label": "Good New Label"},
                    t_delete: {"action": "delete", "reason": "pollution"},
                    t_merge: {"action": "merge", "target_id": t_target, "reason": "dup"},
                }
            )
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", dispatch),
        ):
            result = runner.invoke(
                taxonomy, ["review", "--auto", "--collection", "proj"], input="y\n",
            )

        assert result.exit_code == 0, result.output
        assert (
            "1 accepted, 1 renamed, 1 deleted, 1 merged, 0 skipped, 0 failed."
            in result.output
        )


class TestDispatchFailureRollup:
    """nexus-l1qpj: dispatch failures exercised against the REAL non-zero-
    exit branch (patching ``asyncio.create_subprocess_exec`` one level
    below, NOT ``claude_dispatch`` wholesale — every prior test patched the
    dispatcher out, which is why the per-failure WARNING landed untested).
    The batch functions fill the caller's ``failures`` collector while the
    return contract stays byte-identical (all ``None``)."""

    @staticmethod
    def _failing_proc():
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(
            return_value=(b'{"type":"result","subtype":"error"}', b""),
        )
        return proc

    @pytest.mark.asyncio
    async def test_verdicts_batch_fills_collector_on_real_exit_1(self) -> None:
        from nexus.commands.taxonomy_cmd import _generate_review_verdicts_batch

        failures: list[str] = []
        items = [(1, "label-a", ["term"], ["doc"], "coll")]
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=self._failing_proc()),
        ):
            results = await _generate_review_verdicts_batch(
                items, failures=failures,
            )
        assert results == [None]  # fail-open contract unchanged
        assert len(failures) == 1
        assert "dispatch-harness failure" in failures[0]

    @pytest.mark.asyncio
    async def test_labels_batch_fills_collector_on_real_exit_1(self) -> None:
        from nexus.commands.taxonomy_cmd import _generate_labels_batch

        failures: list[str] = []
        items = [(["term-a"], ["doc-a"])]
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=self._failing_proc()),
        ):
            results = await _generate_labels_batch(items, failures=failures)
        assert results == [None]
        assert len(failures) == 1

    @pytest.mark.asyncio
    async def test_real_failure_is_demoted_to_info_inside_batch(self) -> None:
        # The choke-point event must arrive at INFO (rolled-up scope set by
        # the batch function), keeping per-failure noise off the terminal
        # while the run log still captures it.
        from nexus.commands.taxonomy_cmd import _generate_review_verdicts_batch

        items = [(1, "label-a", ["term"], ["doc"], "coll")]
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=self._failing_proc()),
        ):
            with patch("nexus.operators.dispatch._log") as mock_log:
                await _generate_review_verdicts_batch(items, failures=[])
        assert mock_log.info.called
        assert not mock_log.warning.called

    @pytest.mark.asyncio
    async def test_no_collector_still_fail_open_and_still_warns(self) -> None:
        # Round-3 critique HIGH-2: callers on the failures=None convention
        # keep the OLD visibility too — the choke-point event stays a
        # WARNING (no rollup exists to compensate for a demotion), and the
        # return contract stays fail-open. Asserting only the return value
        # was false assurance.
        from nexus.commands.taxonomy_cmd import _generate_review_verdicts_batch

        items = [(1, "label-a", ["term"], ["doc"], "coll")]
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=self._failing_proc()),
        ):
            with patch("nexus.operators.dispatch._log") as mock_log:
                results = await _generate_review_verdicts_batch(items)
        assert results == [None]
        assert mock_log.warning.called
        assert not mock_log.info.called
