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
    values. This is the shape ``_endpoint_resolve.read_config_yml_credentials``
    is a narrow mirror of, not a general YAML writer.
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
    def __init__(self, status: int = 200, reject_extra_dims: frozenset[str] | None = None) -> None:
        self.status = status
        # bead nexus-cnzei.6 item 2: simulates a below-floor engine (older
        # than engine-service-v0.1.118) that has not declared these dim
        # names yet -- any request whose "dims" carries one of them gets a
        # 400, exactly the SchemaViolation shape TemplateRegistry/
        # TupleRepository actually returns for an undeclared dimension.
        self.reject_extra_dims = reject_extra_dims or frozenset()
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
                    parsed = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError:
                    parsed = {}
                engine.requests.append(parsed)
                if self.path != "/v1/tuples/out":
                    self.send_response(404)
                    self.end_headers()
                    return
                dims = parsed.get("dims") if isinstance(parsed, dict) else None
                status = engine.status
                response_body = b"{}"
                rejected = (
                    (set(dims) & engine.reject_extra_dims)
                    if engine.reject_extra_dims and isinstance(dims, dict) else set()
                )
                if rejected:
                    status = 400
                    # The REAL engine's SchemaViolationException/TupleHandler
                    # shape for an undeclared dimension -- see
                    # tuple_ledger_project.py's _UNDECLARED_DIM_DETAIL_RE,
                    # which this mock's body must match for the fallback
                    # tests below to exercise the real detection logic
                    # rather than a bare-status shortcut (fix round 1, CRE
                    # finding 2).
                    field = sorted(rejected)[0]
                    response_body = json.dumps({
                        "error": "SchemaViolation",
                        "detail": f"field '{field}': not a declared dimension for this template",
                    }).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(response_body)

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

    def make(status: int = 200, reject_extra_dims: frozenset[str] | None = None) -> _MockTupleEngine:
        e = _MockTupleEngine(status=status, reject_extra_dims=reject_extra_dims)
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
    # No agent_transcript_path in this payload -- verify=absent, no
    # commit/t2_ref (bead nexus-cnzei.6 item 2).
    assert body["dims"] == {"agent_type": "", "verify": "absent"}
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
    # nexus-scc9t: loose hang guard, not a precision timing check. This
    # is a real subprocess (bash -> the projection script) hitting a
    # refused connection (ECONNREFUSED), which fails near-instantly --
    # the transport's own _POST_TIMEOUT_S is 5s, so 10.0s (2x that) is
    # never reached in correct operation and guards against the
    # connection-refused path somehow falling through to the full
    # transport timeout (or worse, hanging past it), while staying clear
    # of ordinary -n auto process-spawn + scheduler delay.
    assert elapsed < 10.0, f"engine-down path took {elapsed:.2f}s -- should be bounded by the transport timeout"
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


def test_post_never_follows_a_redirect(tmp_path: Path, mock_engine) -> None:
    """nexus-em75s.42 review finding: the engine URL is fixed and
    internal, so a redirect response must never be followed -- following
    one would resend the Authorization header to whatever host the
    redirect names. The attacker/second server must see zero requests;
    the 3xx itself is logged as a plain HTTP status, not silently
    swallowed."""
    import importlib.util
    from http.server import BaseHTTPRequestHandler

    attacker = mock_engine(status=200)

    spec = importlib.util.spec_from_file_location("tuple_ledger_project_redirect", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length) if length else b""
            self.send_response(302)
            self.send_header("Location", attacker.base_url + "/v1/tuples/out")
            self.end_headers()

    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        with pytest.raises(module._Skip, match=r"engine returned HTTP 302"):
            module._post_via_urllib(
                f"http://{host}:{port}", "tok",
                {"subspace": "ledger/x", "keys": {"agent_id": "a", "kind": "start"}, "dims": {}},
                is_local_supervisor=True,
            )
    finally:
        server.shutdown()
        server.server_close()
    assert attacker.requests == [], "redirect must never be followed"


def test_post_ignores_ambient_proxy_env_for_a_local_supervisor_endpoint(
    tmp_path: Path, mock_engine, monkeypatch,
) -> None:
    """nexus-em75s.42 review finding, scoped by the fix round: a LOCAL
    supervisor endpoint (``is_local_supervisor=True``) must never route
    through an ambient http_proxy/https_proxy -- point the proxy env at
    a port nothing listens on and confirm the POST still reaches the
    real engine directly."""
    import importlib.util
    import socket

    dead_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    dead_sock.bind(("127.0.0.1", 0))
    dead_port = dead_sock.getsockname()[1]
    dead_sock.close()

    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{dead_port}")
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{dead_port}")

    engine = mock_engine(status=200)
    spec = importlib.util.spec_from_file_location("tuple_ledger_project_proxy", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module._post_via_urllib(
        engine.base_url, "tok",
        {"subspace": "ledger/x", "keys": {"agent_id": "a", "kind": "start"}, "dims": {}},
        is_local_supervisor=True,
    )
    assert len(engine.requests) == 1


def test_post_honours_ambient_proxy_env_for_a_non_local_endpoint(
    tmp_path: Path, monkeypatch,
) -> None:
    """Fix round on nexus-aginu/nexus-em75s.42 review finding 5: a
    MANAGED (non-local-supervisor) endpoint must honour an ambient
    http_proxy/https_proxy, matching t2_prefix_scan.py and
    routing/_lib.py's plain ``urlopen`` -- otherwise a corporate-proxied
    cloud-mode box loses ledger writes silently while the sibling hooks
    keep working. Point the request at a dead port nothing listens on
    directly, but stand up a real HTTP server as the proxy: the POST
    must succeed (via the proxy), proving the ambient proxy env was
    actually used rather than bypassed."""
    proxied: list[str] = []

    class _ProxyHandler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            proxied.append(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length) if length else b""
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    proxy_server = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
    proxy_host, proxy_port = proxy_server.server_address[:2]
    proxy_thread = threading.Thread(target=proxy_server.serve_forever, daemon=True)
    proxy_thread.start()

    import socket

    dead_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    dead_sock.bind(("127.0.0.1", 0))
    dead_host, dead_port = dead_sock.getsockname()[:2]
    dead_sock.close()

    monkeypatch.setenv("http_proxy", f"http://{proxy_host}:{proxy_port}")
    monkeypatch.setenv("HTTP_PROXY", f"http://{proxy_host}:{proxy_port}")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)

    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "tuple_ledger_project_proxy_honoured", SCRIPT,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        module._post_via_urllib(
            f"http://{dead_host}:{dead_port}", "tok",
            {"subspace": "ledger/x", "keys": {"agent_id": "a", "kind": "start"}, "dims": {}},
            is_local_supervisor=False,
        )
    finally:
        proxy_server.shutdown()
        proxy_server.server_close()

    assert len(proxied) == 1, "the request must have gone through the proxy, not directly"
    assert f":{dead_port}/v1/tuples/out" in proxied[0], (
        f"proxy must have received the absolute-form request URI naming the dead port, got {proxied[0]!r}"
    )


def test_post_bounds_the_whole_call_against_a_listening_but_never_accepting_server(
    tmp_path: Path,
) -> None:
    """nexus-em75s.42 review finding: urlopen's own ``timeout`` bounds
    each individual socket operation, not the whole call -- a server
    that completes the TCP handshake (listen(), never accept()) could
    otherwise keep the call alive past any single recv's timeout. The
    whole POST must still return within roughly _POST_TIMEOUT_S."""
    import importlib.util
    import socket
    import time as _time

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    host, port = sock.getsockname()[:2]

    spec = importlib.util.spec_from_file_location("tuple_ledger_project_deadline", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        start = _time.monotonic()
        with pytest.raises(module._Skip):
            module._post_via_urllib(
                f"http://{host}:{port}", "tok",
                {"subspace": "ledger/x", "keys": {"agent_id": "a", "kind": "start"}, "dims": {}},
                is_local_supervisor=True,
            )
        elapsed = _time.monotonic() - start
        assert elapsed < module._POST_TIMEOUT_S + 2.0, (
            f"whole-call deadline not enforced: took {elapsed:.2f}s"
        )
    finally:
        sock.close()


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
        is_local_supervisor=True,
    )

    assert len(engine.requests) == 1
    assert engine.auth_headers[0] == "Bearer never-in-argv"


# ── Size pre-check (bead nexus-r7xao) ────────────────────────────────────────


def test_oversized_agent_id_skips_before_any_post(tmp_path: Path, mock_engine) -> None:
    """An agent_id over the 256-byte keys/dims field cap is refused before
    any POST -- the engine is never even asked."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    proc = _run(
        "start", tmp_path=tmp_path,
        stdin=_payload(agent_id="a" * 257),
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert engine.requests == []
    log = _log_path(tmp_path / "state").read_text()
    assert "SKIP kind=start" in log
    assert "oversized" in log
    assert "257 bytes" in log


def test_agent_id_at_the_cap_is_not_refused_by_the_size_check(tmp_path: Path, mock_engine) -> None:
    """256 bytes is exactly the field cap -- must reach the engine, proving
    the boundary is inclusive."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    proc = _run(
        "start", tmp_path=tmp_path,
        stdin=_payload(agent_id="a" * 256),
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1


def test_oversized_session_id_subspace_skips_before_any_post(tmp_path: Path, mock_engine) -> None:
    """A session_id long enough to push ``ledger/<session_id>`` over the
    256-byte subspace cap is refused before any POST."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    long_session_id = "s" * 260
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    payload = json.dumps({
        "session_id": long_session_id, "hook_event_name": "SubagentStart",
        "agent_id": AGENT_ID, "agent_type": AGENT_TYPE,
    })
    proc = _run(
        "start", tmp_path=tmp_path, stdin=payload,
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert engine.requests == []


# ── Checkable-report VERIFY dims (bead nexus-cnzei.6 item 2) ────────────────
#
# Real-shaped transcript fixtures -- the same convention
# test_subagent_stop_hook.py and test_subagent_stop_writes_scan.py use:
# genuine Claude Code transcript entry shapes (assistant/message/content
# blocks; a SendMessage tool_use's "content" input field, not a
# simplified stand-in string), never a copied literal.


def _assistant_text_entry(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _sendmessage_entry(content_text: str, *, to: str = "main") -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "sm1",
                    "name": "SendMessage",
                    "input": {"to": to, "content": content_text},
                }
            ],
        },
    }


def _write_transcript(tmp_path: Path, entries: list[dict], name: str = "agent_transcript.jsonl") -> Path:
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return p


def test_report_kind_extracts_verify_dims_from_final_assistant_text(
    tmp_path: Path, mock_engine,
) -> None:
    """A real-shaped transcript whose LAST assistant turn is plain text
    carrying VERIFY lines -- the synchronous-dispatch hand-back shape."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    transcript = _write_transcript(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "do the thing"}},
        _assistant_text_entry(
            "Implementation complete.\n"
            "VERIFY: commit=abc1234\n"
            "VERIFY: uv run pytest tests/hooks/test_x.py => rc=0 3 passed\n"
            "VERIFY: t2=nexus/impl-notes.md\n"
        ),
    ])

    proc = _run(
        "report", tmp_path=tmp_path,
        stdin=_payload(agent_transcript_path=str(transcript)),
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1
    dims = engine.requests[0]["dims"]
    assert dims == {
        "agent_type": AGENT_TYPE,
        "verify": "present",
        "commit": "abc1234",
        "t2_ref": "nexus/impl-notes.md",
    }


def test_report_kind_extracts_verify_dims_from_sendmessage_content(
    tmp_path: Path, mock_engine,
) -> None:
    """The background-teammate shape: the final assistant turn carries no
    text at all, but an earlier SendMessage tool_use's "content" field is
    the agent's real report and carries the VERIFY lines."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    transcript = _write_transcript(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "do the thing"}},
        _sendmessage_entry(
            "Done.\nVERIFY: commit=deadbee\nVERIFY: t2=nexus/checkpoint",
        ),
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t9", "name": "Bash", "input": {}}]},
        },
    ])

    proc = _run(
        "report", tmp_path=tmp_path,
        stdin=_payload(agent_transcript_path=str(transcript)),
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1
    dims = engine.requests[0]["dims"]
    assert dims["verify"] == "present"
    assert dims["commit"] == "deadbee"
    assert dims["t2_ref"] == "nexus/checkpoint"


def test_report_kind_verify_absent_when_no_verify_lines_present(
    tmp_path: Path, mock_engine,
) -> None:
    """A readable transcript with a real final assistant turn, but no
    ``VERIFY:`` line anywhere -- ``verify=absent``, no commit/t2_ref."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    transcript = _write_transcript(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "do the thing"}},
        _assistant_text_entry("Done, no checkable claims here."),
    ])

    proc = _run(
        "report", tmp_path=tmp_path,
        stdin=_payload(agent_transcript_path=str(transcript)),
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1
    assert engine.requests[0]["dims"] == {"agent_type": AGENT_TYPE, "verify": "absent"}


def test_report_kind_verify_absent_when_transcript_path_does_not_exist(
    tmp_path: Path, mock_engine,
) -> None:
    """``agent_transcript_path`` present in the payload but naming a file
    that does not exist -- fails open to ``verify=absent``, same as a
    missing field entirely; the row is still written."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    proc = _run(
        "report", tmp_path=tmp_path,
        stdin=_payload(agent_transcript_path=str(tmp_path / "does-not-exist.jsonl")),
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1
    assert engine.requests[0]["dims"] == {"agent_type": AGENT_TYPE, "verify": "absent"}


def test_start_kind_never_parses_verify_lines(tmp_path: Path, mock_engine) -> None:
    """Regression guard: kind=="start" never carries the new dims at all,
    even when a (nonsensical for this kind) agent_transcript_path with
    VERIFY lines is present in the payload -- SubagentStart fires before
    any agent output exists to parse."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    transcript = _write_transcript(tmp_path, [
        _assistant_text_entry("VERIFY: commit=abc1234"),
    ])

    proc = _run(
        "start", tmp_path=tmp_path,
        stdin=_payload(agent_transcript_path=str(transcript)),
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1
    assert engine.requests[0]["dims"] == {"agent_type": AGENT_TYPE}


def test_report_kind_falls_back_to_legacy_dims_when_engine_refuses_new_dims(
    tmp_path: Path, mock_engine,
) -> None:
    """HARD REQUIREMENT (bead nexus-cnzei.6 item 2): an engine older than
    engine-service-v0.1.118 does not declare commit/t2_ref/verify and
    answers an ``out`` naming them with HTTP 400. The row must still land
    -- retried once with the legacy dims-only body, never dropped."""
    engine = mock_engine(status=200, reject_extra_dims=frozenset({"commit", "t2_ref", "verify"}))
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    transcript = _write_transcript(tmp_path, [
        _assistant_text_entry("VERIFY: commit=abc1234\nVERIFY: t2=nexus/notes"),
    ])

    proc = _run(
        "report", tmp_path=tmp_path,
        stdin=_payload(agent_transcript_path=str(transcript)),
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    # First attempt (rejected) + retry (accepted).
    assert len(engine.requests) == 2
    first, second = engine.requests
    assert set(first["dims"]) == {"agent_type", "verify", "commit", "t2_ref"}
    assert second["dims"] == {"agent_type": AGENT_TYPE}
    log = _log_path(tmp_path / "state").read_text()
    assert "SCHEMA_FALLBACK kind=report" in log


def test_report_kind_generic_400_is_not_mistaken_for_a_schema_violation(
    tmp_path: Path, mock_engine,
) -> None:
    """Fix round 1, CRE finding 2: a bare HTTP 400 with a body that is
    NOT the undeclared-dim shape (a different SchemaViolation reason, or
    no recognisable body at all) must never trigger the legacy-dims
    retry -- one POST only, plain SKIP, exit 0."""
    engine = mock_engine(status=400)  # every request gets a bare {} 400
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    transcript = _write_transcript(tmp_path, [
        _assistant_text_entry("VERIFY: commit=abc1234"),
    ])

    proc = _run(
        "report", tmp_path=tmp_path,
        stdin=_payload(agent_transcript_path=str(transcript)),
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1, "a non-schema-violation 400 must never be retried"
    log = _log_path(tmp_path / "state").read_text()
    assert "SKIP kind=report" in log
    assert "SCHEMA_FALLBACK" not in log


def test_report_kind_retry_that_also_gets_a_400_degrades_without_raising(
    tmp_path: Path, mock_engine,
) -> None:
    """The retry's OWN 400 (bead nexus-cnzei.6 fix round 1, coordinator's
    ask): even when the fallback body (agent_type alone) is ALSO refused
    as an undeclared dim by a genuinely broken engine, the script must
    still degrade to a logged SKIP and exit 0 -- never raise, never
    block the hook."""
    engine = mock_engine(
        status=200,
        reject_extra_dims=frozenset({"agent_type", "commit", "t2_ref", "verify"}),
    )
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    transcript = _write_transcript(tmp_path, [
        _assistant_text_entry("VERIFY: commit=abc1234"),
    ])

    proc = _run(
        "report", tmp_path=tmp_path,
        stdin=_payload(agent_transcript_path=str(transcript)),
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 2, "the retry must still be attempted exactly once"
    log = _log_path(tmp_path / "state").read_text()
    assert "SCHEMA_FALLBACK kind=report" in log
    assert "SKIP kind=report" in log


def test_start_kind_400_has_nothing_to_strip_and_degrades_without_raising(
    tmp_path: Path, mock_engine,
) -> None:
    """A start-kind row's dims are always just {agent_type} (len<=1) --
    a 400 on it, even an undeclared-dim-shaped one, has nothing left to
    retry with and must re-raise straight to the outer skip handler:
    ONE POST, logged SKIP, exit 0, never a crash or a second attempt."""
    engine = mock_engine(status=200, reject_extra_dims=frozenset({"agent_type"}))
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    proc = _run(
        "start", tmp_path=tmp_path,
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1, "a start-kind 400 has nothing to strip -- no retry"
    log = _log_path(tmp_path / "state").read_text()
    assert "SKIP kind=start" in log
    assert "SCHEMA_FALLBACK" not in log


# ── Case-insensitive commit=/t2= keys (fix round 1, CRE finding 3) ───────────


def test_verify_commit_key_is_case_insensitive(tmp_path: Path, mock_engine) -> None:
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    transcript = _write_transcript(tmp_path, [
        _assistant_text_entry("VERIFY: Commit=abc1234"),
    ])
    proc = _run(
        "report", tmp_path=tmp_path,
        stdin=_payload(agent_transcript_path=str(transcript)),
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert engine.requests[0]["dims"]["commit"] == "abc1234"


def test_verify_t2_key_is_case_insensitive(tmp_path: Path, mock_engine) -> None:
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    transcript = _write_transcript(tmp_path, [
        _assistant_text_entry("VERIFY: T2=nexus/notes"),
    ])
    proc = _run(
        "report", tmp_path=tmp_path,
        stdin=_payload(agent_transcript_path=str(transcript)),
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert engine.requests[0]["dims"]["t2_ref"] == "nexus/notes"


# ── _is_undeclared_dim_violation (fix round 1, CRE finding 2) ────────────────


def test_is_undeclared_dim_violation_true_for_the_real_engine_shape() -> None:
    module = _load_module_directly()
    body = json.dumps({
        "error": "SchemaViolation",
        "detail": "field 'commit': not a declared dimension for this template",
    }).encode("utf-8")
    assert module._is_undeclared_dim_violation(body) is True


def test_is_undeclared_dim_violation_false_for_a_different_schema_violation() -> None:
    module = _load_module_directly()
    body = json.dumps({
        "error": "SchemaViolation",
        "detail": "field 'ttl_seconds': must be positive",
    }).encode("utf-8")
    assert module._is_undeclared_dim_violation(body) is False


def test_is_undeclared_dim_violation_false_for_non_schema_error() -> None:
    module = _load_module_directly()
    body = json.dumps({"error": "UnknownSubspace", "detail": "no such subspace"}).encode("utf-8")
    assert module._is_undeclared_dim_violation(body) is False


def test_is_undeclared_dim_violation_false_for_unparseable_body() -> None:
    module = _load_module_directly()
    assert module._is_undeclared_dim_violation(b"not json at all") is False
    assert module._is_undeclared_dim_violation(b"") is False


def test_oversized_t2_ref_dim_is_dropped_but_row_still_written(
    tmp_path: Path, mock_engine,
) -> None:
    """An oversized VERIFY-derived dim value is dropped (logged), not a
    reason to skip the whole row -- ``verify`` still lands."""
    engine = mock_engine(status=200)
    config_dir = tmp_path / "config"
    _write_data_token_lease(config_dir, base_url=engine.base_url, token="fresh-data-token")

    transcript = _write_transcript(tmp_path, [
        _assistant_text_entry(f"VERIFY: t2={'x' * 300}"),
    ])

    proc = _run(
        "report", tmp_path=tmp_path,
        stdin=_payload(agent_transcript_path=str(transcript)),
        env_overrides={"NX_SERVICE_URL": engine.base_url},
    )
    assert proc.returncode == 0, proc.stderr
    assert len(engine.requests) == 1
    dims = engine.requests[0]["dims"]
    assert dims == {"agent_type": AGENT_TYPE, "verify": "present"}
    assert "t2_ref" not in dims
    log = _log_path(tmp_path / "state").read_text()
    assert "SKIP dims.t2_ref oversized, dropping" in log
    assert "SKIP kind=report" not in log


def _load_module_directly():
    import importlib.util

    spec = importlib.util.spec_from_file_location("tuple_ledger_project_verify_unit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extract_verify_dims_ignores_a_malformed_commit_value(tmp_path: Path) -> None:
    """A ``commit=`` line that is not 7-40 hex chars still counts toward
    ``verify=present`` (a VERIFY claim was made), but is not parsed as a
    commit dim -- a malformed value must never masquerade as a sha."""
    module = _load_module_directly()
    transcript = _write_transcript(tmp_path, [
        _assistant_text_entry("VERIFY: commit=not-hex!!\nVERIFY: some other claim"),
    ])
    assert module._extract_verify_dims(str(transcript)) == {"verify": "present"}


def test_extract_verify_dims_first_match_wins_for_commit_and_t2_ref(tmp_path: Path) -> None:
    """Multiple VERIFY lines of the same shape: the first is kept, not
    the last -- deterministic and simple, matching the common case of one
    commit and one t2 line per report."""
    module = _load_module_directly()
    transcript = _write_transcript(tmp_path, [
        _assistant_text_entry(
            "VERIFY: commit=aaaaaaa\nVERIFY: commit=bbbbbbb\n"
            "VERIFY: t2=nexus/one\nVERIFY: t2=nexus/two"
        ),
    ])
    assert module._extract_verify_dims(str(transcript)) == {
        "verify": "present", "commit": "aaaaaaa", "t2_ref": "nexus/one",
    }
