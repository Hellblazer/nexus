# SPDX-License-Identifier: AGPL-3.0-or-later
"""A SIGTERM during the real engine's model init must exit 143, never crash (nexus-o5xyx.1).

WHY THIS EXISTS. ``OrtInitGate`` is proven at the class level by the Java tests, but the
defect lived in ``Main``'s ordering: signal handlers installed FIRST, then ``new
Bge768Embedder()`` on the main thread. Only the real jar exercises that ordering and the
real ONNX Runtime. Before the fix, a SIGTERM landing in ``OrtSession`` creation made
onnxruntime-java's own shutdown hook free ORT's logging manager under the live init:
SIGSEGV, abort, ``rc == -6`` (exit 134 through a shell), an ``hs_err`` file. tests/
test_main_bootstrap_failure.py stops engines with ``terminate()`` and only required that
they stopped, so it passed through the crash (nexus-o5xyx).

HOW. Spawn the jar, wait until it logs ``onnx_model_root`` (Main is about to construct the
embedder), then SIGTERM at a sweep of offsets across the init window. Every run must exit
143. Non-vacuity: several runs must have LANDED in init, which the fixed engine proves by
logging ``ort_init_shutdown_wait`` (a pre-fix engine logs nothing there, so it fails this
guard even on offsets where it happened not to crash). A second case runs the same sweep
with the wait bound at zero, where the engine must halt with 143 instead (nexus-y6aj0).

MODEL-GATED. Needs the ~416MB bge model. Nothing provisioned: skip, loudly. Half
provisioned, or ``NX_REQUIRE_ORT_MODEL=1``: FAIL, so a skip cannot hide a regression on a
host that is meant to have it (see ``_require_model_or_skip``).

Self-contained on purpose (own spawn helper): tests/test_main_bootstrap_failure.py is
owned by another change and this must not couple to its internals.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from nexus.db.service_bge_model import service_bge_model_dir, service_bge_model_present
from tests._child_process import popen_in_group
from tests._engine_substrate import ensure_engine

_REPO_ROOT = Path(__file__).resolve().parents[1]
_JAR = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"

#: Seconds after the ``onnx_model_root`` log line. Bge session creation takes about half a
#: second warm; the crash window measured on hellmini sat 0.05 to 0.5 s into it.
_OFFSETS_S = (0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.7)

#: Runs in which the deferral must have engaged, or the sweep proved nothing.
_MIN_LANDED = 3

_BOOT_TIMEOUT_S = 90


def _require_model_or_skip() -> None:
    if service_bge_model_present():
        return
    d = service_bge_model_dir()
    if os.environ.get("NX_REQUIRE_ORT_MODEL") == "1":
        pytest.fail(f"NX_REQUIRE_ORT_MODEL=1 but the bge model is not complete at {d}")
    if d.is_dir():
        pytest.fail(
            f"the bge model directory {d} exists but is incomplete; a half-provisioned model must "
            "not turn this crash regression test into a silent skip"
        )
    pytest.skip(f"SKIPPED (not passed): bge model not provisioned at {d}")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _spawn(state: dict, dbname: str, log_path: Path, extra_env: dict[str, str]):
    java = shutil.which("java")
    assert java is not None, "no java on PATH"
    env = {
        **os.environ,
        "NX_SERVICE_PORT": str(_free_port()),
        "NX_SERVICE_TOKEN": "sigterm-" + uuid.uuid4().hex,
        "NX_DB_URL": f"jdbc:postgresql://127.0.0.1:{state['pg_port']}/{dbname}",
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "nexus_svc_pass",
        "NX_POOL_SIZE": "4",
        "NX_DB_ADMIN_URL": f"jdbc:postgresql://127.0.0.1:{state['pg_port']}/{dbname}",
        "NX_DB_ADMIN_USER": state["pg_user"],
        "NX_DB_ADMIN_PASS": "",
        **extra_env,
    }
    env.pop("NX_STORAGE_BACKEND", None)
    env.pop("NX_VOYAGE_API_KEY", None)  # local mode: bge loads at boot
    fh = open(log_path, "wb")  # noqa: SIM115 — closed by the caller after reaping
    # popen_in_group records the JVM for the crash sweep (nexus-qsqc6) and makes it a group leader, which the
    # sweep's group kill needs.
    return popen_in_group(
        [java, f"-XX:ErrorFile={log_path.parent}/hs_err_%p.log", "-jar", str(_JAR)],
        popen=subprocess.Popen, env=env, stdout=fh, stderr=fh,
    ), fh


#: (extra engine env, offsets, the log event that proves the sweep reached the path under
#: test). The second case sets the wait bound to zero, so it expires with init in flight on
#: every run that lands there: what a loaded host does to the default bound (nexus-y6aj0, a
#: 6-CPU cgroup with five suites running). The expiry must halt, never ``System.exit``.
#: Fewer offsets there: each one is an engine boot, and this is the slowest test in the suite.
_CASES = (
    pytest.param({}, _OFFSETS_S, "ort_init_shutdown_wait", id="init-fits-the-bound"),
    pytest.param(
        {"NX_ORT_INIT_SHUTDOWN_WAIT_MS": "0"}, (0.05, 0.1, 0.2, 0.3, 0.4),
        "ort_init_shutdown_halt", id="bound-expires",
    ),
)


@pytest.mark.needs_stamped_jar
@pytest.mark.parametrize(("extra_env", "offsets", "landed_event"), _CASES)
def test_sigterm_during_bge_init_exits_143_and_never_crashes(
    tmp_path: Path, extra_env: dict[str, str], offsets: tuple[float, ...], landed_event: str
) -> None:
    _require_model_or_skip()
    state = ensure_engine()
    bindir = Path(state["pg_bin"])
    landed = 0
    results: list[str] = []
    for offset in offsets:
        db = "nx_sigterm_" + uuid.uuid4().hex[:12]
        subprocess.run(
            [str(bindir / "createdb"), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
             "-U", state["pg_user"], db],
            check=True, capture_output=True, timeout=60,
        )
        log = tmp_path / f"boot-{offset}.log"
        proc, fh = _spawn(state, db, log, extra_env)
        try:
            deadline = time.time() + _BOOT_TIMEOUT_S
            while time.time() < deadline and "onnx_model_root" not in log.read_text(errors="replace"):
                assert proc.poll() is None, f"engine died before model init: {log.read_text(errors='replace')[-1500:]}"
                time.sleep(0.02)
            assert "onnx_model_root" in log.read_text(errors="replace"), "engine never reached model init"
            time.sleep(offset)
            proc.send_signal(signal.SIGTERM)
            try:
                rc = proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                pytest.fail(f"engine did not exit within 60 s of SIGTERM at +{offset}s")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            fh.close()
            subprocess.run(
                [str(bindir / "dropdb"), "--force", "-h", "127.0.0.1", "-p", str(state["pg_port"]),
                 "-U", state["pg_user"], db],
                capture_output=True, timeout=60,
            )
        text = log.read_text(errors="replace")
        waited = landed_event in text
        landed += waited
        results.append(f"+{offset}s rc={rc} waited={waited}")
        assert "A fatal error has been detected" not in text, (
            f"JVM crash banner after SIGTERM at +{offset}s:\n{text[-2500:]}"
        )
        assert rc == 143, (
            f"SIGTERM {offset}s after model init began: rc={rc}, want 143 (-6 is SIGABRT after the "
            f"ORT logging-manager SEGV, nexus-o5xyx.1). log tail:\n{text[-2500:]}"
        )
    assert landed >= _MIN_LANDED, (
        f"non-vacuity: SIGTERM must have landed inside model init ({landed_event} logged) on >= "
        f"{_MIN_LANDED} runs, got {landed}: {results}"
    )
