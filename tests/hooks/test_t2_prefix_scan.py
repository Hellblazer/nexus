# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nexus.hooks.t2_prefix_scan.scan()`` over the engine's T2 HTTP API.

nexus-8fvp2: T2 moved to Postgres (behind the engine's ``/v1/memory`` HTTP
API) at RDR-158 P4; the plugin script this module was ported from was
frozen reading a dead SQLite file for six weeks with zero signal. The
behaviour pinned here survived the port (RDR-215 bead nexus-b5ugt) and the
plugin copy's deletion (nexus-z9cz2): entries render per namespace, a
reachability failure is a visible warning and never a silent empty, a
stale freshest entry warns alongside the data, one bad namespace does not
discard the others, and the namespace count and wall-clock budget bound
the fetch loop.

Endpoint and credential resolution are not re-pinned here. The plugin
copy carried its own stdlib re-implementation of them, and its tests went
with it; the wheel module reaches the shared resolver through
``HttpMemoryStore``, which has its own suites.

``scan()`` runs in a SUBPROCESS against a mock engine on a real socket, for
the reason ``test_t2_prefix_scan_stdout.py`` gives: it configures
structlog on entry, which must not reconfigure this test process.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

_TOKEN = "test-bearer-token"
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

_SCAN_PROBE = (
    "import sys\n"
    "from nexus.hooks.t2_prefix_scan import scan\n"
    "sys.stdout.write(scan(sys.argv[1]))\n"
)


# ── Mock engine ──────────────────────────────────────────────────────────────


class _MockMemoryEngine:
    """Minimal stand-in for the Java engine's ``/v1/memory`` HTTP surface.

    Serves exactly the two GET routes ``scan()`` calls through
    ``HttpMemoryStore``: ``/v1/memory/projects?prefix=`` and
    ``/v1/memory/all?project=``. Any other path is a 404.
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
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
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
) -> str:
    """Run ``scan(project_name)`` in a child interpreter and return what it
    returned. Isolated from any ambient ``NX_SERVICE_*`` env (the suite's
    own substrate fixtures set it) and from the real config dir and home,
    so no live engine or lease on this box can answer."""
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("NX_SERVICE_")}
    full_env["NEXUS_CONFIG_DIR"] = str(config_dir)
    full_env["HOME"] = str(config_dir)
    full_env.update(env or {})
    result = subprocess.run(
        [sys.executable, "-c", _SCAN_PROBE, project_name],
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"scan exit {result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    return result.stdout


def _engine_env(engine: _MockMemoryEngine, **extra: str) -> dict[str, str]:
    return {"NX_SERVICE_URL": engine.base_url, "NX_SERVICE_TOKEN": _TOKEN, **extra}


# ── Happy path ───────────────────────────────────────────────────────────────


def test_runs_over_http_and_surfaces_entries(tmp_path: Path, mock_engine) -> None:
    """Talks HTTP, never SQLite; surfaces entries under per-namespace labels."""
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
    out = _run("nexus", config_dir=tmp_path, env=_engine_env(engine))
    assert "### T2 Memory" in out
    assert "release-5-3-0-validation" in out
    assert "rdr-memory-audit" in out
    assert "### T2 Memory (rdr)" in out
    assert "rdr-129" in out
    assert "WARNING" not in out
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
    out = _run("nexus", config_dir=tmp_path, env=_engine_env(engine))
    assert out.index("release-5-3-0-validation") < out.index("rdr-memory-audit")


# ── Two-arm freshness assert (nexus-8fvp2 enlargement (d)) ──────────────────


def test_empty_t2_is_not_confused_with_unreachable(tmp_path: Path, mock_engine) -> None:
    """nexus-8fvp2 enlargement (b): reachable engine, zero matching
    namespaces (a fresh install's genuinely empty T2) is a clean empty
    result, never rendered as (or alongside) an unreachable warning."""
    engine = mock_engine(projects=[])
    out = _run("unknown_project_xyz", config_dir=tmp_path, env=_engine_env(engine))
    assert out == ""
    assert any("/v1/memory/projects" in p for p in engine.requests), (
        "empty output proves nothing unless the engine was actually asked"
    )


def test_unreachable_arm_warns_when_no_endpoint_resolvable(tmp_path: Path) -> None:
    """Arm 1: no env, a config dir that does not exist yet, no lease (fresh
    install / no supervisor running) -> a VISIBLE warning line, never the
    pre-fix silent no-op."""
    out = _run("nexus", config_dir=tmp_path / "does-not-exist-yet", env={})
    assert "WARNING" in out
    assert "unreachable" in out.lower()


def test_unreachable_arm_warns_on_connection_refused(tmp_path: Path) -> None:
    """Arm 1: endpoint resolves (env is set) but nothing is listening ->
    still a visible warning, not a silent empty result."""
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
        entries_by_project={"nexus": [_entry("old-entry", "This is old.", stale_ts)]},
    )
    out = _run(
        "nexus", config_dir=tmp_path, env=_engine_env(engine, NX_T2_SCAN_STALE_DAYS="14")
    )
    assert "WARNING" in out
    assert "d old" in out  # the "Nd old" phrasing
    assert "old-entry" in out  # data is still surfaced, not dropped


def test_fresh_entries_produce_no_staleness_warning(tmp_path: Path, mock_engine) -> None:
    now = _now()
    engine = mock_engine(
        projects=[{"project": "nexus", "last_updated": _iso(now)}],
        entries_by_project={"nexus": [_entry("fresh-entry", "Recent.", now)]},
    )
    out = _run(
        "nexus", config_dir=tmp_path, env=_engine_env(engine, NX_T2_SCAN_STALE_DAYS="14")
    )
    assert "WARNING" not in out
    assert "fresh-entry" in out


# ── Bounded, failure-isolated fetch loop ─────────────────────────────────────


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
    out = _run("nexus", config_dir=tmp_path, env=_engine_env(engine))
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
    entries = {p["project"]: [_entry(f"entry-{p['project']}", "content", now)] for p in projects}
    engine = mock_engine(projects=projects, entries_by_project=entries)
    out = _run("nexus", config_dir=tmp_path, env=_engine_env(engine))
    all_requests = [r for r in engine.requests if "/v1/memory/all" in r]
    assert 0 < len(all_requests) <= 5
    assert "not checked" in out


def test_scan_budget_stops_the_fetch_loop(tmp_path: Path, mock_engine) -> None:
    """A zero scan budget must stop the per-namespace loop before issuing
    any fetch and say so visibly — proves the wall-clock budget is enforced
    independent of ``_HARD_CAP`` (nexus-9xado)."""
    now = _now()
    projects = [
        {"project": f"nexus_ns{i}", "last_updated": _iso(now - timedelta(minutes=i))}
        for i in range(3)
    ]
    entries = {p["project"]: [_entry(f"entry-{p['project']}", "content", now)] for p in projects}
    engine = mock_engine(projects=projects, entries_by_project=entries)
    out = _run("nexus", config_dir=tmp_path, env=_engine_env(engine, NX_T2_SCAN_BUDGET_S="0"))
    assert "scan budget exceeded" in out
    assert not [r for r in engine.requests if "/v1/memory/all" in r]


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
# a shipped hook actually opens SQLite. Scoped to the shipped-hook surface,
# which is WHERE THE HOOKS LIVE, not one directory (nexus-44812): it scanned
# only ``conexus/hooks/scripts/`` while RDR-215 and nexus-t9klx moved every
# hook but a handful into ``src/nexus/hooks/``, so its coverage shrank to
# "the hooks not yet ported" with its green unchanged. A hypothetical live
# SQLite read elsewhere in ``src/nexus/`` is still NOT caught here.


#: Stdlib modules that back an embedded/local-file database — the
#: "retired persistence substrate" class, not just the literal name
#: ``sqlite3``. ``shelve``/``dbm`` are stdlib wrappers around the same
#: kind of on-disk file store T2 retired away from (RDR-158 P4).
_BANNED_DB_MODULES = frozenset({"sqlite3", "shelve", "dbm"})

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Every directory a shipped hook's code lives in, each with the number of
#: ``.py`` files measured there (2026-09-23, nexus-44812). A floor, not an
#: exact count: raise it when a root grows, and read a drop as the walk
#: going blind before reading it as a deletion. A root emptied on purpose
#: is REMOVED from this map, never left at zero, because a floor of zero is
#: satisfied by a walk that sees nothing. There is no ``.sh`` arm: RDR-215
#: deleted every plugin shell script, and a scan over a directory with none
#: in it is the vacuous half this lint used to have. ``conexus/hooks/scripts``
#: dropped 12 -> 1 at nexus-z9cz2, which deleted the eleven plugin copies
#: nothing shipped executed; ``divergence-language-scan.py`` remains.
_HOOK_CODE_ROOTS: dict[str, int] = {
    "conexus/hooks/scripts": 1,
    "sn/hooks/scripts": 5,
    "src/nexus/hooks": 32,
    "src/nexus/_hook_runtime": 4,
}


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
    offenders: dict[str, list[str]] = {}
    scanned: dict[str, int] = {}
    for root, floor in _HOOK_CODE_ROOTS.items():
        files = sorted(p for p in (_REPO_ROOT / root).rglob("*.py") if p.is_file())
        scanned[root] = len(files)
        for path in files:
            reasons = _python_offenders(path)
            if reasons:
                offenders[str(path.relative_to(_REPO_ROOT))] = reasons
    short = {r: n for r, n in scanned.items() if n < _HOOK_CODE_ROOTS[r]}
    assert not short, (
        f"scanned fewer hook files than measured: {short} against floors "
        f"{_HOOK_CODE_ROOTS}. A clean result over less than was measured "
        "is not a clean result; rule out the walk going blind first."
    )
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
