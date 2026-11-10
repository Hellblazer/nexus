# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shakedown 7.94.0 class A, round 2: the ``Upgrade ladder`` row's remedy has to be able to clear the row.

``nx doctor`` runs the pending-rungs sweep as unattended, so on a managed service a rung that can only answer
from a whole-catalog or whole-table read reports "not checked", and the row names an interactive ``nx upgrade``
as the way out. Round 1 tested that with a one-rung registry (manifest-section-labels, the one rung that checks
its record before it refuses). ``note-tags-array`` and ``aspect-queue-tuples`` refused without
looking, so on every managed install the row stayed a warning no interactive run could clear.

These tests run the PRODUCTION registry (``default_registry()``, every rung built with its own defaults) on a
managed install whose engine, tuple space, queue, ledger and T2 are in-memory fakes: before any record, then
through the interactive run the remedy names, then again.
"""
from __future__ import annotations

import pytest

from nexus import health
from nexus.upgrade_ladder.completion import CompletionRecord
from nexus.upgrade_ladder.registry import (
    RUNG_MANIFEST_SECTION_LABELS,
    RUNG_MANIFEST_SPAN_FILL,
    RUNG_NOTE_TAGS_ARRAY,
    LadderRegistry,
    default_registry,
)
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome
from nexus.upgrade_ladder.rungs.manifest_section_labels import TITLE_RULE_MARK
from tests.upgrade.conftest import InMemoryCompletionLedger

#: The rungs that answer an unattended run on a managed service only from a record (or, on an engine with the route,
#: manifest-section-labels from the engine's count; this install's fake engine has none). The aspect-queue rung
#: was retired with the table (nexus-hbnml). manifest-span-fill has no engine count at all (nexus-l8o33).
REFUSABLE = (RUNG_NOTE_TAGS_ARRAY, RUNG_MANIFEST_SECTION_LABELS, RUNG_MANIFEST_SPAN_FILL)


class _Install:
    """What the managed install holds, and a count of every read of a whole catalog or table."""

    def __init__(self) -> None:
        self.ledger = InMemoryCompletionLedger()
        self.memo: dict[tuple[str, str], str] = {}
        self.scan_reads: list[str] = []


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch, cloud_mode) -> _Install:
    inst = _Install()

    class Ledger:
        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            pass

        def close(self) -> None:
            pass

        def completions(self) -> dict[str, CompletionRecord]:
            return inst.ledger.completions()

    class Memory:
        def get(self, project: str = "", title: str = "", **_kw):
            content = inst.memo.get((project, title))
            return None if content is None else {"content": content}

        def put(self, project: str, title: str, content: str, **_kw) -> None:
            inst.memo[(project, title)] = content

        def delete(self, project: str = "", title: str = "", **_kw) -> None:
            inst.memo.pop((project, title), None)

        def close(self) -> None:
            pass

    class Catalog:
        """No documents, no notes: an interactive scan finishes at once, and every scan is counted."""

        def all_documents(self, limit: int = 0, **_kw):
            inst.scan_reads.append("all_documents")
            return []

        def distinct_doc_collections(self):
            inst.scan_reads.append("distinct_doc_collections")
            return []

        def list_by_collection(self, *_a, **_k):  # pragma: no cover - no collections to list
            inst.scan_reads.append("list_by_collection")
            return []

        def get_manifests(self, *_a, **_k):  # pragma: no cover - no documents to read
            inst.scan_reads.append("get_manifests")
            return {}

        def stats(self) -> dict:
            return {"doc_count": 0, "chunk_count": 0}

        def close(self) -> None:
            pass

    monkeypatch.setattr("nexus.upgrade_ladder.http_store.HttpLadderStore", Ledger)
    monkeypatch.setattr("nexus.db.t2.http_memory_store.HttpMemoryStore", Memory)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: Catalog())
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda *a, **k: Catalog())
    monkeypatch.setattr("nexus.db.make_t3", lambda *a, **k: object())
    return inst


def _row() -> health.HealthResult:
    (row,) = health._check_pending_rungs()
    return row


def _not_checked(row: health.HealthResult) -> str:
    """The 'not checked: ...' part of the row's detail, or ''."""
    return next((p for p in row.detail.split("; ") if p.startswith("not checked")), "")


def test_before_any_record_every_refused_rung_is_named_with_a_remedy_that_clears_it(install: _Install) -> None:
    row = _row()
    assert row.warn and not row.ok
    named = _not_checked(row)
    for rung in REFUSABLE:
        assert rung in named, (rung, row.detail)
    assert install.scan_reads == [], "doctor read no whole catalog or table"
    # The remedy is the interactive run. What makes it true is below: it records each rung it verifies,
    # and the same row then answers from those records.
    assert any("interactively" in s and "records" in s for s in row.fix_suggestions), row.fix_suggestions


def test_the_interactive_run_the_remedy_names_clears_every_not_checked_entry(install: _Install) -> None:
    """Run the remedy: an interactive walk (not unattended) over the production rungs that refuse. Each reads what
    doctor did not, verifies and records. Then the production-registry row has no 'not checked' entry."""
    assert _not_checked(_row()) != "", "precondition: the row starts with entries not checked"

    refusing = LadderRegistry(tuple(r for r in default_registry() if r.name in REFUSABLE))
    report = LadderRunner(refusing, install.ledger, package_version_fn=lambda: "9.9.9").run()
    assert [(r.name, r.outcome) for r in report.runs] == [(n, RungOutcome.RECORDED) for n in (
        RUNG_NOTE_TAGS_ARRAY, RUNG_MANIFEST_SECTION_LABELS, RUNG_MANIFEST_SPAN_FILL)], [
        (r.name, r.outcome, r.detail) for r in report.runs]
    assert install.ledger.verified_rungs() == frozenset(REFUSABLE)
    install.scan_reads.clear()

    row = _row()
    assert _not_checked(row) == "", row.detail
    head = row.detail.split("converged per a completion record")[0]
    for rung in REFUSABLE:
        assert rung not in head, (rung, row.detail)
    assert install.scan_reads == [], "the row answered from the records, without a scan"
    # rdr192-manifest-backfill has no record here and says so: pending, which `nx upgrade` clears.
    assert "rdr192-manifest-backfill" in row.detail


def test_a_walk_over_the_production_registry_under_auto_lists_nothing_once_recorded(install: _Install) -> None:
    """Same records, the unattended walk (`nx upgrade --auto`): no rung lists a catalog or table to confirm them."""
    from nexus.upgrade_ladder.unattended import unattended_run

    for rung in REFUSABLE:
        # the two manifest rungs read a record as current only under the title rule (nexus-l8o33)
        detail = TITLE_RULE_MARK if rung in (RUNG_MANIFEST_SECTION_LABELS, RUNG_MANIFEST_SPAN_FILL) else ""
        install.ledger.record_verified(rung, package_version="9.9.9", detail=detail)
    refusing = LadderRegistry(tuple(r for r in default_registry() if r.name in REFUSABLE))
    with unattended_run():
        report = LadderRunner(refusing, install.ledger, package_version_fn=lambda: "9.9.9").run()
    assert {r.outcome for r in report.runs} == {RungOutcome.ALREADY_RECORDED}, [
        (r.name, r.outcome, r.detail) for r in report.runs]
    assert install.scan_reads == []


def test_a_row_answered_from_a_record_says_so_and_when_the_record_was_made(install: _Install) -> None:
    """A record can go stale (an older client writes comma-string tags or movable queue rows afterwards), so
    a rung that answers 'converged' from one without reading the data says that in the row, with the record's
    time. A rung that read the data (manifest-section-labels here) is not named."""
    refusing = LadderRegistry(tuple(r for r in default_registry() if r.name in REFUSABLE))
    LadderRunner(refusing, install.ledger, package_version_fn=lambda: "9.9.9").run()
    records = install.ledger.completions()
    answered = (RUNG_NOTE_TAGS_ARRAY,)
    assert all(records[r].verified_at for r in answered), "non-vacuity: the records carry a time"

    row = _row()

    _, _, note = row.detail.partition("converged per a completion record")
    assert note, row.detail
    assert "not a read of the data" in row.detail and "stale" in note
    for rung in answered:
        assert f"{rung} (recorded {records[rung].verified_at})" in note, (rung, row.detail)
    assert RUNG_MANIFEST_SECTION_LABELS not in note and RUNG_MANIFEST_SPAN_FILL not in note
