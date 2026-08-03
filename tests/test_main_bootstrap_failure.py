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

import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from tests._engine_substrate import ensure_engine

_REPO_ROOT = Path(__file__).resolve().parents[1]
_JAR = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"

#: Kept in step with TokenStore.ROOT_TOKEN_LABEL. Asserted against the DB below rather
#: than trusted: if the constant moves, the seed assertion fails loudly instead of this
#: file quietly testing an empty table.
_ROOT_LABEL = "bootstrap-legacy-token"

_BOOT_TIMEOUT_S = 90


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
    fh = open(log_path, "wb")  # noqa: SIM115 — closed by the caller after reaping
    return subprocess.Popen([java, "-jar", str(_JAR)], env=env, stdout=fh, stderr=fh), fh


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
    try:
        deadline = time.time() + _BOOT_TIMEOUT_S
        seeded = ""
        while time.time() < deadline:
            if proc1.poll() is not None:
                break
            try:
                seeded = _psql(
                    state,
                    f"SELECT count(*) FROM nexus.service_tokens WHERE label = '{_ROOT_LABEL}'",
                    dbname,
                )
            except AssertionError:
                seeded = ""          # schema not applied yet
            if seeded == "1":
                break
            time.sleep(1.0)
        assert seeded == "1", (
            f"first boot never seeded the root row (label {_ROOT_LABEL!r}); "
            f"log tail:\n{log1.read_text(errors='replace')[-1500:]}"
        )
    finally:
        proc1.terminate()
        try:
            proc1.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc1.kill()
        fh1.close()

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
    try:
        deadline = time.time() + _BOOT_TIMEOUT_S
        while time.time() < deadline:
            if proc1.poll() is not None:
                break
            try:
                if _psql(state,
                         f"SELECT count(*) FROM nexus.service_tokens WHERE label = '{_ROOT_LABEL}'",
                         dbname) == "1":
                    break
            except AssertionError:
                pass
            time.sleep(1.0)
    finally:
        proc1.terminate()
        try:
            proc1.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc1.kill()
        fh1.close()

    log2 = tmp_path / "r2.log"
    proc2, fh2 = _spawn_engine(state, dbname, "rotate-v2", log2)
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
    finally:
        proc2.terminate()
        try:
            proc2.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc2.kill()
        fh2.close()
