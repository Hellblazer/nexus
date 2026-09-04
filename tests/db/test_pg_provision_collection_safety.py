# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-v460j: pytest collection must never depend on the network.

PR #1474 (2026-08-23): conftest imported ``tests._engine_substrate``, whose
module-level ``pg_bin_dir()`` hit a PG-bundle cache miss and downloaded the
bundle + sigstore attestation at COLLECTION time; one reset TCP connection
aborted the whole leg with zero tests run — including the ``-m lint`` leg,
which is configured to touch no substrate at all. Worse, the connection
reset surfaced as ``BinaryVerificationError`` (the download wrapper's type),
which the provisioning classifier's name check re-raised as if it were a
tamper signal.

Three pins:

1. ``tests/_engine_substrate.py`` performs NO module-level ``pg_bin_dir()``
   call (AST check — the collection-time network dependency stays gone).
2. The lazy resolver honours the import-time AMBIENT env snapshot, not
   whatever a test has monkeypatched at first-use time.
3. The provisioning classifier degrades ``BinaryDownloadError``
   (could-not-fetch: reset, 404, timeout) to the documented skip-sentinel,
   while a genuine verification failure still re-raises.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nexus.daemon.binary_install import BinaryDownloadError, BinaryVerificationError
from tests.db import _service_fixture as sf

_SUBSTRATE_SRC = Path(__file__).resolve().parents[1] / "_engine_substrate.py"


def test_engine_substrate_has_no_module_level_pg_resolution():
    """The import-time ``_PG_BIN = pg_bin_dir()`` shape must not return: it
    made every pytest leg's COLLECTION a network operation on a cold cache."""
    tree = ast.parse(_SUBSTRATE_SRC.read_text())
    offenders = [
        node.lineno
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Expr))
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "pg_bin_dir"
    ]
    assert not offenders, (
        f"module-level pg_bin_dir() call at lines {offenders} of "
        f"{_SUBSTRATE_SRC.name} — this downloads at collection time on a "
        "cold cache (nexus-v460j); resolve through _pg_bin() instead"
    )


def test_lazy_resolver_uses_the_import_time_ambient_env(monkeypatch, tmp_path):
    """First-use resolution must see the AMBIENT env captured at collection
    start, not a per-test monkeypatched one — the exact contract the old
    import-time resolution existed to provide."""
    from tests import _engine_substrate as es  # noqa: PLC0415 — deferred import, function-local by this file's convention

    ambient_bin = tmp_path / "ambient-pg" / "bin"
    ambient_bin.mkdir(parents=True)
    # An explicit NEXUS_PG_BIN must contain the four binaries discovery
    # validates (a SET-but-broken override re-raises by policy).
    for name in ("initdb", "pg_ctl", "psql", "createdb"):
        (ambient_bin / name).touch()
    monkeypatch.setitem(es._PG_AMBIENT_ENV, "NEXUS_PG_BIN", str(ambient_bin))
    # A test-time override that must NOT win:
    monkeypatch.setenv("NEXUS_PG_BIN", str(tmp_path / "test-time-override"))

    saved = es._pg_bin_resolved
    es._pg_bin_resolved = None
    try:
        resolved = es._pg_bin()
        assert resolved == ambient_bin
        # And the test-time env is restored untouched afterwards.
        import os  # noqa: PLC0415 — deferred import, function-local by this file's convention

        assert os.environ["NEXUS_PG_BIN"] == str(tmp_path / "test-time-override")
    finally:
        es._pg_bin_resolved = saved


def _cold_cache(monkeypatch, tmp_path):
    """Point the provisioning cache at an empty HOME so the download leg runs."""
    monkeypatch.setenv("HOME", str(tmp_path / "virgin-home"))


def test_download_failure_degrades_to_skip_sentinel(monkeypatch, tmp_path):
    """A could-not-fetch is an offline-box condition: warn + None (the callers
    then skip cleanly), never an aborted collection."""
    _cold_cache(monkeypatch, tmp_path)

    def _reset(*a, **k):
        raise BinaryDownloadError(
            "failed to download https://example.invalid/x.sigstore.json: "
            "<urlopen error [Errno 104] Connection reset by peer>"
        )

    monkeypatch.setattr("nexus.daemon.binary_install.install_pg_bundle", _reset)
    with pytest.warns(UserWarning, match="self-provisioning failed"):
        assert sf._self_provision_pg_bundle() is None


def test_verification_failure_still_re_raises(monkeypatch, tmp_path):
    """A genuine verification failure is a security signal — the carve-out for
    download errors must not widen past them."""
    _cold_cache(monkeypatch, tmp_path)

    def _tampered(*a, **k):
        raise BinaryVerificationError("sha256 mismatch: expected deadbeef, got cafebabe")

    monkeypatch.setattr("nexus.daemon.binary_install.install_pg_bundle", _tampered)
    with pytest.raises(BinaryVerificationError):
        sf._self_provision_pg_bundle()


# nexus-9rnfr: one provisioner per box, TUF refresh retried under the lock.


def _wire_fake_bundle(monkeypatch, tmp_path, install):
    """Route the fixture's product seams at a fake: *install* records calls and
    marks the cache provisioned; ``extracted_bin_dir`` reads that mark."""
    state = {"provisioned": False, "calls": 0}
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()

    def _install(tag, cache_dir):
        state["calls"] += 1
        install(state)
        state["provisioned"] = True

    monkeypatch.setattr("nexus.daemon.binary_install.install_pg_bundle", _install)
    monkeypatch.setattr(
        "nexus.db.pg_bundle.extracted_bin_dir",
        lambda cache_dir: bin_dir if state["provisioned"] else None,
    )
    monkeypatch.setattr("nexus.db.pg_bundle.ensure_pg_bundle", lambda cache_dir, search_dirs: bin_dir)
    return state, bin_dir


def test_concurrent_provisioners_download_once_and_share_the_result(monkeypatch, tmp_path):
    """Eight callers on a cold cache (the xdist shape): exactly one install,
    every caller gets the bundle. Threads open the lock file separately, so
    the flock they contend on is the cross-process one."""
    import threading  # noqa: PLC0415 — deferred import, function-local by this file's convention
    import time  # noqa: PLC0415 — deferred import, function-local by this file's convention

    from tests.db import _service_fixture as sf  # noqa: PLC0415 — deferred import, function-local by this file's convention

    _cold_cache(monkeypatch, tmp_path)
    state, bin_dir = _wire_fake_bundle(monkeypatch, tmp_path, lambda st: time.sleep(0.3))

    results: list[object] = []
    def _go():
        results.append(sf._self_provision_pg_bundle())
    threads = [threading.Thread(target=_go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert state["calls"] == 1, f"{state['calls']} downloads for one cold cache"
    assert results == [bin_dir] * 8


def test_tuf_refresh_failure_is_retried_then_succeeds(monkeypatch, tmp_path):
    from nexus.daemon.binary_install import BinaryVerificationError  # noqa: PLC0415 — deferred import, function-local by this file's convention
    from tests.db import _service_fixture as sf  # noqa: PLC0415 — deferred import, function-local by this file's convention

    _cold_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(sf, "_TUF_RETRY_BACKOFF_S", 0.0)

    def _flaky(st):
        if st["calls"] < 3:
            raise BinaryVerificationError(
                "signature verification failed for nexus-pg-mac-arm64.txz: Failed to refresh TUF metadata"
            )

    state, bin_dir = _wire_fake_bundle(monkeypatch, tmp_path, _flaky)
    assert sf._self_provision_pg_bundle() == bin_dir
    assert state["calls"] == 3


def test_a_real_verification_failure_is_not_retried(monkeypatch, tmp_path):
    from nexus.daemon.binary_install import BinaryVerificationError  # noqa: PLC0415 — deferred import, function-local by this file's convention
    from tests.db import _service_fixture as sf  # noqa: PLC0415 — deferred import, function-local by this file's convention

    _cold_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(sf, "_TUF_RETRY_BACKOFF_S", 0.0)

    def _tampered(st):
        raise BinaryVerificationError("signature verification failed: identity mismatch")

    state, _ = _wire_fake_bundle(monkeypatch, tmp_path, _tampered)
    with pytest.raises(BinaryVerificationError):
        sf._self_provision_pg_bundle()
    assert state["calls"] == 1


def test_kill_control_without_the_lock_every_caller_downloads(monkeypatch, tmp_path):
    """Falsifiability: with the lock a no-op, the same eight callers race the
    cold cache and several download — the shape nexus-9rnfr observed."""
    import threading  # noqa: PLC0415 — deferred import, function-local by this file's convention
    import time  # noqa: PLC0415 — deferred import, function-local by this file's convention

    from tests.db import _service_fixture as sf  # noqa: PLC0415 — deferred import, function-local by this file's convention

    _cold_cache(monkeypatch, tmp_path)
    monkeypatch.setattr("nexus._locking.lock_file", lambda fh, *, blocking: None)
    monkeypatch.setattr("nexus._locking.unlock_file", lambda fh: None)
    state, _ = _wire_fake_bundle(monkeypatch, tmp_path, lambda st: time.sleep(0.3))
    threads = [threading.Thread(target=sf._self_provision_pg_bundle) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert state["calls"] > 1, "the lock is what serialises provisioning; without it the race is back"
