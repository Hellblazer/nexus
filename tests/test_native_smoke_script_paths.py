"""nexus-s5lfh: service/native-smoke.sh's start, readiness and cleanup paths, run for real.

The sibling native-smoke tests pin substrings of the script. These run it, with a stub
``docker`` and a no-op ``sleep`` first on PATH and a fake ``BIN`` in place of the native
binary, so the throwaway-container lifecycle is exercised without a native build or a
Docker daemon: the per-run container name, the loopback publish, the readiness loop,
and that every path that started a container also removes that same container. The
stub docker records its argv, one call per line.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests._tmp_root import child_env_with_tmpdir

SERVICE = Path(__file__).resolve().parent.parent / "service"
SMOKE = SERVICE / "native-smoke.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None or shutil.which("curl") is None,
    reason="the smoke script is bash and curls the engine",
)

_DOCKER = """#!/bin/sh
echo "$*" >> "$DOCKER_LOG"
case "$1" in
  run) exit "${DOCKER_RUN_RC:-0}" ;;
  exec) exit "${PG_READY_RC:-0}" ;;
  *) exit 0 ;;
esac
"""

# The fake engine: "exit" dies at once; "health" answers 200 {} to every GET, so the script
# gets past its health wait and fails at its first real assertion (no migration in /version).
_BIN = """#!/usr/bin/env python3
import http.server, os, signal, sys
signal.alarm(120)  # never outlive a run whose bash was killed before its EXIT trap ran
with open(os.environ["FAKE_BIN_PIDS"], "a") as f:
    f.write(f"{os.getpid()}\\n")
if os.environ["FAKE_BIN_MODE"] == "exit":
    sys.exit(3)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")
    def log_message(self, *args):
        pass
http.server.HTTPServer(("127.0.0.1", int(os.environ["NX_SERVICE_PORT"])), H).serve_forever()
"""


def _run(tmp_path: Path, *, mode: str, run_rc: int = 0, ready_rc: int = 0) -> tuple[subprocess.CompletedProcess[str], list[str], list[int]]:
    stubs = tmp_path / "stubs"
    stubs.mkdir(parents=True)
    # A 50 ms sleep, not none: the script's waits stay short but keep room for the fake engine to start.
    for name, body in (("docker", _DOCKER), ("sleep", "#!/bin/sh\nexec /bin/sleep 0.05\n")):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    fake = tmp_path / "nexus-service"
    fake.write_text(_BIN)
    fake.chmod(0o755)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    log, pids = tmp_path / "docker.log", tmp_path / "bin.pids"
    env = child_env_with_tmpdir({k: v for k, v in os.environ.items() if not k.startswith("NX_DB_")}, scratch)
    env.update(
        PATH=f"{stubs}{os.pathsep}{env.get('PATH', '')}", BIN=str(fake),
        DOCKER_LOG=str(log), DOCKER_RUN_RC=str(run_rc), PG_READY_RC=str(ready_rc),
        FAKE_BIN_MODE=mode, FAKE_BIN_PIDS=str(pids), NX_NO_TELEMETRY="1",
    )
    proc = subprocess.run(["bash", str(SMOKE)], cwd=SERVICE, env=env, capture_output=True, text=True, timeout=180)
    calls = log.read_text().splitlines() if log.exists() else []
    started = [int(p) for p in pids.read_text().split()] if pids.exists() else []
    return proc, calls, started


def _container(calls: list[str]) -> str:
    runs = [c for c in calls if c.startswith("run ")]
    assert len(runs) == 1, calls
    m = re.search(r"--name (nx-smoke-pg-\d+-\d+) ", runs[0])
    assert m, runs[0]
    assert re.search(r" -p 127\.0\.0\.1:\d+:5432 ", runs[0]), f"published beyond loopback: {runs[0]}"
    return m.group(1)


def _gone(pid: int, within: float = 2.0) -> bool:
    """True once ``pid`` has exited; the cleanup trap's kill is asynchronous."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_a_binary_that_dies_at_boot_fails_and_removes_its_container(tmp_path: Path) -> None:
    proc, calls, started = _run(tmp_path, mode="exit")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "FAIL: service exited during startup" in out
    name = _container(calls)
    assert f"exec {name} pg_isready -U nexus" in calls
    assert calls[-1] == f"rm -f {name}", calls
    assert len(started) == 1, "the binary ran once"
    assert "logs kept in" in out, "a failed run keeps its scratch dir and says where"


def test_a_binary_that_answers_health_is_killed_and_its_container_removed(tmp_path: Path) -> None:
    proc, calls, started = _run(tmp_path, mode="health")
    out = proc.stdout + proc.stderr
    try:
        assert proc.returncode == 1, out
        assert "FAIL: migration did not apply" in out, "past the health wait, at the first real assertion"
        name = _container(calls)
        assert calls[-1] == f"rm -f {name}", calls
        assert len(started) == 1 and _gone(started[0]), "the cleanup trap killed the engine"
    finally:
        for pid in started:
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)


def test_a_container_that_never_gets_ready_is_removed_before_the_binary_runs(tmp_path: Path) -> None:
    proc, calls, started = _run(tmp_path, mode="exit", ready_rc=1)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, out
    name = _container(calls)
    assert f"FAIL: throwaway pgvector {name} not ready after 90 s" in out
    assert calls.count(f"exec {name} pg_isready -U nexus") == 90
    assert calls[-1] == f"rm -f {name}", calls
    assert started == [], "the binary never ran"


def test_a_container_that_cannot_start_stops_the_run_before_the_binary(tmp_path: Path) -> None:
    proc, calls, started = _run(tmp_path, mode="exit", run_rc=125)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, out
    assert "FAIL: could not start the throwaway pgvector container nx-smoke-pg-" in out
    assert [c.split()[0] for c in calls] == ["run"], calls
    assert started == []


def test_two_runs_name_their_containers_apart(tmp_path: Path) -> None:
    names = {_container(_run(tmp_path / str(i), mode="exit")[1]) for i in range(2)}
    assert len(names) == 2, names


@pytest.mark.parametrize(
    "script", ["native-smoke.sh", "native-boot-shapes-smoke.sh", "linux-native-verify.sh", "trace-native.sh"],
)
def test_every_docker_call_in_the_script_is_bounded(script: str) -> None:
    """nexus-ub4ge bounded native-smoke.sh's docker calls through _bounded (timeout/gtimeout); its
    not-ready rm -f, and every call in native-boot-shapes-smoke.sh, were left unbounded (nexus-s5lfh);
    the two hand-run spike scripts followed (nexus-rc4pz).
    Comment lines are not code."""
    code = [
        (n, line) for n, line in enumerate((SERVICE / script).read_text().splitlines(), 1)
        if not line.lstrip().startswith("#")
    ]
    calls = [(n, line) for n, line in code if re.search(r"(^|[\s;{(]|&&|\|\|)docker\s", line)]
    assert calls, "no docker call found; this test needs updating"  # non-vacuity
    unbounded = [(n, line.strip()) for n, line in calls if not re.search(r"_bounded\s+\d+\s+docker\s", line)]
    assert unbounded == [], unbounded


# --- native-boot-shapes-smoke.sh (nexus-rc4pz) ---------------------------------------
#
# The boot-shapes script starts every boot under ``env -i``, so the fake engine learns
# nothing from the test's environment: its pid file and its defect are baked into its text.
# It answers each shape the way the real engine's probe does: the presence line, then
# Voyage-only readiness (key, no bge files), the two-remedy refusal (neither) or the
# size-floor refusal after extracting ONNX Runtime (both files present).

BOOT_SHAPES = SERVICE / "native-boot-shapes-smoke.sh"

_SHAPES_BIN = """#!/usr/bin/env python3
import http.server, json, os, signal, sys
signal.alarm(120)  # never outlive a run whose bash was killed before its EXIT trap ran
DEFECT = {defect!r}
with open({pids!r}, "a") as f:
    f.write(f"{{os.getpid()}}\\n")
def log(line):
    print(line, flush=True)
bge = os.path.join(os.environ["NX_ONNX_MODEL_DIR"], "bge-base-en-v1.5", "onnx")
model = os.path.join(bge, "model.onnx")
present = os.path.exists(model) and os.path.exists(os.path.join(bge, "tokenizer.json"))
key = bool(os.environ.get("NX_VOYAGE_API_KEY"))
log(f"event=embedder_presence bge_present={{str(present).lower()}} voyage_key_present={{str(key).lower()}} path={{bge}}/")
if present:
    ort = os.environ["NX_ORT_NATIVE_DIR"]
    if DEFECT != "truncated-skips-extraction":
        log(f"event=ort_native_dir source=extracted dir={{ort}}")
        open(os.path.join(ort, "libonnxruntime.so"), "w").close()
    if DEFECT == "truncated-reads-as-absent":
        log("event=no_embedder_available")
    else:
        log(f"model at {{model}} is {{os.path.getsize(model)}} bytes, far below the standard fp32 bge export")
    sys.exit(1)
if not key:
    remedies = "set NX_VOYAGE_API_KEY" if DEFECT == "neither-names-one-remedy" else "set NX_VOYAGE_API_KEY or run nx init"
    log(f"event=no_embedder_available remedy: {{remedies}}")
    sys.exit(1)
if DEFECT == "cloud-extracts-ort":
    log("event=ort_native_dir source=extracted")
if DEFECT == "cloud-ignores-sigterm":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({{"embedding_mode": "voyage", "embedding_models": ["voyage-context-3"]}}).encode())
    def log_message(self, *args):
        pass
srv = http.server.HTTPServer(("127.0.0.1", int(os.environ["NX_SERVICE_PORT"])), H)
log("event=service_ready")
srv.serve_forever()
"""


def _run_shapes(
    tmp_path: Path, *, defect: str = "", db_url: str | None = None, require_shapes: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str], list[int]]:
    stubs = tmp_path / "stubs"
    stubs.mkdir(parents=True)
    # 100 ms: the script's waits are 1 s loops (90 for readiness, 30 after SIGTERM); a tenth keeps
    # a run to seconds and leaves a loaded CI worker 9 s for the fake engine to bind its port.
    for name, body in (("docker", _DOCKER), ("sleep", "#!/bin/sh\nexec /bin/sleep 0.1\n")):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    log, pids = tmp_path / "docker.log", tmp_path / "bin.pids"
    fake = tmp_path / "nexus-service"
    fake.write_text(_SHAPES_BIN.format(defect=defect, pids=str(pids)))
    fake.chmod(0o755)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    base = {k: v for k, v in os.environ.items() if not k.startswith(("NX_DB_", "BOOT_SHAPES_", "NATIVE_SMOKE_"))}
    env = child_env_with_tmpdir(base, scratch)
    env.update(PATH=f"{stubs}{os.pathsep}{env.get('PATH', '')}", BIN=str(fake), DOCKER_LOG=str(log), NX_NO_TELEMETRY="1")
    if db_url is not None:
        env["NX_DB_URL"] = db_url
    if require_shapes:
        env["NATIVE_SMOKE_REQUIRE_SHAPES"] = "1"
    try:
        proc = subprocess.run(["bash", str(BOOT_SHAPES)], cwd=SERVICE, env=env, capture_output=True, text=True, timeout=180)
    finally:
        started = [int(p) for p in pids.read_text().split()] if pids.exists() else []
        for pid in started:
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls, started


def test_the_four_boot_shapes_pass_against_an_engine_that_answers_each_as_specified(tmp_path: Path) -> None:
    proc, calls, started = _run_shapes(tmp_path)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert len(re.findall(r"^PASS ", out, re.MULTILINE)) == 4, out
    for shape in ("PASS cloud-shape", "PASS neither-family", "PASS truncated-model (keyless", "PASS truncated-model (keyed"):
        assert shape in out, out
    assert "FAIL" not in out, out
    assert "BOOT SHAPES PASS" in out
    assert len(started) == 4, "one engine per boot"
    # The throwaway pgvector: cleared first, started once on loopback, one fresh database per boot,
    # removed last.
    runs = [c for c in calls if c.startswith("run ")]
    assert len(runs) == 1, calls
    m = re.search(r"--name (nx-bootshapes-pg-\d+) ", runs[0])
    assert m, runs[0]
    name = m.group(1)
    assert re.search(r" -p 127\.0\.0\.1:\d+:5432 ", runs[0]), runs[0]
    assert calls[0] == f"rm -f {name}" and calls[-1] == f"rm -f {name}", calls
    created = [c for c in calls if "CREATE DATABASE" in c]
    assert len(created) == 4 and len(set(created)) == 4, created


@pytest.mark.parametrize(
    ("defect", "failed_shape"),
    [
        ("cloud-extracts-ort", "FAIL cloud-shape"),
        ("cloud-ignores-sigterm", "FAIL cloud-shape"),
        ("neither-names-one-remedy", "FAIL neither-family"),
        ("truncated-reads-as-absent", "FAIL truncated-model (keyless"),
        ("truncated-skips-extraction", "FAIL truncated-model (keyless"),
    ],
)
def test_each_shape_fails_on_an_engine_that_breaks_its_contract(tmp_path: Path, defect: str, failed_shape: str) -> None:
    """The checks bite: one defect in the fake engine fails its shape and the run."""
    proc, _, _ = _run_shapes(tmp_path, defect=defect)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert failed_shape in out, out
    assert "BOOT SHAPES FAIL" in out
    assert "logs kept in" in out, "a failed run keeps its scratch dir and says where"


def test_an_external_database_skips_the_shapes_loudly_and_fails_when_they_are_required(tmp_path: Path) -> None:
    url = "jdbc:postgresql://127.0.0.1:1/external"
    proc, calls, started = _run_shapes(tmp_path / "a", db_url=url)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SKIP boot shapes" in proc.stdout and "NOT covered" in proc.stdout
    assert calls == [] and started == []
    proc, calls, started = _run_shapes(tmp_path / "b", db_url=url, require_shapes=True)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "NATIVE_SMOKE_REQUIRE_SHAPES=1 and the boot shapes were skipped" in proc.stdout
    assert calls == [] and started == []
