# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wbfpw.12: log 'superseded_sweep_kept' at the two silent guard
returns in ``catalog/store_hook._reap_superseded_note_chunks``.

Same defect shape as ``mcp_infra._sweep_superseded_vectors`` (see
tests/test_superseded_vector_sweep.py's own nexus-wbfpw.12 section):
``_reap_superseded_note_chunks`` reuses the identical two guards (union,
then note) and had the identical silence — a candidate set that survives
either guard entirely (nothing left to delete) returned with no log line
at all. A sweep that kept everything looked, in the logs, identical to a
sweep with nothing to do.

Pure unit tests, MagicMock reader — no engine substrate needed. Mirrors
tests/test_superseded_vector_sweep.py's ``_cat`` helper shape rather than
the real-engine suite in tests/test_bb6n2_supersede_reap.py,
since only the logging behavior at each guard is under test here, not the
guards' own correctness (already covered there and in
tests/test_indexer_utils_live_note_chashes.py).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from structlog.testing import capture_logs

from nexus.catalog.store_hook import _reap_superseded_note_chunks


def _allow_info_logs() -> None:
    # The event is logged at INFO; the suite default structlog filter is
    # WARNING (tests/conftest.py::pytest_configure), which would drop it
    # before capture_logs() ever sees it. The suite's own
    # _restore_structlog_after_test autouse fixture restores the saved
    # config after this test regardless.
    import logging

    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))


def _reader(refs: dict[str, list[str]], docs: list | None = None) -> MagicMock:
    r = MagicMock()
    r.docs_for_chashes.return_value = refs
    r.list_by_collection.return_value = docs or []
    return r


def test_union_guard_clearing_every_candidate_logs_kept() -> None:
    """Every dropped chash is shared with another live document — the
    all-shared-chash early return the bead names."""
    _allow_info_logs()
    reader = _reader({"a": ["other-doc-1"], "b": ["other-doc-2"]})
    col = MagicMock()
    with capture_logs() as logs, \
            patch("nexus.db.make_t3", return_value=MagicMock(
                get_collection=MagicMock(return_value=col))):
        _reap_superseded_note_chunks(reader, "doc-A", {"a", "b"}, collection="coll")
    col.delete.assert_not_called()
    kept_events = [l for l in logs if l.get("event") == "superseded_sweep_kept"]
    assert len(kept_events) == 1, f"expected exactly one kept event, got: {logs}"
    ev = kept_events[0]
    assert ev["site"] == "_reap_superseded_note_chunks"
    assert ev["collection"] == "coll"
    assert ev["doc_id"] == "doc-A"
    assert ev["dropped"] == 2
    assert ev["kept"] == 2
    assert ev["kept_notes"] == 0


def test_note_guard_clearing_every_candidate_logs_kept() -> None:
    """The union guard finds no other document reference at all, but the
    sole survivor is itself a manifest-less note's identity — the
    note-guard filter return the bead names."""
    _allow_info_logs()
    note = SimpleNamespace(file_path="", meta={"doc_id": "note-chash"})
    reader = _reader({}, docs=[note])
    col = MagicMock()
    with capture_logs() as logs, \
            patch("nexus.db.make_t3", return_value=MagicMock(
                get_collection=MagicMock(return_value=col))):
        _reap_superseded_note_chunks(reader, "doc-A", {"note-chash"}, collection="coll")
    col.delete.assert_not_called()
    kept_events = [l for l in logs if l.get("event") == "superseded_sweep_kept"]
    assert len(kept_events) == 1, f"expected exactly one kept event, got: {logs}"
    ev = kept_events[0]
    assert ev["site"] == "_reap_superseded_note_chunks"
    assert ev["collection"] == "coll"
    assert ev["doc_id"] == "doc-A"
    assert ev["dropped"] == 1
    assert ev["kept"] == 1
    assert ev["kept_notes"] == 1


def test_genuine_orphan_still_deletes_and_logs_no_kept_event() -> None:
    """Non-vacuity control: when a genuine orphan survives both guards,
    the sweep proceeds to delete() as before, and the new kept-event must
    NOT fire (nothing was silently kept — it was actually deleted)."""
    _allow_info_logs()
    reader = _reader({})
    col = MagicMock()
    with capture_logs() as logs, \
            patch("nexus.db.make_t3", return_value=MagicMock(
                get_collection=MagicMock(return_value=col))):
        _reap_superseded_note_chunks(reader, "doc-A", {"genuine-orphan"}, collection="coll")
    col.delete.assert_called_once()
    assert col.delete.call_args.kwargs["ids"] == ["genuine-orphan"]
    assert not any(l.get("event") == "superseded_sweep_kept" for l in logs)
