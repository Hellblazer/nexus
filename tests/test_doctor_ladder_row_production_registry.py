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

from nexus import health, upgrade_finish
from nexus.commands.upgrade import _run_ladder
from nexus.db.http_vector_client import VectorServiceError
from nexus.upgrade_ladder.auto_guard import INTERACTIVE_REMEDY
from nexus.upgrade_ladder.completion import CompletionRecord
from nexus.upgrade_ladder.registry import (
    RUNG_MANIFEST_SECTION_LABELS,
    RUNG_MANIFEST_SPAN_FILL,
    RUNG_NOTE_TAGS_ARRAY,
    RUNG_RETIRE_KEYS_SWEEP,
    LadderRegistry,
    default_registry,
)
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome
from nexus.upgrade_ladder.rungs.manifest_section_labels import TITLE_RULE_MARK
from nexus.upgrade_ladder.unattended import bounded_reads, unattended_run
from tests.upgrade.conftest import InMemoryCompletionLedger

#: The rungs that answer an unattended run on a managed service only from a record (or, on an engine with the route,
#: manifest-section-labels from the engine's count; this install's fake engine has none). The aspect-queue rung
#: was retired with the table (nexus-hbnml). manifest-span-fill has no engine count at all (nexus-l8o33), and nor does
#: retire-keys-sweep, whose dry run is an engine job (nexus-6pfgb.43).
REFUSABLE = (RUNG_NOTE_TAGS_ARRAY, RUNG_MANIFEST_SECTION_LABELS, RUNG_MANIFEST_SPAN_FILL, RUNG_RETIRE_KEYS_SWEEP)


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

    class T3:
        """The retire-keys dry run of an install with nothing to sweep (an engine job, not a catalog read)."""

        def chunk_keys_dry_run(self, **_kw) -> dict:
            return {"tenant": "t", "collections": [], "preconditions": {}, "blocking": [],
                    "totals": {"retired_key_rows": 0, "sweepable_rows": 0, "blocked_rows": 0, "wrong_width_rows": 0}}

    monkeypatch.setattr("nexus.db.make_t3", lambda *a, **k: T3())
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
        RUNG_NOTE_TAGS_ARRAY, RUNG_MANIFEST_SECTION_LABELS, RUNG_MANIFEST_SPAN_FILL, RUNG_RETIRE_KEYS_SWEEP)], [
        (r.name, r.outcome, r.detail) for r in report.runs]
    assert install.ledger.verified_rungs() == frozenset(REFUSABLE)
    install.scan_reads.clear()

    row = _row()
    assert _not_checked(row) == "", row.detail
    head = row.detail.split("converged per a completion record")[0]
    for rung in REFUSABLE:
        assert rung not in head, (rung, row.detail)
    assert install.scan_reads == [], "the row answered from the records, without a scan"
    # rdr192-manifest-backfill has no record here, but the retire-keys sweep is recorded now, and the backfill refuses to
    # run after it (it attributes a chunk by the keys the sweep removed): not applicable, so it is not named pending.
    assert "rdr192-manifest-backfill" not in row.detail


def test_a_walk_over_the_production_registry_under_auto_lists_nothing_once_recorded(install: _Install) -> None:
    """Same records, the unattended walk (`nx upgrade --auto`): no rung lists a catalog or table to confirm them."""
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
    answered = (RUNG_NOTE_TAGS_ARRAY, RUNG_RETIRE_KEYS_SWEEP)
    assert all(records[r].verified_at for r in answered), "non-vacuity: the records carry a time"

    row = _row()

    _, _, note = row.detail.partition("converged per a completion record")
    assert note, row.detail
    assert "not a read of the data" in row.detail and "stale" in note
    for rung in answered:
        assert f"{rung} (recorded {records[rung].verified_at})" in note, (rung, row.detail)
    assert RUNG_MANIFEST_SECTION_LABELS not in note and RUNG_MANIFEST_SPAN_FILL not in note


# ── retire-keys-sweep on a managed client that holds a mint or data credential (Sam 2026-11-22, item a) ────────────
#
# The sweep route takes a tenant or root credential and refuses a data credential with 403, and the client presents
# the minted data token on every route whenever `mint_token` is configured (`http_vector_client._request`,
# `service_endpoint.mint_armed`). So on such a client the rung's sweep is the operator verb `nx t3 retire-keys`, and
# no interactive `nx upgrade` clears the row: it is not applicable here, never a warning. The test is `mint_token`
# configured and nothing more: a static data-scope token with no `mint_token` is not detected and keeps its warning.

NA_TEXT = (
    "not applicable on a managed deployment with mint_token configured: retire-keys-sweep (its sweep is the operator "
    "verb `nx t3 retire-keys`, which needs the root token and runs from a host without mint_token)"
)


@pytest.fixture
def mint_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_MINT_TOKEN", "mint-credential-for-the-test")


@pytest.fixture
def tenant_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_MINT_TOKEN", raising=False)
    monkeypatch.setenv("NX_SERVICE_TOKEN", "tenant-credential-for-the-test")


def test_a_managed_mint_credential_client_reads_retire_keys_sweep_as_not_applicable(
    install: _Install, mint_credential: None,
) -> None:
    """(a): named not applicable with the operator verb and the credential it needs, out of the not-checked list,
    out of the checked count, and the remedy that cannot clear it is not offered for it."""
    row = _row()
    assert NA_TEXT in row.detail, row.detail
    assert RUNG_RETIRE_KEYS_SWEEP not in _not_checked(row), row.detail
    # Four rungs refuse here; one of them is now not applicable, so three are not checked out of the rest.
    registered = len(list(default_registry()))
    assert f"({registered - 1 - len(_not_checked_names(row))} of {registered})" in row.detail, row.detail
    assert install.scan_reads == []


def test_a_managed_mint_credential_client_whose_only_open_rung_is_the_sweep_has_no_warning(
    install: _Install, mint_credential: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) again, end to end: over the four refusing rungs, record every one but the sweep, and the row is a pass
    over what was checked, with the n/a line, never `ok` with a warning and never a clean pass that hides the rung."""
    refusing = LadderRegistry(tuple(r for r in default_registry() if r.name in REFUSABLE))
    monkeypatch.setattr("nexus.upgrade_ladder.registry.default_registry", lambda **kw: refusing)
    others = [r for r in REFUSABLE if r != RUNG_RETIRE_KEYS_SWEEP]
    for rung in others:
        detail = TITLE_RULE_MARK if rung in (RUNG_MANIFEST_SECTION_LABELS, RUNG_MANIFEST_SPAN_FILL) else ""
        install.ledger.record_verified(rung, package_version="9.9.9", detail=detail)
    row = _row()
    assert NA_TEXT in row.detail, row.detail
    assert not row.warn, row.detail
    assert _not_checked(row) == "", row.detail
    assert f"{len(REFUSABLE) - 1} checked of {len(REFUSABLE)} registered" in row.detail, row.detail


def test_the_sweep_alone_is_a_not_checked_row_not_a_pass_for_a_mint_credential_client(
    install: _Install, mint_credential: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a): when the sweep is the only rung registered nothing stands behind the row: `[ ]` (ok and skipped, no
    warning), never a checkmark."""
    only = LadderRegistry(tuple(r for r in default_registry() if r.name == RUNG_RETIRE_KEYS_SWEEP))
    monkeypatch.setattr("nexus.upgrade_ladder.registry.default_registry", lambda **kw: only)
    row = _row()
    assert row.ok and row.skipped and not row.warn, row
    assert NA_TEXT in row.detail


def test_a_managed_tenant_credential_client_still_has_retire_keys_sweep_not_checked_with_a_warning(
    install: _Install, tenant_credential: None,
) -> None:
    """(b): a tenant or root credential CAN run the sweep, so an interactive `nx upgrade` clears it."""
    row = _row()
    assert row.warn and not row.ok, row
    assert RUNG_RETIRE_KEYS_SWEEP in _not_checked(row), row.detail
    assert "not applicable" not in row.detail, row.detail


def test_a_local_install_keeps_retire_keys_sweep_as_it_was(
    install: _Install, mint_credential: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c): the mint credential changes nothing off a managed deployment (doctor's bounded reads decline the rung)."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    row = _row()
    assert row.warn and not row.ok, row
    assert RUNG_RETIRE_KEYS_SWEEP in _not_checked(row), row.detail
    assert "not applicable" not in row.detail, row.detail


def test_the_mint_credential_leaves_the_rdr192_backfill_and_the_other_declined_rungs_not_checked(
    install: _Install, mint_credential: None,
) -> None:
    """(d): only the sweep is split out. Every other declined rung is still named, with the warning."""
    row = _row()
    assert row.warn and not row.ok, row
    named = _not_checked(row)
    for rung in REFUSABLE:
        if rung != RUNG_RETIRE_KEYS_SWEEP:
            assert rung in named, (rung, row.detail)
    assert "rdr192-manifest-backfill" in row.detail, row.detail
    assert NA_TEXT in row.detail, "non-vacuity: the split happened, and only for the sweep"


def test_the_rung_and_the_finish_gate_still_read_the_sweep_as_unverified_for_a_mint_credential_client(
    install: _Install, mint_credential: None,
) -> None:
    """(e): the split is in the surfaces, not the rung. The rung's own detect() still declines (not applicable, with
    the interactive remedy)."""
    rung = next(r for r in default_registry() if r.name == RUNG_RETIRE_KEYS_SWEEP)
    with unattended_run(), bounded_reads():
        status = rung.detect()
    assert not status.applicable and INTERACTIVE_REMEDY in (status.pending_detail or ""), status


def _not_checked_names(row: health.HealthResult) -> list[str]:
    """The rung names in the 'not checked: ...' part (each entry starts with its rung name)."""
    text = _not_checked(row).removeprefix("not checked: ")
    return [name for name in (r.name for r in default_registry()) if name in text.split(" (each answers")[0]]


def test_an_empty_registry_is_still_no_pending_rungs_with_a_mint_credential(
    mint_credential: None, cloud_mode, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `[ ]` branch is for a registry whose every rung is not applicable here, not for one with no rungs."""
    monkeypatch.setattr("nexus.upgrade_ladder.registry.default_registry", lambda **kw: LadderRegistry(()))
    row = _row()
    assert row.ok and not row.skipped and not row.warn, row
    assert "no pending rungs (0 registered)" in row.detail, row.detail


def test_the_version_change_callout_says_not_applicable_for_a_mint_credential_client(
    install: _Install, mint_credential: None,
) -> None:
    """The callout `nx` prints once per version change told this client to run `nx upgrade` interactively for
    retire-keys-sweep, which cannot run the sweep. It says not applicable and names the operator verb; the other
    declined rung keeps its line."""
    lines = upgrade_finish.pending_data_rung_callout()
    sweep = [ln for ln in lines if RUNG_RETIRE_KEYS_SWEEP in ln]
    assert len(sweep) == 1, lines
    assert "not applicable" in sweep[0] and "`nx t3 retire-keys`" in sweep[0] and "mint_token" in sweep[0], sweep
    assert "interactively" not in sweep[0], sweep
    assert any("rdr192-manifest-backfill" in ln and "run `nx upgrade` interactively" in ln for ln in lines), lines


def test_the_version_change_callout_still_sends_a_tenant_credential_client_to_nx_upgrade(
    install: _Install, tenant_credential: None,
) -> None:
    (sweep,) = [ln for ln in upgrade_finish.pending_data_rung_callout() if RUNG_RETIRE_KEYS_SWEEP in ln]
    assert "run `nx upgrade` interactively" in sweep and "not applicable" not in sweep, sweep


def test_nx_upgrade_dry_run_lists_no_pending_line_for_the_sweep_on_a_mint_credential_client(
    install: _Install, mint_credential: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """`commands/upgrade.py`'s dry-run "pending" line: the engine refuses this client's dry run with 403, so detect()
    reads the sweep as not applicable and the line is never printed for it (no change was needed there). The tenant
    credential client's dry run is accepted and reads its count, so the line is a real one."""
    class Refusing:
        def chunk_keys_dry_run(self, **_kw) -> dict:
            raise VectorServiceError("the sweep needs a tenant or root credential, not scope data", code=403)

    monkeypatch.setattr("nexus.db.make_t3", lambda *a, **k: Refusing())
    _run_ladder(dry_run=True, auto_mode=False)
    out = capsys.readouterr().out
    assert not [ln for ln in out.splitlines() if RUNG_RETIRE_KEYS_SWEEP in ln and "pending" in ln], out
