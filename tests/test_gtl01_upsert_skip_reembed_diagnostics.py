# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-gtl01: diagnostics for ``_upsert_skip_reembed``'s silent-loss
candidates (doc_indexer.py:935-1081), landed alongside nexus-nb3yg
(client refusal-message diagnostics) and nexus-c8hl7 (engine-side
completion-refusal logging) as the 2026-08-08 self-diagnosis arc — see
T2 ``debug-scenario-journeys-parallel-red-2026-08-08`` for the
investigation this responds to.

The debugger's finding: two named, unexcluded silent-loss candidates in
this function — (a) a false-positive ``existing_ids`` probe paired with
an ``update_chunks`` response whose ``missing`` list is empty (the
metadata-only branch then writes NOTHING, silently); (b) the reroute path
at :1043-1073 (already covered by ``update_chunks_missing_rerouted`` in
``tests/test_5xn3k_update_chunks_missing.py``). This file pins the NEW
structured-logging decision points added to close the observability gap:
per-batch probe verdict, branch taken, and the update_chunks
missing-list disposition — so the NEXT occurrence of the anomalous
scenario-journey completion refusal has a client-side trail to read,
where before there was none.

Uses the same ``MagicMock``-``db`` pattern as
``tests/test_index_existing_shortcircuit.py`` (this function's db
parameter is duck-typed, not tied to a concrete client) plus
``structlog.testing.capture_logs()`` (the precedent for asserting
structlog events directly, see
``tests/test_5xn3k_update_chunks_missing.py``'s bottom section).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import structlog
from structlog.testing import capture_logs

from nexus.db.http_vector_client import HttpVectorClient
from nexus.doc_indexer import _upsert_skip_reembed

_COLL = "code__nexus-1-1__voyage-code-3__v1"
_IDS = ["aa" * 16, "bb" * 16, "cc" * 16]
_DOCS = ["doc-a", "doc-b", "doc-c"]
_EMB = [[], [], []]
_METAS = [{"source_path": "a.py"}, {"source_path": "b.py"}, {"source_path": "c.py"}]


def _capture_debug_logs():
    """``capture_logs()`` only swaps structlog's *processors*, not
    ``wrapper_class`` — this repo's default structlog config filters below
    WARNING (``nexus.logging_setup``), so the DEBUG-level events this
    bead's new instrumentation uses would never reach the ``LogCapture``
    without also resetting ``wrapper_class`` to structlog's own
    non-filtering default first. Same precedent as
    ``tests/test_5xn3k_update_chunks_missing.py``'s
    ``test_update_chunks_missing_unreported_throttled_to_once_per_process``.
    """
    structlog.reset_defaults()
    return capture_logs()


def _service_db(monkeypatch, existing: set[str]) -> MagicMock:
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    db = MagicMock(spec=HttpVectorClient)
    db.existing_ids.return_value = existing
    return db


def _events(logs: list[dict], event: str) -> list[dict]:
    return [e for e in logs if e["event"] == event]


class TestProbeVerdictLogged:
    """Per-batch existing-probe verdict: counts only (bounded), never the
    chash lists themselves — logged unconditionally, not just on probe
    failure (which was already logged before this bead)."""

    def test_mixed_batch_logs_total_present_new(self, monkeypatch):
        db = _service_db(monkeypatch, existing={_IDS[0], _IDS[2]})
        with _capture_debug_logs() as logs:
            _upsert_skip_reembed(db, _COLL, _IDS, _DOCS, _EMB, _METAS)

        verdicts = _events(logs, "upsert_skip_reembed_probe")
        assert len(verdicts) == 1, f"expected exactly one, got: {logs}"
        assert verdicts[0]["collection"] == _COLL
        assert verdicts[0]["total"] == 3
        assert verdicts[0]["present"] == 2
        assert verdicts[0]["new"] == 1

    def test_all_new_logs_zero_present(self, monkeypatch):
        db = _service_db(monkeypatch, existing=set())
        with _capture_debug_logs() as logs:
            _upsert_skip_reembed(db, _COLL, _IDS, _DOCS, _EMB, _METAS)

        verdicts = _events(logs, "upsert_skip_reembed_probe")
        assert len(verdicts) == 1
        assert verdicts[0]["present"] == 0
        assert verdicts[0]["new"] == 3

    def test_force_bypasses_the_probe_entirely_no_verdict_logged(self, monkeypatch):
        # force=True skips existing_ids altogether (RDR-181 §Approach step
        # 3, pre-existing contract) — there is no probe verdict to log.
        db = _service_db(monkeypatch, existing={_IDS[0]})
        with _capture_debug_logs() as logs:
            _upsert_skip_reembed(db, _COLL, _IDS, _DOCS, _EMB, _METAS, force=True)

        assert _events(logs, "upsert_skip_reembed_probe") == []


class TestBranchTakenLogged:
    """Which of the three branches (full upsert / metadata-only+split /
    force) actually ran — a decision point with previously no trace for
    two of the three (only the final total/embedded/skipped summary
    existed, and only on the branch that reaches it)."""

    def test_full_upsert_no_existing_branch(self, monkeypatch):
        db = _service_db(monkeypatch, existing=set())
        with _capture_debug_logs() as logs:
            _upsert_skip_reembed(db, _COLL, _IDS, _DOCS, _EMB, _METAS)

        branches = _events(logs, "upsert_skip_reembed_branch")
        assert len(branches) == 1
        assert branches[0]["branch"] == "full_upsert_no_existing"
        assert branches[0]["count"] == 3

    def test_force_full_upsert_branch(self, monkeypatch):
        db = _service_db(monkeypatch, existing=set())
        with _capture_debug_logs() as logs:
            _upsert_skip_reembed(db, _COLL, _IDS, _DOCS, _EMB, _METAS, force=True)

        branches = _events(logs, "upsert_skip_reembed_branch")
        assert len(branches) == 1
        assert branches[0]["branch"] == "force_full_upsert"
        assert branches[0]["count"] == 3

    def test_split_branch_reports_content_write_and_metadata_only_counts(self, monkeypatch):
        db = _service_db(monkeypatch, existing={_IDS[0], _IDS[2]})
        db.update_chunks.return_value = []  # no reroute
        with _capture_debug_logs() as logs:
            _upsert_skip_reembed(db, _COLL, _IDS, _DOCS, _EMB, _METAS)

        branches = _events(logs, "upsert_skip_reembed_branch")
        assert len(branches) == 1
        assert branches[0]["branch"] == "split"
        assert branches[0]["content_write"] == 1
        assert branches[0]["metadata_only_candidate"] == 2


class TestUpdateChunksDispositionLogged:
    """The update_chunks "missing"-list disposition, logged at the decision
    point regardless of downstream outcome — previously the routine
    "missing == []" case (the common, correct happy path) had NO trace
    anywhere in doc_indexer."""

    def test_missing_empty_list_disposition(self, monkeypatch):
        db = _service_db(monkeypatch, existing=set(_IDS))
        db.update_chunks.return_value = []
        with _capture_debug_logs() as logs:
            sent = _upsert_skip_reembed(db, _COLL, _IDS, _DOCS, _EMB, _METAS)

        assert sent == 0
        dispositions = _events(logs, "upsert_skip_reembed_update_chunks_disposition")
        assert len(dispositions) == 1
        assert dispositions[0]["candidate_count"] == 3
        assert dispositions[0]["missing_reported"] is True
        assert dispositions[0]["missing_count"] == 0

    def test_missing_nonempty_disposition_precedes_reroute(self, monkeypatch):
        db = _service_db(monkeypatch, existing=set(_IDS))
        db.update_chunks.return_value = [_IDS[0]]
        with _capture_debug_logs() as logs:
            _upsert_skip_reembed(db, _COLL, _IDS, _DOCS, _EMB, _METAS)

        dispositions = _events(logs, "upsert_skip_reembed_update_chunks_disposition")
        assert len(dispositions) == 1
        assert dispositions[0]["missing_reported"] is True
        assert dispositions[0]["missing_count"] == 1
        # The disposition log and the existing reroute WARNING coexist —
        # this bead adds the former without disturbing the latter.
        rerouted = _events(logs, "update_chunks_missing_rerouted")
        assert len(rerouted) == 1
        assert rerouted[0]["count"] == 1

    def test_missing_none_disposition_precedes_the_raise(self, monkeypatch):
        from nexus.errors import ChunkLandingUnverifiedError

        db = _service_db(monkeypatch, existing=set(_IDS))
        db.update_chunks.return_value = None  # engine omitted "missing" entirely
        with _capture_debug_logs() as logs:
            try:
                _upsert_skip_reembed(db, _COLL, _IDS, _DOCS, _EMB, _METAS)
                raised = False
            except ChunkLandingUnverifiedError:
                raised = True

        assert raised, "a None disposition must still raise ChunkLandingUnverifiedError"
        dispositions = _events(logs, "upsert_skip_reembed_update_chunks_disposition")
        assert len(dispositions) == 1, (
            "the disposition must be logged BEFORE the raise, not skipped by it"
        )
        assert dispositions[0]["missing_reported"] is False
        assert dispositions[0]["missing_count"] is None
