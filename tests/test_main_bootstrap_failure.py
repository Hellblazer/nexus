# SPDX-License-Identifier: AGPL-3.0-or-later
"""Main's startup failure handlers must be DIAGNOSABLE, proven against the real jar.

WHY THIS EXISTS (nexus-9j8yw, from nexus-kjjab).
``Main.main`` is invoked by NO other test in either suite: every service test constructs
``NexusService`` directly, so the whole startup sequence — the Liquibase catch, the
root-token seeding catch, the PoolerModeCheck catch — is executed by nothing. Three of
those steps are FAILURE HANDLERS, and a handler no test executes is indistinguishable
from a wrong one.

That distinction is the entire severity of nexus-kjjab. The arbiter defect underneath it
was ordinary; what made it P0-ops was that it surfaced as a bare stack trace with HTTP
never bound, from the one code path every install runs. Fixing the arbiter and pinning it
at the ``TokenStore`` layer proves ``TokenStore``, NOT the boot — a test below the layer
production uses proves the layer, not the feature. This is the missing layer.

WHY PYTEST AND NOT JUNIT. ``service-ci`` is not a required check on develop or main
(nexus-hq9na), so a Java version of this would be advisory at merge — no gate at all for a
class whose whole point is that its failures are silent. ``pytest-gate`` IS required.
Same reasoning as tests/catalog/test_collection_scoped_tables_schema_parity.py.

WHY IT SPAWNS THE JAR. Extracting ``Main`` into a testable ``Bootstrap.run()`` would cover
more logic for less effort, but it cannot cover the EXIT PATH or the bare-JVM behaviour —
which is exactly where kjjab's severity lived. This asserts what actually ships: process
exit status, and whether the operator is told the remedy or handed a stack trace.

ISOLATION. Each test provisions its OWN database on the session substrate's Postgres, so
it never mutates ``service_tokens`` in the shared substrate other tests authenticate
against. The engine applies Liquibase to a fresh database on boot, so the database is
schema-complete without this test knowing the changelog.
"""

from __future__ import annotations

import ast
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import NamedTuple

import pytest

from tests._engine_substrate import engine_argv, ensure_engine
from tests.db._service_fixture import jvm_error_file_arg

_REPO_ROOT = Path(__file__).resolve().parents[1]
_JAR = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"

#: Kept in step with TokenStore.ROOT_TOKEN_LABEL. Asserted against the DB below rather
#: than trusted: if the constant moves, the seed assertion fails loudly instead of this
#: file quietly testing an empty table.
_ROOT_LABEL = "bootstrap-legacy-token"

_BOOT_TIMEOUT_S = 90

#: Main logs this once ``service.start()`` has returned, which is AFTER the ONNX model
#: sessions are created (Bge768Embedder / CrossEncoderReranker). It is the earliest point
#: at which SIGTERM cannot land inside ORT session init.
_READY_EVENT = "event=service_ready"

#: The only exits a READY engine may have after SIGTERM: 143 (128+15, the JVM's own
#: orderly shutdown), -15 (a runtime that exits by the signal itself) and 0. An allowlist
#: on purpose: SIGKILL after a hung shutdown (-9), SIGBUS (-10), SIGILL (-4) and every
#: other signal death must fail too, not just the two signals seen so far.
_CLEAN_STOP_RCS = frozenset({0, 143, -15})

#: Signal deaths that mean the JVM CRASHED: SIGABRT (-6 / 134, what the JVM raises after
#: handling a SIGSEGV) and SIGSEGV (-11 / 139). Used for the refused boot, whose exit is
#: non-zero by design, so an allowlist does not apply there.
_SIGNAL_CRASH_RCS = frozenset({-6, -11, 134, 139})

#: nexus-o5xyx.2. SIGTERM during startup, while the main thread is still inside
#: OrtSession creation, kills the JVM with a SEGV in ONNX Runtime's LoggingManager (exit
#: 134). It is invisible unless the exit status is asserted, because these tests only
#: needed the engine to STOP. Where an early kill is not the point, wait for readiness
#: first. Nothing in this file kills early on purpose; a test that does belongs with the
#: engine fix (nexus-o5xyx.1).


def _psql(state: dict, sql: str, dbname: str) -> str:
    psql = Path(state["pg_bin"]) / "psql"
    proc = subprocess.run(
        [str(psql), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", state["pg_user"], "-d", dbname, "-tAc", sql],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"psql failed: {sql}\n{proc.stderr}"
    return proc.stdout.strip()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _spawn_engine(state: dict, dbname: str, token: str, log_path: Path):
    """Start the real jar against *dbname*. Returns the Popen; caller must reap it."""
    java = shutil.which("java")
    assert java is not None, "no java on PATH"
    env = {
        **os.environ,
        "NX_SERVICE_PORT": str(_free_port()),
        "NX_SERVICE_TOKEN": token,
        "NX_DB_URL": f"jdbc:postgresql://127.0.0.1:{state['pg_port']}/{dbname}",
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "nexus_svc_pass",
        "NX_POOL_SIZE": "4",
        "NX_DB_ADMIN_URL": f"jdbc:postgresql://127.0.0.1:{state['pg_port']}/{dbname}",
        "NX_DB_ADMIN_USER": state["pg_user"],
        "NX_DB_ADMIN_PASS": "",
    }
    env.pop("NX_STORAGE_BACKEND", None)
    # The substrate's provisioned models. Without them a fresh runner (no models in the
    # default location) would never reach service_ready, and this file now waits for it.
    if state.get("onnx_root"):
        env["NX_ONNX_MODEL_DIR"] = state["onnx_root"]
    fh = open(log_path, "wb")  # noqa: SIM115 — closed by the caller after reaping
    # ErrorFile: a crash writes hs_err into the run's temp dir, never the repo cwd.
    argv = [java, jvm_error_file_arg(), "-jar", str(_JAR)]
    return subprocess.Popen(argv, env=env, stdout=fh, stderr=fh), fh


def _wait_ready(proc: subprocess.Popen, log_path: Path) -> bool:
    """True once the engine logged service_ready; False if it exited first or timed out."""
    deadline = time.time() + _BOOT_TIMEOUT_S
    while time.time() < deadline:
        if _READY_EVENT in log_path.read_text(errors="replace"):
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.25)
    return False


class _Stop(NamedTuple):
    """How an engine stop ended: its exit status, and whether SIGTERM was not enough."""

    rc: int | None
    sigkilled: bool


def _stop_engine(proc: subprocess.Popen, fh) -> _Stop:
    """SIGTERM, reap (SIGKILL after 30s), close the log."""
    proc.terminate()
    sigkilled = False
    try:
        rc = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        sigkilled = True
        proc.kill()
        rc = proc.wait()
    fh.close()
    return _Stop(rc, sigkilled)


def _assert_not_signal_crash(rc: int | None, log_path: Path, what: str) -> None:
    """For an exit that is non-zero by design (the refused boot): not a JVM crash."""
    assert rc not in _SIGNAL_CRASH_RCS, (
        f"{what}: the engine JVM CRASHED (exit {rc}), it did not shut down. "
        "134/-6 is SIGABRT after a SEGV (nexus-o5xyx: ONNX Runtime LoggingManager when "
        "SIGTERM lands during model init); 139/-11 is SIGSEGV. These tests pass on a "
        f"crash unless this is asserted. log tail:\n{log_path.read_text(errors='replace')[-1500:]}"
    )


def _assert_clean_stop(stop: _Stop | None, log_path: Path, what: str) -> None:
    """For a stop after service_ready: SIGTERM alone ended it, with an orderly status."""
    assert stop is not None, f"{what}: the engine was never stopped"
    assert not stop.sigkilled, (
        f"{what}: SIGTERM did not stop the engine within 30s and it had to be SIGKILLed "
        f"(exit {stop.rc}): a hung shutdown. log tail:\n"
        f"{log_path.read_text(errors='replace')[-1500:]}"
    )
    assert stop.rc in _CLEAN_STOP_RCS, (
        f"{what}: exit {stop.rc} is not an orderly SIGTERM shutdown "
        f"(expected one of {sorted(_CLEAN_STOP_RCS)}). 134/-6 and 139/-11 are JVM crashes "
        f"(nexus-o5xyx). log tail:\n{log_path.read_text(errors='replace')[-1500:]}"
    )


@pytest.fixture
def fresh_db(request: pytest.FixtureRequest) -> tuple[dict, str]:
    """A brand-new database on the substrate's PG, dropped afterwards."""
    state = ensure_engine()
    name = "nx_boot_" + uuid.uuid4().hex[:12]
    createdb = Path(state["pg_bin"]) / "createdb"
    subprocess.run(
        [str(createdb), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", state["pg_user"], name],
        check=True, capture_output=True, timeout=60,
    )

    def _drop() -> None:
        dropdb = Path(state["pg_bin"]) / "dropdb"
        subprocess.run(
            [str(dropdb), "--force", "-h", "127.0.0.1", "-p", str(state["pg_port"]),
             "-U", state["pg_user"], name],
            capture_output=True, timeout=60,
        )

    request.addfinalizer(_drop)
    return state, name


@pytest.mark.needs_stamped_jar
def test_revoked_root_slot_exits_nonzero_with_a_remedy_not_a_stack_trace(
    fresh_db: tuple[dict, str], tmp_path: Path,
) -> None:
    """The refusal path nexus-kjjab introduced must reach the operator DIAGNOSABLY.

    Boot once to apply Liquibase and seed the root token; revoke it; boot again with a
    DIFFERENT token. The second boot must refuse — and the assertion that matters is not
    merely 'it failed' but that it failed the way the two neighbouring startup checks fail:
    a logged error naming the remedy, then a non-zero exit. A bare stack trace here is the
    original kjjab defect wearing a different trigger.
    """
    state, dbname = fresh_db
    assert _JAR.exists(), f"gate jar missing at {_JAR}; run scripts/build-gate-jar.sh"

    # 1. First boot: applies the changelog and seeds the root row.
    log1 = tmp_path / "boot1.log"
    proc1, fh1 = _spawn_engine(state, dbname, "root-token-original", log1)
    stop1: _Stop | None = None
    try:
        # Wait for READY, not merely for the seed row: the seed lands before the ONNX
        # sessions are created, and a SIGTERM in that gap crashes the JVM (nexus-o5xyx.2).
        assert _wait_ready(proc1, log1), (
            f"first boot never reached service_ready; "
            f"log tail:\n{log1.read_text(errors='replace')[-1500:]}"
        )
        seeded = _psql(
            state,
            f"SELECT count(*) FROM nexus.service_tokens WHERE label = '{_ROOT_LABEL}'",
            dbname,
        )
        assert seeded == "1", (
            f"first boot never seeded the root row (label {_ROOT_LABEL!r}); "
            f"log tail:\n{log1.read_text(errors='replace')[-1500:]}"
        )
    finally:
        stop1 = _stop_engine(proc1, fh1)
    _assert_clean_stop(stop1, log1, "first boot, stopped after service_ready")

    # 2. Revoke it — the slot stays occupied, because the partial index has no
    #    revoked_at term. That is the arrangement the refusal exists for.
    _psql(state,
          f"UPDATE nexus.service_tokens SET revoked_at = now() WHERE label = '{_ROOT_LABEL}'",
          dbname)

    # 3. Second boot with a DIFFERENT token must refuse.
    log2 = tmp_path / "boot2.log"
    proc2, fh2 = _spawn_engine(state, dbname, "root-token-rotated", log2)
    try:
        rc = proc2.wait(timeout=_BOOT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc2.kill()
        fh2.close()
        pytest.fail(
            "engine did NOT exit on a revoked root slot — it must refuse, not bind. "
            f"log tail:\n{log2.read_text(errors='replace')[-2000:]}"
        )
    finally:
        fh2.close()

    out = log2.read_text(errors="replace")
    assert rc != 0, f"expected a non-zero exit, got {rc}. log tail:\n{out[-2000:]}"
    # A refusal is an orderly exit(1), not a crash: a non-zero status alone would
    # also accept a SIGABRT (134).
    _assert_not_signal_crash(rc, log2, "second boot (revoked root slot)")

    # THE POINT OF THE TEST. Not 'it failed' — that was true of the original defect too.
    # It must name the event and the remedy, the way the migration and pooler checks do.
    assert "root_token_seed_refused" in out, (
        "the refusal must be reported as a named event, not a bare stack trace — that "
        f"un-diagnosability IS the nexus-kjjab defect. log tail:\n{out[-2000:]}"
    )
    assert "REVOKED" in out, (
        f"the message must name WHY it refused so an operator can act. log tail:\n{out[-2000:]}"
    )


@pytest.mark.needs_stamped_jar
def test_rotating_the_provisioned_token_boots_cleanly(
    fresh_db: tuple[dict, str], tmp_path: Path,
) -> None:
    """NON-VACUITY, and the actual nexus-kjjab regression at the BOOT layer.

    The test above proves a refusal is diagnosable; it would still pass if the engine
    refused EVERY rotation, which is the pre-fix behaviour dressed up. This asserts the
    ordinary rotation — new NX_SERVICE_TOKEN, live incumbent — reaches a running service.
    Pre-fix this exited non-zero on an unhandled 23505.
    """
    state, dbname = fresh_db
    assert _JAR.exists(), f"gate jar missing at {_JAR}; run scripts/build-gate-jar.sh"

    log1 = tmp_path / "r1.log"
    proc1, fh1 = _spawn_engine(state, dbname, "rotate-v1", log1)
    stop1: _Stop | None = None
    try:
        assert _wait_ready(proc1, log1), (
            f"first boot never reached service_ready; "
            f"log tail:\n{log1.read_text(errors='replace')[-1500:]}"
        )
    finally:
        stop1 = _stop_engine(proc1, fh1)
    _assert_clean_stop(stop1, log1, "first boot, stopped after service_ready")

    log2 = tmp_path / "r2.log"
    proc2, fh2 = _spawn_engine(state, dbname, "rotate-v2", log2)
    stop2: _Stop | None = None
    try:
        deadline = time.time() + _BOOT_TIMEOUT_S
        rotated = False
        while time.time() < deadline:
            if proc2.poll() is not None:
                break
            hashes = _psql(
                state,
                f"SELECT count(*) FROM nexus.service_tokens WHERE label = '{_ROOT_LABEL}'",
                dbname)
            if hashes == "1" and "root_token_rotated" in log2.read_text(errors="replace"):
                rotated = True
                break
            time.sleep(1.0)

        assert proc2.poll() is None, (
            "a rotated NX_SERVICE_TOKEN must NOT abort startup — this is the nexus-kjjab "
            f"regression, at the layer it actually broke. log tail:\n"
            f"{log2.read_text(errors='replace')[-2000:]}"
        )
        assert rotated, (
            "expected the rotation to be logged as root_token_rotated; replacing the root "
            f"credential must be findable afterwards. log tail:\n"
            f"{log2.read_text(errors='replace')[-2000:]}"
        )
        # Exactly one root row survives — the single-root invariant the index exists for.
        assert _psql(
            state,
            f"SELECT count(*) FROM nexus.service_tokens WHERE label = '{_ROOT_LABEL}'",
            dbname) == "1"
        # root_token_rotated is logged before the ONNX sessions exist. Do not stop the
        # engine until it is ready, or the stop can crash it (nexus-o5xyx.2).
        assert _wait_ready(proc2, log2), (
            f"rotated boot never reached service_ready. log tail:\n"
            f"{log2.read_text(errors='replace')[-2000:]}"
        )
    finally:
        stop2 = _stop_engine(proc2, fh2)
    _assert_clean_stop(stop2, log2, "rotated boot, stopped after service_ready")


# ── The guards themselves (no jar, no substrate) ────────────────────────────────────
#
# The stop assertions above only mean something if they CAN fail. These feed them the exit
# statuses a crashed or hung JVM produces, through a stand-in for the Popen that
# _stop_engine reaps, so a regression that neutered a check (or dropped the ErrorFile
# flag) fails here on every run rather than only on a machine that happens to hit the race.


class _FakeProc:
    """Popen stand-in. ``hang`` makes the first bounded wait() time out, as a JVM stuck in
    shutdown does; kill() then lets it end with -9."""

    def __init__(self, rc: int, *, hang: bool = False, polls: list[int | None] | None = None) -> None:
        self._rc = rc
        self._hang = hang
        self._polls = list(polls or [])
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        if self._hang and not self.killed:
            raise subprocess.TimeoutExpired("java", timeout or 0)
        return -9 if self.killed else self._rc

    def poll(self) -> int | None:
        return self._polls.pop(0) if self._polls else None


class _FakeLog:
    def close(self) -> None:
        pass


@pytest.mark.parametrize("rc", [134, -6, 139, -11, -9, -10, -4, 1, 137])
def test_a_ready_engine_stop_must_be_an_orderly_exit(rc: int, tmp_path: Path) -> None:
    """Allowlist: 134 is the observed nexus-o5xyx crash, but SIGBUS, SIGILL and a plain
    non-zero exit after service_ready are failures too."""
    log = tmp_path / "engine.log"
    log.write_text("event=service_ready port=1\n")
    proc = _FakeProc(rc)
    stop = _stop_engine(proc, _FakeLog())
    assert proc.terminated and stop == _Stop(rc, False)
    with pytest.raises(AssertionError, match=rf"exit {rc} is not an orderly"):
        _assert_clean_stop(stop, log, "unit")


@pytest.mark.parametrize("rc", [0, 143, -15])
def test_an_orderly_ready_engine_stop_passes(rc: int, tmp_path: Path) -> None:
    log = tmp_path / "engine.log"
    log.write_text("")
    _assert_clean_stop(_stop_engine(_FakeProc(rc), _FakeLog()), log, "unit")


def test_a_hung_shutdown_that_needed_sigkill_fails(tmp_path: Path) -> None:
    """The 30s SIGTERM wait times out, the engine is SIGKILLed and reaps as -9. That is a
    hung shutdown, and it must be reported as one rather than as a mere odd exit code."""
    log = tmp_path / "engine.log"
    log.write_text("")
    proc = _FakeProc(143, hang=True)
    stop = _stop_engine(proc, _FakeLog())
    assert proc.terminated and proc.killed
    assert stop == _Stop(-9, True)
    with pytest.raises(AssertionError, match="SIGKILLed"):
        _assert_clean_stop(stop, log, "unit")


def test_a_missing_stop_result_fails(tmp_path: Path) -> None:
    log = tmp_path / "engine.log"
    log.write_text("")
    with pytest.raises(AssertionError, match="never stopped"):
        _assert_clean_stop(None, log, "unit")


@pytest.mark.parametrize("rc", [134, -6, 139, -11])
def test_a_signal_crash_fails_the_refused_boot_check(rc: int, tmp_path: Path) -> None:
    log = tmp_path / "engine.log"
    log.write_text("")
    with pytest.raises(AssertionError, match=rf"CRASHED \(exit {rc}\)"):
        _assert_not_signal_crash(rc, log, "unit")


@pytest.mark.parametrize("rc", [1, 2, 143])
def test_an_orderly_refusal_passes_the_refused_boot_check(rc: int, tmp_path: Path) -> None:
    log = tmp_path / "engine.log"
    log.write_text("")
    _assert_not_signal_crash(rc, log, "unit")


# ── _wait_ready ──────────────────────────────────────────────────────────────


def test_wait_ready_returns_true_once_the_ready_event_is_logged(tmp_path: Path) -> None:
    log = tmp_path / "e.log"
    log.write_text("boot\nevent=service_ready port=7\n")
    assert _wait_ready(_FakeProc(0), log) is True


def test_wait_ready_ready_event_wins_over_a_later_exit(tmp_path: Path) -> None:
    """The log is checked before the process: an engine that logged ready and then exited
    is still 'ready' (the stop assertions then judge the exit)."""
    log = tmp_path / "e.log"
    log.write_text("event=service_ready\n")
    assert _wait_ready(_FakeProc(0, polls=[1]), log) is True


def test_wait_ready_returns_false_when_the_engine_exits_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "e.log"
    log.write_text("booting\n")
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    assert _wait_ready(_FakeProc(1, polls=[None, None, 1]), log) is False


def test_wait_ready_returns_false_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "e.log"
    log.write_text("booting\n")
    monkeypatch.setattr(sys.modules[__name__], "_BOOT_TIMEOUT_S", 0.05)
    assert _wait_ready(_FakeProc(0), log) is False


# ── The jar-launching tests really apply the stop checks ─────────────────────


def _called_names(fn: ast.FunctionDef) -> set[str]:
    return {
        c.func.id for c in ast.walk(fn)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }


def test_the_jar_tests_wait_for_ready_and_assert_the_stop() -> None:
    """The stop assertions protect nothing if a test stops calling them. Pin, from this
    file's own source, that every jar-spawning test waits for readiness and asserts a
    clean stop, and that the refused-boot test asserts it did not crash."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    jar_tests = {
        f.name: f for f in ast.walk(tree)
        if isinstance(f, ast.FunctionDef) and f.name.startswith("test_")
        and "_spawn_engine" in _called_names(f)
    }
    assert set(jar_tests) == {
        "test_revoked_root_slot_exits_nonzero_with_a_remedy_not_a_stack_trace",
        "test_rotating_the_provisioned_token_boots_cleanly",
    }, sorted(jar_tests)
    for name, fn in jar_tests.items():
        calls = _called_names(fn)
        assert "_wait_ready" in calls, f"{name} must wait for service_ready before stopping"
        assert "_assert_clean_stop" in calls, f"{name} must assert its stop was orderly"
        assert "_stop_engine" in calls, name
    refused = jar_tests["test_revoked_root_slot_exits_nonzero_with_a_remedy_not_a_stack_trace"]
    assert "_assert_not_signal_crash" in _called_names(refused)


# ── Launch argv ──────────────────────────────────────────────────────────────


def _capture_spawn(state: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list, dict]:
    seen: dict = {}

    class _Popen:
        def __init__(self, argv, **kwargs) -> None:
            seen["argv"] = argv
            seen["env"] = kwargs["env"]

    monkeypatch.setattr(subprocess, "Popen", _Popen)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/java")
    _spawn_engine(state, "db", "tok", tmp_path / "e.log")[1].close()
    return seen["argv"], seen["env"]


def test_the_engine_spawn_redirects_hs_err_out_of_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hs_err must land in the run's temp dir, not the JVM's cwd (the repo)."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    argv, _env = _capture_spawn({"pg_port": 1, "pg_user": "u"}, tmp_path, monkeypatch)
    flag = f"-XX:ErrorFile={tmp_path}/hs_err_%p.log"
    assert flag in argv, argv
    assert argv.index(flag) < argv.index("-jar"), (
        "JVM options must precede -jar or they become program arguments"
    )


def test_the_engine_spawn_gets_the_substrates_onnx_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The test now waits for service_ready, which needs the models: hand the engine the
    substrate's provisioned dir, and leave the variable alone when there is none."""
    monkeypatch.delenv("NX_ONNX_MODEL_DIR", raising=False)
    _argv, env = _capture_spawn(
        {"pg_port": 1, "pg_user": "u", "onnx_root": "/models/onnx"}, tmp_path, monkeypatch,
    )
    assert env["NX_ONNX_MODEL_DIR"] == "/models/onnx"
    _argv, env = _capture_spawn(
        {"pg_port": 1, "pg_user": "u", "onnx_root": None}, tmp_path, monkeypatch,
    )
    assert "NX_ONNX_MODEL_DIR" not in env


def test_the_substrate_engine_argv_redirects_hs_err(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    argv = engine_argv("/usr/bin/java")
    flag = f"-XX:ErrorFile={tmp_path}/hs_err_%p.log"
    assert flag in argv, argv
    assert argv.index(flag) < argv.index("-jar")
