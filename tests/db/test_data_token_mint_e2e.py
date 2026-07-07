# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-868dq + nexus-x1h07 Task 6.3 — the full consumer journey, end to end:

    operator issues a mint-scoped credential via the ACTUAL consumer surface
    (``nx service token issue --scope mint``)
      -> the mint credential POSTs /v1/data-tokens/mint {tenant}
      -> the returned short-TTL data token authenticates on the data path
      -> the mint credential itself is REJECTED on the data path
         (AuthFilter confines it to the mint surface).

Real jar + hermetic Postgres 16 (the test_health_service_integration pattern);
jar freshness gated by ``jar_freshness_skip_reason`` so a stale jar skips loudly
instead of testing pre-change routes. EXACT assertions (== N / == status).

Run locally:
    uv run pytest -m integration tests/db/test_data_token_mint_e2e.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from click.testing import CliRunner

from tests.db._service_fixture import SERVICE_ROLES_SQL, jar_freshness_skip_reason

# ── Prerequisites (mirrors test_health_service_integration) ──────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR       = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN    = Path("/opt/homebrew/opt/postgresql@16/bin")

_INITDB   = _PG_BIN / "initdb"
_PG_CTL   = _PG_BIN / "pg_ctl"
_PSQL     = _PG_BIN / "psql"
_CREATEDB = _PG_BIN / "createdb"

_JAVA_HOME = os.environ.get("JAVA_HOME", "")
_JAVA = Path(_JAVA_HOME) / "bin" / "java" if _JAVA_HOME else Path(shutil.which("java") or "java")

_ALL_PREREQS = (
    _JAR.exists()
    and _INITDB.exists()
    and _PG_CTL.exists()
    and _PSQL.exists()
    and _CREATEDB.exists()
    and (_JAVA.exists() if _JAVA_HOME else shutil.which("java") is not None)
)

_JAR_STALE = jar_freshness_skip_reason()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _ALL_PREREQS,
        reason=(
            "skipped: missing jar or pg16 binaries "
            f"(jar={_JAR.exists()}, pg16={_PG_CTL.exists()}, java={_JAVA})"
        ),
    ),
    pytest.mark.skipif(bool(_JAR_STALE), reason=str(_JAR_STALE)),
]

_ROOT_TOKEN = "inttest-data-token-mint-root"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout: float = 30.0) -> None:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"port {port} on {host} not reachable after {timeout}s")


@pytest.fixture(scope="module")
def pg_instance():
    """Hermetic Postgres 16 cluster (trust auth)."""
    pgdata = tempfile.mkdtemp(prefix="nexus_dtmint_pg_")
    pg_port = _free_port()
    pglog = os.path.join(pgdata, "pg.log")
    pg_user = os.environ["USER"]
    try:
        subprocess.run(
            [str(_INITDB), "-D", pgdata, "--no-locale", "-E", "UTF8", "--auth=trust"],
            check=True, capture_output=True,
        )
        with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
            f.write(f"\nport = {pg_port}\nlisten_addresses = '127.0.0.1'\n")
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "-l", pglog,
             "-o", f"-p {pg_port} -k {pgdata}", "start", "-w"],
            check=True, capture_output=True,
        )
        subprocess.run(
            [str(_CREATEDB), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "nexustest"],
            check=True, capture_output=True,
        )
        proc = subprocess.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "-d", "nexustest",
             "-v", "ON_ERROR_STOP=1", "-c", SERVICE_ROLES_SQL],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"SERVICE_ROLES_SQL failed: {proc.stderr}")
        yield {"port": pg_port, "dbname": "nexustest", "user": pg_user}
    finally:
        subprocess.run([str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
                       capture_output=True)
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def service(pg_instance):
    """Real jar with Liquibase applied; NX_SERVICE_TOKEN seeds the root credential."""
    svc_port = _free_port()
    env = {
        **os.environ,
        "NX_SERVICE_PORT": str(svc_port),
        "NX_SERVICE_TOKEN": _ROOT_TOKEN,
        "NX_DB_URL": f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}/{pg_instance['dbname']}",
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "nexus_svc_pass",
        "NX_POOL_SIZE": "3",
        "NX_DB_ADMIN_URL":
            f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}/{pg_instance['dbname']}",
        "NX_DB_ADMIN_USER": pg_instance["user"],
        "NX_DB_ADMIN_PASS": "",
        "NX_CHROMA_PATH": tempfile.mkdtemp(prefix="nexus-dtmint-chroma-"),
    }
    env.pop("NX_STORAGE_BACKEND", None)
    proc = subprocess.Popen(
        [str(_JAVA), "-jar", str(_JAR)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    try:
        _wait_tcp("127.0.0.1", svc_port, timeout=30.0)
        yield {"port": svc_port, "token": _ROOT_TOKEN}
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


def _post(port: int, bearer: str, path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _get_status(port: int, bearer: str, path: str) -> int:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_cli_issues_mint_credential_then_full_data_token_journey(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CLI is the ACTUAL consumer surface for issuing the edge credential.
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", str(service["port"]))
    monkeypatch.setenv("NX_SERVICE_TOKEN", service["token"])
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)

    from nexus.commands.service_cmd import service as service_cli

    result = CliRunner().invoke(service_cli, [
        "token", "issue", "--tenant", "conexus-edge",
        "--label", "edge-mint-e2e", "--scope", "mint",
    ])
    assert result.exit_code == 0, result.output
    # The raw token is the LAST non-blank stdout line (_print_issued's echo
    # order — deliberate coupling to the human-readable CLI surface; a shifted
    # line would fail loud on the exact length assertion below, never grab a
    # wrong-but-plausible value).
    lines = [ln.strip() for ln in result.output.splitlines() if ln.strip()]
    mint_token = lines[-1]
    assert len(mint_token) == 43  # 32 urlsafe-base64 bytes, unpadded — exact, not >=

    # Mint a data token for a DIFFERENT tenant (pin: cross-tenant allowed).
    status, body = _post(service["port"], mint_token,
                         "/v1/data-tokens/mint", {"tenant": "acme"})
    assert status == 200, body
    data_token = body["data_token"]
    assert len(data_token) == 43
    assert body["expires_in_seconds"] == 300

    # The data token has REAL data-path authority for acme...
    assert _get_status(service["port"], data_token, "/v1/_whoami") == 200
    # ...while the mint credential itself has NONE (confined to the mint surface).
    assert _get_status(service["port"], mint_token, "/v1/_whoami") == 403
    # And the data token cannot self-replicate via the mint endpoint.
    status, _ = _post(service["port"], data_token,
                      "/v1/data-tokens/mint", {"tenant": "acme"})
    assert status == 403
