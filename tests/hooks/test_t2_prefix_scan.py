# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-vg6d4 / nexus-8fvp2: ``t2_prefix_scan.py`` must be stdlib-only AND
must talk to the engine's T2 HTTP API, never the retired SQLite
``memory.db``.

The plugin's ``_run_python_hook.sh`` wrapper probes bare ``python3.13`` /
``python3.12`` to invoke ``session_start_hook.py``, which calls
``t2_prefix_scan.py``. On a ``uv tool install conexus`` deployment the
wrapper's resolved interpreter cannot import the ``nexus`` package (it
lives in conexus's own venv) — this pins that the script runs under a
vanilla Python with only stdlib available, over a stdlib ``urllib``
client against a mocked HTTP engine (never a real ``nexus`` import, never
SQLite).

nexus-8fvp2: T2 moved to Postgres (behind the engine's ``/v1/memory`` HTTP
API) at RDR-158 P4; the script was frozen reading a dead SQLite file for
six weeks with zero signal. This suite pins the HTTP-transport rewrite:
endpoint resolution (env, then the local supervisor's on-disk lease file)
and the two-arm freshness assert (source-unreachable, and
freshest-entry-too-old) that replace the old silent failure mode.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus"
    / "hooks"
    / "scripts"
    / "t2_prefix_scan.py"
)

_TOKEN = "test-bearer-token"
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


# ── Mock engine ──────────────────────────────────────────────────────────────


class _MockMemoryEngine:
    """Minimal stand-in for the Java engine's ``/v1/memory`` HTTP surface.

    Serves exactly the two GET routes ``t2_prefix_scan.py`` calls:
    ``/v1/memory/projects?prefix=`` and ``/v1/memory/all?project=``. Any
    other path or method is a 404/405 — the script never calls those.
    """

    def __init__(
        self,
        projects: list[dict[str, str]],
        entries_by_project: dict[str, list[dict[str, str]]],
        *,
        expected_token: str = _TOKEN,
        fail_projects: set[str] | None = None,
    ) -> None:
        self.projects = projects
        self.entries_by_project = entries_by_project
        self.expected_token = expected_token
        #: nexus-eg6qe: projects in this set get a 500 from /v1/memory/all,
        #: simulating a single bad/slow namespace mid-scan.
        self.fail_projects = fail_projects or set()
        self.requests: list[str] = []

        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — stdlib signature
                pass  # silence request logging in test output

            def do_GET(self) -> None:  # noqa: N802 — stdlib method name
                engine.requests.append(self.path)
                if self.headers.get("Authorization") != f"Bearer {engine.expected_token}":
                    self._send_json(401, {"error": "unauthorized"})
                    return
                parsed = urlsplit(self.path)
                params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if parsed.path == "/v1/memory/projects":
                    prefix = params.get("prefix", "")
                    rows = [p for p in engine.projects if p["project"].startswith(prefix)]
                    self._send_json(200, rows)
                elif parsed.path == "/v1/memory/all":
                    project = params.get("project", "")
                    if project in engine.fail_projects:
                        self._send_json(500, {"error": "simulated namespace failure"})
                        return
                    self._send_json(200, engine.entries_by_project.get(project, []))
                else:
                    self._send_json(404, {"error": "not found"})

            def _send_json(self, code: int, payload: object) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def host_port(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return host, port

    @property
    def base_url(self) -> str:
        host, port = self.host_port
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def mock_engine():
    engines: list[_MockMemoryEngine] = []

    def _make(
        projects: list[dict[str, str]],
        entries_by_project: dict[str, list[dict[str, str]]] | None = None,
        **kwargs: object,
    ) -> _MockMemoryEngine:
        engine = _MockMemoryEngine(projects, entries_by_project or {}, **kwargs)  # type: ignore[arg-type]
        engines.append(engine)
        return engine

    yield _make
    for engine in engines:
        engine.close()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_TS_FMT)


def _entry(title: str, content: str, timestamp: datetime) -> dict[str, str]:
    return {"title": title, "content": content, "timestamp": _iso(timestamp)}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Invocation helper ──────────────────────────────────────────────────────


def _run(
    project_name: str,
    *,
    config_dir: Path,
    env: dict[str, str] | None = None,
    interpreter: str = sys.executable,
) -> str:
    """Invoke the script, isolated from any ambient NX_SERVICE_* env the
    outer test process may be running under (nexus-8fvp2: the whole point
    of this suite is testing endpoint resolution in isolation)."""
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("NX_SERVICE_")}
    full_env.pop("PYTHONPATH", None)  # approximate the bare-interpreter invocation
    full_env["NEXUS_CONFIG_DIR"] = str(config_dir)
    full_env.update(env or {})
    result = subprocess.run(
        [interpreter, str(SCRIPT), project_name],
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"script exit {result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    return result.stdout


# ── Happy path (HTTP transport) ──────────────────────────────────────────────


def test_runs_over_http_and_surfaces_entries(tmp_path: Path, mock_engine) -> None:
    """The headline regression: talks HTTP, never SQLite; surfaces entries."""
    now = _now()
    engine = mock_engine(
        projects=[
            {"project": "nexus", "last_updated": _iso(now)},
            {"project": "nexus_rdr", "last_updated": _iso(now - timedelta(hours=1))},
        ],
        entries_by_project={
            "nexus": [
                _entry("release-5-3-0-validation", "Validated 5.3.0 release pipeline end-to-end.", now),
                _entry("rdr-memory-audit", "Audited 17 RDR memories; 9 stale; refreshed.", now - timedelta(minutes=30)),
            ],
            "nexus_rdr": [
                _entry("rdr-129", "RDR-129 closed 2026-05-27; T2 daemon write-path hardening.", now - timedelta(hours=1)),
            ],
        },
    )
    out = _run(
        "nexus",
        config_dir=tmp_path,
        env={"NX_SERVICE_URL": engine.base_url, "NX_SERVICE_TOKEN": _TOKEN},
    )
    assert "### T2 Memory" in out
    assert "release-5-3-0-validation" in out
    assert "rdr-memory-audit" in out
    assert "### T2 Memory (rdr)" in out
    assert "rdr-129" in out
    assert "WARNING" not in out
    # Must not have leaked the pre-fix import-error or SQLite-era message.
    assert "T2 not available" not in out
    assert "No module named" not in out
    # Must have actually gone over HTTP, not touched a SQLite file.
    assert any("/v1/memory/projects" in p for p in engine.requests)
    assert any("/v1/memory/all" in p for p in engine.requests)


def test_recency_ordering_within_namespace(tmp_path: Path, mock_engine) -> None:
    """Entries inside a namespace appear in the order the engine returns
    them (server-side DESC — MemoryRepository.getAll)."""
    now = _now()
    engine = mock_engine(
        projects=[{"project": "nexus", "last_updated": _iso(now)}],
        entries_by_project={
            "nexus": [
                _entry("release-5-3-0-validation", "newer", now),
                _entry("rdr-memory-audit", "older", now - timedelta(minutes=30)),
            ],
        },
    )
    out = _run(
        "nexus",
        config_dir=tmp_path,
        env={"NX_SERVICE_URL": engine.base_url, "NX_SERVICE_TOKEN": _TOKEN},
    )
    pos_release = out.index("release-5-3-0-validation")
    pos_audit = out.index("rdr-memory-audit")
    assert pos_release < pos_audit


def test_no_namespaces_means_no_output(tmp_path: Path, mock_engine) -> None:
    """Reachable engine, zero matching namespaces: clean empty output, no
    warning — a genuinely empty result is not the same as unreachable."""
    engine = mock_engine(projects=[])
    out = _run(
        "unknown_project_xyz",
        config_dir=tmp_path,
        env={"NX_SERVICE_URL": engine.base_url, "NX_SERVICE_TOKEN": _TOKEN},
    )
    assert out == ""


# ── Two-arm freshness assert (nexus-8fvp2 enlargement (d)) ──────────────────


def test_unreachable_arm_warns_when_no_endpoint_resolvable(tmp_path: Path) -> None:
    """Arm 1: no env, no lease file (fresh install / no supervisor running)
    -> a VISIBLE warning line, never a silent no-op."""
    out = _run("nexus", config_dir=tmp_path, env={})
    assert "WARNING" in out
    assert "unreachable" in out.lower()


def test_unreachable_arm_warns_on_connection_refused(tmp_path: Path) -> None:
    """Arm 1: endpoint resolves (env is set) but nothing is listening ->
    still a visible warning, not a silent empty result."""
    # Bind a socket to grab a free port, then close it so nothing answers.
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    out = _run(
        "nexus",
        config_dir=tmp_path,
        env={"NX_SERVICE_URL": f"http://127.0.0.1:{port}", "NX_SERVICE_TOKEN": _TOKEN},
    )
    assert "WARNING" in out
    assert "unreachable" in out.lower()


def test_stale_arm_warns_but_still_shows_entries(tmp_path: Path, mock_engine) -> None:
    """Arm 2: reachable, has entries, but the freshest is older than the
    threshold -> a visible warning line ALONGSIDE the (still-shown) data."""
    stale_ts = _now() - timedelta(days=40)
    engine = mock_engine(
        projects=[{"project": "nexus", "last_updated": _iso(stale_ts)}],
        entries_by_project={
            "nexus": [_entry("old-entry", "This is old.", stale_ts)],
        },
    )
    out = _run(
        "nexus",
        config_dir=tmp_path,
        env={
            "NX_SERVICE_URL": engine.base_url,
            "NX_SERVICE_TOKEN": _TOKEN,
            "NX_T2_SCAN_STALE_DAYS": "14",
        },
    )
    assert "WARNING" in out
    assert "old" in out.lower()  # the "Nd old" phrasing
    assert "old-entry" in out  # data is still surfaced, not dropped


def test_fresh_entries_produce_no_staleness_warning(tmp_path: Path, mock_engine) -> None:
    now = _now()
    engine = mock_engine(
        projects=[{"project": "nexus", "last_updated": _iso(now)}],
        entries_by_project={"nexus": [_entry("fresh-entry", "Recent.", now)]},
    )
    out = _run(
        "nexus",
        config_dir=tmp_path,
        env={
            "NX_SERVICE_URL": engine.base_url,
            "NX_SERVICE_TOKEN": _TOKEN,
            "NX_T2_SCAN_STALE_DAYS": "14",
        },
    )
    assert "WARNING" not in out
    assert "fresh-entry" in out


def test_empty_t2_is_not_confused_with_unreachable(tmp_path: Path, mock_engine) -> None:
    """nexus-8fvp2 enlargement (b): a fresh install's genuinely empty T2
    must stay silent, never render as (or alongside) an unreachable
    warning."""
    engine = mock_engine(projects=[])
    out = _run(
        "nexus",
        config_dir=tmp_path,
        env={"NX_SERVICE_URL": engine.base_url, "NX_SERVICE_TOKEN": _TOKEN},
    )
    assert out == ""
    assert "WARNING" not in out


# ── Lease-file endpoint resolution (no env vars set) ─────────────────────────


def _write_lease(
    config_dir: Path,
    *,
    host: str,
    port: int,
    token: str,
    status: str = "live",
    heartbeat_age_s: float = 0.0,
    ttl: float = 15.0,
) -> None:
    import time as _time

    record = {
        "scope_key": str(os.getuid()),
        "generation": 1,
        "owner_token": "test-owner",
        "heartbeat_epoch": _time.time() - heartbeat_age_s,
        "ttl": ttl,
        "endpoint": {"host": host, "port": port, "token": token},
        "version": "test",
        "payload": {},
        "status": status,
        "format_version": 1,
    }
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / f"storage_service_addr.{os.getuid()}").write_text(json.dumps(record))


def test_resolves_endpoint_from_supervisor_lease_with_no_env(tmp_path: Path, mock_engine) -> None:
    """No NX_SERVICE_* env at all — resolution falls through to the local
    supervisor's on-disk lease file, exactly like every other T2/T3 HTTP
    client (nexus.db.service_endpoint.discover_lease)."""
    now = _now()
    engine = mock_engine(
        projects=[{"project": "nexus", "last_updated": _iso(now)}],
        entries_by_project={"nexus": [_entry("lease-resolved", "Via lease file.", now)]},
        expected_token="lease-token-xyz",
    )
    host, port = engine.host_port
    _write_lease(tmp_path, host=host, port=port, token="lease-token-xyz")

    out = _run("nexus", config_dir=tmp_path, env={})
    assert "lease-resolved" in out
    assert "WARNING" not in out


def test_expired_lease_falls_through_to_unreachable(tmp_path: Path, mock_engine) -> None:
    """A lease file present but past its TTL is treated exactly like no
    lease at all — never trusted as a live endpoint."""
    engine = mock_engine(projects=[])
    host, port = engine.host_port
    _write_lease(tmp_path, host=host, port=port, token="lease-token-xyz", heartbeat_age_s=60.0, ttl=15.0)

    out = _run("nexus", config_dir=tmp_path, env={})
    assert "WARNING" in out
    assert "unreachable" in out.lower()


def test_shutting_down_lease_falls_through_to_unreachable(tmp_path: Path, mock_engine) -> None:
    engine = mock_engine(projects=[])
    host, port = engine.host_port
    _write_lease(tmp_path, host=host, port=port, token="lease-token-xyz", status="shutting_down")

    out = _run("nexus", config_dir=tmp_path, env={})
    assert "WARNING" in out


def test_env_takes_precedence_over_lease_file(tmp_path: Path, mock_engine) -> None:
    """An explicit NX_SERVICE_URL must win over a (deliberately wrong)
    lease file — env is checked first."""
    now = _now()
    engine = mock_engine(
        projects=[{"project": "nexus", "last_updated": _iso(now)}],
        entries_by_project={"nexus": [_entry("env-resolved", "Via env.", now)]},
        expected_token=_TOKEN,
    )
    # Point the lease file at a dead port — must be ignored.
    _write_lease(tmp_path, host="127.0.0.1", port=1, token="wrong-token")

    out = _run(
        "nexus",
        config_dir=tmp_path,
        env={"NX_SERVICE_URL": engine.base_url, "NX_SERVICE_TOKEN": _TOKEN},
    )
    assert "env-resolved" in out
    assert "WARNING" not in out


# ── config.yml credential resolution (nexus-sdtsx) ───────────────────────────


def test_resolves_service_url_and_token_from_config_yml(
    tmp_path: Path, mock_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canonical managed-cloud onboarding path (``nx config set
    service_url``/``service_token``, docs/managed-onboarding.md) and every
    Desktop ``.mcpb`` install (docs/desktop-deployment.md: the .mcpb reads
    config.yml, never inherits shell env) persist credentials ONLY in
    config.yml — no NX_SERVICE_* env is ever set for that population.
    Pinned against the REAL writer (``nexus.config.set_credential``), not
    a hand-authored fixture file, so a future change to the persisted
    shape is caught here rather than silently drifting from what this
    hook's line-oriented scanner parses."""
    import nexus.config as nexus_config

    now = _now()
    engine = mock_engine(
        projects=[{"project": "nexus", "last_updated": _iso(now)}],
        entries_by_project={"nexus": [_entry("cloud-resolved", "Via config.yml.", now)]},
        expected_token="config-yml-token",
    )
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    nexus_config.set_credential("service_url", engine.base_url)
    nexus_config.set_credential("service_token", "config-yml-token")

    out = _run("nexus", config_dir=tmp_path, env={})
    assert "cloud-resolved" in out
    assert "WARNING" not in out


def test_env_service_url_takes_precedence_over_config_yml(
    tmp_path: Path, mock_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``NX_SERVICE_URL`` env must still win over a persisted config.yml
    value — matches ``nexus.config.get_credential``'s env-then-config.yml
    precedence, which the real client (``resolve_service_endpoint``)
    follows."""
    import nexus.config as nexus_config

    now = _now()
    engine = mock_engine(
        projects=[{"project": "nexus", "last_updated": _iso(now)}],
        entries_by_project={"nexus": [_entry("env-resolved-over-yml", "Via env.", now)]},
        expected_token=_TOKEN,
    )
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    # A deliberately wrong config.yml value — must be ignored when env is set.
    nexus_config.set_credential("service_url", "http://127.0.0.1:1")
    nexus_config.set_credential("service_token", "wrong-token")

    out = _run(
        "nexus",
        config_dir=tmp_path,
        env={"NX_SERVICE_URL": engine.base_url, "NX_SERVICE_TOKEN": _TOKEN},
    )
    assert "env-resolved-over-yml" in out
    assert "WARNING" not in out


def test_config_yml_service_url_with_missing_token_warns_actionably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``service_url`` resolves from config.yml but no token is resolvable
    anywhere (no env, no config.yml service_token, no lease) — a visible,
    actionable warning naming the real remedy, never a silent no-op."""
    import nexus.config as nexus_config

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    nexus_config.set_credential("service_url", "https://api.example.com")

    out = _run("nexus", config_dir=tmp_path, env={})
    assert "WARNING" in out
    assert "token" in out.lower()
    assert "nx config set service_token" in out


def test_final_fallback_warning_names_both_local_and_cloud_remedies(tmp_path: Path) -> None:
    """nexus-sdtsx: with NO evidence of either topology (no env, no
    config.yml, no lease), the catch-all unreachable message must not
    unconditionally push the local-mode-only ``nx daemon service start``
    remedy — a cloud/mcpb install following it gets nothing, since it has
    no local storage_service to start. Both remedies must be named."""
    out = _run("nexus", config_dir=tmp_path, env={})
    assert "WARNING" in out
    assert "nx daemon service start" in out
    assert "nx config set service_url" in out


# ── Multi-namespace fan-out: isolation, cap, budget (nexus-eg6qe/nexus-9xado) ─


def test_one_bad_namespace_does_not_discard_others(tmp_path: Path, mock_engine) -> None:
    """A failure fetching namespace N's entries must not discard the
    already-rendered output from namespaces before it — the pre-fix outer
    try/except around the whole scan wiped everything on ANY namespace's
    failure (nexus-eg6qe). Namespace order here matters: the failing
    namespace sits BETWEEN two good ones."""
    now = _now()
    engine = mock_engine(
        projects=[
            {"project": "nexus", "last_updated": _iso(now)},
            {"project": "nexus_bad", "last_updated": _iso(now - timedelta(minutes=1))},
            {"project": "nexus_rdr", "last_updated": _iso(now - timedelta(minutes=2))},
        ],
        entries_by_project={
            "nexus": [_entry("good-entry-before", "First good namespace.", now)],
            "nexus_rdr": [
                _entry("good-entry-after", "Third namespace, still good.", now - timedelta(minutes=2)),
            ],
        },
        fail_projects={"nexus_bad"},
    )
    out = _run(
        "nexus",
        config_dir=tmp_path,
        env={"NX_SERVICE_URL": engine.base_url, "NX_SERVICE_TOKEN": _TOKEN},
    )
    assert "good-entry-before" in out
    assert "good-entry-after" in out
    assert "WARNING" in out
    assert "nexus_bad" in out


def test_namespace_count_is_capped(tmp_path: Path, mock_engine) -> None:
    """More matching namespaces than ``_MAX_NAMESPACES`` (5) — only the
    most-recent 5 get a per-namespace ``/v1/memory/all`` fetch; the rest
    are reported as skipped rather than silently driving an unbounded
    number of sequential HTTP round-trips (nexus-9xado)."""
    now = _now()
    projects = [
        {"project": f"nexus_ns{i}", "last_updated": _iso(now - timedelta(minutes=i))}
        for i in range(8)
    ]
    entries = {
        p["project"]: [_entry(f"entry-{p['project']}", "content", now)] for p in projects
    }
    engine = mock_engine(projects=projects, entries_by_project=entries)
    out = _run(
        "nexus",
        config_dir=tmp_path,
        env={"NX_SERVICE_URL": engine.base_url, "NX_SERVICE_TOKEN": _TOKEN},
    )
    all_requests = [r for r in engine.requests if "/v1/memory/all" in r]
    assert len(all_requests) <= 5
    assert "not checked" in out


def test_scan_budget_stops_the_fetch_loop(tmp_path: Path, mock_engine) -> None:
    """A near-zero scan budget must stop the per-namespace loop before
    issuing further requests and say so visibly — proves the wall-clock
    budget is enforced independent of ``_HARD_CAP`` (nexus-9xado)."""
    now = _now()
    projects = [
        {"project": f"nexus_ns{i}", "last_updated": _iso(now - timedelta(minutes=i))}
        for i in range(3)
    ]
    entries = {
        p["project"]: [_entry(f"entry-{p['project']}", "content", now)] for p in projects
    }
    engine = mock_engine(projects=projects, entries_by_project=entries)
    out = _run(
        "nexus",
        config_dir=tmp_path,
        env={
            "NX_SERVICE_URL": engine.base_url,
            "NX_SERVICE_TOKEN": _TOKEN,
            "NX_T2_SCAN_BUDGET_S": "0",
        },
    )
    assert "scan budget exceeded" in out


# ── Bare-interpreter / no-nexus-import invariant (nexus-vg6d4) ──────────────


def test_missing_config_dir_and_no_env_is_visible_not_silent(tmp_path: Path) -> None:
    """A fresh install (no config dir contents at all) must still surface a
    WARNING, not the pre-fix silent no-op nexus-8fvp2 exists to close."""
    empty_dir = tmp_path / "does-not-exist-yet"
    out = _run("nexus", config_dir=empty_dir, env={})
    assert "WARNING" in out


def test_script_never_imports_nexus_package() -> None:
    """Static check: no ``import nexus`` / ``from nexus`` anywhere in the
    script (nexus-vg6d4) — the whole point is running under a bare
    interpreter that cannot see the ``nexus`` package."""
    text = SCRIPT.read_text()
    assert not re.search(r"^\s*(import nexus\b|from nexus\b)", text, re.MULTILINE)


# ── NO-SQLITE lint (nexus-8fvp2 enlargement (a); de-vacuated nexus-ozfct) ────
#
# nexus-ozfct (code-review-expert, 2026-08-16): the original regex lint
# (``\bimport\s+sqlite3\b`` + the exact literal ``"memory.db"``) was
# concretely defeated by a probe doing ``from sqlite3 import connect;
# connect("t2_local_cache.db")`` — a different import FORM and a
# differently-spelled ``*.db`` filename both sailed through clean. This is
# a mechanized guard for health.py's stranded-install "frozen rollback
# artifact, not live data" advisory (``_check_stranded_install`` /
# ``LAST_MIGRATION_CAPABLE``): the advisory's ``ok=True`` claim is a lie if
# a shipped hook actually opens SQLite. Deliberately narrow in SCOPE too —
# this lint only ever covers ``conexus/hooks/scripts/*.{py,sh}`` (the
# shipped-hook surface T2 injection runs through); a hypothetical live
# SQLite read introduced elsewhere in ``src/nexus/`` is NOT caught here —
# see the T2 write-back for this session's explicit note on that boundary.


#: Stdlib modules that back an embedded/local-file database — the
#: "retired persistence substrate" class, not just the literal name
#: ``sqlite3``. ``shelve``/``dbm`` are stdlib wrappers around the same
#: kind of on-disk file store T2 retired away from (RDR-158 P4).
_BANNED_DB_MODULES = frozenset({"sqlite3", "shelve", "dbm"})

#: .sh hooks have no AST to walk — kept as a text regex arm. Word-boundary
#: on the bare module name catches `sqlite3` in any shell invocation form
#: (`python3 -c "import sqlite3"`, `command -v sqlite3`, etc.) rather than
#: only one particular import spelling.
_SH_BANNED_MODULE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(m) for m in _BANNED_DB_MODULES) + r")\b"
)
#: Any quoted string ending in ``.db`` — not just the exact literal
#: ``"memory.db"`` — so a renamed local-cache filename doesn't evade this.
_SH_DB_LITERAL_RE = re.compile(r"""["'][^"'\n]*\.db["']""")


def _module_root(dotted: str) -> str:
    return dotted.split(".", 1)[0]


def _python_offenders(path: Path) -> list[str]:
    """AST-based scan of one ``.py`` hook script (nexus-ozfct).

    Walks the parsed AST rather than regexing source text, so import
    aliasing/whitespace/form cannot evade it. Flags, each with its line
    number for a debuggable failure message:

      - ``import sqlite3`` / ``import shelve`` / ``import dbm`` (and any
        dotted submodule, e.g. ``dbm.gnu``) in EITHER ``import`` or
        ``from ... import`` form
      - ``importlib.import_module("sqlite3")`` (or shelve/dbm) by string
        literal argument
      - any string literal ending in ``.db`` — not just the exact
        spelling ``"memory.db"`` the original regex matched

    A file this cannot parse (SyntaxError) is itself reported as an
    offender — a parse failure must never look like a clean pass.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return [f"could not parse as Python (SyntaxError: {exc})"]

    reasons: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_root(alias.name) in _BANNED_DB_MODULES:
                    reasons.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and _module_root(node.module) in _BANNED_DB_MODULES:
                reasons.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Call):
            func = node.func
            is_import_module = (
                isinstance(func, ast.Attribute) and func.attr == "import_module"
            ) or (isinstance(func, ast.Name) and func.id == "import_module")
            if is_import_module and node.args:
                arg0 = node.args[0]
                if (
                    isinstance(arg0, ast.Constant)
                    and isinstance(arg0.value, str)
                    and _module_root(arg0.value) in _BANNED_DB_MODULES
                ):
                    reasons.append(
                        f"line {node.lineno}: importlib.import_module({arg0.value!r})"
                    )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.endswith(".db"):
                reasons.append(f"line {node.lineno}: string literal {node.value!r}")
    return reasons


def _shell_offenders(path: Path) -> list[str]:
    """Text-regex scan of one ``.sh`` hook script — no AST available."""
    text = path.read_text(encoding="utf-8", errors="replace")
    reasons: list[str] = []
    if _SH_BANNED_MODULE_RE.search(text):
        reasons.append("mentions a banned db module (sqlite3/shelve/dbm)")
    match = _SH_DB_LITERAL_RE.search(text)
    if match:
        reasons.append(f"*.db string literal: {match.group()!r}")
    return reasons


@pytest.mark.lint
def test_no_hook_script_reads_memory_db_or_sqlite() -> None:
    """Cheap mechanization for health.py's ``memory.db`` advisory (a
    "frozen rollback artifact, not live data" claim): no shipped hook
    script may open a SQLite/shelve/dbm file as a literal ``*.db`` path or
    import one of those modules in any form — if one ever did again, the
    advisory's ``ok=True`` would be a lie exactly as it silently was for
    six weeks pre-nexus-8fvp2. Rewritten AST-based at nexus-ozfct after a
    regex-import-form + exact-literal-filename gap was concretely
    demonstrated (see module comment above).
    """
    hooks_dir = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
    offenders: dict[str, list[str]] = {}
    for path in sorted(hooks_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix == ".py":
            reasons = _python_offenders(path)
        elif path.suffix == ".sh":
            reasons = _shell_offenders(path)
        else:
            continue
        if reasons:
            offenders[str(path.relative_to(hooks_dir))] = reasons
    assert not offenders, (
        f"hook script(s) reference a banned db module or *.db literal: "
        f"{offenders} — T2 is Postgres via the engine's HTTP API in every "
        "mode since RDR-158 P4 (nexus-8fvp2); a hook reading a local db "
        "file reintroduces the six-week silent-freeze regression this "
        "lint exists to catch."
    )


def test_strengthened_lint_catches_the_reviewer_probe(tmp_path: Path) -> None:
    """nexus-ozfct regression fixture: pin the EXACT probe shape the
    code-review-expert used to demonstrate the pre-fix regex lint's gap
    (``from sqlite3 import connect; connect("t2_local_cache.db")``) against
    the strengthened AST scanner directly — proves the gap is closed
    without needing a live offending file under ``conexus/hooks/scripts``.
    """
    probe = tmp_path / "probe_hook.py"
    probe.write_text(
        'from sqlite3 import connect\n'
        'conn = connect("t2_local_cache.db")\n'
    )
    reasons = _python_offenders(probe)
    assert any("from sqlite3 import" in r for r in reasons), reasons
    assert any("t2_local_cache.db" in r for r in reasons), reasons
