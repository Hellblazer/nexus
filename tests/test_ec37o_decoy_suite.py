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


def _no_line_breaks(value: str) -> str:
    """A header value built from request text carries no CR or LF, so it cannot split the response."""
    return value.replace("\r", "").replace("\n", "")


# ── the two servers ──────────────────────────────────────────────────────────


class _Server:
    """A port-0 HTTP server recording ``(path, Authorization)`` of every request.

    It answers every route with something the callers accept (a mint reply, an empty list, a
    plain object) so a caller that gets a credential is never held up by a 4xx and a retry; the
    assertions are on what arrived, not on what the caller did with the reply.
    """

    def __init__(self, bind: str = "127.0.0.1") -> None:
        self.requests: list[tuple[str, str | None]] = []
        #: set to a base URL and every request but the T3 client's unauthenticated ``/version`` compatibility
        #: probe is answered ``redirect_code`` to that URL plus the request path. The probe is answered normally:
        #: it carries no bearer, and a redirected probe fails the T3 caller before its prefetch thread has sent
        #: the bearer-carrying request this test is about, so what reached the bound origin would depend on a race
        #: (the mint POST lost it on chas every run, nexus-ec37o re-land).
        self.redirect_to: str | None = None
        self.redirect_code = 302
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length:
                    self.rfile.read(length)
                outer.requests.append((self.path, self.headers.get("Authorization")))
                if outer.redirect_to is not None and self.path != "/version":
                    self.send_response(outer.redirect_code)
                    self.send_header("Location", outer.redirect_to + _no_line_breaks(self.path))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
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

            def do_CONNECT(self) -> None:  # noqa: N802 -- stdlib callback name
                # a proxy tunnel request: record it and refuse (no upstream exists)
                outer.requests.append((f"CONNECT {self.path}", self.headers.get("Authorization")))
                self.send_error(502)

            do_GET = do_POST = _answer  # noqa: N815 -- stdlib callback names

            def log_message(self, *_a: object) -> None:
                pass

        self._httpd = http.server.ThreadingHTTPServer((bind, 0), _Handler)
        self.port = self._httpd.server_address[1]
        self.url = f"http://{bind}:{self.port}"
        #: a URL for this server whose host the cleartext rule does not call "this machine" (``off_machine``)
        self.off_machine_url = self.url
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


def _account_home(tmp_path: Path) -> Path:
    """The account home the routing poster child is given through ``_audience._ACCOUNT_HOME_OVERRIDE``, the in-process
    seam, since the poster reads the account's own config directory and no environment variable (RDR-259 Decision 9)."""
    return tmp_path / "acct"


def _config_dir(tmp_path: Path) -> Path:
    """The one config directory every caller reads: the nexus callers through ``NEXUS_CONFIG_DIR``, the routing poster as
    ``<account home>/.config/nexus``."""
    cfg = _account_home(tmp_path) / ".config" / "nexus"
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

#: NX_LOCAL is the provisioned suite's own mode switch (it makes the T3 client skip its /version compatibility
#: probe); a decoy case about what a caller sends must not depend on it, so no child inherits it
_ENV_SCRUBBED = ("NX_SERVICE_", "NX_MINT_", "NX_LOCAL")
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
    "import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + '/..'); "
    "import _audience; _audience._ACCOUNT_HOME_OVERRIDE = sys.argv[2]; import _lib; "
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
    #: False for the routing poster: an env URL, host or port names no endpoint for it at all (RDR-259 Decision 9), so in
    #: every env leg it posts to the origin the account's config binds the credential to, and never to the env's
    reads_env_endpoint: bool = True
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
    """``_NX_TEST_ACCOUNT_HOME`` in *env* names the account home (default :func:`_account_home`); it is not passed on."""
    home = env.get("_NX_TEST_ACCOUNT_HOME") or str(_account_home(tmp_path))
    child = {k: v for k, v in env.items() if k != "_NX_TEST_ACCOUNT_HOME"}
    return _spawn([sys.executable, "-c", _ROUTING, str(_ROUTING_DIR), home], tmp_path, child)


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
    _Caller(
        "routing_telemetry_poster", _routing, tuple(KINDS), sends_nothing_for=frozenset({"saved_mint_token"}), after=_wait_for_poster,
        reads_env_endpoint=False,
    ),
    _Caller("doctor_whoami_probe", _py(_WHOAMI), ("saved_service_token_with_url", "saved_service_token_no_url"), legs=URL_LEGS),
    _Caller("mint_armed_bearer_for", _bearer_for, ("saved_mint_token", "data_token_lease"), legs=URL_LEGS),
)


# ── which cases refuse (the storage-touch guard) ────────────────────────────
#
# A case in which a caller is refused a credential, or finds no endpoint, constructs the resolver's error
# in the child, and the storage-touch guard (``tests/_storage_touch_guard.py``) charges that to the test
# unless it carries ``expects_unresolvable_endpoint``. A case that sends nothing for another reason
# (a plugin script that never constructs a nexus error class, the routing poster) does not construct one,
# and the guard calls the mark on it STALE. So the mark is per case, and the guard holds both directions:
# a refusing case without it fails, a marked case that does not refuse fails.

_UNRESOLVABLE = pytest.mark.expects_unresolvable_endpoint
#: callers that build the nexus storage clients themselves, so every refusal raises in the child
STORAGE_CLIENTS = frozenset({"nx_subprocess", "nx_t3_vector_client", "t2_prefix_scan_hook", "mcp_t2_construction"})
#: the wheel drain and the doctor probe resolve through the same primitive but stop earlier for some kinds
#: and legs; these are the (kind, leg) cases where each one raises it
_REFUSED_BY_DRAIN = frozenset({
    ("saved_service_token_with_url", "url_decoy"), ("saved_service_token_no_url", "url_real"),
    ("saved_service_token_no_url", "url_decoy"), ("supervisor_lease", "url_decoy"), ("supervisor_lease", "hostport_decoy"),
})
_REFUSED_BY_DOCTOR = _REFUSED_BY_DRAIN - {("supervisor_lease", "url_decoy"), ("supervisor_lease", "hostport_decoy")}


def _refuses(caller: str, kind: str, leg: str) -> bool:
    if caller in STORAGE_CLIENTS:
        # an env host and port that names no lease leaves a saved token alone: nothing is resolved, nothing refused
        return not _env_names_the_bound_origin(kind, leg) and not (kind == "saved_service_token_with_url" and leg in HOSTPORT_LEGS)
    if caller == "drain_wheel_verb":
        return (kind, leg) in _REFUSED_BY_DRAIN
    if caller == "doctor_whoami_probe":
        return (kind, leg) in _REFUSED_BY_DOCTOR
    return False  # the plugin drain and the routing poster are stdlib: they construct no nexus error


#: the T3 client reads /version from whatever answers; a 3xx, or a recording proxy standing in for the engine,
#: is not a conformant engine, and the client raises ManagedServiceIncompatible (a guarded error) in the child


def _marks(refuses: bool) -> tuple[pytest.MarkDecorator, ...]:
    return (_UNRESOLVABLE,) if refuses else ()


def _matrix() -> list[pytest.param]:
    params = []
    for caller in CALLERS:
        for kind in caller.kinds:
            for leg in caller.legs:
                if leg in HOSTPORT_LEGS and kind not in ("supervisor_lease", "saved_service_token_with_url"):
                    continue  # an env host and port never reaches a credential that is not a lease or a saved token
                params.append(pytest.param(caller, kind, leg, id=f"{caller.name}-{kind}-{leg}", marks=_marks(_refuses(caller.name, kind, leg))))
    return params


@pytest.mark.parametrize(("caller", "kind", "leg"), _matrix())
def test_a_decoy_gets_no_credential_and_the_bound_origin_does(tmp_path, real, decoy, caller, kind, leg):
    KINDS[kind].store(tmp_path, real)
    env = _leg_env(leg, real, decoy)
    if caller.name == "mint_armed_bearer_for":
        env = {**env, "_NX_TEST_REAL_URL": real.url}
    proc = caller.run(tmp_path, env)

    # the poster takes no endpoint from the env, so every leg reaches the bound origin; a token saved with no URL still has none
    names_bound = _env_names_the_bound_origin(kind, leg) if caller.reads_env_endpoint else kind != "saved_service_token_no_url"
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



def _redirect_matrix() -> list[pytest.param]:
    params = []
    for caller in CALLERS:
        for kind in caller.kinds:
            if kind == "saved_service_token_no_url":
                continue  # bound to the managed default: it goes nowhere on this box
            for code in (302, 307):
                # no refusal mark: the probe is answered normally here (``_Server.redirect_to``), so the T3 caller
                # reaches its bearer-carrying request and constructs no resolver error; the guard calls a mark stale
                params.append(pytest.param(caller, kind, code, id=f"{caller.name}-{kind}-{code}"))
    return params


@pytest.mark.parametrize(("caller", "kind", "code"), _redirect_matrix())
def test_a_redirect_from_the_bound_origin_carries_no_credential_to_the_decoy(tmp_path, real, decoy, caller, kind, code):
    """The bound origin answers 302 / 307 to the decoy (a compromised or misconfigured origin, or one an
    attacker can make redirect): urllib would copy ``Authorization`` to the target, so every opener that
    carries a bearer follows none, and httpx follows none by default. The decoy sees nothing from any caller."""
    KINDS[kind].store(tmp_path, real)
    real.redirect_to, real.redirect_code = decoy.url, code
    env = _leg_env("url_real", real, decoy)
    if caller.name == "mint_armed_bearer_for":
        env = {**env, "_NX_TEST_REAL_URL": real.url}
    proc = caller.run(tmp_path, env)
    expected = KINDS[kind].header if kind not in caller.sends_nothing_for else None
    if caller.after is not None:
        caller.after(tmp_path, real, decoy, expected is not None)
    context = (proc.returncode, proc.stdout[-400:], proc.stderr[-400:], real.requests, decoy.requests)
    assert decoy.requests == [] and decoy.auth == [], context  # not the credential, and not the request at all
    if expected is not None and not (caller.name == "mint_armed_bearer_for" and kind == "data_token_lease"):
        assert expected in real.auth or "Bearer minted-data-token" in real.auth or caller.name == "mint_armed_bearer_for", context  # non-vacuity: it was sent to the bound origin


# ── a planted config directory: the design's claim that one directory serves each caller ──
#
# A repository's env can point a caller's config directory elsewhere (``NEXUS_CONFIG_DIR``, or a
# ``HOME`` that holds ``.config/nexus``) at a directory the repository planted, with its own
# ``config.yml`` naming the decoy and carrying its own token. The claim: a caller reads ONE directory,
# so the user's real credential (in the directory the planted one replaced) is never read and so never
# sent anywhere, and the planted token goes only where the planted config binds it (the decoy it names).
# ``XDG_CONFIG_HOME`` is not read at all, so a config planted there is inert.

PLANTED = "Bearer PLANTED-TOKEN"
_PLANT_VARIANTS = ("NEXUS_CONFIG_DIR", "HOME", "XDG_CONFIG_HOME")
_PLANT_CALLERS = tuple(
    c for c in CALLERS
    if c.name in ("nx_subprocess", "nx_t3_vector_client", "drain_wheel_verb", "drain_plugin_script", "t2_prefix_scan_hook", "mcp_t2_construction", "routing_telemetry_poster")
)


def _write_config(directory: Path, credentials: dict[str, str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.yml"
    path.write_text(yaml.safe_dump({"credentials": credentials}), encoding="utf-8")
    restrict_to_owner(path)


def _wait_for_either(tmp_path: Path, real: _Server, decoy: _Server) -> None:
    """The routing poster is a detached child: wait for a request to either server, or for its drop record."""
    dropped = tmp_path / "dropped.jsonl"
    settle(
        lambda: not (real.auth or decoy.auth or (dropped.exists() and dropped.read_text(encoding="utf-8"))),
        what="the poster child's POST or drop record",
    )


@pytest.mark.parametrize("variant", _PLANT_VARIANTS)
@pytest.mark.parametrize("caller", _PLANT_CALLERS, ids=lambda c: c.name)
def test_a_planted_config_dir_never_gets_the_users_credential(tmp_path, real, decoy, caller, variant):
    home = tmp_path / "home"
    _write_config(home / ".config" / "nexus", {"service_url": real.url, "service_token": "SAVED-SERVICE-TOKEN"})  # the user's own
    planted = tmp_path / "planted"
    planted_config = {"service_url": decoy.url, "service_token": "PLANTED-TOKEN"}
    env: dict[str, str] = {"HOME": str(home), "USERPROFILE": str(home), "NEXUS_CONFIG_DIR": "", "_NX_TEST_ACCOUNT_HOME": str(home)}
    if variant == "NEXUS_CONFIG_DIR":
        _write_config(planted, planted_config)
        env["NEXUS_CONFIG_DIR"] = str(planted)
    elif variant == "HOME":
        _write_config(planted / ".config" / "nexus", planted_config)
        env["HOME"] = env["USERPROFILE"] = str(planted)
    else:
        _write_config(planted / "nexus", planted_config)
        env["XDG_CONFIG_HOME"] = str(planted)
    proc = caller.run(tmp_path, env)
    if caller.after is not None:
        _wait_for_either(tmp_path, real, decoy)
    context = (proc.returncode, proc.stdout[-300:], proc.stderr[-300:], real.requests, decoy.requests)

    if not caller.reads_env_endpoint:
        # RDR-259 Decision 9: the poster reads the account's own directory, whatever HOME or NEXUS_CONFIG_DIR the repository
        # names, so the planted config and its token are never read, and the user's own token goes to its own origin only
        assert decoy.auth == [] and PLANTED not in real.auth, context
        assert set(real.auth) == {SAVED}, context  # non-vacuity: the account's config was read and its credential sent
        return
    assert SAVED not in decoy.auth + real.auth or variant == "XDG_CONFIG_HOME", context  # the user's own is never read from a planted dir
    assert SAVED not in decoy.auth, context  # and never reaches the decoy, whichever way the repository points the caller
    assert set(decoy.auth) <= {PLANTED}, context  # the decoy gets at most the token the planted config itself carries
    assert PLANTED not in real.auth, context  # which is never spent at the user's real service
    if variant == "XDG_CONFIG_HOME":
        assert decoy.requests in ([], [("/version", None)]), context  # an XDG dir is not read: nothing names the decoy
    else:
        assert real.auth == [], context  # the real service sees nothing from a caller pointed at the planted dir
        if caller.name == "nx_subprocess":
            assert PLANTED in decoy.auth, context  # non-vacuity: the planted config was read, so the absences above are about the user's token


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


@pytest.mark.parametrize("caller", [pytest.param(c, id=c.name, marks=_marks(c.name in STORAGE_CLIENTS)) for c in CALLERS])
def test_residual_two_an_env_url_with_no_credential_is_sent_no_credential(tmp_path, real, decoy, caller):
    """Residual two as Sam accepted it: an env URL with no token still lets a repository-chosen
    host be named. On this tree no caller builds an authenticated or row-reading request to it
    without a credential (the mailbox drain, the T2 prefix scan, the MCP server and ``nx`` stop at
    "not resolvable"). Of the callers in this matrix the one request that goes out is the T3 client's
    unauthenticated ``GET /version`` compatibility probe; the other unauthenticated probes that read the
    env URL (the managed-endpoint probe, ``serving_engine_version``, ``nx doctor``'s ``/version`` and
    ``/health``) are held by the probe test below. What a probe reads can steer what it reports (the
    T3 client takes ``embedding_mode`` and capability flags from ``/version``), which the docs name. The
    ruling kept this as a documented limit rather than a detector; this pins the boundary as it stands,
    so a caller that starts sending a read to the env URL turns the residual real and fails here first."""
    env = {"NX_SERVICE_URL": decoy.url}
    if caller.name == "mint_armed_bearer_for":
        env["_NX_TEST_REAL_URL"] = real.url
    proc = caller.run(tmp_path, env)
    if caller.after is not None:
        caller.after(tmp_path, real, decoy, False)
    allowed = [("/version", None)] if caller.name == "nx_t3_vector_client" else []
    assert decoy.requests == allowed and real.requests == [], (proc.returncode, proc.stdout[-300:], proc.stderr[-600:])


def _probe_managed_service(url: str) -> None:
    from nexus.db.managed_endpoint import probe_managed_service  # noqa: PLC0415 -- this probe only

    try:
        probe_managed_service(base_url=url, timeout=5.0)
    except Exception:  # noqa: BLE001 -- an incompatible or unreachable answer is fine; what was SENT is asserted
        pass


def _probe_serving_engine_version(url: str) -> None:
    from nexus.db.serving_engine_version import probe_serving_engine_version  # noqa: PLC0415 -- this probe only

    probe_serving_engine_version()


@pytest.mark.real_engine_probe  # conftest stubs the serving-engine probe unless asked not to
@pytest.mark.parametrize("probe", [_probe_managed_service, _probe_serving_engine_version], ids=lambda f: f.__name__.removeprefix("_probe_"))
def test_residual_two_the_other_unauthenticated_probes_reach_the_env_url_with_no_credential(tmp_path, decoy, monkeypatch, probe):
    """The probes besides the T3 client's read ``/version`` from the env URL, so a repository that names a
    host steers what they report; none of them builds an Authorization header, even with a stored credential
    beside them."""
    _save(tmp_path, {"service_url": decoy.url, "service_token": "SAVED-SERVICE-TOKEN", "mint_token": "SAVED-MINT-TOKEN"})
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(_config_dir(tmp_path)))
    monkeypatch.setenv("NX_SERVICE_URL", decoy.url)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    probe(decoy.url)
    assert any(path.startswith("/version") for path in decoy.paths), decoy.requests  # it did reach the env URL: the residual is real
    assert decoy.auth == [], decoy.requests  # and carried no credential


# ── transport rules (step 5): no bearer in the clear off this machine, no proxy for a lease token ──
#
# The refusal has to be observable: a caller that sent a bearer anyway must put it on the recording
# server. So the server is reached through a host the cleartext rule does not call "this machine"
# (neither ``localhost`` nor a loopback address literal) that still lands on it: ``127.1`` where
# the platform resolver reads it as 127.0.0.1 (macOS, Linux), otherwise the machine's own LAN
# address with the server bound to it (Windows does not resolve ``127.1``). A box with neither
# fails the fixture, loudly, rather than passing the absence checks over a server nothing reached.

def _lan_address() -> str | None:
    """This machine's outbound IPv4 address (a UDP connect sends no packet), or None with no network."""
    import socket  # noqa: PLC0415 -- this helper only

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
        except OSError:
            return None


def _reaches(server: _Server) -> bool:
    import urllib.request  # noqa: PLC0415 -- this helper only

    try:
        with urllib.request.urlopen(server.off_machine_url + "/probe", timeout=10) as resp:  # noqa: S310 -- our own server
            return resp.status == 200
    except OSError:
        return False


@pytest.fixture
def off_machine() -> Iterator[_Server]:
    """A recording server reached through a host that is not "this machine" to the cleartext rule."""
    from nexus.db.service_endpoint import is_loopback_host  # noqa: PLC0415 -- fixture only

    lan = _lan_address()
    candidates: list[tuple[str, str]] = [("127.0.0.1", "127.1")]
    if lan:
        candidates.append((lan, lan))
    tried: list[str] = []
    for bind, host in candidates:
        if is_loopback_host(host):
            tried.append(f"{host} (counts as loopback)")
            continue
        server = _Server(bind=bind)
        server.off_machine_url = f"http://{host}:{server.port}"
        if _reaches(server):
            server.requests.clear()
            yield server
            server.close()
            return
        server.close()
        tried.append(f"{host} (does not reach a local server)")
    pytest.fail("no host that is not 'this machine' reaches a local server here, so the cleartext cases would pass over nothing: " + "; ".join(tried))


def test_the_off_machine_host_really_reaches_the_recording_server(off_machine):
    """Non-vacuity: the cleartext cases assert an absence, so the address they use is shown to land."""
    assert _reaches(off_machine) and off_machine.paths == ["/probe"]


def _cleartext_case(tmp_path: Path, server: _Server, source: str) -> dict[str, str]:
    url = server.off_machine_url
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


def _cleartext_refuses(caller: str, source: str) -> bool:
    if caller in STORAGE_CLIENTS:
        return True
    return caller in ("drain_wheel_verb", "doctor_whoami_probe") and source in ("exported_token", "saved_token_and_url")


@pytest.mark.parametrize(
    ("caller", "source"),
    [pytest.param(c, src, id=f"{c.name}-{src}", marks=_marks(_cleartext_refuses(c.name, src))) for c in CALLERS if c.name != "mint_armed_bearer_for" for src in _CLEARTEXT_SOURCES],
)
def test_no_bearer_over_plain_http_to_a_host_that_is_not_this_machine(tmp_path, off_machine, caller, source):
    env = _cleartext_case(tmp_path, off_machine, source)
    proc = caller.run(tmp_path, env)
    if caller.after is not None:
        caller.after(tmp_path, off_machine, off_machine, False)
    assert off_machine.auth == [], (proc.returncode, proc.stderr[-400:], off_machine.requests)
    if caller.name != "routing_telemetry_poster" and source in ("exported_token", "saved_token_and_url"):
        # refused for the right reason: the transport rule names itself (the poster child is silent)
        output = proc.stdout + proc.stderr
        assert "plain http" in output or "InsecureServiceTransportError" in output, output[-400:]
        assert off_machine.off_machine_url.split("//")[1] not in output, output[-400:]  # names the source, never the URL


@pytest.mark.parametrize("source", [pytest.param(src, marks=_marks(src in ("saved_mint_and_url", "exported_mint"))) for src in _CLEARTEXT_SOURCES])
def test_no_mint_credential_over_plain_http_to_a_host_that_is_not_this_machine(tmp_path, off_machine, source):
    env = _cleartext_case(tmp_path, off_machine, source)
    caller = next(c for c in CALLERS if c.name == "mint_armed_bearer_for")
    proc = caller.run(tmp_path, {**env, "NX_SERVICE_URL": off_machine.off_machine_url, "_NX_TEST_REAL_URL": off_machine.url})
    assert off_machine.auth == [], (proc.stdout, proc.stderr[-400:], off_machine.requests)


def _proxy_env(proxy: _Server) -> dict[str, str]:
    """What a repository's settings could set: every proxy variable, no exemption."""
    env = {name: proxy.url for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    env.update({"NO_PROXY": "", "no_proxy": ""})
    return env


def _proxy_matrix() -> list[pytest.param]:
    params = []
    for caller in CALLERS:
        if caller.name in ("doctor_whoami_probe", "mint_armed_bearer_for"):
            continue  # neither carries a lease token
        for leg in ("bare", "url_real", "hostport_real"):
            params.append(pytest.param(caller, leg, id=f"{caller.name}-{leg}", marks=_marks(caller.name == "nx_t3_vector_client" and leg == "url_real")))
    return params


@pytest.mark.parametrize(("caller", "leg"), _proxy_matrix())
def test_a_lease_token_never_goes_through_a_proxy(tmp_path, real, decoy, caller, leg):
    """The supervisor lease token authorizes engine writes and is bound to a loopback engine, so a
    proxy named by the environment (a repository's ``http_proxy``) must never carry it, whether the
    URL came from the lease or from an env URL or port that names the same engine. ``decoy`` stands
    in as the proxy; the loopback engine still gets its token directly."""
    _store_supervisor_lease(tmp_path, real)
    proxy_env = {**_proxy_env(decoy), **_leg_env(leg, real, decoy)}
    proc = caller.run(tmp_path, proxy_env)
    if caller.after is not None:
        caller.after(tmp_path, real, decoy, True)
    context = (proc.returncode, proc.stderr[-400:], real.requests, decoy.requests)
    # nothing, not just no credential: a request with a bearer dials the engine directly. The one request
    # that may meet a proxy is the T3 client's unauthenticated GET /version compatibility probe (no bearer).
    allowed = [("http://127.0.0.1:%d/version" % real.port, None)] if caller.name == "nx_t3_vector_client" else []
    assert decoy.auth == [] and all(r in allowed for r in decoy.requests), context
    assert LEASE in real.auth, context


@pytest.mark.parametrize("caller", [pytest.param(c, id=c.name, marks=_marks(c.name == "nx_t3_vector_client")) for c in _TOKEN_CALLERS])
def test_an_exported_token_at_a_plain_http_url_never_goes_through_a_proxy(tmp_path, real, decoy, caller):
    """The env-token leg of the same rule: the bearer goes straight to the engine whatever proxy is named."""
    proc = caller.run(tmp_path, {"NX_SERVICE_URL": real.url, "NX_SERVICE_TOKEN": "exported-by-the-user", **_proxy_env(decoy)})
    if caller.after is not None:
        caller.after(tmp_path, real, decoy, True)
    context = (proc.returncode, proc.stderr[-400:], real.requests, decoy.requests)
    allowed = [(f"{real.url}/version", None)] if caller.name == "nx_t3_vector_client" else []
    assert decoy.auth == [] and all(r in allowed for r in decoy.requests), context
    assert EXPORTED in real.auth, context


@pytest.mark.parametrize("caller", [pytest.param(c, id=c.name, marks=_marks(c.name == "nx_t3_vector_client")) for c in _TOKEN_CALLERS])
def test_an_https_engine_is_not_reached_through_an_environment_proxy(tmp_path, decoy, caller):
    """A request that carries a bearer to an https origin reads no proxy variable either (the proxy and CA come from
    ``config.yml`` alone; ``tests/test_ec37o_netcfg_suite.py`` runs a TLS-terminating proxy against every caller).
    The proxy here is a CONNECT-refusing recorder: it reads no credential, and no hook-side caller reaches it at
    all. The ``nx`` callers may send it a CONNECT for an unauthenticated probe (``/version``), which the rule leaves
    alone, so for them only the absence of a credential is asserted."""
    proc = caller.run(tmp_path, {"NX_SERVICE_URL": "https://engine.example:8443", "NX_SERVICE_TOKEN": "exported-by-the-user", **_proxy_env(decoy)})
    if caller.after is not None:
        caller.after(tmp_path, decoy, decoy, False)
    assert decoy.auth == [], (proc.returncode, proc.stderr[-400:], decoy.requests)
    if not caller.name.startswith("nx_") and caller.name != "mcp_t2_construction":
        assert decoy.requests == [], (proc.stdout[-300:], proc.stderr[-300:], decoy.requests)


def _no_secret_anywhere(tmp_path: Path, proc: subprocess.CompletedProcess[str], secret: str) -> None:
    assert secret not in proc.stdout + proc.stderr, (proc.stdout + proc.stderr)[-400:]
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(encoding="utf-8", errors="replace"), f"{secret!r} written to {path}"


_PASSWORD = "S3CR3T-PASSWORD"


@pytest.mark.parametrize("caller", [pytest.param(c, id=c.name, marks=_marks(c.name == "mint_armed_bearer_for")) for c in CALLERS if c.name != "doctor_whoami_probe"])
def test_a_url_with_userinfo_is_refused_for_a_bearer_and_its_password_is_never_written(tmp_path, real, caller):
    """A URL ``normalize_origin`` cannot read (user:password@) gets no bearer, and nothing echoes the password."""
    url = f"http://user:{_PASSWORD}@127.0.0.1:{real.port}"
    env = {"NX_SERVICE_URL": url, "NX_SERVICE_TOKEN": "exported-by-the-user", "NX_MINT_TOKEN": "exported-mint"}
    if caller.name == "mint_armed_bearer_for":
        env["_NX_TEST_REAL_URL"] = real.url
    proc = caller.run(tmp_path, env)
    if caller.after is not None:
        caller.after(tmp_path, real, real, False)
    assert real.auth == [], (proc.returncode, proc.stderr[-400:], real.requests)
    _no_secret_anywhere(tmp_path, proc, _PASSWORD)


@pytest.mark.parametrize("caller", [pytest.param(c, id=c.name, marks=_marks(c.name != "drain_plugin_script")) for c in CALLERS if c.name in ("nx_subprocess", "drain_wheel_verb", "drain_plugin_script")])
def test_an_env_host_that_is_not_this_machine_gets_no_bearer_and_the_refusal_names_the_source(tmp_path, off_machine, caller):
    """NX_SERVICE_HOST / NX_SERVICE_PORT naming a plain-http host off the machine, with an exported token."""
    host = off_machine.off_machine_url.split("//")[1].rsplit(":", 1)[0]
    proc = caller.run(tmp_path, {"NX_SERVICE_HOST": host, "NX_SERVICE_PORT": str(off_machine.port), "NX_SERVICE_TOKEN": "exported-by-the-user"})
    output = proc.stdout + proc.stderr
    assert off_machine.auth == [], (proc.returncode, output[-400:], off_machine.requests)
    assert "NX_SERVICE_HOST / NX_SERVICE_PORT" in output, output[-400:]
    assert f"{host}:{off_machine.port}" not in output and host not in output, output[-400:]


@pytest.mark.parametrize("caller", [pytest.param(c, id=c.name, marks=_marks(c.name != "drain_plugin_script")) for c in CALLERS if c.name in ("nx_subprocess", "drain_wheel_verb", "drain_plugin_script")])
def test_a_data_token_lease_for_a_plain_http_host_off_the_machine_is_not_sent(tmp_path, off_machine, caller):
    url = off_machine.off_machine_url
    _save(tmp_path, {"service_url": url, "mint_token": "SAVED-MINT-TOKEN"})
    _write_lease_file(_config_dir(tmp_path), url, scheme="http", tenant="default").chmod(0o600)
    proc = caller.run(tmp_path, {"NX_SERVICE_URL": url})
    output = proc.stdout + proc.stderr
    assert off_machine.auth == [], (proc.returncode, output[-400:], off_machine.requests)
    assert "plain http" in output or "InsecureServiceTransportError" in output, output[-400:]
    assert url.split("//")[1] not in output, output[-400:]
