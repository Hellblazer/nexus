# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Step 6 (nexus-u5m5g.7), fix round r1: which rows the hook stages, and what it says of the outcome.

Driven with a recording writer (no engine); the same rows against the real engine are in
``tests/test_catalog_hook_reconcile_contract.py`` and ``tests/integration/test_rdr193_reconcile_e2e.py``.

* a complete walk stages a ``keep`` row for every file it saw but could not give a ``present`` row (the ephemeral
  guard, a row that could not be built), so the engine does not count its existing row missing and sweep it;
* every staged present row must come back in the outcome: a missing one is named (``no_outcome_row``) and warned of;
* the outcome's renamed, swept and explicitly deleted documents are logged one event each, as housekeeping and the
  since-head delete used to.
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import structlog
import structlog.testing

from nexus.catalog import reconcile_run


@pytest.fixture
def info_logs():
    """The suite filters structlog at WARNING, which drops info events before ``capture_logs`` sees them; a test of an
    info event lowers the filter for its body and puts it back."""
    old = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    try:
        yield
    finally:
        structlog.configure(wrapper_class=old)


class _Reader:
    """*held* answers ``resolve_many``: tumbler -> the physical_collection of that row (a tumbler left out is a row the
    read does not return)."""

    def __init__(self, held: dict[str, str] | None = None, *, fail: bool = False) -> None:
        self.held, self.fail, self.resolved = held or {}, fail, []

    def owner_for_repo(self, repo_hash):  # noqa: ANN001, ARG002
        return "1.7"

    def resolve_many(self, doc_ids):  # noqa: ANN001
        self.resolved.append(list(doc_ids))
        if self.fail:
            raise RuntimeError("engine unreachable")
        return {t: SimpleNamespace(physical_collection=self.held[t]) for t in doc_ids if t in self.held}

    def close(self) -> None:
        pass


class _Writer:
    def __init__(self, outcome: dict | None = None) -> None:
        self.rows: list[dict] = []
        self.outcome = outcome
        self.flagged: list[str] = []

    def set_sensitivity(self, tumblers, flag, *, reason=""):  # noqa: ANN001, ARG002
        self.flagged.extend(tumblers)

    def reconcile_stage(self, owner, run_id, walk, expected_rows, rows):  # noqa: ANN001, ARG002
        self.rows.extend(rows)
        return len(rows)

    def reconcile_commit(self, owner, run_id):  # noqa: ANN001, ARG002
        if self.outcome is not None:
            return self.outcome
        return {"rows": [{"file_path": r["file_path"], "tumbler": f"1.7.{r['seq']}", "action": "created",
                          "sensitive": False, "content_hash_changed": False, "index_state": None,
                          "index_content_hash": ""} for r in self.rows if r["kind"] == "present"],
                "trashed": [], "renamed": [], "swept": [], "duplicate_path": [],
                "deleted_skipped_foreign_owner": [], "sensitivity_pending": []}

    def close(self) -> None:
        pass


def _hook(root: Path, files, writer: _Writer, *, reader: _Reader | None = None, **kw):
    from nexus.indexer import _catalog_hook

    reader = reader or _Reader()
    with patch("nexus.catalog.factory.make_catalog_reader", lambda **k: reader), patch(
        "nexus.catalog.factory.make_catalog_writer", lambda **k: writer,
    ):
        return _catalog_hook(repo=root, repo_name="r", repo_hash="abcd1234", head_hash="h", indexed_files=files, **kw)


# ── keep rows for files the walk saw but staged no present row for ──────────────────────────────────────────────

def test_an_ephemeral_path_is_staged_as_a_keep_row_so_its_existing_row_is_not_swept(tmp_path: Path) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text("x = 1\n")
    eph = tmp_path / ".claude" / "worktrees" / "agent" / "x.py"
    eph.parent.mkdir(parents=True)
    eph.write_text("y = 2\n")
    writer = _Writer()
    unregistered: dict[Path, str] = {}
    _hook(tmp_path, [(ok, "code", "code__r"), (eph, "code", "code__r")], writer, unregistered=unregistered)
    assert unregistered == {eph: "ephemeral:worktree_or_tempdir"}
    by_path = {r["file_path"]: r["kind"] for r in writer.rows}
    assert by_path == {"ok.py": "present", ".claude/worktrees/agent/x.py": "keep"}, writer.rows


def test_a_file_whose_row_could_not_be_built_is_staged_as_a_keep_row(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text("x = 1\n")
    bad = tmp_path / "bad.py"
    bad.write_text("y = 2\n")
    writer = _Writer()
    unregistered: dict[Path, str] = {}
    real = __import__("nexus.indexer", fromlist=["_register_time_meta"])._register_time_meta

    def meta(rel, file_hash):  # noqa: ANN001
        if rel == "bad.py":
            raise ValueError("cannot build")
        return real(rel, file_hash)

    with patch("nexus.indexer._register_time_meta", meta):
        _hook(tmp_path, [(good, "code", "code__r"), (bad, "code", "code__r")], writer, unregistered=unregistered)
    assert unregistered == {bad: "register_failed"}
    assert {r["file_path"]: r["kind"] for r in writer.rows} == {"good.py": "present", "bad.py": "keep"}


def test_a_held_file_and_a_skipped_one_are_staged_once(tmp_path: Path) -> None:
    """A path in ``keep_paths`` that the walk also skipped is one keep row, not two (the engine takes one row a path)."""
    eph = tmp_path / ".claude" / "worktrees" / "agent" / "x.py"
    eph.parent.mkdir(parents=True)
    eph.write_text("y = 2\n")
    writer = _Writer()
    _hook(tmp_path, [(eph, "code", "code__r")], writer, keep_paths=[eph])
    assert [(r["kind"], r["file_path"]) for r in writer.rows] == [("keep", ".claude/worktrees/agent/x.py")]


# ── S1: every staged present row comes back ─────────────────────────────────────────────────────────────────────

def _row(path: str, action: str, tumbler: str | None, **kw) -> dict:
    return {"file_path": path, "tumbler": tumbler, "action": action, "content_hash_changed": False, "sensitive": False,
            "index_state": None, "index_content_hash": "", **kw}


def _outcome(*rows: dict, **extra) -> dict:
    return {"rows": list(rows), "trashed": [], "renamed": [], "swept": [], "duplicate_path": [],
            "deleted_skipped_foreign_owner": [], "sensitivity_pending": [], **extra}


_STAGED = {"content_type": "code", "content_hash": "h", "physical_collection": "c"}


def test_a_staged_row_the_outcome_does_not_answer_is_named_and_warned_of(tmp_path: Path) -> None:
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    with structlog.testing.capture_logs() as logs:
        applied = reconcile_run.apply_outcome(
            _outcome(_row("a.py", "created", "1.7.1")),
            rel_to_abs={"a.py": a, "b.py": b}, staged={"a.py": _STAGED, "b.py": _STAGED})
    assert applied.file_to_doc_id == {a: "1.7.1"}
    assert applied.missing == [b]
    ev = [e for e in logs if e.get("event") == "catalog_reconcile_row_missing_from_outcome"]
    assert len(ev) == 1 and ev[0]["log_level"] == "warning" and ev[0]["file_path"] == "b.py", logs


def test_an_answered_run_has_no_missing_row(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    applied = reconcile_run.apply_outcome(
        _outcome(_row("a.py", "unchanged", "1.7.1")), rel_to_abs={"a.py": a}, staged={"a.py": _STAGED})
    assert applied.missing == []


def test_the_hook_names_a_row_the_commit_did_not_answer_as_no_outcome_row(tmp_path: Path) -> None:
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_text("a\n")
    b.write_text("b\n")
    writer = _Writer(_outcome(_row("a.py", "created", "1.7.1")))
    unregistered: dict[Path, str] = {}
    ids = _hook(tmp_path, [(a, "code", "code__r"), (b, "code", "code__r")], writer, unregistered=unregistered)
    assert ids == {a: "1.7.1"}
    assert unregistered == {b: "no_outcome_row"}


# ── GH #1611: a row another owner holds in ANOTHER collection takes none of this run's chunks ───────────────────

def _foreign(path: str, tumbler: str, **kw) -> dict:
    return _row(path, "foreign_owner", tumbler, owner="1.7", other_owner="1.23", **kw)


def test_a_foreign_owner_row_in_another_collection_gets_no_document_id_and_is_named(tmp_path: Path) -> None:
    """The engine resolved the file onto a live row of another owner (a curator's ``nx index pdf --collection``
    document) whose physical_collection is that owner's collection. Chunks this run wrote under its tumbler landed in
    the repo's collection, where the row does not say they are, and the curator collection's live count lost them.
    The file is the other owner's document: it gets no document id here and is listed in ``foreign``."""
    a, b = tmp_path / "a.py", tmp_path / "paper.pdf"
    applied = reconcile_run.apply_outcome(
        _outcome(_row("a.py", "created", "1.7.1"), _foreign("paper.pdf", "1.23.96")),
        rel_to_abs={"a.py": a, "paper.pdf": b}, staged={"a.py": _STAGED, "paper.pdf": _STAGED},
        foreign_collections={"1.23.96": "knowledge__curator"})
    assert applied.file_to_doc_id == {a: "1.7.1"}
    assert applied.foreign == {b: "1.23.96"}
    assert applied.reconciled == []
    assert applied.refused == {} and applied.missing == []


@pytest.mark.parametrize("held_in", [_STAGED["physical_collection"], ""], ids=["same-collection", "no-collection"])
def test_a_foreign_owner_row_that_holds_this_runs_collection_or_none_is_adopted_as_before(
        tmp_path: Path, held_in: str) -> None:
    """The nexus-53cae curator rows of repo files: another owner's row whose chunks live in the collection this run
    writes the file to (or that holds none yet). The file keeps that tumbler and is listed in ``reconciled``; its row is
    left as it is. Skipping these would leave the repo's own copy unindexed with the base advanced."""
    b = tmp_path / "notes.md"
    applied = reconcile_run.apply_outcome(
        _outcome(_foreign("notes.md", "1.23.5")), rel_to_abs={"notes.md": b}, staged={"notes.md": _STAGED},
        foreign_collections={"1.23.5": held_in})
    assert applied.file_to_doc_id == {b: "1.23.5"}
    assert applied.reconciled == [(b, "1.23.5")]
    assert applied.foreign == {} and applied.refused == {}


def test_a_foreign_owner_row_the_read_did_not_answer_is_refused_not_adopted_or_skipped(tmp_path: Path) -> None:
    """Without the other row's collection the run cannot tell which shape it is in: it is refused (a failure that
    holds the --since-head base and is retried), never silently adopted (the #1611 split) or silently skipped."""
    b = tmp_path / "paper.pdf"
    applied = reconcile_run.apply_outcome(
        _outcome(_foreign("paper.pdf", "1.23.96")), rel_to_abs={"paper.pdf": b}, staged={"paper.pdf": _STAGED},
        foreign_collections={})
    assert applied.refused == {b: "foreign_owner_row_unreadable"}
    assert applied.file_to_doc_id == {} and applied.foreign == {} and applied.reconciled == []


def test_a_same_owner_row_found_by_uri_keeps_its_document_id(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    applied = reconcile_run.apply_outcome(
        _outcome(_row("a.py", "same_owner_by_uri", "1.7.9")), rel_to_abs={"a.py": a}, staged={"a.py": _STAGED})
    assert applied.file_to_doc_id == {a: "1.7.9"}
    assert applied.reconciled == [(a, "1.7.9")]
    assert applied.foreign == {}


def test_a_skipped_foreign_document_is_not_flagged_by_this_run_but_an_adopted_one_still_is(tmp_path: Path) -> None:
    """``sensitivity_pending`` names the target of every sensitive-glob row the commit left unflagged, a skipped
    foreign row included. This run does not index a skipped file, so it does not flag the other owner's document
    for it; an adopted row (indexed by this run) keeps being raised."""
    skipped, adopted = tmp_path / "paper.pdf", tmp_path / "notes.md"
    staged = {**_STAGED, "sensitive": True}
    applied = reconcile_run.apply_outcome(
        _outcome(_foreign("paper.pdf", "1.23.96", sensitive=False), _foreign("notes.md", "1.23.5", sensitive=False),
                 sensitivity_pending=["1.23.96", "1.23.5"]),
        rel_to_abs={"paper.pdf": skipped, "notes.md": adopted}, staged={"paper.pdf": staged, "notes.md": staged},
        foreign_collections={"1.23.96": "knowledge__curator", "1.23.5": "c"})
    assert applied.foreign == {skipped: "1.23.96"}
    assert applied.sensitivity_pending == {"1.23.5": [adopted]}


def test_the_hook_skips_a_foreign_owner_file_in_another_collection_loudly(tmp_path: Path, capsys) -> None:
    a, b = tmp_path / "a.py", tmp_path / "paper.pdf"
    a.write_text("a\n")
    b.write_bytes(b"%PDF-1.4\n")
    writer = _Writer(_outcome(_row("a.py", "created", "1.7.1"), _foreign("paper.pdf", "1.23.96")))
    reader = _Reader({"1.23.96": "knowledge__curator"})
    unregistered: dict[Path, str] = {}
    with structlog.testing.capture_logs() as logs:
        ids = _hook(tmp_path, [(a, "code", "code__r"), (b, "pdf", "docs__r")], writer, reader=reader,
                    unregistered=unregistered)
    assert ids == {a: "1.7.1"}
    assert unregistered == {b: "foreign_owner:1.23.96"}
    assert reader.resolved == [["1.23.96"]], "one batched read of the foreign rows"
    ev = [e for e in logs if e.get("event") == "catalog_register_skipped_foreign_owner"]
    assert len(ev) == 1 and ev[0]["log_level"] == "warning" and ev[0]["tumbler"] == "1.23.96", logs
    assert ev[0]["held_in"] == "knowledge__curator" and ev[0]["written_to"] == "docs__r", ev
    err = capsys.readouterr().err
    assert "1 held by another owner's document in another collection, not written: paper.pdf" in err, err


def test_the_summary_names_the_first_few_skipped_files_and_counts_the_rest(tmp_path: Path, capsys) -> None:
    names = [f"p{i}.pdf" for i in range(5)]
    files = []
    for n in names:
        (tmp_path / n).write_bytes(b"%PDF-1.4\n")
        files.append((tmp_path / n, "pdf", "docs__r"))
    writer = _Writer(_outcome(*[_foreign(n, f"1.23.{i}") for i, n in enumerate(names)]))
    reader = _Reader({f"1.23.{i}": "knowledge__curator" for i in range(5)})
    _hook(tmp_path, files, writer, reader=reader, unregistered={})
    err = capsys.readouterr().err
    assert "5 held by another owner's document in another collection, not written: p0.pdf, p1.pdf, p2.pdf, +2 more" in err, err


def test_the_hook_adopts_a_foreign_owner_row_that_holds_the_runs_collection(tmp_path: Path, capsys) -> None:
    b = tmp_path / "notes.md"
    b.write_text("# n\n")
    writer = _Writer(_outcome(_foreign("notes.md", "1.23.5")))
    unregistered: dict[Path, str] = {}
    with structlog.testing.capture_logs() as logs:
        ids = _hook(tmp_path, [(b, "prose", "docs__r")], writer, reader=_Reader({"1.23.5": "docs__r"}),
                    unregistered=unregistered)
    assert ids == {b: "1.23.5"} and unregistered == {}
    assert [e for e in logs if e.get("event") == "catalog_register_reconciled_onto_existing_row"
            and e["tumbler"] == "1.23.5"]
    assert not [e for e in logs if e.get("event") == "catalog_register_skipped_foreign_owner"]
    assert "reconciled onto an existing row by source URI" in capsys.readouterr().err


def test_the_hook_refuses_a_foreign_owner_file_whose_row_cannot_be_read(tmp_path: Path) -> None:
    b = tmp_path / "paper.pdf"
    b.write_bytes(b"%PDF-1.4\n")
    writer = _Writer(_outcome(_foreign("paper.pdf", "1.23.96")))
    unregistered: dict[Path, str] = {}
    with structlog.testing.capture_logs() as logs:
        ids = _hook(tmp_path, [(b, "pdf", "docs__r")], writer, reader=_Reader(fail=True), unregistered=unregistered)
    assert ids == {}
    assert unregistered == {b: "register_refused"}, "a failure (holds the base, retried), not a deliberate skip"
    assert [e for e in logs if e.get("event") == "catalog_foreign_owner_rows_unreadable"]
    assert [e for e in logs if e.get("event") == "catalog_hook_register_refused"
            and e["reason"] == "foreign_owner_row_unreadable"], logs


def test_a_skipped_foreign_sensitive_glob_file_is_not_reported_as_an_unset_flag(tmp_path: Path) -> None:
    """The run's ``--sensitive-glob`` check reads ``wanted - confirmed``: a file left to another owner's document is
    neither indexed nor flagged, so it must not read as "the flag could not be set"."""
    b = tmp_path / "secret.pdf"
    b.write_bytes(b"%PDF-1.4\n")
    writer = _Writer(_outcome(_foreign("secret.pdf", "1.23.96"), sensitivity_pending=["1.23.96"]))
    wanted: set[Path] = set()
    confirmed: set[Path] = set()
    _hook(tmp_path, [(b, "pdf", "docs__r")], writer, reader=_Reader({"1.23.96": "knowledge__curator"}),
          unregistered={}, sensitive_globs=("secret*",), sensitive_wanted=wanted, sensitive_confirmed=confirmed)
    assert wanted - confirmed == set()
    assert writer.flagged == [], "the other owner's document is not flagged by this run"


# ── per-item log events from the outcome ────────────────────────────────────────────────────────────────────────

def test_the_outcome_logs_one_event_for_each_rename_sweep_and_explicit_delete(info_logs) -> None:
    outcome = _outcome(
        _row("new.py", "updated", "1.7.9"),
        _row("gone.py", "deleted", "1.7.5"),
        _row("never.py", "deleted", None),
        trashed=["1.7.2", "1.7.4", "1.7.5"],
        renamed=[{"old": "1.7.2", "new": "1.7.9"}],
        swept=["1.7.4"],
    )
    with structlog.testing.capture_logs() as logs:
        reconcile_run.log_outcome(outcome, owner="1.7", repo="r")
    by_event = {}
    for e in logs:
        by_event.setdefault(e["event"], []).append(e)
    assert [(e["old_tumbler"], e["new_tumbler"], e["new_path"]) for e in by_event["housekeeping_rename_detected"]] == [
        ("1.7.2", "1.7.9", "new.py")]
    assert [e["tumbler"] for e in by_event["housekeeping_orphan_deleted"]] == ["1.7.4"]
    assert [(e["tumbler"], e["file_path"]) for e in by_event["since_head_deleted_doc"]] == [("1.7.5", "gone.py")]
    assert set(by_event) == {"housekeeping_rename_detected", "housekeeping_orphan_deleted", "since_head_deleted_doc"}


def test_an_outcome_with_nothing_renamed_swept_or_deleted_logs_nothing(info_logs) -> None:
    with structlog.testing.capture_logs() as logs:
        reconcile_run.log_outcome(_outcome(_row("a.py", "unchanged", "1.7.1")), owner="1.7", repo="r")
    assert logs == []


def test_the_hook_logs_the_outcomes_items(tmp_path: Path, info_logs) -> None:
    a = tmp_path / "a.py"
    a.write_text("a\n")
    writer = _Writer(_outcome(_row("a.py", "unchanged", "1.7.1"), _row("gone.py", "deleted", "1.7.5"),
                              swept=["1.7.4"], trashed=["1.7.4", "1.7.5"]))
    with structlog.testing.capture_logs() as logs:
        _hook(tmp_path, [(a, "code", "code__r")], writer, walk="partial", deleted_relpaths=["gone.py"])
    events = {e["event"] for e in logs}
    assert {"housekeeping_orphan_deleted", "since_head_deleted_doc"} <= events, events
