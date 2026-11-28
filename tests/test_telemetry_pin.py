# SPDX-License-Identifier: AGPL-3.0-or-later
"""The telemetry senders are pinned to the account's own configuration (RDR-259 Decision 9, nexus-sby49.10).

Both send paths end in a detached child: the routing poster (``conexus/hooks/scripts/routing/_lib.py``, stdlib ``urllib``) and
the dispatch recorder (``nexus.hooks.agent_dispatch_record``, which resolves, mints and sends through the nexus client). A
repository's own settings can set an environment variable, so neither may take its endpoint, its token or its proxy from one.

* units: ``telemetry_child_env`` (both byte-identical copies) and ``pinned_endpoint``;
* decoy, end to end: every variable of Decision 9's list, plus ``NEXUS_CONFIG_DIR``, set to a decoy that records any request,
  and a stub engine named only in ``<account home>/.config/nexus/config.yml``: the events reach the stub, the decoy sees nothing;
* a missing endpoint and a missing account home send nothing and say why.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests._child_encoding import PYTHON_CHILD_ENCODING
from tests._hook_wiring import REPO_ROOT
from tests.hooks.audience_harness import plain_repo, write_user_settings

SCRIPTS = REPO_ROOT / "conexus" / "hooks" / "scripts"
HARNESS = REPO_ROOT / "tests" / "hooks" / "audience_harness.py"
GUARD = SCRIPTS / "routing" / "credential_print_guard.py"


def _load(name: str, path: pathlib.Path):
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


@pytest.fixture(scope="module")
def plugin_audience():
    return _load("nx_pin_audience", SCRIPTS / "_audience.py")


@pytest.fixture(scope="module")
def src_audience():
    from nexus.hooks import _audience

    return _audience


@pytest.fixture(scope="module")
def ep():
    return _load("nx_pin_endpoint_resolve", SCRIPTS / "_endpoint_resolve.py")


# --- a recording stub engine --------------------------------------------------------------------


class Stub:
    """An HTTP server on port 0 that records every request (method, path, Authorization header, JSON body)."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str, dict]] = []
        stub = self

        class _H(BaseHTTPRequestHandler):
            def _take(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                stub.requests.append((self.command, self.path, self.headers.get("Authorization", ""), body))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_POST = do_GET = do_PUT = do_CONNECT = _take  # noqa: N815 -- http.server's naming

            def log_message(self, *_a: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.requests:
                return True
            time.sleep(0.05)
        return False

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stubs():
    made: list[Stub] = []

    def make() -> Stub:
        stub = Stub()
        made.append(stub)
        return stub

    yield make
    for stub in made:
        stub.close()


def _write_config(config_dir: pathlib.Path, url: str, token: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yml").write_text(
        f"credentials:\n  service_url: {url}\n  service_token: {token}\n", encoding="utf-8",
    )


#: What each sender reports (RDR-259 Decision 9); nx settings show and check carry the same missing-endpoint sentence.
NO_ENDPOINT_REPORT = (
    "telemetry is on, but no engine is configured under the account's config directory; "
    "an engine named only in the environment is not used for telemetry"
)
NO_HOME_REPORT = "telemetry is on, but this account has no home directory, so no telemetry is sent"

#: Decision 9's list, plus HOME, USERPROFILE and NEXUS_CONFIG_DIR (the fix check found it missing).
DECOY_NAMES = (
    "NX_SERVICE_URL", "NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN", "NX_MINT_TOKEN", "NX_MINT_TENANT",
    "NX_CONFIG_DIR", "XDG_CONFIG_HOME", "http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "HOME", "USERPROFILE", "NEXUS_CONFIG_DIR",
)


def _decoy_env(decoy: Stub, tmp_path: pathlib.Path) -> dict[str, str]:
    """The ambient environment with every steerable variable pointed at *decoy*: the endpoint, the token, the mint, the
    config directories (each holds a ``config.yml`` naming the decoy) and the proxies."""
    decoy_home = tmp_path / "decoy-home"
    for cfg in (decoy_home / ".config" / "nexus", tmp_path / "decoy-nexus-config", tmp_path / "decoy-xdg" / "nexus"):
        _write_config(cfg, decoy.url, "decoy-config-token")
    env = {k: v for k, v in os.environ.items() if k.upper() not in {n.upper() for n in DECOY_NAMES} | {"NO_PROXY"}}
    env.update({
        "NX_SERVICE_URL": decoy.url, "NX_SERVICE_HOST": "127.0.0.1", "NX_SERVICE_PORT": str(decoy.port),
        "NX_SERVICE_TOKEN": "decoy-env-token", "NX_MINT_TOKEN": "decoy-mint-token", "NX_MINT_TENANT": "decoy-tenant",
        "NX_CONFIG_DIR": str(tmp_path / "decoy-nexus-config"),
        "XDG_CONFIG_HOME": str(tmp_path / "decoy-xdg"),
        "NEXUS_CONFIG_DIR": str(tmp_path / "decoy-nexus-config"),
        "http_proxy": decoy.url, "https_proxy": decoy.url, "all_proxy": decoy.url,
        "HTTP_PROXY": decoy.url, "HTTPS_PROXY": decoy.url, "ALL_PROXY": decoy.url,
        "HOME": str(decoy_home), "USERPROFILE": str(decoy_home),
        "NX_DROPPED_WRITES_LOG_PATH": str(tmp_path / "dropped.jsonl"),
        "NX_HOOK_PYTHON": sys.executable,
    })
    return env


# --- telemetry_child_env, both copies -----------------------------------------------------------


@pytest.fixture(params=["plugin", "src"])
def audience(request, plugin_audience, src_audience, tmp_path, monkeypatch):
    module = plugin_audience if request.param == "plugin" else src_audience
    home = tmp_path / "acct"
    home.mkdir()
    monkeypatch.setattr(module, "_ACCOUNT_HOME_OVERRIDE", str(home))
    return module, str(home)


def test_the_child_env_drops_the_steerable_variables_and_pins_the_account(audience) -> None:
    module, home = audience
    environ = {name: "decoy" for name in DECOY_NAMES}
    environ.update({"PATH": "/bin", "NX_DISPATCH_COUNTER": "off", "SYSTEMROOT": "C:\\Windows", "Http_Proxy": "decoy"})
    before = dict(environ)
    env = module.telemetry_child_env(environ)
    assert environ == before, "the input is not mutated"
    assert env is not None
    assert env["HOME"] == home and env["USERPROFILE"] == home
    assert env["NEXUS_CONFIG_DIR"] == os.path.join(home, ".config", "nexus")
    for name in DECOY_NAMES:
        if name in ("HOME", "USERPROFILE", "NEXUS_CONFIG_DIR"):
            continue
        assert name not in env, name
    assert "Http_Proxy" not in env, "stripped whatever the case"
    assert env["PATH"] == "/bin" and env["NX_DISPATCH_COUNTER"] == "off" and env["SYSTEMROOT"] == "C:\\Windows"
    assert "decoy" not in env.values()


def test_the_child_env_makes_a_relative_drop_log_absolute(audience, tmp_path, monkeypatch) -> None:
    module, _home = audience
    monkeypatch.chdir(tmp_path)
    env = module.telemetry_child_env({"NX_DROPPED_WRITES_LOG_PATH": "rel/drops.jsonl"})
    assert env is not None and env["NX_DROPPED_WRITES_LOG_PATH"] == os.path.abspath("rel/drops.jsonl")


def test_the_child_env_reads_no_environment_variable(audience, monkeypatch) -> None:
    """It takes the environment as an argument: the process's own environment changes nothing."""
    module, home = audience
    monkeypatch.setenv("HOME", "/decoy")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", "/decoy/cfg")
    env = module.telemetry_child_env({})
    assert env == {"HOME": home, "USERPROFILE": home, "NEXUS_CONFIG_DIR": os.path.join(home, ".config", "nexus")}


def test_no_account_home_is_no_environment(audience, monkeypatch) -> None:
    module, _home = audience
    monkeypatch.setattr(module, "account_home", lambda: None)
    assert module.telemetry_child_env({"PATH": "/bin"}) is None


def test_an_account_database_that_raises_is_no_environment(audience, monkeypatch) -> None:
    module, _home = audience

    def boom() -> str:
        raise OSError("account database unreadable")

    monkeypatch.setattr(module, "account_home", boom)
    assert module.telemetry_child_env({}) is None


# --- pinned_endpoint ----------------------------------------------------------------------------


def _lease(config_dir: pathlib.Path, port: int = 4242, token: str = "lease-token") -> None:
    from nexus.daemon.service_registry import LeaseRecord, service_identity

    config_dir.mkdir(parents=True, exist_ok=True)
    record = LeaseRecord(
        scope_key="storage_service", generation=1, owner_token="owner", heartbeat_epoch=time.time(), ttl=600.0,
        endpoint={"host": "127.0.0.1", "port": port, "token": token}, version="0.0.0-fixture",
    )
    (config_dir / f"storage_service_addr.{service_identity()}").write_text(record.to_json(), encoding="utf-8")


def _poison_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DECOY_NAMES:
        monkeypatch.setenv(name, "http://127.0.0.1:9/decoy")


def test_the_saved_url_and_token_are_the_endpoint(ep, tmp_path, monkeypatch) -> None:
    _poison_env(monkeypatch)
    _write_config(tmp_path, "http://127.0.0.1:5000/", "saved-token")
    assert ep.pinned_endpoint(tmp_path) == ("http://127.0.0.1:5000", "saved-token")


def test_the_environment_names_no_endpoint(ep, tmp_path, monkeypatch) -> None:
    """Nothing under the config directory: an engine named only in the environment is not found."""
    _poison_env(monkeypatch)
    monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:5001")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "env-token")
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", "5001")
    assert ep.pinned_endpoint(tmp_path) == (None, None)


def test_the_environment_does_not_move_a_configured_endpoint(ep, tmp_path, monkeypatch) -> None:
    _write_config(tmp_path, "http://127.0.0.1:5000", "saved-token")
    monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:5001")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "env-token")
    assert ep.pinned_endpoint(tmp_path) == ("http://127.0.0.1:5000", "saved-token")


def test_a_bare_lease_is_the_endpoint(ep, tmp_path) -> None:
    _lease(tmp_path)
    assert ep.pinned_endpoint(tmp_path) == ("http://127.0.0.1:4242", "lease-token")


def test_the_lease_token_goes_only_to_the_lease_origin(ep, tmp_path) -> None:
    """nexus-ec37o: a saved URL elsewhere with no saved token does not borrow the lease token."""
    _lease(tmp_path, port=4242)
    (tmp_path / "config.yml").write_text("credentials:\n  service_url: https://engine.example\n", encoding="utf-8")
    assert ep.pinned_endpoint(tmp_path) == (None, None)
    (tmp_path / "config.yml").write_text("credentials:\n  service_url: http://127.0.0.1:4242\n", encoding="utf-8")
    assert ep.pinned_endpoint(tmp_path) == ("http://127.0.0.1:4242", "lease-token")


def test_a_url_carrying_a_password_is_refused(ep, tmp_path) -> None:
    _write_config(tmp_path, "http://user:secret@127.0.0.1:5000", "saved-token")
    assert ep.pinned_endpoint(tmp_path) == (None, None)


def test_a_missing_directory_is_no_endpoint(ep, tmp_path) -> None:
    assert ep.pinned_endpoint(tmp_path / "absent") == (None, None)


def test_the_resolver_never_consults_the_environment_resolvers(ep, tmp_path, monkeypatch) -> None:
    """Not ``default_config_dir`` and not ``resolve_base_url``: both read the environment."""
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("pinned_endpoint asked an environment-reading resolver")

    monkeypatch.setattr(ep, "default_config_dir", boom)
    monkeypatch.setattr(ep, "resolve_base_url", boom)
    _write_config(tmp_path, "http://127.0.0.1:5000", "saved-token")
    assert ep.pinned_endpoint(tmp_path)[0] == "http://127.0.0.1:5000"


# --- the routing poster, end to end -------------------------------------------------------------


def _credential_read(cwd: str, home: str, env: dict) -> dict:
    payload = {"tool_name": "Bash", "tool_input": {"command": "cat /proc/1234/environ"}, "session_id": "s-pin", "cwd": cwd}
    proc = subprocess.run(
        [sys.executable, str(HARNESS), home, str(GUARD)],
        input=json.dumps(payload), capture_output=True, text=True, encoding=PYTHON_CHILD_ENCODING, timeout=60, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]


def _drops(path: pathlib.Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()] if path.exists() else []


def _settled(real: Stub, dropped: pathlib.Path, *, expect: bool) -> bool:
    deadline = time.monotonic() + (20.0 if expect else 3.0)
    while time.monotonic() < deadline:
        if real.requests or _drops(dropped):
            return True
        time.sleep(0.05)
    return False


def test_the_poster_ignores_every_decoy_and_reaches_the_account_engine(stubs, tmp_path) -> None:
    real, decoy = stubs(), stubs()
    home = write_user_settings(tmp_path / "acct", telemetry=True)
    _write_config(pathlib.Path(home) / ".config" / "nexus", real.url, "account-token")
    env = _decoy_env(decoy, tmp_path)
    out = _credential_read(plain_repo(tmp_path / "repo"), home, env)
    assert out["permissionDecision"] == "deny"
    assert _settled(real, tmp_path / "dropped.jsonl", expect=True)
    assert [(m, p) for m, p, _a, _b in real.requests] == [("POST", "/v1/telemetry/routing_events/record")]
    assert real.requests[0][2] == "Bearer account-token"
    time.sleep(0.5)  # a decoy hit would be as fast as the real one
    assert decoy.requests == [], decoy.requests
    assert _drops(tmp_path / "dropped.jsonl") == []


def test_a_missing_endpoint_sends_nothing_and_says_why(stubs, tmp_path) -> None:
    """Telemetry on, the account's config directory names no engine, a live engine in the environment: nothing is sent,
    one ``unresolvable`` drop carries the report."""
    live, decoy = stubs(), stubs()
    home = write_user_settings(tmp_path / "acct", telemetry=True)
    env = _decoy_env(decoy, tmp_path)
    env.update({"NX_SERVICE_URL": live.url, "NX_SERVICE_TOKEN": "env-token", "NX_SERVICE_HOST": "127.0.0.1",
                "NX_SERVICE_PORT": str(live.port)})
    out = _credential_read(plain_repo(tmp_path / "repo"), home, env)
    assert out["permissionDecision"] == "deny"
    assert _settled(live, tmp_path / "dropped.jsonl", expect=True)
    time.sleep(0.5)
    assert live.requests == [] and decoy.requests == []
    [drop] = _drops(tmp_path / "dropped.jsonl")
    assert drop["hook"] == "routing_events" and drop["cause"] == "unresolvable"
    assert drop["error"] == NO_ENDPOINT_REPORT
    assert "environment is not used for telemetry" in drop["error"]


def test_no_account_home_starts_no_poster_and_reports_it(plugin_account, tmp_path, monkeypatch) -> None:
    """Telemetry is read as on (the setting is stubbed, since with no home it reads off) and the account has no home: no
    child starts, no request is made, one drop carries the report."""
    plugin_account(telemetry=True)
    lib = _load("nx_pin_routing_lib", SCRIPTS / "routing" / "_lib.py")
    audience = sys.modules["_audience"]
    monkeypatch.setattr(audience, "account_home", lambda: None)
    monkeypatch.setattr(lib, "_telemetry_enabled", lambda: True)
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(tmp_path / "dropped.jsonl"))
    monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:9")
    started: list[object] = []
    monkeypatch.setattr(lib, "_spawn_poster", lambda *a, **k: started.append(a))
    lib.log_routing_event(rule="r", outcome="deny")
    assert started == []
    [drop] = _drops(tmp_path / "dropped.jsonl")
    assert drop["cause"] == "unresolvable" and drop["error"] == NO_HOME_REPORT


def test_the_poster_child_is_started_with_the_built_environment(plugin_account, tmp_path, monkeypatch) -> None:
    """A real child: the module's own ``__file__`` names a script that writes its environment to a file, so what the child
    inherits is read from the child itself, with no stdlib module patched."""
    from tests._settle import settle  # noqa: PLC0415

    plugin_account(telemetry=True)
    lib = _load("nx_pin_routing_lib_env", SCRIPTS / "routing" / "_lib.py")
    for name in DECOY_NAMES:
        monkeypatch.setenv(name, "decoy")
    out = tmp_path / "child-env.json"
    dump = tmp_path / "dump_env.py"
    dump.write_text(
        "import json, os, sys\n"
        "sys.stdin.buffer.read()\n"
        # Write aside, then rename: the file appears only once it is whole, so the settle below can
        # never read it half-written (a 2026-11-28 develop red read it empty, under CI load).
        f"open({str(out) + '.tmp'!r}, 'w', encoding='utf-8').write(json.dumps(dict(os.environ)))\n"
        f"os.replace({str(out) + '.tmp'!r}, {str(out)!r})\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(lib, "__file__", str(dump))
    assert lib._spawn_poster({"rule": "r"}, "http://127.0.0.1:1", "t") is True
    settle(lambda: not out.exists(), what="the poster child's environment dump")
    env = json.loads(out.read_text(encoding="utf-8"))
    assert env, "non-vacuity: the child ran and dumped an environment"
    for name in DECOY_NAMES:
        assert env.get(name) != "decoy", name
    assert "NX_SERVICE_URL" not in env and "HTTPS_PROXY" not in env


# --- the dispatch recorder, end to end ----------------------------------------------------------


def test_the_recorder_ignores_every_decoy_and_reaches_the_account_engine(stubs, tmp_path, src_audience, monkeypatch) -> None:
    from nexus.hooks import agent_dispatch_record as rec

    real, decoy = stubs(), stubs()
    home = tmp_path / "acct"
    _write_config(home / ".config" / "nexus", real.url, "account-token")
    monkeypatch.setattr(src_audience, "_ACCOUNT_HOME_OVERRIDE", str(home))
    for name, value in _decoy_env(decoy, tmp_path).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(rec.OFF_ENV, raising=False)
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT / "src"))
    assert rec.spawn("s-pin", "a-pin", "developer", 2) is True
    assert real.wait(60), "the recorder child never reached the account engine"
    method, path, auth, body = real.requests[0]
    assert (method, path) == ("POST", "/v1/telemetry/agent_dispatches/record")
    assert auth == "Bearer account-token"
    assert (body["session_id"], body["agent_id"], body["t1_puts"]) == ("s-pin", "a-pin", 2)
    time.sleep(0.5)
    assert decoy.requests == [], decoy.requests


def test_the_recorder_child_gets_the_built_environment(tmp_path, src_audience, monkeypatch) -> None:
    from nexus.hooks import agent_dispatch_record as rec
    from tests._module_seam import setattr_in

    monkeypatch.setattr(src_audience, "_ACCOUNT_HOME_OVERRIDE", str(tmp_path / "acct"))
    monkeypatch.delenv(rec.OFF_ENV, raising=False)
    for name in DECOY_NAMES:
        monkeypatch.setenv(name, "decoy")
    seen: list[dict] = []

    class _P:
        def __init__(self, argv, **kw) -> None:
            seen.append(kw)

    setattr_in(monkeypatch, rec, "subprocess.Popen", _P)
    assert rec.spawn("s", "a", "developer", 1) is True
    [kw] = seen
    assert kw["env"]["HOME"] == str(tmp_path / "acct")
    assert "NX_SERVICE_URL" not in kw["env"] and "NX_MINT_TOKEN" not in kw["env"]


def test_the_recorder_with_no_account_home_starts_no_child_and_reports_it(tmp_path, src_audience, monkeypatch) -> None:
    from nexus.hooks import agent_dispatch_record as rec
    from tests._module_seam import setattr_in

    monkeypatch.setattr(src_audience, "account_home", lambda: None)
    monkeypatch.delenv(rec.OFF_ENV, raising=False)
    monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(tmp_path / "dropped.jsonl"))
    started: list[object] = []
    setattr_in(monkeypatch, rec, "subprocess.Popen", lambda *a, **k: started.append(a))
    assert rec.spawn("s", "a", "developer", 1) is False
    assert started == []
    [drop] = _drops(tmp_path / "dropped.jsonl")
    assert drop["hook"] == "agent_dispatches" and drop["error"] == NO_HOME_REPORT
