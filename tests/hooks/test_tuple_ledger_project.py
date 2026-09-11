# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-205 Phase 2 Step 3 (bead nexus-em75s.11): the async ledger-tuple
projection body.

Stdlib-only mirror of ``nexus.db.data_token``'s cross-process lease-file
format (same pattern as ``tests/hooks/test_t2_prefix_scan.py``'s
data-token-lease tests) and of ``nexus.daemon.service_registry``'s
``storage_service_addr.<uid>`` discovery file. Pins:

- NO HOOK MINTS ANYTHING: a missing or near-expiry data-token lease is a
  SKIP (logged), never a fall-back to a static/mint-locked token and
  never a mint call.
- The wire shape matches ``HttpTupleStore.out`` posting to
  ``/v1/tuples/out`` against the ``ledger/<session_id>`` template:
  ``{"subspace": "ledger/<sid>", "keys": {"agent_id", "kind"}, "dims":
  {"agent_type"}}``.
- Engine up / down / 429 (RDR-205 Phase 2 Step 3's own "what this step
  measures"): every path exits 0 and logs its outcome to
  ``<session_id>.tuple-projection.log`` beside the session's
  ``.expectations`` ledger; nothing here ever raises or blocks.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus"
    / "hooks"
    / "scripts"
    / "tuple_ledger_project.py"
)

SESSION_ID = "sess-tuple-proj"
AGENT_ID = "aworker1234567890abcdef"
AGENT_TYPE = "developer"


def _payload(**overrides: str) -> str:
    base = {
        "session_id": SESSION_ID,
        "hook_event_name": "SubagentStart",
        "agent_id": AGENT_ID,
        "agent_type": AGENT_TYPE,
    }
    base.update(overrides)
    return json.dumps(base)


def _log_path(state_dir: Path) -> Path:
    return state_dir / "nexus" / "orchestration" / f"{SESSION_ID}.tuple-projection.log"


def _run(
    kind: str,
    *,
    tmp_path: Path,
    stdin: str = "",
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    env = {k: v for k, v in os.environ.items() if not k.startswith("NX_SERVICE_")}
    env["NEXUS_CONFIG_DIR"] = str(config_dir)
    env["XDG_STATE_HOME"] = str(state_dir)
    env.update(env_overrides or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), kind],
        input=stdin or _payload(),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


# ── Lease-file writers (mirrors nexus.db.data_token / ServiceRegistry) ──────


def _write_storage_lease(
    config_dir: Path,
    *,
    host: str,
    port: int,
    status: str = "live",
    heartbeat_age_s: float = 0.0,
    ttl: float = 30.0,
) -> None:
    record = {
        "scope_key": str(os.getuid()),
        "generation": 1,
        "owner_token": "test-owner",
        "heartbeat_epoch": time.time() - heartbeat_age_s,
        "ttl": ttl,
        "endpoint": {"host": host, "port": port, "token": "static-mint-locked"},
        "version": "test",
        "payload": {},
        "status": status,
        "format_version": 1,
    }
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / f"storage_service_addr.{os.getuid()}").write_text(json.dumps(record))


def _write_config_yml(config_dir: Path, credentials: dict[str, str]) -> None:
    """Hand-written ``config.yml`` matching ``nexus.config.set_config_value``'s
    actual on-disk shape (nexus-0zsmg) -- verified against a live
    ``~/.config/nexus/config.yml``: a zero-indent ``credentials:`` block
    with each key at a fixed 2-space indent, bare (unquoted) scalar
    values. This is the shape ``_read_persisted_service_url`` is a narrow
    mirror of, not a general YAML writer.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    lines = ["credentials:"]
    for k, v in credentials.items():
        lines.append(f"  {k}: {v}")
    (config_dir / "config.yml").write_text("\n".join(lines) + "\n")


def _data_token_digest(base_url: str, tenant: str) -> str:
    from urllib.parse import urlsplit

    host = urlsplit(base_url).netloc or base_url
    return hashlib.sha256(f"{host}\x00{tenant}".encode("utf-8")).hexdigest()


def _write_data_token_lease(
    config_dir: Path,
    *,
    base_url: str,
    token: str,
    tenant: str = "default",
    ttl_seconds: float = 3600.0,
    remaining_s: float = 3600.0,
) -> None:
    digest = _data_token_digest(base_url, tenant)
    record = {
        "format_version": 1,
        "token": token,
        "tenant": tenant,
        "base_url_digest": digest,
        "expires_at": time.time() + remaining_s,
        "ttl_seconds": ttl_seconds,
        "minted_by_pid": os.getpid(),
    }
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / f"data_token_lease.{digest}").write_text(json.dumps(record))


# ── Mock /v1/tuples/out engine ──────────────────────────────────────────────


class _MockTupleEngine:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[dict] = []
        self.auth_headers: list[str] = []
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                engine.auth_headers.append(self.headers.get("Authorization", ""))
                try:
                    engine.requests.append(json.loads(body.decode("utf-8")))
                except json.JSONDecodeError:
                    engine.requests.append({})
                if self.path != "/v1/tuples/out":
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(engine.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def mock_engine():
    engines: list[_MockTupleEngine] = []

    def make(status: int = 200) -> _MockTupleEngine:
        e = _MockTupleEngine(status=status)
        engines.append(e)
        return e

    yield make
    for e in engines:
        e.close()


# ── Tests ────────────────────────────────────────────────────────────────


def test_missing_lease_skips_and_logs(tmp_path: Path) -> None:
    """No storage lease, no env, no data-token lease -- SKIP, exit 0,
    reason logged beside the ledger."""
    proc = _run("start", tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    log = _log_path(tmp_path / "state")
    assert log.exists()
    content = log.read_text()
    assert "SKIP kind=start" in content
    assert AGENT_ID in content


def test_fresh_lease_posts_the_ledger_start_tuple(tmp_path: Path, mock_engine) -> None:
    """A fresh data-token lease + a live engine: the tuple lands with the
    exact RDR-205 ledger.yaml wire shape."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    proc = _run(
        "start",
        tmp_path=tmp_path,
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1
    body = engine.requests[0]
    assert body["subspace"] == f"ledger/{SESSION_ID}"
    assert body["keys"] == {"agent_id": AGENT_ID, "kind": "start"}
    assert body["dims"] == {"agent_type": AGENT_TYPE}
    assert engine.auth_headers[0] == "Bearer fresh-data-token"
    # No skip should have been logged on the happy path.
    log = _log_path(tmp_path / "state")
    assert not log.exists() or "SKIP" not in log.read_text()


def test_report_kind_posts_kind_report(tmp_path: Path, mock_engine) -> None:
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    proc = _run(
        "report",
        tmp_path=tmp_path,
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert engine.requests[0]["keys"] == {"agent_id": AGENT_ID, "kind": "report"}


def test_near_expiry_lease_is_treated_as_absent(tmp_path: Path, mock_engine) -> None:
    """Remaining TTL at/under the 20% threshold must SKIP, not present a
    borderline-stale bearer."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(
        config_dir, base_url=engine.base_url, token="stale-soon",
        ttl_seconds=3600.0, remaining_s=100.0,  # 100s << 3600*0.20 = 720s
    )

    proc = _run(
        "start",
        tmp_path=tmp_path,
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert engine.requests == []
    log = _log_path(tmp_path / "state")
    assert "SKIP kind=start" in log.read_text()
    assert "no fresh data-token lease" in log.read_text()


def test_expired_lease_is_treated_as_absent(tmp_path: Path, mock_engine) -> None:
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(
        config_dir, base_url=engine.base_url, token="expired",
        ttl_seconds=3600.0, remaining_s=-10.0,
    )
    proc = _run("start", tmp_path=tmp_path, env_overrides={"NX_SERVICE_URL": engine.base_url})
    assert proc.returncode == 0, proc.stderr
    assert engine.requests == []


def test_lease_for_a_different_host_is_ignored(tmp_path: Path, mock_engine) -> None:
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url="http://127.0.0.1:1", token="wrong-host")
    proc = _run("start", tmp_path=tmp_path, env_overrides={"NX_SERVICE_URL": engine.base_url})
    assert proc.returncode == 0, proc.stderr
    assert engine.requests == []


def test_two_leases_for_one_host_picks_the_resolved_tenant(tmp_path: Path, mock_engine) -> None:
    """nexus-em75s.12 review fix: lease selection filters on the resolved
    tenant ("default", matching HttpTupleStore's DEFAULT_TENANT), not
    only on host-digest self-consistency. A fresher, longer-lived lease
    for a DIFFERENT tenant on the same host must never win -- before the
    fix it would, because the digest is recomputed from the same file's
    own tenant field and so always self-matches."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    # Fresher and longer-lived, but the wrong tenant -- must be ignored.
    _write_data_token_lease(
        config_dir, base_url=engine.base_url, token="wrong-tenant-token",
        tenant="other-tenant", ttl_seconds=7200.0, remaining_s=7200.0,
    )
    # Shorter remaining TTL, but the resolved ("default") tenant -- must win.
    _write_data_token_lease(
        config_dir, base_url=engine.base_url, token="right-tenant-token",
        tenant="default", ttl_seconds=3600.0, remaining_s=3600.0,
    )

    proc = _run("start", tmp_path=tmp_path, env_overrides={"NX_SERVICE_URL": engine.base_url})
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1
    assert engine.auth_headers[0] == "Bearer right-tenant-token"


def test_resolves_host_port_from_storage_service_lease(tmp_path: Path, mock_engine) -> None:
    """No NX_SERVICE_* env at all -- host/port come from the storage
    lease, exactly like every other T2/T3 client's local-supervisor leg."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    from urllib.parse import urlsplit

    parsed = urlsplit(engine.base_url)
    _write_storage_lease(config_dir, host=parsed.hostname, port=parsed.port)
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="via-lease")

    proc = _run("start", tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1
    assert engine.auth_headers[0] == "Bearer via-lease"


def test_local_supervisor_with_no_data_token_lease_falls_back_to_its_own_token(
    tmp_path: Path, mock_engine
) -> None:
    """nexus-g2lln: the exact dead-on-every-local-install shape proven by
    the 7.41.0 shakeout (T2
    ``nexus/shakeout-7.41.0-projector-local-install-proof-2026-09-11``) --
    a local supervisor lease, NO data-token lease at all (the default
    local install has no ``mint_token`` configured, so no client ever
    writes one). Must fall back to the storage lease's own ``endpoint.
    token`` field -- the same static credential the real local client
    presents on this box -- and post successfully, no SKIP."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    from urllib.parse import urlsplit

    parsed = urlsplit(engine.base_url)
    _write_storage_lease(config_dir, host=parsed.hostname, port=parsed.port)
    lease_path = config_dir / f"storage_service_addr.{os.getuid()}"
    os.chmod(lease_path, 0o600)
    # No data-token lease written at all.

    proc = _run("start", tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1
    assert engine.auth_headers[0] == "Bearer static-mint-locked"
    log = _log_path(tmp_path / "state")
    assert not log.exists() or "SKIP" not in log.read_text()


def test_local_supervisor_with_stale_data_token_lease_falls_back_to_its_own_token(
    tmp_path: Path, mock_engine
) -> None:
    """A data-token lease exists but is past the near-expiry threshold --
    treated as absent, exactly like the existing near-expiry pin -- and
    the local supervisor endpoint still falls back to its own token."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    from urllib.parse import urlsplit

    parsed = urlsplit(engine.base_url)
    _write_storage_lease(config_dir, host=parsed.hostname, port=parsed.port)
    os.chmod(config_dir / f"storage_service_addr.{os.getuid()}", 0o600)
    _write_data_token_lease(
        config_dir, base_url=engine.base_url, token="stale-data-token",
        ttl_seconds=3600.0, remaining_s=100.0,  # well within the 20% near-expiry band
    )

    proc = _run("start", tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1
    assert engine.auth_headers[0] == "Bearer static-mint-locked"


def test_local_supervisor_token_refused_when_lease_file_is_group_or_world_readable(
    tmp_path: Path, mock_engine
) -> None:
    """nexus-g2lln: the fallback token authorizes real engine writes, so a
    lease file another local account could read must never be trusted as
    its source -- SKIP, and the reason names the permission problem."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    from urllib.parse import urlsplit

    parsed = urlsplit(engine.base_url)
    _write_storage_lease(config_dir, host=parsed.hostname, port=parsed.port)
    lease_path = config_dir / f"storage_service_addr.{os.getuid()}"
    os.chmod(lease_path, 0o644)  # group/other-readable -- must be refused

    proc = _run("start", tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert engine.requests == []
    log = _log_path(tmp_path / "state")
    content = log.read_text()
    assert "SKIP kind=start" in content
    assert "group/other-accessible" in content


def test_managed_endpoint_never_falls_back_to_a_local_lease_token(
    tmp_path: Path, mock_engine
) -> None:
    """nexus-em75s.12's guarantee must survive nexus-g2lln's fallback: a
    MANAGED endpoint (``service_url`` resolved) with no fresh data-token
    lease must SKIP even when a live, owner-only local supervisor lease
    with a usable token happens to also exist on the same box (e.g. a
    dev box running both a local supervisor and pointed at a managed
    service_url for testing) -- the local static token is never an
    acceptable substitute for a managed-endpoint bearer."""
    engine = mock_engine(status=200)
    managed_engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    from urllib.parse import urlsplit

    parsed = urlsplit(engine.base_url)
    _write_storage_lease(config_dir, host=parsed.hostname, port=parsed.port)
    os.chmod(config_dir / f"storage_service_addr.{os.getuid()}", 0o600)
    # No data-token lease for the managed endpoint.

    proc = _run(
        "start", tmp_path=tmp_path, env_overrides={"NX_SERVICE_URL": managed_engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert engine.requests == []
    assert managed_engine.requests == []
    log = _log_path(tmp_path / "state")
    assert "SKIP kind=start" in log.read_text()
    assert "no fresh data-token lease" in log.read_text()


def test_cloud_mode_persisted_service_url_resolves_with_no_env_and_no_lease(
    tmp_path: Path, mock_engine
) -> None:
    """nexus-0zsmg: the exact cloud-mode shape -- a managed-service
    ``config.yml`` (``credentials.service_url`` persisted by ``nx config
    set``, no ``NX_SERVICE_URL``/``NX_SERVICE_PORT`` env, no local
    supervisor lease at all) must still resolve the endpoint and post,
    matching ``nexus.db.service_endpoint.resolve_service_endpoint``'s own
    precedence. Before the fix this always fell through to 'no service
    endpoint resolvable' on a real cloud-mode box."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_config_yml(config_dir, {"service_url": engine.base_url, "service_token": "irrelevant-here"})
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="cloud-data-token")

    proc = _run("start", tmp_path=tmp_path)  # NO env_overrides -- config.yml only
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1
    assert engine.auth_headers[0] == "Bearer cloud-data-token"
    log = _log_path(tmp_path / "state")
    assert not log.exists() or "SKIP" not in log.read_text()


def test_no_env_no_persisted_url_no_lease_names_persisted_config_in_skip(tmp_path: Path) -> None:
    """The absence case: a config.yml exists (some unrelated credential
    persisted) but carries no ``service_url``, and there is no env and no
    local lease -- SKIP, and the reason names the persisted-config leg
    that was checked, not just the env/lease legs."""
    config_dir = tmp_path / "config"
    _write_config_yml(config_dir, {"voyage_api_key": "unrelated"})

    proc = _run("start", tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    log = _log_path(tmp_path / "state")
    content = log.read_text()
    assert "SKIP kind=start" in content
    assert "persisted config.yml service_url" in content


def test_env_service_url_wins_over_persisted_config_service_url(tmp_path: Path, mock_engine) -> None:
    """Precedence: NX_SERVICE_URL env must win over a DIFFERENT persisted
    config.yml service_url, exactly like
    ``nexus.config.get_credential``'s env-first contract -- a stale or
    wrong persisted value must never override an explicit env pin."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_config_yml(config_dir, {"service_url": "https://wrong-host.example.invalid"})
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="via-env")

    proc = _run("start", tmp_path=tmp_path, env_overrides={"NX_SERVICE_URL": engine.base_url})
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1
    assert engine.auth_headers[0] == "Bearer via-env"


def test_report_kind_tolerates_missing_agent_type(tmp_path: Path, mock_engine) -> None:
    """nexus-0zsmg: a SubagentStop payload without ``agent_type`` must
    still project the report tuple -- the ledger.yaml template's
    ``agent_type`` dimension is not ``required: true``, so a blank
    dimension value is engine-safe. session_id + agent_id remain
    mandatory."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    proc = _run(
        "report",
        tmp_path=tmp_path,
        stdin=json.dumps({"session_id": SESSION_ID, "agent_id": AGENT_ID}),  # no agent_type
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1
    body = engine.requests[0]
    assert body["keys"] == {"agent_id": AGENT_ID, "kind": "report"}
    assert body["dims"] == {"agent_type": ""}
    log = _log_path(tmp_path / "state")
    assert not log.exists() or "SKIP" not in log.read_text()


def test_start_kind_still_requires_agent_type(tmp_path: Path, mock_engine) -> None:
    """Regression guard: the report-only tolerance above must not loosen
    the start path -- SubagentStart's agent_type is the dispatch's own
    subagent_type and is expected to always be present; a start payload
    missing it still SKIPs without calling the engine."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    proc = _run(
        "start",
        tmp_path=tmp_path,
        stdin=json.dumps({"session_id": SESSION_ID, "agent_id": AGENT_ID}),  # no agent_type
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert engine.requests == []
    log = _log_path(tmp_path / "state")
    assert "SKIP kind=start" in log.read_text()


def test_engine_returns_429_is_logged_and_exits_zero(tmp_path: Path, mock_engine) -> None:
    """RDR-205 Phase 2 Step 3: 'the real projection script with the
    engine up, down, and rate limiting.' A 429 must never propagate as a
    failure -- logged, exit 0."""
    engine = mock_engine(status=429)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    proc = _run("start", tmp_path=tmp_path, env_overrides={"NX_SERVICE_URL": engine.base_url})
    assert proc.returncode == 0, proc.stderr
    log = _log_path(tmp_path / "state")
    assert "429" in log.read_text()


def test_engine_down_is_logged_and_exits_zero_fast(tmp_path: Path) -> None:
    """Arm 2 of the Test Plan's 'hook append with the engine down'
    scenario: the SCRIPT's own exit code and latency stay unaffected by
    an unreachable engine -- bounded by the curl timeout, never hanging,
    and the failure is logged rather than raised."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing is listening on this port now
    base_url = f"http://127.0.0.1:{port}"

    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=base_url, token="fresh-data-token")

    start = time.monotonic()
    proc = _run("start", tmp_path=tmp_path, env_overrides={"NX_SERVICE_URL": base_url})
    elapsed = time.monotonic() - start

    assert proc.returncode == 0, proc.stderr
    assert elapsed < 10.0, f"engine-down path took {elapsed:.2f}s -- should be bounded by the curl timeout"
    log = _log_path(tmp_path / "state")
    assert "SKIP kind=start" in log.read_text()


def test_incomplete_payload_skips_without_calling_the_engine(tmp_path: Path, mock_engine) -> None:
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    proc = _run(
        "start",
        tmp_path=tmp_path,
        stdin=json.dumps({"session_id": SESSION_ID}),  # no agent_id/agent_type
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert engine.requests == []


def test_report_kind_with_no_agent_id_logs_nothing(tmp_path: Path, mock_engine) -> None:
    """nexus-aginu: SubagentStop fires for stops this ledger has no
    tracked agent for (measured live on this box at ~250 occurrences per
    session, every one with a present, valid session_id). Nothing is
    ever lost by this -- the real agent's own report, when one exists,
    is keyed on ITS OWN agent_id and lands as a separate invocation --
    so this case must project NOTHING, including no diagnostic log
    line: at that volume a repeated, non-actionable line is pure noise,
    unlike every other incomplete-payload case, which keeps its line."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    proc = _run(
        "report",
        tmp_path=tmp_path,
        stdin=json.dumps({"session_id": SESSION_ID}),  # valid session_id, no agent_id
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert engine.requests == []
    log = _log_path(tmp_path / "state")
    assert not log.exists(), f"expected no log line for a report with no agent_id, got: {log.read_text()}"


def test_start_kind_with_no_agent_id_still_logs(tmp_path: Path, mock_engine) -> None:
    """Regression guard: the report-only no-agent_id silence above must
    not spread to the start path -- a start payload missing agent_id
    still SKIPs WITH a logged reason, exactly as before."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    proc = _run(
        "start",
        tmp_path=tmp_path,
        stdin=json.dumps({"session_id": SESSION_ID}),  # no agent_id
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert engine.requests == []
    log = _log_path(tmp_path / "state")
    assert "SKIP kind=start" in log.read_text()


def test_never_mints_never_imports_nexus_package() -> None:
    """Stdlib-only, like t2_prefix_scan.py / routing/_lib.py. The only
    route this module ever posts to is ``_ROUTE`` -- pinned to
    ``/v1/tuples/out``, never the mint endpoint (the module docstring
    names ``/v1/data-tokens/mint`` only in prose, to document what this
    module deliberately does NOT call -- ``test_fresh_lease_posts_the_
    ledger_start_tuple`` above is the behavioral proof of the actual
    POST path)."""
    src = SCRIPT.read_text()
    assert "import nexus" not in src
    assert "from nexus" not in src

    import importlib.util

    spec = importlib.util.spec_from_file_location("tuple_ledger_project", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._ROUTE == "/v1/tuples/out"


def test_script_never_spawns_a_subprocess_for_the_post() -> None:
    """nexus-em75s.12 review fix: the bearer must never appear in a
    subprocess argv (readable by any co-resident user via ps/proc for the
    life of the call). The POST goes over stdlib ``urllib.request``, not
    ``curl`` or any other shellout."""
    src = SCRIPT.read_text()
    assert '"curl"' not in src
    assert "import subprocess" not in src
    assert "urllib.request" in src


def test_bearer_never_appears_in_a_spawned_subprocess(tmp_path: Path, mock_engine, monkeypatch) -> None:
    """Behavioral proof, not just a source grep: patch subprocess.run to
    fail loudly if the script's own process ever calls it, then run the
    real POST path end to end and confirm the token still reaches the
    engine -- via the Authorization header, never via any argv."""
    import importlib.util
    import subprocess as real_subprocess

    engine = mock_engine(status=200)

    spec = importlib.util.spec_from_file_location("tuple_ledger_project_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("tuple_ledger_project.py must never spawn a subprocess to POST")

    monkeypatch.setattr(real_subprocess, "run", _forbidden)
    monkeypatch.setattr(real_subprocess, "Popen", _forbidden)

    module._post_via_urllib(
        engine.base_url, "never-in-argv",
        {"subspace": "ledger/x", "keys": {"agent_id": "a", "kind": "start"}, "dims": {}},
    )

    assert len(engine.requests) == 1
    assert engine.auth_headers[0] == "Bearer never-in-argv"
