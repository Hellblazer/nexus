# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-verb journey: discover -> assign -> label (409-injected) -> review
-> split, against the real T2 engine substrate (nexus taxonomy-label-
pipeline fix, 2026-08-19 silent-failure sweep).

Companion to ``tests/test_taxonomy_e2e.py`` (real HDBSCAN + real MiniLM
embeddings, hence ``integration``-marked — not the lightweight ``scenario``
file, whose ~1s/journey and no-fakes-for-T3 conventions this test's real
clustering + KMeans split cost do not fit; see ``tests/AGENTS.md`` § scenario
journeys). T2/taxonomy state is the REAL engine (``t2_service_env`` ->
``tests._engine_substrate.ensure_engine``); T3 vector storage is the
project's standard in-process test substrate (``make_vector_test_client``),
matching ``test_taxonomy_e2e.py``'s own convention.

Builds on the split-conservation guard shipped same-day (commit
b686b4d25, ``SplitConservationViolatedError`` / the split_cmd
"Redistribution:" line) rather than re-testing it — this journey only
needs split to succeed and conserve, which that guard already protects.

Proves, in one continuous run:

1. A single T2 persist 409 during ``label`` does not abort the run — the
   other topics in the batch still get relabeled (defect #1).
2. The failed topic's label is unchanged and it is the ONLY topic a
   second ``label`` run re-selects — already-relabeled topics are not
   re-fed to the LLM (defect #2), and the pipeline converges instead of
   reprocessing the same topic forever.
3. The 409's failure record actually lands in the run log, not silently
   dropped (defect #3).
4. ``review --auto`` still operates correctly on topics coming out of the
   label step (label and review stay separate axes — GH #241 Item 3).
5. ``split`` on a post-review topic conserves every assignment (the
   b686b4d25 guard holds end-to-end).
6. doc_count totals conserve at every step transition (no docs silently
   gained or lost crossing discover -> assign -> label -> review -> split).
"""
from __future__ import annotations

import itertools
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from click.testing import CliRunner

import nexus.mcp_infra as _mi
from nexus.commands.taxonomy_cmd import taxonomy
from nexus.commands import taxonomy_cmd
from nexus.db.local_ef import LocalEmbeddingFunction
from nexus.db.t2 import T2Database
from nexus.logging_setup import configure_logging

from tests._t2_fixture_ops import canonical_chunk_id
from tests.conftest import make_vector_test_client

pytestmark = pytest.mark.integration

_current_tenant: str | None = None


@pytest.fixture(autouse=True)
def _engine_substrate(t2_service_env: str):
    global _current_tenant
    _current_tenant = t2_service_env
    yield
    _current_tenant = None


def _seed_chunks_for_tenant(
    tenant: str, collection: str, chash_hexes: list[str], *, dim: int = 384,
) -> None:
    """Seed real nexus.chunks rows so topic_assignments_chunk_fk is
    satisfied. Duplicated locally per this repo's established convention
    (test_taxonomy_e2e.py / test_taxonomy_review_auto.py both carry their
    own copy — no shared import path across test modules)."""
    from tests._engine_substrate import ensure_engine  # noqa: PLC0415 — laziness contract

    if not chash_hexes:
        return
    state = ensure_engine()
    embed_col = {384: "embedding_384", 768: "embedding_768", 1024: "embedding_1024"}[dim]
    vec = "[" + ",".join(["0"] * dim) + "]"
    values = ", ".join(
        f"('{tenant}', '{collection}', decode('{c}', 'hex'), 'seed', '{vec}'::vector)"
        for c in chash_hexes
    )
    sql = (
        f"INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('{tenant}', '{collection}') "
        "ON CONFLICT DO NOTHING; "
        f"INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, {embed_col}) "
        f"VALUES {values} ON CONFLICT DO NOTHING;"
    )
    psql = Path(state["pg_bin"]) / "psql"
    proc = subprocess.run(
        [
            str(psql), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
            "-U", state["pg_user"], "-d", state["pg_dbname"],
            "-v", "ON_ERROR_STOP=1", "-c", sql,
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"_seed_chunks_for_tenant failed: {proc.stdout}\n{proc.stderr}"


def _t2_router(db_path: Path):
    def _router(fn):
        with T2Database(db_path) as db:
            return fn(db)
    return _router


def _t2_router_label_fails_for(db_path: Path, *failing_ids: int):
    def _router(fn):
        with T2Database(db_path) as db:
            orig_update = db.taxonomy.update_topic_label

            def _update(topic_id, new_label, **kw):
                if topic_id in failing_ids:
                    raise RuntimeError("409 Conflict: topic modified concurrently")
                return orig_update(topic_id, new_label, **kw)

            db.taxonomy.update_topic_label = _update
            return fn(db)
    return _router


def _make_globally_unique_label_dispatch():
    """Labels unique ACROSS every call the fake makes, not just within one
    batch's numbered list.

    ``relabel_topics`` persists labels to T2, which enforces a (tenant,
    collection, label) uniqueness constraint on root topics. A per-call
    1-based numbering ("Real Label 1", "Real Label 2", ...) repeats
    across separate ``relabel_topics`` invocations (batch_size=1 forces
    one topic per batch here, and this journey calls relabel_topics
    twice — the initial label pass and the defect-#2 retry) — a second
    call would re-mint "Real Label 1" and collide with a label already
    persisted by the first, tripping a genuine 409 the journey isn't
    trying to test. One shared counter across every invocation of the
    returned fake keeps every minted label unique for the run."""
    counter = itertools.count(1)

    async def _fake(prompt: str, schema: dict, **kw):  # noqa: ARG001
        n = len(re.findall(r"^\d+\. terms=", prompt, flags=re.MULTILINE))
        return {"labels": [{"idx": i, "label": f"Real Label {next(counter)}"} for i in range(1, n + 1)]}

    return _fake


def _fake_review_accept_all():
    """Verdict dispatch keyed by real topic id (parsed from the prompt's
    ``id=<n>`` markers) — mirrors test_taxonomy_review_auto.py's
    ``_dispatch_by_topic_id``: accept whatever is asked."""
    async def _fake(prompt: str, schema: dict, **kw):  # noqa: ARG001
        ids_in_prompt = {int(m) for m in re.findall(r"id=(\d+)", prompt)}
        return {"verdicts": [{"id": tid, "action": "accept"} for tid in ids_in_prompt]}
    return _fake


def _build_two_domain_corpus() -> tuple[list[str], list[str]]:
    """30 docs, 2 well-separated domains — same shape proven to produce
    clean HDBSCAN clusters in test_taxonomy_e2e.py's ``_build_corpus``."""
    http = [
        f"def handle_request(request): response = json_response(status={200 + i}); return response"
        for i in range(8)
    ] + [
        f"@app.route('/api/v{i}') def endpoint(): return jsonify(data)"
        for i in range(7)
    ]
    db = [
        f"cursor.execute('SELECT id, name FROM users WHERE age > {i}') rows = cursor.fetchall()"
        for i in range(8)
    ] + [
        f"conn.execute('INSERT INTO logs (event, ts) VALUES (?, ?)', (event_{i}, now()))"
        for i in range(7)
    ]
    texts = http + db
    doc_ids = (
        [canonical_chunk_id(f"journey/http/{i}.py") for i in range(15)]
        + [canonical_chunk_id(f"journey/db/{i}.py") for i in range(15)]
    )
    return doc_ids, texts


class TestLabelPipelineJourney:

    def test_discover_assign_label_409_review_split_conserves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path = tmp_path / "memory.db"
        collection = "journey__label-pipeline"
        # Redirect the run log to a scratch dir — never write to the
        # operator's real ~/.config/nexus (this box may run a live install).
        config_dir = tmp_path / "cfg"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))
        # open_run_log's documented precondition (test_logging_setup.py's
        # "Real CLI precondition"): every `nx` invocation goes through
        # cli()'s group callback, which calls this before any subcommand
        # body runs. This journey calls relabel_topics() directly,
        # bypassing that group — without this the structlog event never
        # bridges to the stdlib root logger the run-log file handler
        # listens on, and 3b's log-content assertion sees an empty file.
        # The autouse ``_restore_structlog_after_test`` fixture reverts
        # this after the test.
        configure_logging("cli")

        doc_ids, texts = _build_two_domain_corpus()
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        embeddings = np.asarray(ef(texts), dtype=np.float32)

        _seed_chunks_for_tenant(_current_tenant, collection, doc_ids)

        # ── 1. discover: real HDBSCAN over real MiniLM embeddings ──────────
        with T2Database(db_path) as db:
            topic_count = db.taxonomy.discover_topics(
                collection, doc_ids, embeddings, texts,
            )
        assert topic_count >= 1, "discover produced no topics from a 2-domain corpus"

        with T2Database(db_path) as db:
            topics_after_discover = db.taxonomy.get_topics_for_collection(collection)
        discover_total = sum(t["doc_count"] for t in topics_after_discover)
        assert discover_total > 0
        assert discover_total <= len(doc_ids), (
            "discover assigned more docs than exist in the corpus"
        )
        # Every freshly-discovered topic's label is its own c-TF-IDF
        # auto-label by construction (compute_discovered_topics) — the
        # selection fix (defect #2) must treat every one of them as
        # "needs labeling".
        for t in topics_after_discover:
            assert taxonomy_cmd._topic_has_auto_label(t), (
                f"topic {t['id']!r} freshly discovered but already reads "
                "as labeled — _topic_has_auto_label is wrong"
            )

        # ── 2. assign: one new doc lands on the nearest topic ──────────────
        new_text = "def handle_delete_request(request): return no_content()"
        new_emb = np.asarray(ef([new_text])[0], dtype=np.float32)
        with T2Database(db_path) as db:
            assign_result = db.taxonomy.assign_single(collection, new_emb, None)
        assert assign_result is not None, "assign_single found no centroid to assign to"
        target_id = assign_result.topic_id

        new_doc_id = canonical_chunk_id("journey/http/new-0.py")
        _seed_chunks_for_tenant(_current_tenant, collection, [new_doc_id])
        with T2Database(db_path) as db:
            db.taxonomy.assign_topic(
                new_doc_id, target_id, assigned_by="assign_single",
                similarity=assign_result.similarity, source_collection=collection,
            )

        with T2Database(db_path) as db:
            topics_after_assign = db.taxonomy.get_topics_for_collection(collection)
        assign_total = sum(t["doc_count"] for t in topics_after_assign)
        assert assign_total == discover_total + 1, (
            f"expected exactly one new doc counted after assign: "
            f"{discover_total} -> {assign_total}"
        )

        # ── 3. label: batched relabel with an injected mid-batch 409 ───────
        # batch_size=1 forces one topic per batch, so failing exactly one
        # topic's persist proves the OTHERS still complete (defect #1) —
        # under the pre-fix code this 409 aborts the whole run instead.
        failing_id = topics_after_assign[0]["id"]
        other_ids = [t["id"] for t in topics_after_assign if t["id"] != failing_id]

        # Shared across BOTH label calls (this one and step 4's retry) so
        # every minted label is unique for the whole run — see
        # _make_globally_unique_label_dispatch's docstring.
        fake_label_dispatch = _make_globally_unique_label_dispatch()

        with (
            patch.object(_mi, "t2_index_write", _t2_router_label_fails_for(db_path, failing_id)),
            patch("nexus.operators.dispatch.claude_dispatch", fake_label_dispatch),
        ):
            with T2Database(db_path) as db:
                labeled = taxonomy_cmd.relabel_topics(
                    db.taxonomy, collection=collection, batch_size=1, workers=1,
                )
        assert labeled == len(other_ids), (
            f"expected every topic but the failing one to be relabeled: "
            f"got {labeled}, expected {len(other_ids)}"
        )

        with T2Database(db_path) as db:
            topics_by_id_after_label = {
                t["id"]: t for t in db.taxonomy.get_topics_for_collection(collection)
            }
        failing_original_label = next(
            t["label"] for t in topics_after_assign if t["id"] == failing_id
        )
        assert topics_by_id_after_label[failing_id]["label"] == failing_original_label, (
            "the topic whose persist failed must keep its pre-existing label"
        )
        for oid in other_ids:
            original_label = next(t["label"] for t in topics_after_assign if t["id"] == oid)
            assert topics_by_id_after_label[oid]["label"] != original_label, (
                f"topic {oid} should have been relabeled despite topic "
                f"{failing_id}'s persist failure"
            )
        # label/review stay separate axes: nothing advances review_status
        # just from being relabeled (GH #241 Item 3).
        for t in topics_by_id_after_label.values():
            assert t["review_status"] == "pending"
        label_total = sum(t["doc_count"] for t in topics_by_id_after_label.values())
        assert label_total == assign_total, "labeling must never move doc counts"

        # ── 3b. run log: the 409 actually landed in taxonomy-label.log ─────
        log_path = config_dir / "logs" / "taxonomy-label.log"
        assert log_path.exists(), "taxonomy-label.log was never created"
        log_content = log_path.read_text()
        assert "taxonomy_label_persist_failed" in log_content, (
            "the 409 failure was never recorded in the run log"
        )
        assert str(failing_id) in log_content

        # ── 4. defect #2 regression: a second label run selects ONLY the
        # topic whose label never actually changed ─────────────────────────
        with (
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", fake_label_dispatch),
        ):
            with T2Database(db_path) as db:
                second_run = taxonomy_cmd.relabel_topics(
                    db.taxonomy, collection=collection,
                )
        assert second_run == 1, (
            "a converged label run must re-select only the previously-"
            "failed topic, not every still-pending topic"
        )
        with T2Database(db_path) as db:
            failing_after_retry = db.taxonomy.get_topic_by_id(failing_id)
        assert failing_after_retry["label"] != failing_original_label, (
            "the previously-failed topic should now be relabeled on retry"
        )

        # ── 5. review --auto: accept every now-pending topic ────────────────
        with T2Database(db_path) as db:
            pending_ids = [
                t["id"] for t in db.taxonomy.get_unreviewed_topics(
                    collection=collection, limit=100,
                )
            ]
        assert set(pending_ids) == set(topics_by_id_after_label), (
            "every journey topic should still be pending review at this point"
        )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", _fake_review_accept_all()),
        ):
            result = runner.invoke(
                taxonomy, ["review", "--auto", "--collection", collection],
            )
        assert result.exit_code == 0, result.output

        with T2Database(db_path) as db:
            topics_after_review = db.taxonomy.get_topics_for_collection(collection)
        assert all(t["review_status"] == "accepted" for t in topics_after_review)
        review_total = sum(t["doc_count"] for t in topics_after_review)
        assert review_total == label_total, "review must never move doc counts"

        # ── 6. split: break the largest post-review topic into 2 children,
        # against real T3 (the conservation guard from b686b4d25) ──────────
        split_target = max(topics_after_review, key=lambda t: t["doc_count"])
        pre_split_count = split_target["doc_count"]
        assert pre_split_count >= 2, "split target too small to exercise k=2"

        with T2Database(db_path) as db:
            split_doc_ids = db.taxonomy.get_all_topic_doc_ids(split_target["id"])

        chroma = make_vector_test_client()
        coll = chroma.get_or_create_collection(collection, embedding_function=None)
        # Populate T3 for every doc_id used anywhere in this journey so the
        # split target's fetch (whichever docs it actually holds) always
        # resolves — same simplification as tests/test_taxonomy.py's
        # TestSplitCLI (add the full corpus, not a pre-computed subset).
        all_ids = doc_ids + [new_doc_id]
        all_texts = texts + [new_text]
        all_embeddings = np.vstack([embeddings, new_emb.reshape(1, -1)])
        coll.add(ids=all_ids, documents=all_texts, embeddings=all_embeddings.tolist())

        with T2Database(db_path) as db:
            n_children = db.taxonomy.split_topic(split_target["id"], 2, chroma)
        assert n_children == 2, (
            f"split refused or produced an unexpected child count "
            f"(fetch coverage: {len(split_doc_ids)} doc_ids requested)"
        )

        with T2Database(db_path) as db:
            children = db.taxonomy.get_topics(parent_id=split_target["id"])
        split_children_total = sum(c["doc_count"] for c in children)
        assert split_children_total == pre_split_count, (
            f"split must conserve every assignment: parent had "
            f"{pre_split_count}, children sum to {split_children_total}"
        )

        # ── overall conservation: total docs tracked across the WHOLE
        # journey never silently grows or shrinks beyond the one doc we
        # explicitly added in the assign step ───────────────────────────────
        with T2Database(db_path) as db:
            final_topics = db.taxonomy.get_topics_for_collection(collection)
        final_total = sum(t["doc_count"] for t in final_topics)
        assert final_total == assign_total, (
            f"end-to-end conservation broken: {assign_total} after assign "
            f"vs {final_total} at journey end"
        )
