# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for the RDR-112 P0-gate fixes (2026-05-14):

- nexus-907o: ``run_if_needed`` no-op under ``NX_STORAGE_MODE=daemon``
- nexus-cy3o: ``taxonomy_cmd._t2_ctx`` delegates to ``mcp_infra.t2_ctx``
- nexus-46xu: ``aspect_worker._worker_lock_path`` resolves via
  ``nexus_config_dir`` (respects ``NEXUS_CONFIG_DIR``)
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest


# ── nexus-907o: run_if_needed daemon-mode no-op ────────────────────────────


def test_run_if_needed_skips_under_daemon_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under ``NX_STORAGE_MODE=daemon`` the client-side call must not
    open a sqlite3.Connection (which would race the daemon's WAL writer).
    """
    from nexus.db import migrations

    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

    with patch("sqlite3.connect") as fake_connect:
        migrations.run_if_needed(db_path)

    fake_connect.assert_not_called()
    # And the path must NOT have been touched.
    assert not db_path.exists()


def test_run_if_needed_skipped_when_storage_mode_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-507q (RDR-112 P6.3 cutover, 2026-05-17): unset env now
    resolves to daemon mode, so client-side migrations are skipped
    (the daemon owns the WAL writer)."""
    from nexus.db import migrations

    db_path = tmp_path / "memory.db"
    monkeypatch.delenv("NX_STORAGE_MODE", raising=False)
    migrations._upgrade_done.clear()

    migrations.run_if_needed(db_path)
    assert not db_path.exists(), (
        "post-cutover: unset env -> daemon mode -> no client-side migration "
        "(daemon owns the WAL writer). The db file must NOT be created here."
    )


def test_run_if_needed_runs_when_storage_mode_direct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit direct mode still runs the migration (direct-mode path)."""
    from nexus.db import migrations

    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("NX_STORAGE_MODE", "direct")
    migrations._upgrade_done.clear()

    migrations.run_if_needed(db_path)
    assert db_path.exists()


def test_run_if_needed_runs_under_non_daemon_storage_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Other ``NX_STORAGE_MODE`` values (e.g. ``in-process``) do not skip."""
    from nexus.db import migrations

    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("NX_STORAGE_MODE", "in-process")
    migrations._upgrade_done.clear()

    migrations.run_if_needed(db_path)
    assert db_path.exists()


# ── nexus-cy3o: taxonomy_cmd._t2_ctx delegates to mcp_infra.t2_ctx ─────────


def test_taxonomy_cmd_t2_ctx_delegates_to_mcp_infra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shim must route through ``mcp_infra.t2_ctx`` so Phase-1's
    daemon swap is honoured. nexus-qw21: pin direct mode so the
    resolver path is exercised; daemon-mode bypass is covered by the
    sibling test below.
    """
    from nexus import mcp_infra
    from nexus.commands import taxonomy_cmd

    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("NX_STORAGE_MODE", "direct")
    monkeypatch.setattr(
        "nexus.commands.taxonomy_cmd._default_db_path", lambda: db_path,
    )

    with patch.object(mcp_infra, "t2_ctx", wraps=mcp_infra.t2_ctx) as spy:
        with taxonomy_cmd._t2_ctx() as db:
            pass  # opening + closing exercises the seam.

    spy.assert_called_once()
    # The kwarg-passed resolver must be exactly the module-local symbol
    # so future test patches on _default_db_path keep propagating.
    call_kwargs = spy.call_args.kwargs
    assert "_path_resolver" in call_kwargs
    assert call_kwargs["_path_resolver"] is taxonomy_cmd._default_db_path


def test_taxonomy_cmd_t2_ctx_skips_path_resolver_under_daemon_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-qw21: under daemon mode the daemon owns the path. The
    shim must call ``mcp_infra.t2_ctx()`` with no ``_path_resolver``
    kwarg; passing one raises ``RuntimeError`` in ``mcp_infra``.
    """
    from nexus import mcp_infra
    from nexus.commands import taxonomy_cmd

    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

    # Patch t2_ctx to avoid actually opening a daemon RPC; just observe
    # the call shape. Returning a no-op context manager keeps the
    # ``with`` body unreachable but the call itself is what we assert.
    from contextlib import contextmanager

    @contextmanager
    def _fake_t2_ctx(**kwargs):
        # Capture and re-raise the guard if the wrapper accidentally
        # forwards a resolver — this is the regression we are guarding.
        if "_path_resolver" in kwargs and kwargs["_path_resolver"] is not None:
            raise RuntimeError(
                "regression: _t2_ctx forwarded _path_resolver under daemon mode"
            )
        yield None

    with patch.object(mcp_infra, "t2_ctx", side_effect=_fake_t2_ctx) as spy:
        with taxonomy_cmd._t2_ctx():
            pass

    spy.assert_called_once()
    call_kwargs = spy.call_args.kwargs
    assert "_path_resolver" not in call_kwargs or call_kwargs["_path_resolver"] is None


def test_taxonomy_cmd_t2_ctx_respects_default_db_path_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The long-standing ``taxonomy_cmd._default_db_path`` patch pattern
    must continue to redirect the T2 open at the tmp path.
    """
    from nexus.commands import taxonomy_cmd

    db_path = tmp_path / "from-taxonomy-shim.db"
    monkeypatch.setattr(
        "nexus.commands.taxonomy_cmd._default_db_path", lambda: db_path,
    )

    with taxonomy_cmd._t2_ctx() as db:
        # T2Database stores the path under _path on construction.
        assert Path(db._path).resolve() == db_path.resolve()


# ── nexus-46xu: aspect_worker._worker_lock_path respects NEXUS_CONFIG_DIR ──


def test_worker_lock_path_default_uses_nexus_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit ``locks_dir``, the default must come from
    ``nexus_config_dir`` (which respects ``NEXUS_CONFIG_DIR``).
    """
    from nexus import aspect_worker

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))

    lock_path = aspect_worker._worker_lock_path()
    assert lock_path.parent == tmp_path / "locks"
    assert lock_path.name.startswith("aspect_worker.")


def test_worker_lock_path_respects_explicit_locks_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``locks_dir`` argument still wins (test-isolation contract)."""
    from nexus import aspect_worker

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "ignored"))
    explicit = tmp_path / "isolated-locks"
    lock_path = aspect_worker._worker_lock_path(explicit)
    assert lock_path.parent == explicit


def test_worker_lock_path_does_not_leak_home_when_env_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy ``Path.home() / .config / nexus / locks`` literal must
    not leak into the lock path when ``NEXUS_CONFIG_DIR`` is set.
    """
    from nexus import aspect_worker

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    lock_path = aspect_worker._worker_lock_path()
    home_legacy = Path.home() / ".config" / "nexus" / "locks"
    assert home_legacy not in lock_path.parents


# ── nexus-k8ma cleanup: make_ephemeral_t3 contract ─────────────────────────


def test_make_ephemeral_t3_returns_working_t3() -> None:
    """``make_ephemeral_t3`` returns a T3Database backed by an in-process
    EphemeralClient + a usable embedding function. Indirectly exercised
    by ``nx index --dry-run``; this gives it a direct unit pin so a
    refactor of ``src/nexus/db/__init__.py`` doesn't silently break that
    path.
    """
    from nexus.db import make_ephemeral_t3
    from nexus.db.t3 import T3Database

    t3 = make_ephemeral_t3()
    assert isinstance(t3, T3Database)
    # The chroma client must be present so callers can drive ingestion.
    assert t3._client is not None
    # And it must serve a get_or_create_collection round-trip without
    # talking to the network.
    col = t3._client.get_or_create_collection("ephemeral_test")
    assert col.count() == 0


# ── Phase-1 prereq: daemon-mode hard-rejects on diagnostic sites ───────────


def test_reject_under_daemon_mode_raises_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.db import DaemonModeDiagnosticError, reject_under_daemon_mode

    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
    with pytest.raises(DaemonModeDiagnosticError):
        reject_under_daemon_mode("test op")


def test_reject_under_daemon_mode_raises_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-507q (RDR-112 P6.3 cutover): unset env now resolves to
    daemon, so the guard fires. The cutover doc reframes the
    direct-mode behaviour as an explicit opt-in
    (``NX_STORAGE_MODE=direct``).
    """
    from nexus.db import DaemonModeDiagnosticError, reject_under_daemon_mode

    monkeypatch.delenv("NX_STORAGE_MODE", raising=False)
    with pytest.raises(DaemonModeDiagnosticError):
        reject_under_daemon_mode("test op")


def test_reject_under_daemon_mode_noop_when_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit direct mode bypasses the guard (the legacy direct-open
    callsites still work for operators that opt back in)."""
    from nexus.db import reject_under_daemon_mode

    monkeypatch.setenv("NX_STORAGE_MODE", "direct")
    reject_under_daemon_mode("test op")  # must NOT raise


def test_reject_under_daemon_mode_noop_when_other_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.db import reject_under_daemon_mode

    monkeypatch.setenv("NX_STORAGE_MODE", "in-process")
    reject_under_daemon_mode("test op")  # must NOT raise


def test_t2_ctx_rejects_path_resolver_under_daemon_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """``t2_ctx(_path_resolver=...)`` is incompatible with daemon mode
    (the daemon owns the path; a client-side override would silently
    pick a different file).
    """
    from nexus.mcp_infra import t2_ctx

    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
    with pytest.raises(RuntimeError, match="incompatible with"):
        t2_ctx(_path_resolver=lambda: tmp_path / "should-not-open.db")


# ── EventLog.is_empty PermissionError tolerance ───────────────────────────


def test_event_log_is_empty_tolerates_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``EventLog.is_empty`` returns True on any OSError (not just
    FileNotFoundError) to match the prior ``Path.exists()`` permissive
    behaviour in sandboxed environments.
    """
    from nexus.catalog.event_log import EventLog

    log = EventLog(tmp_path)

    def _boom(*_a, **_kw):
        raise PermissionError("sandbox forbids stat")

    monkeypatch.setattr(type(log._path), "stat", _boom)
    assert log.is_empty() is True
