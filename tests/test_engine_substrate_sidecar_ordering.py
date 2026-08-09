# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sidecar-write ordering invariant (nexus-ui654 follow-up, critic Q3).

CENTRAL FINDING this pins: the "(b) startup orphan reap is already done by
nexus-lgdy1" claim (this bead's own first-pass report) was an OVERCLAIM.
Before this fix, ``_write_sidecar()`` was called only at the very END of
``_boot()`` -- AFTER createdb + role bootstrap + JVM spawn + up to 60s of
TCP-wait. A kill anywhere in that window left a SIDECAR-LESS cluster, which
``_sweep_legacy_cluster()`` explicitly and permanently refuses to
auto-reap (by design -- see that function's docstring). That is a live,
ongoing generator of exactly the un-reapable debris the bead was filed
about, not the "one-time, shrinking population" the pre-fix docstring
claimed.

This test drives ``_boot()`` itself (not just the sweep helpers) against
entirely FAKE process/subprocess machinery -- no real PG, no real JVM --
and asserts the sidecar file exists and already carries the postmaster
identity by the time ``_wait_tcp`` (the long-pole, up-to-60s wait) is
called. Written to FAIL against the pre-fix ordering first (see the
bead/commit history for the falsifying run), then made to pass by moving
the sidecar write in ``_boot()``.

    NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_engine_substrate_sidecar_ordering.py -q
"""
from __future__ import annotations

import contextlib
import json
import subprocess
import tempfile
from pathlib import Path

from tests import _engine_substrate as sub


class TestSidecarWrittenBeforeTcpWait:
    """Regression guard: the sidecar (or at minimum the postmaster-identity
    half of it) must exist BEFORE the up-to-60s ``_wait_tcp`` window, not
    only after the whole boot succeeds."""

    def test_sidecar_exists_with_postmaster_identity_before_wait_tcp(
        self, tmp_path, monkeypatch,
    ) -> None:
        # Redirect the substrate's cluster dir into tmp_path instead of the
        # real system tempdir -- fully hermetic, never touches a real or
        # concurrent session's clusters.
        real_mkdtemp = tempfile.mkdtemp
        created: list[Path] = []

        def _fake_mkdtemp(*, prefix):
            d = real_mkdtemp(prefix=prefix, dir=str(tmp_path))
            created.append(Path(d))
            return d

        monkeypatch.setattr(sub.tempfile, "mkdtemp", _fake_mkdtemp)
        # Never touch the box's real stray population, and never wait on
        # the real cross-process boot semaphore for this ordering-only test
        # (the semaphore itself has its own dedicated test file).
        monkeypatch.setattr(
            sub, "sweep_stale_substrate_clusters", lambda **_kw: sub.SweepResult(),
        )
        monkeypatch.setattr(sub, "_boot_semaphore_slot", contextlib.nullcontext)
        monkeypatch.setattr(sub, "jar_freshness_skip_reason", lambda *_a, **_kw: None)
        monkeypatch.setattr(sub, "_PG_BIN", tmp_path)  # any dir that exists

        def _fake_run(args, **_kwargs):
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(sub.subprocess, "run", _fake_run)

        class _FakePopen:
            def __init__(self, *_args, **_kwargs) -> None:
                self.pid = 424242

        monkeypatch.setattr(sub.subprocess, "Popen", _FakePopen)

        # _read_postmaster_pid reads a real postmaster.pid file our fake
        # pg_ctl never writes -- feed it a deterministic fake identity
        # instead of leaving it to resolve None, so this test can assert
        # on it landing in the early sidecar write.
        monkeypatch.setattr(sub, "_read_postmaster_pid", lambda _pgdata: 313131)

        observed: list[dict | None] = []

        def _fake_wait_tcp(_host, _port, timeout):
            assert created, "cluster dir must exist by the time _wait_tcp is called"
            sidecar_path = created[0] / sub._SIDECAR_FILENAME
            observed.append(
                json.loads(sidecar_path.read_text()) if sidecar_path.exists() else None,
            )

        monkeypatch.setattr(sub, "_wait_tcp", _fake_wait_tcp)

        sub._boot()

        assert observed and observed[0] is not None, (
            "REGRESSION GUARD (nexus-ui654 follow-up, critic Q3): the "
            "sidecar must exist BEFORE _wait_tcp's up-to-60s window -- a "
            "kill during that window used to leave a permanently "
            "un-reapable sidecar-less cluster (_sweep_legacy_cluster "
            "refuses to auto-reap those by design)."
        )
        assert observed[0]["postmaster_pid"] == 313131, (
            "the EARLY sidecar write must already carry the postmaster "
            "identity -- the one piece needed to reap a cluster killed "
            "before the engine ever starts"
        )

    def test_final_sidecar_carries_full_engine_identity_too(
        self, tmp_path, monkeypatch,
    ) -> None:
        """The two-checkpoint write must not regress the FINAL sidecar's
        completeness -- engine_pid/svc_port must still land once known,
        immediately at JVM spawn (not deferred to TCP-ready either)."""
        real_mkdtemp = tempfile.mkdtemp
        created: list[Path] = []

        def _fake_mkdtemp(*, prefix):
            d = real_mkdtemp(prefix=prefix, dir=str(tmp_path))
            created.append(Path(d))
            return d

        monkeypatch.setattr(sub.tempfile, "mkdtemp", _fake_mkdtemp)
        monkeypatch.setattr(
            sub, "sweep_stale_substrate_clusters", lambda **_kw: sub.SweepResult(),
        )
        monkeypatch.setattr(sub, "_boot_semaphore_slot", contextlib.nullcontext)
        monkeypatch.setattr(sub, "jar_freshness_skip_reason", lambda *_a, **_kw: None)
        monkeypatch.setattr(sub, "_PG_BIN", tmp_path)

        def _fake_run(args, **_kwargs):
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(sub.subprocess, "run", _fake_run)

        class _FakePopen:
            def __init__(self, *_args, **_kwargs) -> None:
                self.pid = 424242

        monkeypatch.setattr(sub.subprocess, "Popen", _FakePopen)
        monkeypatch.setattr(sub, "_read_postmaster_pid", lambda _pgdata: 313131)

        sidecar_at_wait_tcp: list[dict] = []

        def _fake_wait_tcp(_host, _port, timeout):
            sidecar_path = created[0] / sub._SIDECAR_FILENAME
            sidecar_at_wait_tcp.append(json.loads(sidecar_path.read_text()))

        monkeypatch.setattr(sub, "_wait_tcp", _fake_wait_tcp)

        sub._boot()

        # engine_pid/svc_port must already be present BY THE TIME
        # _wait_tcp runs -- Popen returns the pid synchronously, there is
        # no reason to defer recording it until TCP readiness.
        assert sidecar_at_wait_tcp, "the wait_tcp fake must have been called"
        assert sidecar_at_wait_tcp[0]["engine_pid"] == 424242, (
            "engine_pid is known synchronously at Popen() -- it must be "
            "recorded immediately, not deferred to _wait_tcp succeeding"
        )

        final_sidecar = json.loads((created[0] / sub._SIDECAR_FILENAME).read_text())
        assert final_sidecar["postmaster_pid"] == 313131
        assert final_sidecar["engine_pid"] == 424242
        assert final_sidecar["svc_port"] is not None
