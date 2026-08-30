# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit-suite coverage for service/native-smoke.sh's real-Python-client probes
(nexus-cm5km).

The two probe files under ``service/smoke-probes/`` (``t1_real_client.py``,
``t2_real_client.py``) are extracted verbatim from what used to be inline
``uv run python -c '...'`` heredocs in ``service/native-smoke.sh``. Before
this extraction, the ONLY execution of that Python ever happened either
(a) against a real GraalVM native binary during a release build, or
(b) inside the pre-tag ``--shakeout`` rehearsal image, where both blocks
self-skip (no ``pyproject.toml`` there — ``rehearse_shakeout.sh:400-420``)
because ``uv``/the repo checkout aren't present. So a plain Python bug in
either block — like ``HttpPlanLibrary.save_plan`` losing its required
``verb=`` kwarg after hygiene-001 (b9ab65606) — had NO pre-tag gate at all:
it burned engine-service-v0.1.90 on both linux release legs (762b9d7ba fixed
the immediate break). Running the identical files here, against the same
self-provisioned engine substrate every other test in this suite already
uses, closes that gap: a regression in either probe now fails on every
``pytest -n auto`` run, not just on a release cut.

These tests deliberately do NOT hand-roll their own service env. Every test
in this suite already gets a session-scoped hermetic PG + service JAR with a
freshly minted tenant/token via the autouse ``_pin_t2_substrate`` fixture
(which sets ``NX_SERVICE_URL`` / ``NX_SERVICE_TOKEN`` / ``NX_LOCAL=1``) and a
freshly minted, PG-backed T1 session via the autouse ``_isolate_t1_sessions``
fixture (which sets ``NX_T1_SESSION`` / ``NX_T1_SESSION_ID``) — see
tests/conftest.py. A subprocess spawned with no explicit ``env=`` override
inherits ``os.environ``, so it inherits both by construction (the same
inheritance path ``test_config_dir_isolation.py::test_subprocess_inherits_
redirected_path`` already relies on). ``NEXUS_CONFIG_DIR`` is likewise
already redirected under this test's ``tmp_path`` by the autouse
``_isolate_config_dir`` fixture, so the probe subprocess never touches a
real ``~/.config/nexus/``.

This diverges from native-smoke.sh's own env wiring (which explicitly sets
``NX_SERVICE_URL=''`` + ``NX_SERVICE_HOST``/``NX_SERVICE_PORT`` because it
boots a throwaway native binary at a freshly chosen port with no ambient
substrate to inherit from). Here the ambient substrate IS the fixture
contract every other test already depends on, and ``resolve_service_endpoint()``
accepts ``NX_SERVICE_URL``/``NX_SERVICE_TOKEN`` as a first-class resolution
path (see ``nexus/db/t2/_refreshable_client.py``), so there is nothing to
override.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_PROBE_DIR = REPO_ROOT / "service" / "smoke-probes"
_T1_PROBE = _PROBE_DIR / "t1_real_client.py"
_T2_PROBE = _PROBE_DIR / "t2_real_client.py"

#: Same bound native-smoke.sh applies via its own $TIMEOUT_CMD (60s).
_PROBE_TIMEOUT_S = 60


def _run_probe(probe: Path, *, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run *probe* as a real subprocess, inheriting the current (fixture-
    provisioned) environment plus *extra_env*.

    No ``cwd`` override is needed: ``sys.executable`` inside a ``uv run
    pytest`` process already resolves to the venv interpreter with ``nexus``
    importable, so the probe's ``from nexus.db... import ...`` lines work
    unconditionally, unlike native-smoke.sh's ``uv run python`` invocation
    (which must ``cd`` to the repo root for ``uv`` to find ``pyproject.toml``).
    """
    env = os.environ.copy()
    env.update(extra_env)
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, str(probe)],
        env=env,
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT_S,
    )


def _assert_probe_ok(result: subprocess.CompletedProcess[str]) -> None:
    combined = result.stdout + result.stderr
    output_lines = combined.splitlines()
    assert result.returncode == 0 and "OK" in output_lines, (
        f"probe subprocess did not report OK (exit={result.returncode}):\n"
        f"{combined}"
    )


class TestT1RealClientProbe:
    """service/smoke-probes/t1_real_client.py against the engine substrate."""

    def test_t1_probe_reports_ok(self) -> None:
        assert _T1_PROBE.is_file(), f"probe file missing: {_T1_PROBE}"
        result = _run_probe(
            _T1_PROBE,
            extra_env={
                "NX_STORAGE_BACKEND": "service",
                "NATIVE_SMOKE_CLEANUP_ROWS": "1",
            },
        )
        _assert_probe_ok(result)


class TestT2RealClientProbe:
    """service/smoke-probes/t2_real_client.py against the engine substrate."""

    def test_t2_probe_reports_ok(self) -> None:
        assert _T2_PROBE.is_file(), f"probe file missing: {_T2_PROBE}"
        result = _run_probe(
            _T2_PROBE,
            extra_env={"NATIVE_SMOKE_CLEANUP_ROWS": "1"},
        )
        _assert_probe_ok(result)
