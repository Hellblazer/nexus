"""The native smoke scripts under service/, run for real (nexus-s5lfh, nexus-rc4pz).

The sibling native-smoke tests pin substrings of the scripts. These run them, with a stub
``docker`` and a short ``sleep`` first on PATH and a fake ``BIN`` in place of the native
binary, so the throwaway-container lifecycle and the boot-shape checks are exercised
without a native build or a Docker daemon. The stub docker records its argv, one call per
line. native-smoke.sh: the per-run container name, the loopback publish, the readiness
loop, and that every path that started a container also removes that same container.
native-boot-shapes-smoke.sh: each shape against an engine that answers as the published
engine-service-v0.1.164 linux-amd64 binary did on chas (2026-10-23, all five shapes PASS),
and against engines that each break one check.
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
  exec) case "$*" in *"CREATE DATABASE"*) exit "${CREATE_DB_RC:-0}" ;; *) exit "${PG_READY_RC:-0}" ;; esac ;;
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


_SCRIPTS = ["native-smoke.sh", "native-boot-shapes-smoke.sh", "linux-native-verify.sh", "trace-native.sh"]


@pytest.mark.parametrize("script", _SCRIPTS)
def test_every_docker_call_in_the_script_is_bounded(script: str) -> None:
    """nexus-ub4ge bounded native-smoke.sh's docker calls through _bounded (timeout/gtimeout); its
    not-ready rm -f, and every call in native-boot-shapes-smoke.sh, were left unbounded (nexus-s5lfh);
    the two hand-run spike scripts followed (nexus-rc4pz). An ``until`` loop over a docker call is an
    unbounded wait however each call is bounded, so none is allowed. Comment lines are not code."""
    code = [
        (n, line) for n, line in enumerate((SERVICE / script).read_text().splitlines(), 1)
        if not line.lstrip().startswith("#")
    ]
    calls = [(n, line) for n, line in code if re.search(r"(^|[\s;{(]|&&|\|\|)docker\s", line)]
    assert calls, "no docker call found; this test needs updating"  # non-vacuity
    unbounded = [(n, line.strip()) for n, line in calls if not re.search(r"_bounded\s+\d+\s+docker\s", line)]
    assert unbounded == [], unbounded
    waits = [(n, line.strip()) for n, line in code if re.search(r"\buntil\b.*\bdocker\b", line)]
    assert waits == [], waits


def _bounded_fn(script: str) -> str:
    m = re.search(r"^_bounded\(\) \{\n.*?^\}\n", (SERVICE / script).read_text(), re.MULTILINE | re.DOTALL)
    assert m, f"{script} defines no _bounded"
    return m.group(0)


@pytest.mark.parametrize("script", _SCRIPTS)
def test_bounded_ends_a_hung_command_and_lets_ctrl_c_through(script: str) -> None:
    """timeout without --foreground puts itself in its own process group, so a terminal's Ctrl-C
    never reaches the command it runs (the rc4pz review: a 2 h native build could not be
    interrupted). The SIGINT here goes to the script's whole process group, as a terminal sends it."""
    fn = _bounded_fn(script)
    if shutil.which("timeout") or shutil.which("gtimeout"):
        t0 = time.monotonic()
        out = subprocess.run(["bash", "-c", fn + "_bounded 1 sleep 30; echo rc=$?"], capture_output=True, text=True, timeout=20)
        assert out.stdout.strip() == "rc=124" and time.monotonic() - t0 < 10, out
    else:  # a vanilla Mac: the helper runs the command bare, by design
        out = subprocess.run(["bash", "-c", fn + "_bounded 1 sh -c 'exit 7'; echo rc=$?"], capture_output=True, text=True, timeout=20)
        assert out.stdout.strip() == "rc=7", out
    proc = subprocess.Popen(
        ["bash", "-c", fn + "_bounded 60 sleep 60\necho after"],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        time.sleep(0.5)
        t0 = time.monotonic()
        os.killpg(proc.pid, signal.SIGINT)
        stdout, _ = proc.communicate(timeout=20)
        assert time.monotonic() - t0 < 10 and "after" not in stdout, "Ctrl-C did not reach the bounded command"
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


# --- native-boot-shapes-smoke.sh (nexus-rc4pz) ---------------------------------------
#
# The script starts every boot under ``env -i``, so the fake engine learns nothing from the
# test's environment: its pid file and its defect are written into its header. It answers each
# shape as engine-service-v0.1.164 did on chas (2026-10-23): the presence line in the engine's
# own shape, then Voyage-only readiness (key, no bge files), the two-remedy refusal (neither),
# the size floor after extracting ONNX Runtime (a model of a few bytes), or ONNX Runtime
# refusing the model in createSession (a 250 MB model of zeros). Failures leave as the JVM's
# uncaught-exception trace on stderr, as the engine's do.

BOOT_SHAPES = SERVICE / "native-boot-shapes-smoke.sh"

_SHAPES_BODY = r'''
import http.server, json, os, signal, sys, time

signal.alarm(120)  # never outlive a run whose bash was killed before its EXIT trap ran
with open(PIDS, "a") as f:
    f.write(f"{os.getpid()} {os.environ['NX_DB_URL']}\n")


def log(line):
    print(line, flush=True)


def die(trace):
    print('Exception in thread "main" ' + trace, file=sys.stderr, flush=True)
    sys.exit(1)


bge = os.path.join(os.environ["NX_ONNX_MODEL_DIR"], "bge-base-en-v1.5", "onnx")
model, tok = os.path.join(bge, "model.onnx"), os.path.join(bge, "tokenizer.json")
present = os.path.exists(model) and os.path.exists(tok)
key = bool(os.environ.get("NX_VOYAGE_API_KEY"))
shown = "/opt/elsewhere/onnx" if DEFECT == "cloud-wrong-probe-path" else bge
state = lambda p: "present" if os.path.exists(p) else "absent"
log(f"event=embedder_presence bge_present={str(present).lower()} voyage_key_present={str(key).lower()} "
    f'bge="model={state(model)} path={shown}/model.onnx tokenizer={state(tok)} path={shown}/tokenizer.json"')
if DEFECT == "truncated-keyed-boots" and present and key:
    present = False  # a key lets the engine boot Voyage-only past a broken model (Sam's ruling: it must not)

if present:
    ort = os.environ["NX_ORT_NATIVE_DIR"]
    if DEFECT != "truncated-skips-extraction":
        log(f"event=ort_native_dir source=extracted dir={ort}/1.20.0-linux-x64")
        open(os.path.join(ort, "libonnxruntime.so"), "w").close()
    if DEFECT == "truncated-reads-as-absent":
        log("event=no_embedder_available")
    size = os.path.getsize(model)
    if size < 1_000_000:
        if DEFECT == "truncated-no-size-floor":
            die("java.lang.IllegalStateException: cannot load the bge model")
        die(f"java.lang.IllegalStateException: model at {model} is {size} bytes, far below the standard fp32 bge export")
    if DEFECT == "zero-hangs":
        time.sleep(600)
    if DEFECT == "zero-fails-before-session":
        die("java.lang.RuntimeException: Failed to initialise Bge768Embedder: tokenizer refused")
    if DEFECT == "zero-other-failure":
        die("java.lang.IllegalStateException: pool closed\n\tat ai.onnxruntime.OrtSession.createSession(Native Method)")
    die("java.lang.RuntimeException: Failed to initialise Bge768Embedder: Error code - ORT_INVALID_PROTOBUF - message: "
        f"Load model from {model} failed:Protobuf parsing failed.\n"
        "\tat dev.nexus.service.vectors.Bge768Embedder.<init>(Bge768Embedder.java:296)\n"
        "Caused by: ai.onnxruntime.OrtException: Error code - ORT_INVALID_PROTOBUF\n"
        "\tat ai.onnxruntime.OrtSession.createSession(Native Method)")

if not key:
    if DEFECT == "neither-crashes":
        os.kill(os.getpid(), signal.SIGSEGV)
    if DEFECT == "neither-boots":
        log("event=service_ready")
    remedy = {
        "neither-names-one-remedy": "event=no_embedder_available remedy: set NX_VOYAGE_API_KEY",
        "neither-drops-key-remedy": "event=no_embedder_available remedy: run nx init --service",
        "neither-no-event": "no embedder: set NX_VOYAGE_API_KEY or run nx init --service",
    }.get(DEFECT, "event=no_embedder_available remedy: set NX_VOYAGE_API_KEY or run nx init --service")
    log(remedy)
    if DEFECT == "neither-hangs":
        time.sleep(600)
    sys.exit(0 if DEFECT == "neither-exits-zero" else 1)

if DEFECT == "cloud-dies-before-ready":
    die("java.lang.IllegalStateException: Voyage client refused")
if DEFECT == "cloud-extracts-ort":
    log("event=ort_native_dir source=extracted")
if DEFECT == "cloud-fills-ort-dir":
    open(os.path.join(os.environ["NX_ORT_NATIVE_DIR"], "libonnxruntime.so"), "w").close()
if DEFECT == "cloud-builds-onnx-root":
    log("event=onnx_model_root root=/opt/elsewhere source=default")
if DEFECT == "cloud-ignores-sigterm":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
version = {"embedding_mode": "voyage", "embedding_models": ["voyage-context-3", "voyage-code-3"]}
if DEFECT == "cloud-reports-bge":
    version = {"embedding_mode": "local", "embedding_models": ["bge-base-en-v15-768"]}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(version).encode())

    def log_message(self, *args):
        pass


srv = http.server.HTTPServer(("127.0.0.1", int(os.environ["NX_SERVICE_PORT"])), H)
if DEFECT != "cloud-never-ready":
    log("event=service_ready")
srv.serve_forever()
'''

_SHAPES = ("cloud-shape", "neither-family", "truncated-model:keyless", "truncated-model:keyed", "zero-model")


class _ShapesRun:
    def __init__(self, proc: subprocess.CompletedProcess[str], calls: list[str], boots: list[tuple[int, str]],
                 leftover: list[int], scratch: Path) -> None:
        self.proc, self.calls, self.boots, self.leftover, self.scratch = proc, calls, boots, leftover, scratch
        self.out = proc.stdout + proc.stderr

    def verdicts(self) -> dict[str, tuple[str, list[str]]]:
        """shape -> (PASS|FAIL, the indented ``FAIL <check>`` lines printed for it)."""
        found: dict[str, tuple[str, list[str]]] = {}
        fails: list[str] = []
        for line in self.proc.stdout.splitlines():
            if line.startswith("    FAIL "):
                fails.append(line[len("    FAIL "):])
            elif m := re.match(r"^(PASS|FAIL) (\S+?)(?: \((keyless|keyed)\b|\s|$)", line):
                found[m.group(2) + (f":{m.group(3)}" if m.group(3) else "")] = (m.group(1), fails)
                fails = []
        return found

    def kept(self) -> list[Path]:
        return sorted(self.scratch.glob("native-boot-shapes.*"))


def _run_shapes(
    tmp_path: Path, *, defect: str = "", db_url: str | None = None, require_shapes: bool = False,
    borrowed_pg: tuple[str, str] | None = None, **docker_rc: int,
) -> _ShapesRun:
    stubs = tmp_path / "stubs"
    stubs.mkdir(parents=True)
    # 100 ms: the script's waits are 1 s loops (90 for readiness and for a failing boot's exit, 30
    # after SIGTERM); a tenth keeps a run to seconds and leaves a loaded CI worker 9 s to bind a port.
    for name, body in (("docker", _DOCKER), ("sleep", "#!/bin/sh\nexec /bin/sleep 0.1\n")):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    log, pids = tmp_path / "docker.log", tmp_path / "bin.pids"
    fake = tmp_path / "nexus-service"
    fake.write_text(f"#!/usr/bin/env python3\nDEFECT = {defect!r}\nPIDS = {str(pids)!r}\n{_SHAPES_BODY}")
    fake.chmod(0o755)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    base = {k: v for k, v in os.environ.items() if not k.startswith(("NX_DB_", "BOOT_SHAPES_", "NATIVE_SMOKE_"))}
    env = child_env_with_tmpdir(base, scratch)
    env.update(PATH=f"{stubs}{os.pathsep}{env.get('PATH', '')}", BIN=str(fake), DOCKER_LOG=str(log), NX_NO_TELEMETRY="1")
    env.update({k.upper(): str(v) for k, v in docker_rc.items()})
    if db_url is not None:
        env["NX_DB_URL"] = db_url
    if require_shapes:
        env["NATIVE_SMOKE_REQUIRE_SHAPES"] = "1"
    if borrowed_pg is not None:
        env["BOOT_SHAPES_PG_CONTAINER"], env["BOOT_SHAPES_PG_PORT"] = borrowed_pg
    boots: list[tuple[int, str]] = []
    try:
        proc = subprocess.run(["bash", str(BOOT_SHAPES)], cwd=SERVICE, env=env, capture_output=True, text=True, timeout=300)
    finally:
        if pids.exists():
            boots = [(int(pid), url) for pid, url in (line.split(" ", 1) for line in pids.read_text().splitlines())]
        leftover = [pid for pid, _ in boots if not _gone(pid, 1.0)]
        for pid in leftover:
            os.kill(pid, signal.SIGKILL)
    calls = log.read_text().splitlines() if log.exists() else []
    return _ShapesRun(proc, calls, boots, leftover, scratch)


def _own_container(calls: list[str]) -> tuple[str, str]:
    runs = [c for c in calls if c.startswith("run ")]
    assert len(runs) == 1, calls
    m = re.search(r"--name (nx-bootshapes-pg-\d+) .* -p 127\.0\.0\.1:(\d+):5432 ", runs[0])
    assert m, f"not named per run, or published beyond loopback: {runs[0]}"
    return m.group(1), m.group(2)


def _databases(calls: list[str], container: str) -> list[str]:
    return [m.group(1) for c in calls if (m := re.fullmatch(rf"exec {container} psql -U nexus -d nexus -c CREATE DATABASE (\w+);", c))]


def test_every_boot_shape_passes_against_an_engine_that_answers_as_the_published_binary_did(tmp_path: Path) -> None:
    run = _run_shapes(tmp_path)
    assert run.proc.returncode == 0, run.out
    assert run.verdicts() == {shape: ("PASS", []) for shape in _SHAPES}, run.out
    assert "BOOT SHAPES PASS" in run.out
    assert run.leftover == [], "every engine ended before the script did"
    # The throwaway pgvector: cleared first, started once on loopback, removed last; each boot gets
    # its own fresh database, and the engine is pointed at that database on the published port.
    name, port = _own_container(run.calls)
    assert run.calls[0] == f"rm -f {name}" and run.calls[-1] == f"rm -f {name}", run.calls
    dbs = _databases(run.calls, name)
    assert len(dbs) == len(_SHAPES) == len(set(dbs)), run.calls
    assert [url for _, url in run.boots] == [f"jdbc:postgresql://localhost:{port}/{db}" for db in dbs], run.boots
    assert run.kept() == [], "a passing run removes its scratch dir"


def test_a_borrowed_container_is_used_and_never_started_or_removed(tmp_path: Path) -> None:
    """native-smoke.sh hands the script its own pgvector (the release workflow's path)."""
    run = _run_shapes(tmp_path, borrowed_pg=("nx-smoke-pg-1-2", "5999"))
    assert run.proc.returncode == 0, run.out
    assert run.verdicts() == {shape: ("PASS", []) for shape in _SHAPES}, run.out
    assert not [c for c in run.calls if c.split()[0] in ("run", "rm")], run.calls
    dbs = _databases(run.calls, "nx-smoke-pg-1-2")
    assert [url for _, url in run.boots] == [f"jdbc:postgresql://localhost:5999/{db}" for db in dbs] and len(dbs) == 5, run.calls


_EXIT = ("exits within the bound", "exits non-zero, not a crash or a kill")


@pytest.mark.parametrize(
    ("defect", "fails"),
    [
        ("cloud-never-ready", {"cloud-shape": ["reaches service_ready"]}),
        ("cloud-dies-before-ready", {"cloud-shape": ["reaches service_ready"]}),
        ("cloud-wrong-probe-path", {"cloud-shape": ["probe looked in NX_ONNX_MODEL_DIR"]}),
        ("cloud-reports-bge", {"cloud-shape": ["/version reports embedding_mode voyage with Voyage models only"]}),
        ("cloud-extracts-ort", {"cloud-shape": ["no ort_native_dir event"]}),
        ("cloud-builds-onnx-root", {"cloud-shape": ["no onnx_model_root event"]}),
        ("cloud-fills-ort-dir", {"cloud-shape": ["NX_ORT_NATIVE_DIR stays empty"]}),
        ("cloud-ignores-sigterm", {"cloud-shape": ["SIGTERM exits 143"]}),
        ("neither-hangs", {"neither-family": list(_EXIT)}),
        ("neither-exits-zero", {"neither-family": [_EXIT[1]]}),
        ("neither-crashes", {"neither-family": [_EXIT[1], "logs event=no_embedder_available",
                                                 "names the Voyage-key remedy", "names the local-model remedy"]}),
        ("neither-boots", {"neither-family": ["never reaches service_ready"]}),
        ("neither-no-event", {"neither-family": ["logs event=no_embedder_available"]}),
        ("neither-drops-key-remedy", {"neither-family": ["names the Voyage-key remedy"]}),
        ("neither-names-one-remedy", {"neither-family": ["names the local-model remedy"]}),
        ("truncated-reads-as-absent", {leg: ["is not reported as absent"] for leg in ("truncated-model:keyless", "truncated-model:keyed")}),
        ("truncated-no-size-floor", {leg: ["names the size floor"] for leg in ("truncated-model:keyless", "truncated-model:keyed")}),
        ("truncated-skips-extraction", {
            "truncated-model:keyless": ["positive control: applyAtBoot logged", "positive control: NX_ORT_NATIVE_DIR was filled"],
            "truncated-model:keyed": ["positive control: applyAtBoot logged", "positive control: NX_ORT_NATIVE_DIR was filled"],
            "zero-model": ["ONNX Runtime extracted (applyAtBoot ran)"],
        }),
        ("truncated-keyed-boots", {"truncated-model:keyed": [
            *_EXIT, "never reaches service_ready", "positive control: applyAtBoot logged",
            "positive control: NX_ORT_NATIVE_DIR was filled", "names the size floor"]}),
        ("zero-hangs", {"zero-model": [*_EXIT, "reached createSession", "the failure is the embedder's"]}),
        ("zero-fails-before-session", {"zero-model": ["reached createSession"]}),
        ("zero-other-failure", {"zero-model": ["the failure is the embedder's"]}),
    ],
)
def test_each_check_fails_on_an_engine_that_breaks_it_and_nothing_else_does(
    tmp_path: Path, defect: str, fails: dict[str, list[str]],
) -> None:
    """One defect in the fake engine fails exactly the checks it breaks, in the shapes it reaches;
    every other shape still passes, so the fake started and the run went the whole way."""
    run = _run_shapes(tmp_path, defect=defect)
    assert run.proc.returncode == 1, run.out
    verdicts = run.verdicts()
    assert set(verdicts) == set(_SHAPES), run.out
    for shape in _SHAPES:
        want = fails.get(shape, [])
        status, got = verdicts[shape]
        assert status == ("FAIL" if want else "PASS"), (shape, run.out)
        assert len(got) == len(want) and all(any(g.startswith(w) for g in got) for w in want), (shape, got, want)
    assert "BOOT SHAPES FAIL" in run.out
    assert run.leftover == [], "the script ended every engine it started, the hung ones included"
    assert len(run.kept()) == 1 and "logs kept in" in run.out, "a failed run keeps its scratch dir and says where"


def test_a_container_that_cannot_start_or_never_gets_ready_stops_the_run_before_any_boot(tmp_path: Path) -> None:
    run = _run_shapes(tmp_path / "start", docker_run_rc=125)
    assert run.proc.returncode == 2 and "FAIL: cannot start the throwaway pgvector container" in run.out, run.out
    assert run.boots == []
    run = _run_shapes(tmp_path / "ready", pg_ready_rc=1)
    assert run.proc.returncode == 2 and "FAIL: pgvector never became ready" in run.out, run.out
    name, _ = _own_container(run.calls)
    assert run.calls[-1] == f"rm -f {name}" and run.boots == [], run.calls


def test_a_database_that_cannot_be_created_fails_every_shape_without_booting(tmp_path: Path) -> None:
    run = _run_shapes(tmp_path, create_db_rc=1)
    assert run.proc.returncode == 1, run.out
    assert {s: v[0] for s, v in run.verdicts().items()} == dict.fromkeys(_SHAPES, "FAIL"), run.out
    assert run.out.count("FAIL: cannot create database bootshape_") == len(_SHAPES)
    name, _ = _own_container(run.calls)
    assert run.calls[-1] == f"rm -f {name}" and run.boots == [], run.calls


def test_an_external_database_skips_the_shapes_loudly_and_fails_when_they_are_required(tmp_path: Path) -> None:
    url = "jdbc:postgresql://127.0.0.1:1/external"
    run = _run_shapes(tmp_path / "a", db_url=url)
    assert run.proc.returncode == 0, run.out
    assert "SKIP boot shapes" in run.proc.stdout and "NOT covered" in run.proc.stdout
    assert run.calls == [] and run.boots == []
    run = _run_shapes(tmp_path / "b", db_url=url, require_shapes=True)
    assert run.proc.returncode == 3, run.out
    assert "NATIVE_SMOKE_REQUIRE_SHAPES=1 and the boot shapes were skipped" in run.proc.stdout
    assert run.calls == [] and run.boots == []
