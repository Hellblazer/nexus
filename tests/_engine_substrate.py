# SPDX-License-Identifier: AGPL-3.0-or-later
"""Session-scoped engine-backed T2 test substrate (RDR-155 P4b P0a').

Decision D-A (nexus-g37fr, 2026-07-23): the unit suite's T2 substrate is
the REAL engine over the bundled PG — integration-over-mocks and
PG-in-every-mode, applied to the test suite itself. ONE hermetic
PG + one shaded-JAR service boot per pytest session; per-test isolation
comes from a freshly MINTED tenant + tenant-bound token per test: the
engine binds tenant to the BEARER server-side (AuthFilter Decision 1 —
the ``X-Nexus-Tenant`` header is IGNORED), so handing each test its own
token isolates every row via RLS with no sharing and no cleanup.

Laziness contract: nothing boots at import. ``ensure_engine()`` is
memoized; the conftest autouse fixture calls it only when the collected
session actually imported ``nexus.db.t2`` (test modules import at
collection time, so ``sys.modules`` is a correct static signal). A
pure-unit dev run never pays the ~10s boot.

Fail-loud contract (gates-scripted-not-ambient): a missing or stale JAR
FAILS the tests that need the substrate with the build command — never a
silent mass-skip (the vacuous-green class).
"""
from __future__ import annotations

import atexit
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from tests.db._service_fixture import (
    SERVICE_ROLES_SQL,
    jar_freshness_skip_reason,
    pg_bin_dir,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_JAR = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"

_BEARER = "t2-substrate-session-bearer"
_DBNAME = "nexus_t2_substrate"

_lock = threading.Lock()
_state: dict | None = None
_boot_error: str | None = None

#: Resolved at MODULE IMPORT time — conftest imports this module at
#: collection start, before any per-test HOME/NEXUS_CONFIG_DIR
#: monkeypatching, so the bundle-leg discovery sees the AMBIENT config
#: dir (same contract as tests/db/_service_fixture.pg_bin_dir's own
#: import-time resolution note).
_PG_BIN = pg_bin_dir()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"engine substrate: port {port} not reachable after {timeout}s")


def _boot() -> dict:
    """Boot hermetic PG + the shaded service JAR. Called once, under _lock."""
    stale = jar_freshness_skip_reason(_JAR)
    if stale:
        raise RuntimeError(
            f"T2 engine substrate unavailable: {stale}. The unit suite's T2 "
            "substrate is the real engine (RDR-155 P4b P0a', decision D-A) — "
            "build it with: mvn -f service/pom.xml package -DskipTests"
        )
    bin_dir = _PG_BIN
    if not bin_dir.exists():
        raise RuntimeError(
            "T2 engine substrate unavailable: no PostgreSQL binaries "
            "discoverable (NEXUS_PG_BIN / config-dir bundle / Homebrew / "
            "PATH). Install the PG bundle (nx init) or set NEXUS_PG_BIN."
        )

    pgdata = tempfile.mkdtemp(prefix="nexus_t2_substrate_pg_")
    pg_port = _free_port()
    pg_user = os.environ["USER"]
    subprocess.run(
        [str(bin_dir / "initdb"), "-D", pgdata, "--no-locale", "-E", "UTF8",
         "--auth=trust"],
        check=True, capture_output=True,
    )
    with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
        f.write(f"\nport = {pg_port}\nlisten_addresses = '127.0.0.1'\n")
        # The suite issues thousands of tiny transactions; keep fsync off
        # for the throwaway test cluster.
        f.write("fsync = off\nsynchronous_commit = off\nfull_page_writes = off\n")
    subprocess.run(
        [str(bin_dir / "pg_ctl"), "-D", pgdata, "-l",
         os.path.join(pgdata, "pg.log"),
         "-o", f"-p {pg_port} -k {pgdata}", "start", "-w"],
        check=True, capture_output=True,
    )
    def _kill_pg() -> None:
        # Review finding (P0 remainder, Important 1): any failure after
        # pg_ctl start must stop PG before re-raising, or repeated failed
        # boots accumulate zombie postgres + tempdirs (the exact leak
        # class observed live during the flip dry-runs).
        subprocess.run(
            [str(bin_dir / "pg_ctl"), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)

    try:
        subprocess.run(
            [str(bin_dir / "createdb"), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, _DBNAME],
            check=True, capture_output=True,
        )
        proc = subprocess.run(
            [str(bin_dir / "psql"), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "-d", _DBNAME, "-v", "ON_ERROR_STOP=1",
             "-c", SERVICE_ROLES_SQL],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"T2 engine substrate: role bootstrap failed:\n{proc.stderr}"
            )
    except BaseException:
        _kill_pg()
        raise

    svc_port = _free_port()
    env = {
        **os.environ,
        "NX_SERVICE_PORT": str(svc_port),
        "NX_SERVICE_TOKEN": _BEARER,
        "NX_DB_URL": f"jdbc:postgresql://127.0.0.1:{pg_port}/{_DBNAME}",
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "nexus_svc_pass",
        "NX_POOL_SIZE": "8",
        "NX_DB_ADMIN_URL": f"jdbc:postgresql://127.0.0.1:{pg_port}/{_DBNAME}",
        "NX_DB_ADMIN_USER": pg_user,
        "NX_DB_ADMIN_PASS": "",
    }
    env.pop("NX_STORAGE_BACKEND", None)
    java = shutil.which("java")
    if java is None:
        _kill_pg()
        raise RuntimeError("T2 engine substrate: no java on PATH")
    svc = subprocess.Popen(
        [java, "-jar", str(_JAR)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    try:
        _wait_tcp("127.0.0.1", svc_port, timeout=60.0)
    except TimeoutError:
        try:
            os.killpg(os.getpgid(svc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        out = svc.stderr.read().decode(errors="replace")[-2000:] if svc.stderr else ""
        _kill_pg()
        raise RuntimeError(
            f"T2 engine substrate: service did not bind port {svc_port}. "
            f"Tail of stderr:\n{out}"
        ) from None

    state = {
        "base_url": f"http://127.0.0.1:{svc_port}",
        "bearer": _BEARER,
        "pgdata": pgdata,
        "pg_bin": bin_dir,
        "svc": svc,
    }
    atexit.register(_teardown)
    return state


def _teardown() -> None:
    global _state
    if _state is None:
        return
    svc = _state["svc"]
    try:
        os.killpg(os.getpgid(svc.pid), signal.SIGTERM)
        svc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(svc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    subprocess.run(
        [str(_state["pg_bin"] / "pg_ctl"), "-D", _state["pgdata"],
         "stop", "-m", "immediate"],
        capture_output=True,
    )
    shutil.rmtree(_state["pgdata"], ignore_errors=True)
    _state = None


def ensure_engine() -> dict:
    """Return the live substrate state, booting once per process.

    Raises RuntimeError (fail-loud, with remedy) when the JAR or PG
    binaries are missing — a prior boot failure is remembered and
    re-raised immediately so one broken prerequisite doesn't retry the
    boot for thousands of tests.
    """
    global _state, _boot_error
    with _lock:
        if _boot_error is not None:
            raise RuntimeError(_boot_error)
        if _state is None:
            try:
                _state = _boot()
            except Exception as exc:
                _boot_error = str(exc)
                raise
        return _state


_mint_counter = 0


def mint_test_tenant(state: dict) -> tuple[str, str]:
    """Mint a fresh tenant + its first bound token via /v1/tenants/create.

    The boot bearer (the engine's NX_SERVICE_TOKEN root) authorizes the
    admin surface; the returned token is strictly bound to the new
    tenant, which IS the per-test isolation boundary.
    """
    global _mint_counter
    import httpx

    with _lock:
        _mint_counter += 1
        name = f"t{os.getpid()}-{_mint_counter}"
    # 60s + one retry: the dry-run sweep observed intermittent >10s
    # /v1/tenants/create latency after a few hundred mints in one engine
    # (recorded on nexus-g37fr as an engine observation — the bandaid
    # keeps the suite honest about WHAT failed, not silently flaky).
    resp = None
    last_exc: Exception | None = None
    for _attempt in range(2):
        try:
            resp = httpx.post(
                f"{state['base_url']}/v1/tenants/create",
                json={"name": name},
                headers={"Authorization": f"Bearer {state['bearer']}",
                         "Content-Type": "application/json"},
                timeout=60.0,
            )
            break
        except httpx.TimeoutException as exc:
            last_exc = exc
    if resp is None:
        raise RuntimeError(
            f"T2 engine substrate: tenant mint timed out twice: {last_exc}"
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"T2 engine substrate: tenant mint failed "
            f"({resp.status_code}): {resp.text[:300]}"
        )
    body = resp.json()
    token = body.get("token")
    if not token:
        raise RuntimeError(f"tenant mint returned no token: {body}")
    return name, token
