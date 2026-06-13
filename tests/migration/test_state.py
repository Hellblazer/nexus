# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-159 P1a.T (nexus-ue6g7.5) — the ``migration.state`` sentinel contract.

The load-bearing NEW mechanism of RDR-159 (§"Cross-process migration state"):
a single ``~/.config/nexus/migration.state`` sentinel file that the CLI
migration process writes and every *separate, long-lived* process (the MCP
read surfaces, the aspect workers, ``nx index``) polls. A CLI-local flag is
insufficient because those pollers are independent processes.

These tests are the contract for the sentinel implemented in P1a.I
(nexus-ue6g7.6). They pin four locked properties:

* **Atomicity** — writes go through a ``.tmp`` sibling + ``os.rename`` (POSIX-
  atomic), NEVER a bare ``write_text``. The ``phase_review_sentinel`` precedent
  in ``src/nexus/phase_review_sentinel.py`` IS a bare ``write_text`` and is the
  named anti-pattern: a concurrent poller must never observe a partial payload.
* **Cross-process visibility** — a genuinely separate Python process polling
  the file observes the phase transitions the parent writes.
* **Schema** — exactly ``{phase, started_at, collections_total,
  collections_done}`` (+ ``failure`` iff a failure is recorded).
* **State machine** — ``not-migrating`` (default / absent) → ``migrating`` →
  ``migrated`` (cleared) / ``migrated-failed``, with progress RECOMPUTED from
  live source-vs-target counts on a resumed run (derived, never trusted from a
  stale marker).

Every count / key assertion is EXACT (``== N`` / ``== {...}``), never ``>=`` /
superset — an inequality is how a silent-undercount or schema-drift bug passes
(``feedback_exact_assertions_for_fixture_regression``).

Isolation: every test redirects ``NEXUS_CONFIG_DIR`` to a ``tmp_path`` so the
real ``~/.config/nexus/migration.state`` is never touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from nexus.migration.state import (
    MIGRATED,
    MIGRATED_FAILED,
    MIGRATING,
    NOT_MIGRATING,
    MigrationState,
    begin_migration,
    clear_state,
    current_phase,
    is_migrating,
    mark_failed,
    mark_migrated,
    read_state,
    record_progress,
    state_path,
    write_state,
)

_FIXED_STARTED_AT = "2026-06-13T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the whole ``.config/nexus`` footprint at a tmp dir for every test."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    return tmp_path


# --------------------------------------------------------------------------
# Path + default (absent-file) behaviour
# --------------------------------------------------------------------------


def test_state_path_under_config_dir(_isolate_config_dir: Path) -> None:
    assert state_path() == _isolate_config_dir / "migration.state"


def test_read_state_none_when_absent() -> None:
    assert read_state() is None


def test_current_phase_defaults_to_not_migrating_when_absent() -> None:
    assert current_phase() == NOT_MIGRATING
    assert NOT_MIGRATING == "not-migrating"


def test_is_migrating_false_when_absent() -> None:
    assert is_migrating() is False


def test_read_state_none_on_corrupt_file(_isolate_config_dir: Path) -> None:
    # An unparseable file is foreign/corrupt (atomic writes never leave a
    # partial). A poller fails soft to None → not-migrating → serves normally.
    _isolate_config_dir.mkdir(parents=True, exist_ok=True)
    state_path().write_text("{not valid json", encoding="utf-8")
    assert read_state() is None
    assert current_phase() == NOT_MIGRATING


# --------------------------------------------------------------------------
# Schema (exact key set; failure optional)
# --------------------------------------------------------------------------


def test_payload_schema_without_failure() -> None:
    write_state(
        MigrationState(
            phase=MIGRATING,
            started_at=_FIXED_STARTED_AT,
            collections_total=7,
            collections_done=2,
        )
    )
    payload = json.loads(state_path().read_text(encoding="utf-8"))
    assert set(payload.keys()) == {
        "phase",
        "started_at",
        "collections_total",
        "collections_done",
    }
    assert payload["phase"] == "migrating"
    assert payload["started_at"] == _FIXED_STARTED_AT
    assert payload["collections_total"] == 7
    assert payload["collections_done"] == 2


def test_payload_schema_with_failure() -> None:
    write_state(
        MigrationState(
            phase=MIGRATED_FAILED,
            started_at=_FIXED_STARTED_AT,
            collections_total=7,
            collections_done=4,
            failure="3 collections unsupported: re-index required",
        )
    )
    payload = json.loads(state_path().read_text(encoding="utf-8"))
    assert set(payload.keys()) == {
        "phase",
        "started_at",
        "collections_total",
        "collections_done",
        "failure",
    }
    assert payload["failure"] == "3 collections unsupported: re-index required"


def test_round_trip_read_state_returns_dataclass() -> None:
    written = MigrationState(
        phase=MIGRATING,
        started_at=_FIXED_STARTED_AT,
        collections_total=5,
        collections_done=1,
    )
    write_state(written)
    got = read_state()
    assert got == written
    assert got.failure is None


# --------------------------------------------------------------------------
# Atomicity — .tmp sibling + os.rename, NOT bare write_text
# --------------------------------------------------------------------------


def test_write_uses_os_rename_not_bare_write(monkeypatch: pytest.MonkeyPatch) -> None:
    # The explicit rejection of the phase_review_sentinel bare-write_text
    # anti-pattern: a write MUST route through os.rename (the atomic primitive).
    calls: list[tuple[str, str]] = []
    real_rename = os.rename

    def _spy_rename(src, dst, *a, **k):  # type: ignore[no-untyped-def]
        calls.append((str(src), str(dst)))
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(os, "rename", _spy_rename)
    write_state(
        MigrationState(
            phase=MIGRATING,
            started_at=_FIXED_STARTED_AT,
            collections_total=1,
            collections_done=0,
        )
    )
    # Exactly one rename, and its destination is the canonical state path.
    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == str(state_path())
    assert src != dst  # wrote a sibling temp first


def test_write_leaves_no_tmp_sibling(_isolate_config_dir: Path) -> None:
    for done in range(3):
        write_state(
            MigrationState(
                phase=MIGRATING,
                started_at=_FIXED_STARTED_AT,
                collections_total=3,
                collections_done=done,
            )
        )
    # The state file exists; no half-written .tmp scratch survives.
    entries = sorted(p.name for p in _isolate_config_dir.iterdir())
    assert entries == ["migration.state"]


def test_concurrent_reader_never_observes_partial_payload() -> None:
    # A poller reading WHILE a writer churns must only ever see None or a fully
    # valid state — never a JSON parse error or a half-key payload. Atomic
    # rename guarantees this; a bare write_text would intermittently fail here.
    stop = threading.Event()
    errors: list[Exception] = []
    valid_phases = {NOT_MIGRATING, MIGRATING, MIGRATED, MIGRATED_FAILED}

    def _writer() -> None:
        # Vary payload size so a non-atomic writer would expose a torn read.
        for i in range(400):
            failure = ("x" * (i % 257)) if i % 2 else None
            write_state(
                MigrationState(
                    phase=MIGRATING,
                    started_at=_FIXED_STARTED_AT,
                    collections_total=400,
                    collections_done=i,
                    failure=failure,
                )
            )

    def _reader() -> None:
        while not stop.is_set():
            try:
                st = read_state()
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)
                return
            if st is not None and st.phase not in valid_phases:
                errors.append(AssertionError(f"torn phase: {st.phase!r}"))
                return

    rt = threading.Thread(target=_reader)
    rt.start()
    wt = threading.Thread(target=_writer)
    wt.start()
    wt.join()
    stop.set()
    rt.join()
    assert errors == []


# --------------------------------------------------------------------------
# Cross-process visibility — a separate process observes the transitions
# --------------------------------------------------------------------------


def _phase_seen_by_separate_process(config_dir: Path) -> str:
    """Read ``current_phase()`` from a genuinely separate Python process."""
    code = "from nexus.migration.state import current_phase; print(current_phase())"
    env = {**os.environ, "NEXUS_CONFIG_DIR": str(config_dir)}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_separate_process_observes_phase_transitions(_isolate_config_dir: Path) -> None:
    # Default: absent file → not-migrating, seen cross-process.
    assert _phase_seen_by_separate_process(_isolate_config_dir) == NOT_MIGRATING

    begin_migration(collections_total=4, started_at=_FIXED_STARTED_AT)
    assert _phase_seen_by_separate_process(_isolate_config_dir) == MIGRATING

    mark_migrated()
    assert _phase_seen_by_separate_process(_isolate_config_dir) == MIGRATED


# --------------------------------------------------------------------------
# State machine + transitions
# --------------------------------------------------------------------------


def test_begin_migration_writes_migrating() -> None:
    st = begin_migration(collections_total=9, started_at=_FIXED_STARTED_AT)
    assert st.phase == MIGRATING
    assert st.started_at == _FIXED_STARTED_AT
    assert st.collections_total == 9
    assert st.collections_done == 0
    assert st.failure is None
    assert read_state() == st
    assert is_migrating() is True


def test_mark_migrated_preserves_started_at_and_total() -> None:
    begin_migration(collections_total=6, started_at=_FIXED_STARTED_AT)
    record_progress(collections_done=6, collections_total=6)
    st = mark_migrated()
    assert st.phase == MIGRATED
    assert st.started_at == _FIXED_STARTED_AT
    assert st.collections_total == 6
    assert st.collections_done == 6
    assert st.failure is None
    assert current_phase() == MIGRATED
    assert is_migrating() is False


def test_mark_failed_records_failure_and_keeps_progress() -> None:
    begin_migration(collections_total=8, started_at=_FIXED_STARTED_AT)
    record_progress(collections_done=5, collections_total=8)
    st = mark_failed("voyage key absent; 3 collections unsupported")
    assert st.phase == MIGRATED_FAILED
    assert st.failure == "voyage key absent; 3 collections unsupported"
    assert st.collections_done == 5
    assert st.collections_total == 8
    assert st.started_at == _FIXED_STARTED_AT
    assert current_phase() == MIGRATED_FAILED
    assert is_migrating() is False


def test_record_progress_recomputes_from_live_counts_not_stale_marker() -> None:
    # Resumed run: a stale marker claims near-complete; detection recomputes
    # live source-vs-target counts. The DERIVED values overwrite the marker —
    # progress is never trusted from the stale file.
    write_state(
        MigrationState(
            phase=MIGRATING,
            started_at=_FIXED_STARTED_AT,
            collections_total=99,
            collections_done=98,  # stale, optimistic
        )
    )
    st = record_progress(collections_done=3, collections_total=50)
    assert st.collections_done == 3
    assert st.collections_total == 50
    assert st.phase == MIGRATING
    assert st.started_at == _FIXED_STARTED_AT  # preserved across recompute
    assert read_state() == st


def test_clear_state_removes_marker_and_resets_phase() -> None:
    begin_migration(collections_total=4, started_at=_FIXED_STARTED_AT)
    assert state_path().exists()
    clear_state()
    assert not state_path().exists()
    assert read_state() is None
    assert current_phase() == NOT_MIGRATING


def test_clear_state_idempotent_when_absent() -> None:
    # Escape-hatch (nx migration --clear-state) on an already-clean install.
    assert read_state() is None
    clear_state()  # must not raise
    assert read_state() is None
