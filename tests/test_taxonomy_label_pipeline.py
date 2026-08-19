# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the ``nx taxonomy label`` pipeline defects (silent-failure
sweep, 2026-08-19).

Three defects pinned here, mirroring ``tests/test_taxonomy_review_auto.py``'s
conventions (``_seed_topic`` / ``_t2_router`` / ``t2_service_env`` engine
substrate):

1. A single T2 persist failure (e.g. a concurrent-update 409 from
   ``update_topic_label``) used to abort the WHOLE run instead of being
   caught per-topic like ``_review_auto``'s apply loop.
2. ``update_topic_label`` deliberately leaves ``review_status='pending'``
   (label and review are separate axes — GH #241 Item 3), so
   ``get_unreviewed_topics`` re-selected already-relabeled topics forever.
   Fixed at the SELECTION layer via ``_topic_has_auto_label``.
3. The per-failure run log (``taxonomy-label.log``) received nothing on the
   fatal path — the killing exception was never logged before it propagated.

Seeded topics use ``terms=["alpha", "beta"]`` with ``label="alpha beta"``
(the ``" ".join(top_terms[:3])`` auto-label ``compute_discovered_topics``
would assign) to land on the "needs labeling" side of
``_topic_has_auto_label``; an "already labeled" topic uses a label that
does not match its terms.
"""
from __future__ import annotations

import itertools
import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import nexus.mcp_infra as _mi
from nexus.commands import taxonomy_cmd
from nexus.commands.taxonomy_cmd import taxonomy
from nexus.db.t2 import T2Database
from nexus.logging_setup import configure_logging

from tests._t2_fixture_ops import canonical_chunk_id
from tests.conftest import next_import_seed_id

_current_tenant: str | None = None


@pytest.fixture(autouse=True)
def _engine_substrate(t2_service_env):
    """This module runs its T2 against the engine-backed substrate (RDR-155
    P4b P0a' representative batch, per-test minted tenant)."""
    global _current_tenant
    _current_tenant = t2_service_env
    yield
    _current_tenant = None


def _seed_chunks_for_tenant(
    tenant: str, collection: str, chash_hexes: list[str], *, dim: int = 384,
) -> None:
    """Seed real nexus.chunks rows so a topic_assignments insert for
    (tenant, collection, chash) satisfies topic_assignments_chunk_fk.
    Mirrors test_taxonomy_review_auto.py's helper of the same name (no
    import path to it from here)."""
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
    """Insert one topic (+ topic_assignments) and return its id."""
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
            terms=json.dumps(terms if terms is not None else ["alpha", "beta"]),
        )
        n = n_docs if n_docs is not None else doc_count
        _seed_chunks_for_tenant(
            _current_tenant, collection,
            [canonical_chunk_id(f"{label}-doc-{i}.py") for i in range(n)],
        )
        for i in range(n):
            db.taxonomy.import_assignment(
                doc_id=canonical_chunk_id(f"{label}-doc-{i}.py"),
                topic_id=topic_id,
                assigned_by="test-seed",
                similarity=None,
                assigned_at=None,
                source_collection=collection,
            )
    return topic_id


def _t2_router(db_path: Path):
    """Return a t2_index_write stub that routes through db_path."""
    def _router(fn):
        with T2Database(db_path) as db:
            return fn(db)
    return _router


def _t2_router_label_fails_for(db_path: Path, *failing_ids: int, message: str = "409 Conflict"):
    """Like ``_t2_router`` but ``update_topic_label(failing_id, ...)`` raises
    for any topic id in *failing_ids* — simulates a concurrent-update 409
    from the engine's ``/topics/update_label`` route."""
    def _router(fn):
        with T2Database(db_path) as db:
            orig_update = db.taxonomy.update_topic_label

            def _update(topic_id, new_label, **kw):
                if topic_id in failing_ids:
                    raise RuntimeError(message)
                return orig_update(topic_id, new_label, **kw)

            db.taxonomy.update_topic_label = _update
            return fn(db)
    return _router


async def _fake_label_dispatch(prompt: str, schema: dict, **kw):  # noqa: ARG001
    """Positional labeler fake: the label prompt has no topic id, only a
    1-based numbered list — return one real (non-auto) label per line."""
    n = len(re.findall(r"^\d+\. terms=", prompt, flags=re.MULTILINE))
    return {"labels": [{"idx": i, "label": f"Real Label {i}"} for i in range(1, n + 1)]}


def _make_globally_unique_label_dispatch():
    """Like ``_fake_label_dispatch`` but labels are unique ACROSS calls, not
    just within one batch's numbered list.

    ``relabel_topics`` batches topics and persists each batch's real (LLM)
    labels to T2, which enforces a (tenant, collection, label) uniqueness
    constraint on root topics. ``_fake_label_dispatch``'s per-batch
    1-based numbering ("Real Label 1", "Real Label 2", ...) repeats across
    batches, so a multi-batch run assigns the SAME label to two different
    topics and trips a genuine 409 from the engine — a real conflict the
    test itself would be manufacturing, not the one it's trying to pin.
    Use this fake in any test exercising more than one batch."""
    counter = itertools.count(1)

    async def _fake(prompt: str, schema: dict, **kw):  # noqa: ARG001
        n = len(re.findall(r"^\d+\. terms=", prompt, flags=re.MULTILINE))
        return {"labels": [{"idx": i, "label": f"Real Label {next(counter)}"} for i in range(1, n + 1)]}

    return _fake


def _seed_auto_label_topic(db_path: Path, idx: int, **kw) -> int:
    """Seed a topic whose label still matches its own c-TF-IDF auto-label
    pattern (``_topic_has_auto_label`` -> True), with terms unique to *idx*
    so multiple calls in one collection never collide on the
    (tenant, collection, label) uniqueness constraint root topics enforce."""
    terms = [f"term{idx}a", f"term{idx}b"]
    return _seed_topic(db_path, " ".join(terms), terms=terms, **kw)


# ── Defect 1: a single persist failure must not abort the whole run ────────


class TestPersistFailureResilience:

    def test_single_409_does_not_abort_remaining_batches(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        t1 = _seed_auto_label_topic(db_path, 1, doc_count=4)  # will fail
        t2 = _seed_auto_label_topic(db_path, 2, doc_count=3)
        t3 = _seed_auto_label_topic(db_path, 3, doc_count=2)
        t4 = _seed_auto_label_topic(db_path, 4, doc_count=1)

        with (
            patch.object(_mi, "t2_index_write", _t2_router_label_fails_for(db_path, t1)),
            patch(
                "nexus.operators.dispatch.claude_dispatch",
                _make_globally_unique_label_dispatch(),
            ),
        ):
            with T2Database(db_path) as db:
                count = taxonomy_cmd.relabel_topics(
                    db.taxonomy, collection="proj", batch_size=2, workers=1,
                )

        # t1's persist failed; t2/t3/t4 succeeded despite it.
        assert count == 3

        with T2Database(db_path) as db:
            assert db.taxonomy.get_topic_by_id(t1)["label"] == "term1a term1b"
            assert db.taxonomy.get_topic_by_id(t2)["label"] != "term2a term2b"
            assert db.taxonomy.get_topic_by_id(t3)["label"] != "term3a term3b"
            assert db.taxonomy.get_topic_by_id(t4)["label"] != "term4a term4b"

    def test_failing_topic_stays_pending_for_next_run(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        t1 = _seed_topic(db_path, "alpha beta", doc_count=1)

        with (
            patch.object(_mi, "t2_index_write", _t2_router_label_fails_for(db_path, t1)),
            patch("nexus.operators.dispatch.claude_dispatch", _fake_label_dispatch),
        ):
            with T2Database(db_path) as db:
                count = taxonomy_cmd.relabel_topics(db.taxonomy, collection="proj")

        assert count == 0
        with T2Database(db_path) as db:
            topic = db.taxonomy.get_topic_by_id(t1)
        assert topic["review_status"] == "pending"
        assert topic["label"] == "alpha beta"

    def test_cli_label_command_exits_zero_and_reports_persist_warning(
        self, tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "memory.db"
        t1 = _seed_auto_label_topic(db_path, 1, doc_count=2)
        t2 = _seed_auto_label_topic(db_path, 2, doc_count=1)

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch("nexus.commands.taxonomy_cmd._claude_available", return_value=True),
            patch.object(_mi, "t2_index_write", _t2_router_label_fails_for(db_path, t1)),
            patch("nexus.operators.dispatch.claude_dispatch", _fake_label_dispatch),
        ):
            result = runner.invoke(taxonomy, ["label", "--collection", "proj"])

        assert result.exit_code == 0, result.output
        assert "1 label persists failed" in result.output
        with T2Database(db_path) as db:
            assert db.taxonomy.get_topic_by_id(t1)["label"] == "term1a term1b"
            assert db.taxonomy.get_topic_by_id(t2)["label"] != "term2a term2b"


# ── Defect 3: the per-failure run log must actually receive the failure ────


class TestRunLogReceivesFailures:

    def test_persist_failure_is_recorded_in_run_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path = tmp_path / "memory.db"
        config_dir = tmp_path / "cfg"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))

        # open_run_log's documented precondition (nexus-mjc9l /
        # test_logging_setup.py's "Real CLI precondition" convention):
        # it lowers the ALREADY-bridged structlog wrapper/root level to
        # INFO for its duration, it does not itself install the
        # stdlib-logging bridge. In production every `nx` invocation goes
        # through cli()'s group callback, which calls this before any
        # subcommand body runs; calling relabel_topics() directly here
        # (bypassing the Click group) must set up the same precondition
        # or the file handler never receives the event — the autouse
        # ``_restore_structlog_after_test`` fixture reverts this after
        # the test.
        configure_logging("cli")

        t1 = _seed_topic(db_path, "alpha beta", doc_count=1)

        with (
            patch.object(_mi, "t2_index_write", _t2_router_label_fails_for(db_path, t1)),
            patch("nexus.operators.dispatch.claude_dispatch", _fake_label_dispatch),
        ):
            with T2Database(db_path) as db:
                taxonomy_cmd.relabel_topics(db.taxonomy, collection="proj")

        log_path = config_dir / "logs" / "taxonomy-label.log"
        assert log_path.exists(), "run log file was never created"
        content = log_path.read_text()
        assert content.strip() != "", "run log is empty — the failure was never logged"
        assert "taxonomy_label_persist_failed" in content
        assert str(t1) in content


# ── Defect 2: already-relabeled pending topics must not be re-selected ─────


class TestSelectionExcludesAlreadyLabeled:

    def test_already_labeled_pending_topic_not_reselected(self, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        already = _seed_topic(db_path, "Deep Learning Fundamentals", doc_count=2)
        needs = _seed_topic(db_path, "alpha beta", doc_count=1)

        calls: list[str] = []

        async def _fake(prompt: str, schema: dict, **kw):  # noqa: ARG001
            calls.append(prompt)
            return await _fake_label_dispatch(prompt, schema)

        with (
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", _fake),
        ):
            with T2Database(db_path) as db:
                count = taxonomy_cmd.relabel_topics(
                    db.taxonomy, collection="proj", only_pending=True,
                )

        assert count == 1
        assert len(calls) == 1  # one batch, containing only `needs`

        with T2Database(db_path) as db:
            assert db.taxonomy.get_topic_by_id(already)["label"] == "Deep Learning Fundamentals"
            assert db.taxonomy.get_topic_by_id(needs)["label"] == "Real Label 1"

    def test_second_run_after_relabel_selects_nothing_new(self, tmp_path: Path) -> None:
        """Combined regression for #1+#2: a relabel run converges instead
        of reprocessing the same topic forever."""
        db_path = tmp_path / "memory.db"
        t = _seed_topic(db_path, "alpha beta", doc_count=1)

        with (
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", _fake_label_dispatch),
        ):
            with T2Database(db_path) as db:
                first = taxonomy_cmd.relabel_topics(db.taxonomy, collection="proj")
            with T2Database(db_path) as db:
                second = taxonomy_cmd.relabel_topics(db.taxonomy, collection="proj")

        assert first == 1
        assert second == 0
        with T2Database(db_path) as db:
            topic = db.taxonomy.get_topic_by_id(t)
        assert topic["label"] == "Real Label 1"
        # label and review stay separate axes: relabeling never advances
        # review_status on its own.
        assert topic["review_status"] == "pending"

    def test_relabel_all_flag_still_processes_already_labeled_topics(
        self, tmp_path: Path,
    ) -> None:
        """--all bypasses the pending-selection filter entirely (uses
        get_all_topics, not get_unreviewed_topics) — the auto-label
        exclusion must not leak into that path."""
        db_path = tmp_path / "memory.db"
        already = _seed_topic(db_path, "Deep Learning Fundamentals", doc_count=1)

        with (
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", _fake_label_dispatch),
        ):
            with T2Database(db_path) as db:
                count = taxonomy_cmd.relabel_topics(
                    db.taxonomy, collection="proj", only_pending=False,
                )

        assert count == 1
        with T2Database(db_path) as db:
            assert db.taxonomy.get_topic_by_id(already)["label"] == "Real Label 1"

    def test_cli_label_precheck_count_excludes_already_labeled(
        self, tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "memory.db"
        _seed_topic(db_path, "Deep Learning Fundamentals", doc_count=2)
        _seed_topic(db_path, "alpha beta", doc_count=1)

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch("nexus.commands.taxonomy_cmd._claude_available", return_value=True),
            patch.object(_mi, "t2_index_write", _t2_router(db_path)),
            patch("nexus.operators.dispatch.claude_dispatch", _fake_label_dispatch),
        ):
            result = runner.invoke(taxonomy, ["label", "--collection", "proj"])

        assert result.exit_code == 0, result.output
        assert "Labeling 1 topics" in result.output
        assert "Relabeled 1/1 topics." in result.output

# _topic_has_auto_label's own pure-function tests live in
# tests/test_taxonomy_label_selection.py (no engine substrate needed there).
