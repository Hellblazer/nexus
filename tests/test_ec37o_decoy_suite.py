# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o step 6: the end-to-end decoy suite.

A repository's ``.claude/settings.json`` env reaches the plugin hooks, the MCP server and
every ``nx`` the model runs, so ``NX_SERVICE_URL`` / ``NX_SERVICE_HOST`` / ``NX_SERVICE_PORT``
can name any host. Each stored credential (a saved ``service_token`` with and without a saved
``service_url``, a saved ``mint_token``, the supervisor lease token, a data-token lease) is
bound to the origin it was issued for, so a caller pointed at a decoy must send it nothing.

This module is the whole-system check. Two port-0 ``http.server`` instances, a *real* one the
credential belongs to and a *decoy* the env names, record every ``Authorization`` header and
request path. Each caller runs as a real process (the bare-python plugin drain included), in
each env leg, with each credential kind stored. The decoy must see no credential at all, and
where the env names the bound origin the real server must receive it. The per-step suites
(``tests/db/test_ec37o_cli_origin_callers.py``, ``test_data_token_origin_binding.py``,
``test_credential_origin_binding.py``, ``tests/hooks/test_drain_credential_binding.py``,
``test_origin_binding_parity.py``) pin each primitive and caller on its own; this one asserts
the claim across them. The lint over the primitive is ``tests/test_ec37o_credential_read_lint.py``.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from nexus._winsec import restrict_to_owner
from nexus.engine_version import REQUIRED_ENGINE_VERSION
from tests._child_encoding import PYTHON_CHILD_ENCODING
from tests._settle import settle
from tests.db.test_data_token_origin_binding import _write_lease_file
from tests.hooks.test_mailbox_drain_hook import (
    _PLUGIN_ARGV,
    _VERB_ARGV,
    _payload,
    _write_storage_lease,
)

_ROUTING_DIR = Path(__file__).resolve().parents[1] / "conexus" / "hooks" / "scripts" / "routing"

SAVED = "Bearer SAVED-SERVICE-TOKEN"
MINT = "Bearer SAVED-MINT-TOKEN"
LEASE = "Bearer local-supervisor-token"  # what _write_storage_lease puts in the lease
DATA_LEASE = "Bearer LEASED"  # what _write_lease_file puts in a data-token lease
EXPORTED = "Bearer exported-by-the-user"
_ENGINE_VERSION = ".".join(str(part) for part in REQUIRED_ENGINE_VERSION)


# ── the two servers ──────────────────────────────────────────────────────────


class _Server:
    """A port-0 HTTP server recording ``(path, Authorization)`` of every request.

    It answers every route with something the callers accept (a mint reply, an empty list, a
    plain object) so a caller that gets a credential is never held up by a 4xx and a retry; the
    assertions are on what arrived, not on what the caller did with the reply.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, str | None]] = []
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length:
                    self.rfile.read(length)
                outer.requests.append((self.path, self.headers.get("Authorization")))
                if "/data-tokens/mint" in self.path:
                    body: object = {"data_token": "minted-data-token", "expires_in_seconds": 600}
                elif self.path == "/version":
                    # the T3 client's unauthenticated compatibility probe; a current engine passes it
                    body = {"app_version": "1.0", "release_version": _ENGINE_VERSION}
                elif "/memory/list" in self.path or "/memory/projects" in self.path:
                    body = []
                else:
                    body = {"ok": True}
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _answer  # noqa: N815 -- stdlib callback names

            def log_message(self, *_a: object) -> None:
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    @property
    def auth(self) -> list[str]:
        return [a for _p, a in self.requests if a is not None]

    @property
    def paths(self) -> list[str]:
        return [p for p, _a in self.requests]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def real() -> Iterator[_Server]:
    server = _Server()
    yield server
    server.close()


@pytest.fixture
def decoy() -> Iterator[_Server]:
    server = _Server()
    yield server
    server.close()


# ── the stored credentials ───────────────────────────────────────────────────


def _config_dir(tmp_path: Path) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    return cfg


def _save(tmp_path: Path, credentials: dict[str, str]) -> None:
    path = _config_dir(tmp_path) / "config.yml"
    path.write_text(yaml.safe_dump({"credentials": credentials}), encoding="utf-8")
    restrict_to_owner(path)


def _store_saved_with_url(tmp_path: Path, real: _Server) -> None:
    _save(tmp_path, {"service_url": real.url, "service_token": "SAVED-SERVICE-TOKEN"})


def _store_saved_no_url(tmp_path: Path, real: _Server) -> None:
    # Bound to the default managed URL (Sam's ruling 3): neither loopback server is that origin.
    _save(tmp_path, {"service_token": "SAVED-SERVICE-TOKEN"})


def _store_saved_mint(tmp_path: Path, real: _Server) -> None:
    _save(tmp_path, {"service_url": real.url, "mint_token": "SAVED-MINT-TOKEN"})


def _store_supervisor_lease(tmp_path: Path, real: _Server) -> None:
    _write_storage_lease(_config_dir(tmp_path), host="127.0.0.1", port=real.port)


def _store_data_lease(tmp_path: Path, real: _Server) -> None:
    # A data-token lease is a credential only on a mint-armed box (the CLI's data-token override
    # asks for one); the saved mint_token it is armed by is bound to the same origin.
    _store_saved_mint(tmp_path, real)
    path = _write_lease_file(_config_dir(tmp_path), real.url, scheme="http", tenant="default")
    path.chmod(0o600)


@dataclass(frozen=True)
class _Kind:
    store: Callable[[Path, _Server], None]
    #: the Authorization value the real server receives when the env names the bound origin
    header: str | None


KINDS: dict[str, _Kind] = {
    "saved_service_token_with_url": _Kind(_store_saved_with_url, SAVED),
    "saved_service_token_no_url": _Kind(_store_saved_no_url, None),
    "saved_mint_token": _Kind(_store_saved_mint, MINT),
    "supervisor_lease": _Kind(_store_supervisor_lease, LEASE),
    "data_token_lease": _Kind(_store_data_lease, DATA_LEASE),
}

# ── the env legs ─────────────────────────────────────────────────────────────

#: leg -> which server the env names ("" = no env at all, the baseline that proves the harness
#: and the credential work); the host/port legs set NX_SERVICE_HOST and NX_SERVICE_PORT instead
#: of NX_SERVICE_URL.
URL_LEGS = ("bare", "url_real", "url_decoy")
HOSTPORT_LEGS = ("hostport_real", "hostport_decoy")


def _leg_env(leg: str, real: _Server, decoy: _Server) -> dict[str, str]:
    return {
        "bare": {},
        "url_real": {"NX_SERVICE_URL": real.url},
        "url_decoy": {"NX_SERVICE_URL": decoy.url},
        "hostport_real": {"NX_SERVICE_HOST": "127.0.0.1", "NX_SERVICE_PORT": str(real.port)},
        "hostport_decoy": {"NX_SERVICE_HOST": "127.0.0.1", "NX_SERVICE_PORT": str(decoy.port)},
    }[leg]


def _env_names_the_bound_origin(kind: str, leg: str) -> bool:
    """Does this leg name the origin the credential was issued for (so the real server must get it)?

    A saved credential is bound to its saved ``service_url`` and a data-token lease to its netloc,
    both reached by the bare leg or an env URL equal to them. The supervisor lease is bound to its
    own endpoint, which an env host and port can also name. A token saved with no ``service_url``
    is bound to the default managed URL, which no loopback server is.
    """
    if kind == "saved_service_token_no_url":
        return False
    if leg in ("bare", "url_real"):
        return True
    return leg == "hostport_real" and kind == "supervisor_lease"


# ── the callers, each run as a real process ──────────────────────────────────

_ENV_SCRUBBED = ("NX_SERVICE_", "NX_MINT_")
_PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy")


def _child_env(tmp_path: Path, leg_env: dict[str, str]) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(_ENV_SCRUBBED) and k.lower() not in _PROXY_VARS
    }
    env["NEXUS_CONFIG_DIR"] = str(_config_dir(tmp_path))
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env["NX_DROPPED_WRITES_LOG_PATH"] = str(tmp_path / "dropped.jsonl")
    if not any(k.lower() in _PROXY_VARS for k in leg_env):
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    env.update(leg_env)
    return env


def _spawn(argv: list[str], tmp_path: Path, leg_env: dict[str, str], *, stdin: str | None = None):
    return subprocess.run(
        argv, input=stdin, capture_output=True, text=True, encoding=PYTHON_CHILD_ENCODING,
        env=_child_env(tmp_path, leg_env), timeout=300,
    )


_PREFIX_SCAN = "import sys; from nexus.hooks.t2_prefix_scan import scan; sys.stdout.write(scan('nexus'))"

_MCP_T2 = """
from nexus import mcp_infra as m
for probe in (m._service_t2_endpoint_key_uncached, m._engine_file_key):
    try:
        probe()
    except Exception as exc:
        print('PROBE_REFUSED', type(exc).__name__)
try:
    db = m._build_service_t2_db()
    db.memory.get_projects_with_prefix('nexus')
    db.close()
except Exception as exc:
    print('T2_REFUSED', type(exc).__name__)
"""

_WHOAMI = "from nexus.health import _check_static_service_token; print(_check_static_service_token())"

_BEARER_FOR = """
import os, sys
from pathlib import Path
from nexus.db.data_token import DataTokenManager
try:
    print('BEARER=' + str(DataTokenManager(config_dir=Path(os.environ['NEXUS_CONFIG_DIR'])).bearer_for(sys.argv[1], 'default')))
except Exception as exc:
    print('BEARER_REFUSED=' + type(exc).__name__)
"""

_ROUTING = (
    "import sys; sys.path.insert(0, sys.argv[1]); import _lib; "
    "_lib._send_routing_event({'rule': 'r', 'outcome': 'allow'})"
)


@dataclass(frozen=True)
class _Caller:
    name: str
    run: Callable[[Path, dict[str, str]], subprocess.CompletedProcess[str]]
    #: credential kinds the caller consults at all; a kind outside it is not stored for the run
    kinds: tuple[str, ...]
    legs: tuple[str, ...] = URL_LEGS + HOSTPORT_LEGS
    #: kinds the caller reads but, with this env, sends nothing for even at the bound origin
    #: (a hook cannot mint, so a saved mint_token buys it no request)
    sends_nothing_for: frozenset[str] = frozenset()
    #: wait for a detached child (the routing poster) before reading the servers
    after: Callable[[Path, _Server, _Server, bool], None] | None = None
    extra: dict[str, object] = field(default_factory=dict)


def _py(code: str, *args: str) -> Callable[[Path, dict[str, str]], subprocess.CompletedProcess[str]]:
    return lambda tmp_path, env: _spawn([sys.executable, "-c", code, *args], tmp_path, env)


def _drain(argv: list[str]) -> Callable[[Path, dict[str, str]], subprocess.CompletedProcess[str]]:
    return lambda tmp_path, env: _spawn(argv, tmp_path, env, stdin=_payload())


def _nx(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _spawn([sys.executable, "-m", "nexus.cli", "memory", "list", "--project", "nexus"], tmp_path, env)


def _nx_t3(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """``nx search`` reaches the T3 vector client, which resolves its endpoint on its own path."""
    return _spawn([sys.executable, "-m", "nexus.cli", "search", "ec37o decoy"], tmp_path, env)


def _bearer_for(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    url = env.get("NX_SERVICE_URL") or env["_NX_TEST_REAL_URL"]
    return _spawn([sys.executable, "-c", _BEARER_FOR, url], tmp_path, {k: v for k, v in env.items() if k != "_NX_TEST_REAL_URL"})


def _routing(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _spawn([sys.executable, "-c", _ROUTING, str(_ROUTING_DIR)], tmp_path, env)


def _wait_for_poster(tmp_path: Path, real: _Server, decoy: _Server, expects_real: bool) -> None:
    """The routing poster is a detached child: wait for its POST, or for its drop record."""
    dropped = tmp_path / "dropped.jsonl"
    if expects_real:
        settle(lambda: not real.auth, what="the poster child's POST to the bound origin")
    else:
        settle(lambda: not (dropped.exists() and dropped.read_text(encoding="utf-8")) and not real.requests and not decoy.requests,
               what="the poster child's drop record")


CALLERS: tuple[_Caller, ...] = (
    _Caller("nx_subprocess", _nx, tuple(KINDS)),
    _Caller("nx_t3_vector_client", _nx_t3, tuple(KINDS)),
    _Caller("drain_wheel_verb", _drain(_VERB_ARGV), tuple(KINDS), sends_nothing_for=frozenset({"saved_mint_token"})),
    _Caller("drain_plugin_script", _drain(_PLUGIN_ARGV), tuple(KINDS), sends_nothing_for=frozenset({"saved_mint_token"})),
    _Caller("t2_prefix_scan_hook", _py(_PREFIX_SCAN), tuple(KINDS)),
    _Caller("mcp_t2_construction", _py(_MCP_T2), tuple(KINDS)),
    _Caller("routing_telemetry_poster", _routing, tuple(KINDS), sends_nothing_for=frozenset({"saved_mint_token"}), after=_wait_for_poster),
    _Caller("doctor_whoami_probe", _py(_WHOAMI), ("saved_service_token_with_url", "saved_service_token_no_url"), legs=URL_LEGS),
    _Caller("mint_armed_bearer_for", _bearer_for, ("saved_mint_token", "data_token_lease"), legs=URL_LEGS),
)


def _matrix() -> list[pytest.param]:
    params = []
    for caller in CALLERS:
        for kind in caller.kinds:
            for leg in caller.legs:
                if leg in HOSTPORT_LEGS and kind not in ("supervisor_lease", "saved_service_token_with_url"):
                    continue  # an env host and port never reaches a credential that is not a lease or a saved token
                params.append(pytest.param(caller, kind, leg, id=f"{caller.name}-{kind}-{leg}"))
    return params


@pytest.mark.parametrize(("caller", "kind", "leg"), _matrix())
def test_a_decoy_gets_no_credential_and_the_bound_origin_does(tmp_path, real, decoy, caller, kind, leg):
    KINDS[kind].store(tmp_path, real)
    env = _leg_env(leg, real, decoy)
    if caller.name == "mint_armed_bearer_for":
        env = {**env, "_NX_TEST_REAL_URL": real.url}
    proc = caller.run(tmp_path, env)

    names_bound = _env_names_the_bound_origin(kind, leg)
    expected = KINDS[kind].header if names_bound and kind not in caller.sends_nothing_for else None
    if caller.after is not None:
        caller.after(tmp_path, real, decoy, expected is not None)
    context = (proc.returncode, proc.stdout[-600:], proc.stderr[-600:], real.requests, decoy.requests)

    assert decoy.auth == [], context  # no credential of any kind reaches the decoy
    if kind == "saved_service_token_no_url":
        assert real.auth == [], context  # bound to the managed default, which neither server is
    # whatever the bound origin does receive is its own credential, never another kind's
    assert set(real.auth) <= {KINDS[kind].header, "Bearer minted-data-token"}, context
    if expected is None:
        return
    if caller.name == "mint_armed_bearer_for" and kind == "data_token_lease":
        assert "BEARER=LEASED" in proc.stdout, context  # served from the lease file; no request is needed
        return
    assert expected in real.auth, context



# ── the two accepted residuals (Sam's ruling 4, 2026-11-26; T2 nexus/sam-ruling-ec37o-token-host-binding-2026-11-26) ──
#
# These are KNOWN LIMITS, pinned so a change to either is a decision and not an accident.


_TOKEN_CALLERS = tuple(c for c in CALLERS if c.name not in ("mint_armed_bearer_for", "doctor_whoami_probe", "routing_telemetry_poster"))


@pytest.mark.parametrize("caller", _TOKEN_CALLERS, ids=lambda c: c.name)
def test_residual_one_an_exported_service_token_travels_with_the_env_url(tmp_path, real, decoy, caller):
    """Accepted residual (Sam 2026-11-26): a token the user exported (``NX_SERVICE_TOKEN``) is unbound,
    so a repository's env URL carries it to the decoy. A stored credential is bound; an exported one
    is the user's own statement of where it goes, and no detector second-guesses it."""
    _store_supervisor_lease(tmp_path, real)  # a bound credential beside it stays home
    proc = caller.run(tmp_path, {"NX_SERVICE_URL": decoy.url, "NX_SERVICE_TOKEN": "exported-by-the-user"})
    assert EXPORTED in decoy.auth, (proc.returncode, proc.stderr[-400:], decoy.requests)
    assert LEASE not in decoy.auth + real.auth


@pytest.mark.parametrize("caller", [c for c in CALLERS if c.name == "mint_armed_bearer_for"], ids=lambda c: c.name)
def test_residual_one_an_exported_mint_token_travels_with_the_env_url(tmp_path, real, decoy, caller):
    """Same residual for ``NX_MINT_TOKEN``: the mint credential is posted to whatever URL it is given."""
    proc = caller.run(tmp_path, {"NX_SERVICE_URL": decoy.url, "NX_MINT_TOKEN": "exported-mint", "_NX_TEST_REAL_URL": real.url})
    assert "Bearer exported-mint" in decoy.auth, (proc.stdout, proc.stderr[-400:], decoy.requests)


@pytest.mark.parametrize("caller", CALLERS, ids=lambda c: c.name)
def test_residual_two_an_env_url_with_no_credential_is_sent_no_credential(tmp_path, real, decoy, caller):
    """Residual two as Sam accepted it: an env URL with no token still lets a repository-chosen
    host be named. On this tree no caller builds an authenticated or row-reading request to it
    without a credential (the mailbox drain, the T2 prefix scan, the MCP server and ``nx`` stop at
    "not resolvable"). The one request that does go out is the T3 client's unauthenticated
    ``GET /version`` compatibility probe, whose answer only decides an error message. The ruling kept
    this as a documented limit rather than a detector; this pins the boundary as it stands, so a
    caller that starts sending a read to the env URL turns the residual real and fails here first."""
    env = {"NX_SERVICE_URL": decoy.url}
    if caller.name == "mint_armed_bearer_for":
        env["_NX_TEST_REAL_URL"] = real.url
    caller.run(tmp_path, env)
    if caller.after is not None:
        caller.after(tmp_path, real, decoy, False)
    allowed = [("/version", None)] if caller.name == "nx_t3_vector_client" else []
    assert decoy.requests == allowed and real.requests == []


# ── transport rules (step 5): no bearer in the clear off this machine, no proxy for a lease token ──
#
# ``127.1`` reaches the loopback server through the platform resolver, yet is neither ``localhost``
# nor an IP literal, so it is a host that is not "this machine" as far as the cleartext rule can
# tell. That makes the refusal observable: a caller that sent a bearer anyway would put it on the
# recording server.

_OFF_MACHINE_HOST = "127.1"

#: every caller whose requests carry a bearer, and the cases that need step 5 to be green
_STEP_5 = pytest.mark.xfail(strict=True, reason="nexus-ec37o step 5")

#: (caller, leg) pairs that send a lease token through an env-named proxy until step 5 lands
_PROXY_FAILS_UNTIL_STEP_5: frozenset[tuple[str, str]] = frozenset(
    (caller.name, leg)
    for caller in CALLERS
    if caller.name not in ("doctor_whoami_probe", "mint_armed_bearer_for")
    for leg in ("bare", "url_real", "hostport_real")
    # the two hook drains already dial the lease endpoint directly when the URL came from the lease
    if not (caller.name.startswith("drain_") and leg == "bare")
)


def _off_machine_url(server: _Server) -> str:
    return f"http://{_OFF_MACHINE_HOST}:{server.port}"


def test_the_off_machine_host_really_reaches_the_loopback_server(real):
    """Non-vacuity: the cleartext cases below assert an absence, so first prove the address works."""
    import urllib.request  # noqa: PLC0415 -- this test only

    with urllib.request.urlopen(_off_machine_url(real) + "/probe", timeout=20) as resp:  # noqa: S310 -- loopback
        assert resp.status == 200
    assert real.paths == ["/probe"]


def _cleartext_case(tmp_path: Path, server: _Server, source: str) -> dict[str, str]:
    url = _off_machine_url(server)
    if source == "exported_token":
        return {"NX_SERVICE_URL": url, "NX_SERVICE_TOKEN": "exported-by-the-user"}
    if source == "saved_token_and_url":
        _save(tmp_path, {"service_url": url, "service_token": "SAVED-SERVICE-TOKEN"})
        return {}
    if source == "saved_mint_and_url":
        _save(tmp_path, {"service_url": url, "mint_token": "SAVED-MINT-TOKEN"})
        return {}
    if source == "exported_mint":
        return {"NX_SERVICE_URL": url, "NX_MINT_TOKEN": "exported-mint"}
    raise AssertionError(source)


_CLEARTEXT_SOURCES = ("exported_token", "saved_token_and_url", "saved_mint_and_url", "exported_mint")


@pytest.mark.parametrize("source", _CLEARTEXT_SOURCES)
@pytest.mark.parametrize("caller", [c for c in CALLERS if c.name != "mint_armed_bearer_for"], ids=lambda c: c.name)
def test_no_bearer_over_plain_http_to_a_host_that_is_not_this_machine(tmp_path, real, caller, source):
    env = _cleartext_case(tmp_path, real, source)
    proc = caller.run(tmp_path, env)
    if caller.after is not None:
        caller.after(tmp_path, real, real, False)
    assert real.auth == [], (proc.returncode, proc.stderr[-400:], real.requests)
    if caller.name != "routing_telemetry_poster" and source in ("exported_token", "saved_token_and_url"):
        # refused for the right reason: the transport rule names itself (the poster child is silent)
        output = proc.stdout + proc.stderr
        assert "plain http" in output or "InsecureServiceTransportError" in output, output[-400:]


@pytest.mark.parametrize("source", _CLEARTEXT_SOURCES)
def test_no_mint_credential_over_plain_http_to_a_host_that_is_not_this_machine(tmp_path, real, source):
    env = _cleartext_case(tmp_path, real, source)
    caller = next(c for c in CALLERS if c.name == "mint_armed_bearer_for")
    proc = caller.run(tmp_path, {**env, "NX_SERVICE_URL": _off_machine_url(real), "_NX_TEST_REAL_URL": real.url})
    assert real.auth == [], (proc.stdout, proc.stderr[-400:], real.requests)


def _proxy_matrix() -> list[pytest.param]:
    params = []
    for caller in CALLERS:
        if caller.name in ("doctor_whoami_probe", "mint_armed_bearer_for"):
            continue  # neither carries a lease token
        for leg in ("bare", "url_real", "hostport_real"):
            marks = [_STEP_5] if (caller.name, leg) in _PROXY_FAILS_UNTIL_STEP_5 else []
            params.append(pytest.param(caller, leg, marks=marks, id=f"{caller.name}-{leg}"))
    return params


@pytest.mark.parametrize(("caller", "leg"), _proxy_matrix())
def test_a_lease_token_never_goes_through_a_proxy(tmp_path, real, decoy, caller, leg):
    """The supervisor lease token authorizes engine writes and is bound to a loopback engine, so a
    proxy named by the environment (a repository's ``http_proxy``) must never carry it, whether the
    URL came from the lease or from an env URL or port that names the same engine. ``decoy`` stands
    in as the proxy; the loopback engine still gets its token directly."""
    _store_supervisor_lease(tmp_path, real)
    proxy_env = {"http_proxy": decoy.url, "HTTP_PROXY": decoy.url, **_leg_env(leg, real, decoy)}
    proc = caller.run(tmp_path, proxy_env)
    if caller.after is not None:
        caller.after(tmp_path, real, decoy, True)
    context = (proc.returncode, proc.stderr[-400:], real.requests, decoy.requests)
    assert decoy.requests == [], context  # nothing, not just no credential: the lease holder dials the engine directly
    assert LEASE in real.auth, context
