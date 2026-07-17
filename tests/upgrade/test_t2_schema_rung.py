# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P1.1 (nexus-n7u38.8): T2 schema as the FIRST native ladder rung.

The reference implementation proving the seam: detect = stored
``_nexus_version`` row behind ``expected_t2_schema_version()``; converge =
``apply_pending`` under the migration flock; verify = stored row caught
up. ``apply_pending`` refuses to stamp on ``any_skipped`` (the RDR-142
guard at its source), so a ``MigrationRetry`` deferral surfaces as a
DEFERRED converge — non-fatal, NOT recorded, position pinned — mirroring
the proven would-succeed/would-defer/would-gate trichotomy. No behavior
change to T2 migration itself; it now REPORTS through the ladder.

Happy-path tests pin a SYNTHETIC migration registry (deterministic, no
catalog dependency); one integration test runs the REAL registry and
asserts the catalog-absent deferral behaves as designed.
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest

import nexus.db.migrations as migrations
from nexus.db.migrations import (
    Migration,
    MigrationRetry,
    PreconditionVerdict,
    StepOutcome,
    bootstrap_version,
    expected_t2_schema_version,
)
from nexus.upgrade_ladder.completion import CompletionStore
from nexus.upgrade_ladder.protocol import ConvergeOutcome
from nexus.upgrade_ladder.registry import RUNG_T2_SCHEMA, LadderRegistry, default_registry
from nexus.upgrade_ladder.rungs.t2_schema import T2SchemaRung
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome


@pytest.fixture(autouse=True)
def _clear_upgrade_done() -> None:
    migrations._upgrade_done.clear()


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "memory.db"


@pytest.fixture
def synthetic_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin MIGRATIONS to one synthetic no-op step introduced at 99.0.0 —
    deterministic converge behavior with no catalog/aspect preconditions."""
    step = Migration(introduced="99.0.0", name="synthetic-ok", fn=lambda conn: None)
    monkeypatch.setattr(migrations, "MIGRATIONS", [step])


def _bootstrapped_behind(db_path: pathlib.Path, stored: str = "4.1.2") -> None:
    """Create a T2 db whose version row is pinned behind the expected schema."""
    conn = sqlite3.connect(db_path)
    try:
        bootstrap_version(conn)
        conn.execute(
            "UPDATE _nexus_version SET value=? WHERE key='cli_version'", (stored,)
        )
        conn.commit()
    finally:
        conn.close()


def _rung(db_path: pathlib.Path) -> T2SchemaRung:
    return T2SchemaRung(db_path_fn=lambda: db_path)


def _stored(db_path: pathlib.Path) -> str:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()[0]
    finally:
        conn.close()


class _Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    def emit(self, event: str, **fields: object) -> None:
        self.events.append(event)


# ── detect ───────────────────────────────────────────────────────────────────


def test_detect_pending_on_behind_db(db_path: pathlib.Path) -> None:
    _bootstrapped_behind(db_path)
    status = _rung(db_path).detect()
    assert status.applicable
    assert not status.converged
    assert "4.1.2" in status.pending_detail
    assert expected_t2_schema_version() in status.pending_detail


def test_detect_pending_on_missing_db_and_creates_nothing(db_path: pathlib.Path) -> None:
    status = _rung(db_path).detect()
    assert status.pending
    assert "not initialized" in status.pending_detail
    assert not db_path.exists()  # detect is READ-ONLY: no bootstrap side effect


def test_detect_is_read_only_on_existing_db(db_path: pathlib.Path) -> None:
    _bootstrapped_behind(db_path)
    before = db_path.read_bytes()
    _rung(db_path).detect()
    assert db_path.read_bytes() == before


def test_detect_converged_when_stored_at_expected(db_path: pathlib.Path) -> None:
    _bootstrapped_behind(db_path, stored=expected_t2_schema_version())
    status = _rung(db_path).detect()
    assert status.applicable
    assert status.converged


def test_detect_converged_when_stored_ahead(db_path: pathlib.Path) -> None:
    """A rolled-back CLI (stored > code's expected) has nothing to do —
    apply_pending's downgrade guard territory, not a pending rung."""
    _bootstrapped_behind(db_path, stored="999.0.0")
    assert _rung(db_path).detect().converged


def test_detect_reports_step_resolution_truth(
    db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RDR-142 dry-run truth: detect surfaces would-defer steps in the
    pending detail via resolve_pending_steps, not just version arithmetic."""
    _bootstrapped_behind(db_path, stored=expected_t2_schema_version())
    deferring = Migration(
        introduced="99.0.0",
        name="synthetic-deferring-step",
        fn=lambda conn: None,
        precondition=lambda conn: PreconditionVerdict(
            StepOutcome.WOULD_DEFER, detail="catalog absent"
        ),
    )
    monkeypatch.setattr(migrations, "MIGRATIONS", [*migrations.MIGRATIONS, deferring])
    status = _rung(db_path).detect()
    assert status.pending
    assert "would-defer" in status.pending_detail


# ── converge + verify (synthetic registry: deterministic happy paths) ────────


def test_converge_verify_end_to_end(
    db_path: pathlib.Path, synthetic_registry: None
) -> None:
    _bootstrapped_behind(db_path)
    rung = _rung(db_path)
    result = rung.converge(_Recorder())
    assert result.outcome is ConvergeOutcome.COMPLETED
    assert rung.verify() is True
    assert rung.detect().converged
    assert _stored(db_path) == "99.0.0"  # the synthetic registry max


def test_converge_is_idempotent(db_path: pathlib.Path, synthetic_registry: None) -> None:
    _bootstrapped_behind(db_path)
    rung = _rung(db_path)
    rung.converge(_Recorder())
    migrations._upgrade_done.clear()  # drop the in-process fast path
    assert rung.converge(_Recorder()).completed
    assert rung.verify() is True


def test_converge_bootstraps_missing_db(
    db_path: pathlib.Path, synthetic_registry: None
) -> None:
    rung = _rung(db_path)
    assert rung.converge(_Recorder()).completed
    assert db_path.exists()
    assert rung.verify() is True


def test_deferred_step_reports_deferred_not_completed(
    db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE acceptance pin (RDR-142 class): a MigrationRetry deferral means
    apply_pending refuses to stamp — converge reports DEFERRED (non-fatal),
    verify is False, and the runner records NOTHING."""
    _bootstrapped_behind(db_path, stored="4.1.2")

    def _defer(conn: sqlite3.Connection) -> None:
        raise MigrationRetry("catalog db absent — retried next open")

    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        [Migration(introduced="99.0.0", name="synthetic-defer", fn=_defer)],
    )

    rung = _rung(db_path)
    assert rung.detect().pending
    result = rung.converge(_Recorder())
    assert result.outcome is ConvergeOutcome.DEFERRED
    assert "deferred" in result.detail
    assert rung.verify() is False  # stored never advanced past deferred work

    with CompletionStore(db_path.parent / "ladder.db") as store:
        report = LadderRunner(
            LadderRegistry((rung,)), store, package_version_fn=lambda: "test"
        ).run()
        assert [r.outcome for r in report.runs] == [RungOutcome.DEFERRED]
        assert store.verified_rungs() == frozenset()
    assert not report.converged
    assert not report.hard_failed  # deferral is NOT a failure


def test_real_registry_defers_without_catalog(db_path: pathlib.Path) -> None:
    """Integration against the REAL migration registry: an old-version db
    with no catalog defers the RDR-108 PK steps (their designed behavior) —
    the rung reports DEFERRED honestly instead of a false COMPLETED."""
    _bootstrapped_behind(db_path)
    rung = _rung(db_path)
    result = rung.converge(_Recorder())
    assert result.outcome is ConvergeOutcome.DEFERRED
    assert rung.verify() is False


# ── service mode: N/A, never touches the immutable local source ─────────────
# (P1 review Critical: _run_upgrade guards service mode but the ladder walk
# did not — and the autouse NX_STORAGE_BACKEND=sqlite pin meant no test could
# see it. These override the pin / drive the seam.)


def test_service_mode_detect_is_not_applicable(db_path: pathlib.Path) -> None:
    rung = T2SchemaRung(db_path_fn=lambda: db_path, service_mode_fn=lambda: True)
    status = rung.detect()
    assert status.applicable is False
    assert not status.pending
    assert not db_path.exists()  # nothing touched, nothing created


def test_service_mode_converge_refuses(db_path: pathlib.Path) -> None:
    """Defense-in-depth: even a direct converge call must not mutate the
    immutable service-mode source (RDR-176 downgrade guarantee)."""
    rung = T2SchemaRung(db_path_fn=lambda: db_path, service_mode_fn=lambda: True)
    with pytest.raises(RuntimeError, match="immutable migration source"):
        rung.converge(_Recorder())
    assert not db_path.exists()


def test_service_mode_walk_skips_rung_and_writes_no_t2(
    db_path: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The full walk on a service-mode install: rung detect-and-skips (f0pmd),
    memory.db is never created, nothing recorded."""
    rung = T2SchemaRung(db_path_fn=lambda: db_path, service_mode_fn=lambda: True)
    with CompletionStore(tmp_path / "ladder.db") as store:
        report = LadderRunner(
            LadderRegistry((rung,)), store, package_version_fn=lambda: "test"
        ).run()
        assert [r.outcome for r in report.runs] == [RungOutcome.SKIPPED_NOT_APPLICABLE]
        assert store.verified_rungs() == frozenset()
    assert report.converged  # N/A is a clean skip, not a failure
    assert not db_path.exists()


def test_default_service_probe_reads_real_backend_resolver(
    db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DEFAULT service_mode_fn reads storage_backend_for('memory'):
    NX_STORAGE_BACKEND=service (overriding the suite's autouse sqlite pin,
    later setenv wins) must make the production-default rung N/A."""
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    rung = T2SchemaRung(db_path_fn=lambda: db_path)  # production probe
    assert rung.detect().applicable is False
    assert not db_path.exists()


def test_sqlite_backend_rung_is_applicable(db_path: pathlib.Path) -> None:
    """Companion non-vacuity: under the suite's sqlite pin the production
    probe reports applicable — the service test above isn't passing because
    the probe is stuck False."""
    _bootstrapped_behind(db_path)
    assert T2SchemaRung(db_path_fn=lambda: db_path).detect().applicable is True


# ── through the runner / registry ────────────────────────────────────────────


def test_runner_records_t2_rung(
    db_path: pathlib.Path, tmp_path: pathlib.Path, synthetic_registry: None
) -> None:
    _bootstrapped_behind(db_path)
    with CompletionStore(tmp_path / "ladder.db") as store:
        report = LadderRunner(
            LadderRegistry((_rung(db_path),)),
            store,
            package_version_fn=lambda: "6.12.0",
        ).run()
        assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED]
        assert store.ladder_position((RUNG_T2_SCHEMA,)) == 1


def test_default_registry_contains_t2_rung() -> None:
    """Closes P0 validator gap 3: the production registry is no longer empty,
    so the canonical-order registry test is now non-vacuous."""
    names = [r.name for r in default_registry()]
    assert RUNG_T2_SCHEMA in names


def test_default_registry_accepts_injected_db_path(db_path: pathlib.Path) -> None:
    """The db_path_fn seam flows into the T2 rung (how nx upgrade routes its
    _db_path test seam through the walk)."""
    _bootstrapped_behind(db_path)
    registry = default_registry(db_path_fn=lambda: db_path)
    (rung,) = [r for r in registry if r.name == RUNG_T2_SCHEMA]
    assert rung.detect().pending
