# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reaping and attribution of ``data_token_mint_lock.*`` files.

nexus-ndfc1: one lock file is created per (host:port, tenant) minted
against and nothing removed them (82 piled up on one laptop, one per
random-port engine). Stale ones are now reaped inline at mint time.

nexus-28bzt: the writer of the stray files was never identified, so the
first creation of a lock file logs one attributing event.

Real files in ``tmp_path``, the module's own flock primitive, no mocks of
the lock. Staleness is set with ``os.utime`` against real time (the reaper
reads filesystem mtimes, not the manager's injectable clock).
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from nexus._locking import lock_fd, unlock_fd
from nexus.db import data_token
from nexus.db.data_token import (
    _MINT_LOCK_PREFIX,
    _MINT_LOCK_REAP_MAX_PER_MINT,
    _MINT_LOCK_STALE_AFTER_S,
    DataTokenManager,
    _data_token_lease_path,
    _data_token_mint_lock_path,
    _reap_stale_mint_locks,
)
from tests._module_seam import setattr_in

BASE_URL = "http://127.0.0.1:9999"
TENANT = "acme"
OLD = time.time() - _MINT_LOCK_STALE_AFTER_S - 3600.0
RECENT = time.time() - 3600.0


def _hex(n: int) -> str:
    return f"{n:064x}"


def _lock_file(d: Path, n: int, *, mtime: float, prefix: str = _MINT_LOCK_PREFIX) -> Path:
    p = d / f"{prefix}{_hex(n)}"
    p.write_bytes(b"")
    os.utime(p, (mtime, mtime))
    return p


def _poster(url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
    return 200, {"data_token": "tok-SECRET", "expires_in_seconds": 300}, {}


def _manager(config_dir: Path) -> DataTokenManager:
    return DataTokenManager(
        poster=_poster,
        mint_credential=lambda: "mintcred",
        mint_tenant=lambda: "",
        config_dir=config_dir,
        sleep=lambda s: None,
    )


@pytest.fixture(autouse=True)
def debug_logging() -> Iterator[None]:
    prior = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    try:
        yield
    finally:
        structlog.configure(wrapper_class=prior)


# ── reaping (nexus-ndfc1) ────────────────────────────────────────────────


def test_stale_unheld_lock_is_removed_at_mint_time(tmp_path: Path) -> None:
    stale = _lock_file(tmp_path, 1, mtime=OLD)
    assert _manager(tmp_path).bearer_for(BASE_URL, TENANT) == "tok-SECRET"
    assert not stale.exists()
    # the mint's own lock is untouched by the reap
    assert _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path).exists()


def test_recent_lock_is_kept(tmp_path: Path) -> None:
    recent = _lock_file(tmp_path, 1, mtime=RECENT)
    _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    assert recent.exists()


def test_held_stale_lock_is_never_removed(tmp_path: Path) -> None:
    """A lock another open file description holds is skipped even when old;
    its unheld stale neighbour is still removed, so the skip is per file."""
    held = _lock_file(tmp_path, 1, mtime=OLD)
    free = _lock_file(tmp_path, 2, mtime=OLD)
    fd = os.open(str(held), os.O_WRONLY)
    lock_fd(fd, blocking=True)
    try:
        assert _reap_stale_mint_locks(tmp_path) == 1
        assert held.exists()
        assert not free.exists()
    finally:
        unlock_fd(fd)
        os.close(fd)
    # released: now it is reapable, proving the earlier skip was the lock
    assert _reap_stale_mint_locks(tmp_path) == 1
    assert not held.exists()


def test_removal_is_bounded_per_mint(tmp_path: Path) -> None:
    extra = 5
    files = [
        _lock_file(tmp_path, n, mtime=OLD)
        for n in range(1, _MINT_LOCK_REAP_MAX_PER_MINT + extra + 1)
    ]
    _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    assert sum(1 for f in files if f.exists()) == extra
    # a later mint drains the rest
    _manager(tmp_path).bearer_for(BASE_URL, "other-tenant")
    assert not any(f.exists() for f in files)


def test_only_exact_mint_lock_names_are_touched(tmp_path: Path) -> None:
    keepers = [
        tmp_path / f"{_MINT_LOCK_PREFIX}{_hex(1)[:-1]}",  # 63 hex
        tmp_path / f"{_MINT_LOCK_PREFIX}{_hex(1)}0",  # 65 hex
        tmp_path / f"{_MINT_LOCK_PREFIX}{_hex(1)}.bak",
        tmp_path / f"{_MINT_LOCK_PREFIX}{_hex(1).upper().replace('0', 'A')}",
        tmp_path / f"x{_MINT_LOCK_PREFIX}{_hex(1)}",
        tmp_path / "config.yml",
        tmp_path / f"data_token_lease.{_hex(1)}",  # the lease family
    ]
    for p in keepers:
        p.write_bytes(b"")
        os.utime(p, (OLD, OLD))
    dir_named_like_lock = tmp_path / f"{_MINT_LOCK_PREFIX}{_hex(2)}"
    dir_named_like_lock.mkdir()
    os.utime(dir_named_like_lock, (OLD, OLD))
    victim = _lock_file(tmp_path, 3, mtime=OLD)

    assert _reap_stale_mint_locks(tmp_path) == 1
    assert not victim.exists()
    assert all(p.exists() for p in keepers)
    assert dir_named_like_lock.is_dir()


def test_symlink_named_like_a_lock_is_not_followed_or_removed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"")
    os.utime(target, (OLD, OLD))
    link = tmp_path / f"{_MINT_LOCK_PREFIX}{_hex(1)}"
    link.symlink_to(target)
    assert _reap_stale_mint_locks(tmp_path) == 0
    assert link.is_symlink() and target.exists()


def test_the_mints_own_lock_is_touched_and_never_reaped(tmp_path: Path) -> None:
    """mtime means 'last used': a reused lock is refreshed, so an endpoint
    that mints every few days keeps its file; and the caller's own path is
    never a candidate even if stale."""
    own = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    own.write_bytes(b"")
    os.utime(own, (OLD, OLD))
    _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    assert own.exists()
    assert own.stat().st_mtime > time.time() - 60

    os.utime(own, (OLD, OLD))
    assert _reap_stale_mint_locks(tmp_path, keep=own) == 0
    assert own.exists()


def test_scan_failure_never_reaches_the_mint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _lock_file(tmp_path, 1, mtime=OLD)
    real_scandir = os.scandir

    def boom(path: Any = ".") -> Any:
        if Path(path) == tmp_path:
            raise PermissionError("denied")
        return real_scandir(path)

    setattr_in(monkeypatch, data_token, "os.scandir", boom)
    with structlog.testing.capture_logs() as captured:
        assert _manager(tmp_path).bearer_for(BASE_URL, TENANT) == "tok-SECRET"
    assert "data_token_mint_lock_reap_scan_failed" in [e["event"] for e in captured]
    assert [e["log_level"] for e in captured if e["event"].startswith("data_token_mint_lock_reap")] == ["debug"]


def test_unlink_failure_never_reaches_the_mint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _lock_file(tmp_path, 1, mtime=OLD)
    real_unlink = os.unlink

    def boom(path: Any, *a: Any, **kw: Any) -> None:
        if Path(path).name.startswith(_MINT_LOCK_PREFIX):
            raise PermissionError("denied")
        real_unlink(path, *a, **kw)

    setattr_in(monkeypatch, data_token, "os.unlink", boom)
    with structlog.testing.capture_logs() as captured:
        assert _manager(tmp_path).bearer_for(BASE_URL, TENANT) == "tok-SECRET"
    assert stale.exists()
    failed = [e for e in captured if e["event"] == "data_token_mint_lock_reap_failed"]
    assert len(failed) == 1 and failed[0]["log_level"] == "debug"


def test_lease_family_is_not_reaped_by_the_mint(tmp_path: Path) -> None:
    """Pins the decision (see the reaper's docstring): lease files carry
    tokens and have no lock to probe, so the mint-time reap leaves old ones
    alone."""
    lease = tmp_path / f"data_token_lease.{_hex(7)}"
    lease.write_bytes(b"{}")
    os.utime(lease, (OLD, OLD))
    _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    assert lease.exists()
    assert _data_token_lease_path(BASE_URL, TENANT, tmp_path).exists()


# ── attribution (nexus-28bzt) ────────────────────────────────────────────


def test_first_creation_logs_one_attributing_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setattr("nexus.session._command_name_of", lambda pid: "fake-parent")
    with structlog.testing.capture_logs() as captured:
        _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    created = [e for e in captured if e["event"] == "data_token_mint_lock_created"]
    assert len(created) == 1
    ev = created[0]
    assert ev["log_level"] == "info"
    assert ev["pid"] == os.getpid()
    assert ev["ppid"] == os.getppid()
    assert ev["parent_name"] == "fake-parent"
    assert ev["nexus_config_dir"] == str(tmp_path / "cfg")
    assert ev["home_matches_pwd_home"] is False  # HOME was moved off the real home
    assert ev["endpoint"] == "127.0.0.1:9999"
    assert ev["lock_file"] == _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path).name
    # never the token or the credential, in any event of the mint
    dump = repr(captured)
    assert "tok-SECRET" not in dump and "mintcred" not in dump


def test_reused_lock_logs_no_creation_event(tmp_path: Path) -> None:
    _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    # a cold process: new manager, lease gone, lock file still there
    _data_token_lease_path(BASE_URL, TENANT, tmp_path).unlink()
    with structlog.testing.capture_logs() as captured:
        _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    assert "data_token_mint_lock_created" not in [e["event"] for e in captured]
    assert "data_token_minted" in [e["event"] for e in captured]


def test_home_matching_the_real_home_is_reported_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pwd = pytest.importorskip("pwd")
    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    with structlog.testing.capture_logs() as captured:
        _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    ev = next(e for e in captured if e["event"] == "data_token_mint_lock_created")
    assert ev["home_matches_pwd_home"] is True
