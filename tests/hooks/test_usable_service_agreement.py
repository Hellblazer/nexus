# SPDX-License-Identifier: AGPL-3.0-or-later
"""A usable service: the plugin's answer and the real server's answer are the same
(RDR-228 Phase 2 Step 4, nexus-sv2wx.17; residual nexus-9i4d4 item 9).

"Usable" has one definition: an endpoint resolves the way the CLI resolves it,
``nexus.db.service_endpoint.resolve_service_endpoint()`` returning and not raising.
The real server's ``setup_tools.usable_service`` calls that function. The session-start
hook, the setup process and the stand-in cannot import ``nexus``, so the plugin
restates it in ``_endpoint_resolve.endpoint_usable``. Before this step the plugin used
``resolve_base_url``, which returns a saved address with no token and a lease with no
token, where the CLI raises: the hook said "a service is usable" for a machine on which
every Nexus tool failed.

Each case builds one config directory and one environment, then asks all three
resolvers. The four legs of the resolver, in order, and the case of none:

1. ``NX_SERVICE_URL`` in the environment;
2. a saved ``service_url`` in ``config.yml`` (``nx config set service_url``);
3. ``NX_SERVICE_HOST`` / ``NX_SERVICE_PORT`` (and ``NX_SERVICE_TOKEN``) in the environment;
4. a live local supervisor lease.

Reader grace (nexus-wo6sc): a lease that has just passed its TTL is read as live by the
real server when its owner process is alive and its port answers ``/health``. A laptop that
slept would otherwise be told "service down" by the hook. The plugin makes no network
request, so it accepts the part it can see (the recorded owner process is alive, the lease
is within ten TTLs, the host is loopback) and not the ``/health``. The cases below hold
the two together wherever the port answers; the one named difference, an owner that is alive
and whose port does not answer, is pinned as a difference and not hidden.

The Windows lease-read bound: the hook's read retries a sharing violation for at most
``HOOK_LEASE_RETRY_BUDGET_S`` (the registry's own budget is two seconds, against the
hook's 100 ms).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from tests._platform import posix_only

from nexus.daemon.service_registry import ServiceRegistry, service_identity
from nexus.mcp import setup_tools

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "conexus" / "hooks" / "scripts"
_LAUNCH = _ROOT / "conexus" / "launch"


def _load(path: Path, name: str) -> ModuleType:
    for directory in (str(_SCRIPTS), str(_LAUNCH)):
        if directory not in sys.path:
            sys.path.insert(0, directory)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def endpoint() -> ModuleType:
    return _load(_SCRIPTS / "_endpoint_resolve.py", "_endpoint_resolve_agreement")


@pytest.fixture(scope="module")
def nx_setup() -> ModuleType:
    return _load(_LAUNCH / "nx_setup.py", "nx_setup_agreement")


@pytest.fixture(scope="module")
def notice() -> ModuleType:
    return _load(_SCRIPTS / "nx_setup_notice.py", "nx_setup_notice_agreement")


_ENV = (
    "NX_SERVICE_URL", "NX_SERVICE_TOKEN", "NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_MINT_TOKEN", "NX_MINT_TENANT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def _config() -> Path:
    path = Path(os.environ["NEXUS_CONFIG_DIR"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _lease(
    *, host: str = "127.0.0.1", port: int = 4242, token: str = "lease-token", ttl: float = 60.0,
    clock: float | None = None, payload: dict | None = None,
) -> None:
    registry = ServiceRegistry(
        dir=_config(), tier="storage_service", ttl=ttl, **({} if clock is None else {"clock": lambda: clock}),
    )
    registry.publish(
        service_identity(), endpoint={"host": host, "port": port, "token": token}, version="test", owner_token="agreement",
        payload=payload,
    )


def _save(**credentials: str) -> None:
    from nexus.config import set_credential

    for key, value in credentials.items():
        set_credential(key, value)


def _save_raw(**credentials: str) -> None:
    """Write ``config.yml`` directly. ``set_credential`` refuses a URL with userinfo
    (nexus-ujbf8), and a hand-edited file is the case the agreement must hold for."""
    body = "credentials:\n" + "".join(f"  {key}: {value}\n" for key, value in credentials.items())
    (_config() / "config.yml").write_text(body, encoding="utf-8")


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)


Case = Callable[[pytest.MonkeyPatch], None]
#: ``(id, setup, usable)``. ``usable`` is what the real server says; the test asserts it as well as
#: the agreement, so a case cannot agree by both sides being wrong in the same way.
CASES: list[tuple[str, Case, bool]] = [
    # -- none --
    ("none", lambda m: None, False),
    # -- leg 1: NX_SERVICE_URL --
    ("env url, env token", lambda m: _env(m, NX_SERVICE_URL="https://svc.example:443", NX_SERVICE_TOKEN="t"), True),
    ("env url, no token", lambda m: _env(m, NX_SERVICE_URL="https://svc.example:443"), False),
    ("env url, saved token", lambda m: (_env(m, NX_SERVICE_URL="https://svc.example:443"), _save(service_token="t")), True),
    ("env url, lease token", lambda m: (_env(m, NX_SERVICE_URL="https://svc.example:443"), _lease()), True),
    ("env url, lease without token", lambda m: (_env(m, NX_SERVICE_URL="https://svc.example:443"), _lease(token="")), False),
    ("env url, expired lease", lambda m: (_env(m, NX_SERVICE_URL="https://svc.example:443"), _lease(ttl=10.0, clock=100.0)), False),
    ("env url, saved mint token", lambda m: (_env(m, NX_SERVICE_URL="https://svc.example:443"), _save(mint_token="m")), True),
    ("env url, env mint token", lambda m: _env(m, NX_SERVICE_URL="https://svc.example:443", NX_MINT_TOKEN="m"), True),
    ("env url wins over saved url, still no token", lambda m: (_env(m, NX_SERVICE_URL="https://a.example"), _save(service_url="https://b.example")), False),
    ("env url, trailing slash only", lambda m: _env(m, NX_SERVICE_URL="/", NX_SERVICE_TOKEN="t"), False),
    ("env url blank, saved url and token", lambda m: (_env(m, NX_SERVICE_URL="   "), _save(service_url="https://b.example", service_token="t")), False),
    # -- leg 2: a saved service_url --
    ("saved url, saved token", lambda m: _save(service_url="https://svc.example", service_token="t"), True),
    ("saved url, NO token", lambda m: _save(service_url="https://svc.example"), False),
    ("saved url, env token", lambda m: (_save(service_url="https://svc.example"), _env(m, NX_SERVICE_TOKEN="t")), True),
    ("saved url, lease token", lambda m: (_save(service_url="https://svc.example"), _lease()), True),
    ("saved url, lease without token", lambda m: (_save(service_url="https://svc.example"), _lease(token="")), False),
    ("saved url, saved mint token", lambda m: _save(service_url="https://svc.example", mint_token="m"), True),
    ("saved url, token only in the lease of a stopped service", lambda m: (_save(service_url="https://svc.example"), _lease(ttl=10.0, clock=100.0)), False),
    ("saved url is a slash, live lease", lambda m: (_save(service_url="/"), _lease()), True),
    ("saved token without a url", lambda m: _save(service_token="t"), False),
    # -- a service_url carrying user:password@ is refused by the CLI (nexus-ujbf8), so it is not usable --
    ("env url with userinfo, env token", lambda m: _env(m, NX_SERVICE_URL="https://user:pw@svc.example", NX_SERVICE_TOKEN="t"), False),
    ("env url with userinfo, live lease", lambda m: (_env(m, NX_SERVICE_URL="https://user:pw@svc.example"), _lease()), False),
    ("env url with a '#' in the userinfo, env token", lambda m: _env(m, NX_SERVICE_URL="https://user:pw#x@svc.example", NX_SERVICE_TOKEN="t"), False),
    ("saved url with userinfo, saved token", lambda m: _save_raw(service_url="https://user:pw@svc.example", service_token="t"), False),
    ("saved url with userinfo, mint token", lambda m: _save_raw(service_url="https://user:pw@svc.example", mint_token="m"), False),
    ("env url wins over a saved url with userinfo", lambda m: (_env(m, NX_SERVICE_URL="https://svc.example", NX_SERVICE_TOKEN="t"), _save_raw(service_url="https://user:pw@other.example")), True),
    # -- leg 3: NX_SERVICE_HOST / NX_SERVICE_PORT --
    ("host, port, token in env", lambda m: _env(m, NX_SERVICE_HOST="10.0.0.5", NX_SERVICE_PORT="8123", NX_SERVICE_TOKEN="t"), True),
    ("host and port, no token", lambda m: _env(m, NX_SERVICE_HOST="10.0.0.5", NX_SERVICE_PORT="8123"), False),
    ("host and port, token from the lease", lambda m: (_env(m, NX_SERVICE_HOST="10.0.0.5", NX_SERVICE_PORT="8123"), _lease()), True),
    ("host and port, lease without token", lambda m: (_env(m, NX_SERVICE_HOST="10.0.0.5", NX_SERVICE_PORT="8123"), _lease(token="")), False),
    ("port and token, host defaults", lambda m: _env(m, NX_SERVICE_PORT="8123", NX_SERVICE_TOKEN="t"), True),
    ("port only, lease fills host and token", lambda m: (_env(m, NX_SERVICE_PORT="8123"), _lease()), True),
    ("port only, no lease", lambda m: _env(m, NX_SERVICE_PORT="8123"), False),
    ("host and token, lease fills port", lambda m: (_env(m, NX_SERVICE_HOST="10.0.0.5", NX_SERVICE_TOKEN="t"), _lease()), True),
    ("host only, lease fills port and token", lambda m: (_env(m, NX_SERVICE_HOST="10.0.0.5"), _lease()), True),
    ("host only, no lease", lambda m: _env(m, NX_SERVICE_HOST="10.0.0.5"), False),
    ("host and token, no port anywhere", lambda m: _env(m, NX_SERVICE_HOST="10.0.0.5", NX_SERVICE_TOKEN="t"), False),
    ("port is not an integer", lambda m: _env(m, NX_SERVICE_PORT="eighty", NX_SERVICE_TOKEN="t"), False),
    ("saved token does not serve the host and port leg", lambda m: (_env(m, NX_SERVICE_HOST="10.0.0.5", NX_SERVICE_PORT="8123"), _save(service_token="t")), False),
    # -- leg 4: a live local lease --
    ("live lease with a token", lambda m: _lease(), True),
    ("live lease WITHOUT a token", lambda m: _lease(token=""), False),
    ("expired lease", lambda m: _lease(ttl=10.0, clock=100.0), False),
    ("lease with port 0", lambda m: _lease(port=0), False),
]


# -- a lease past its TTL (reader grace) -----------------------------------------------------------


@pytest.fixture(scope="module")
def healthy_port():
    """A loopback server that answers ``GET /health`` as the storage service does, so the real
    server's grace probe finds its service where the lease says it is."""
    import http.server
    import threading

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 -- the stdlib's name
            body = b'{"status": "ok", "db": "up"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _dead_pid() -> int:
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 -- our own interpreter
    child.wait()
    return child.pid


def _stale(port: int, *, age: float = 25.0, ttl: float = 10.0, pid: int | None = None, host: str = "127.0.0.1", token: str = "t") -> None:
    """A lease last stamped *age* seconds ago against a *ttl*, owned by *pid* (default: this process)."""
    payload = {"supervisor_pid": os.getpid() if pid is None else pid}
    _lease(host=host, port=port, token=token, ttl=ttl, clock=time.time() - age, payload=payload)


def _stale_without_pid(port: int) -> None:
    _lease(port=port, ttl=10.0, clock=time.time() - 25.0, payload=None)


def _stale_shutting_down(port: int) -> None:
    _stale(port)
    path = _config() / f"storage_service_addr.{service_identity()}"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = "shutting_down"
    path.write_text(json.dumps(data), encoding="utf-8")


GraceCase = Callable[[pytest.MonkeyPatch, int], None]
GRACE_CASES: list[tuple[str, GraceCase, bool]] = [
    ("stale lease, owner alive, port answers", lambda m, port: _stale(port), True),
    ("stale lease, owner alive, no token in it", lambda m, port: _stale(port, token=""), False),
    ("stale lease, owner dead", lambda m, port: _stale(port, pid=_dead_pid()), False),
    ("stale lease, owner alive, past ten TTLs", lambda m, port: _stale(port, age=1000.0), False),
    ("stale lease, owner alive, just inside ten TTLs", lambda m, port: _stale(port, age=90.0), True),
    ("stale lease, no owner pid recorded", lambda m, port: _stale_without_pid(port), False),
    ("stale lease marked shutting down, owner alive", lambda m, port: _stale_shutting_down(port), False),
    ("stale lease, owner alive, host is not loopback", lambda m, port: _stale(port, host="10.0.0.5"), False),
    ("stale lease, owner alive, localhost", lambda m, port: _stale(port, host="localhost"), True),
    # The same lease on the other legs that read it.
    ("env url, stale lease token, owner alive", lambda m, port: (_env(m, NX_SERVICE_URL="https://svc.example:443"), _stale(port)), True),
    ("env url, stale lease token, owner dead", lambda m, port: (_env(m, NX_SERVICE_URL="https://svc.example:443"), _stale(port, pid=_dead_pid())), False),
    ("saved url, stale lease token, owner alive", lambda m, port: (_save(service_url="https://svc.example"), _stale(port)), True),
    ("host and port env, token from a stale lease, owner alive", lambda m, port: (_env(m, NX_SERVICE_HOST="10.0.0.5", NX_SERVICE_PORT="8123"), _stale(port)), True),
    ("host and port env, token from a stale lease, owner dead", lambda m, port: (_env(m, NX_SERVICE_HOST="10.0.0.5", NX_SERVICE_PORT="8123"), _stale(port, pid=_dead_pid())), False),
]


@pytest.mark.t2_tenant
@pytest.mark.parametrize(("setup", "usable"), [(c[1], c[2]) for c in GRACE_CASES], ids=[c[0] for c in GRACE_CASES])
def test_a_lease_past_its_ttl_is_judged_the_same_when_the_port_answers(
    monkeypatch: pytest.MonkeyPatch, endpoint: ModuleType, nx_setup: ModuleType, notice: ModuleType,
    healthy_port: int, setup: GraceCase, usable: bool,
) -> None:
    setup(monkeypatch, healthy_port)
    config = _config()
    # The plugin first and the real resolver last: discovery REAPS a stale lease it will not
    # honour, and a plugin that read the lease afterwards would agree with "no" for no reason.
    plugin = (endpoint.endpoint_usable(config), notice.service_usable(config))
    real = setup_tools.usable_service()
    assert real is usable, "the case's own expectation is wrong, or the real resolver changed"
    assert plugin == (real, real), "a laptop that slept is not told 'service down'"


@pytest.mark.parametrize(
    ("setup", "usable"), [(c[1], c[2]) for c in GRACE_CASES if c[2]], ids=[c[0] for c in GRACE_CASES if c[2]],
)
def test_the_setup_process_gives_a_lease_past_its_ttl_no_grace(
    monkeypatch: pytest.MonkeyPatch, endpoint: ModuleType, nx_setup: ModuleType, healthy_port: int,
    setup: GraceCase, usable: bool,
) -> None:
    """The hook keeps a stale lease whose owner is alive (it must answer in 100 ms and
    cannot ask the port). The setup process can wait, and it decides whether to START a
    service: a stale lease must not make it skip the start step, so it grants no grace. Every
    True case above is True only through the grace."""
    assert usable
    setup(monkeypatch, healthy_port)
    config = _config()
    assert endpoint.endpoint_usable(config) is True, "the hook keeps it"
    assert endpoint.endpoint_usable(config, grace=False) is False
    assert nx_setup._endpoint_resolves(config) is False, "the setup process does not skip the start step on it"


def test_the_owner_check_wants_a_positive_integer_pid(endpoint: ModuleType) -> None:
    """``_stale_owner_alive`` on its own: a pid that is missing, not an integer or not
    positive denies the grace (the real registry's guard), before any process is asked."""
    base = {"endpoint": {"host": "127.0.0.1", "port": 4242}}
    alive = os.getpid()
    assert endpoint._stale_owner_alive({**base, "payload": {"supervisor_pid": alive}}, 25.0, 10.0) is True
    for payload in (None, {}, {"supervisor_pid": None}, {"supervisor_pid": "x"}, {"supervisor_pid": 0}, {"supervisor_pid": -1}, "pid"):
        assert endpoint._stale_owner_alive({**base, "payload": payload}, 25.0, 10.0) is False, payload
    assert endpoint._stale_owner_alive({**base}, 25.0, 10.0) is False, "no payload at all"


@pytest.mark.t2_tenant
def test_the_named_difference_an_owner_that_is_alive_whose_port_does_not_answer(
    monkeypatch: pytest.MonkeyPatch, endpoint: ModuleType,
) -> None:
    """The one place the two part: the real server probes ``/health`` and the plugin makes no
    network request. A supervisor that is alive and wedged, inside ten TTLs, reads as a
    usable service to the hook and not to the CLI. Pinned so a change on either side is seen."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    _stale(dead_port)
    # The plugin first: the real resolver reaps a stale lease whose port is dead.
    assert endpoint.endpoint_usable(_config()) is True
    assert setup_tools.usable_service() is False


@pytest.mark.t2_tenant
def test_the_setup_process_does_not_share_the_wedged_owner_difference(
    monkeypatch: pytest.MonkeyPatch, nx_setup: ModuleType,
) -> None:
    """The same wedged supervisor, seen by the process that decides whether to start the
    service: it agrees with the CLI (not usable), so ``setup`` starts the service instead of
    ending doctor-failed on a service that is not there."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    _stale(dead_port)
    assert nx_setup._endpoint_resolves(_config()) is False
    assert setup_tools.usable_service() is False


def test_only_the_usable_service_check_grants_the_grace(endpoint: ModuleType, healthy_port: int) -> None:
    """The other hooks read the lease with their own policy (a stale one is not a bearer
    credential), and ``read_storage_service_lease`` keeps its answer for them."""
    _stale(healthy_port)
    assert endpoint.read_storage_service_lease(_config()) is None
    assert endpoint.read_storage_service_lease(_config(), grace=True) is not None


def _corrupt_lease(m: pytest.MonkeyPatch) -> None:
    (_config() / f"storage_service_addr.{service_identity()}").write_text("{ not json", encoding="utf-8")


def _shutdown_lease(m: pytest.MonkeyPatch) -> None:
    _lease()
    path = _config() / f"storage_service_addr.{service_identity()}"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = "shutting_down"
    path.write_text(json.dumps(data), encoding="utf-8")


def _hand_edited(text: str) -> Case:
    """A ``config.yml`` a person wrote by hand (the CLI writes block style only): the
    CLI reads it with PyYAML, the plugin with a line scanner."""
    def apply(m: pytest.MonkeyPatch) -> None:
        (_config() / "config.yml").write_text(text, encoding="utf-8")
    return apply


CASES += [
    ("corrupt lease", _corrupt_lease, False),
    ("lease marked shutting down", _shutdown_lease, False),
    # -- a hand-edited config.yml: the CLI reads any YAML, the plugin a narrow scan --
    ("flow-style credentials, url and token", _hand_edited("credentials: {service_url: https://svc.example, service_token: tok}\n"), True),
    ("flow-style credentials, url only", _hand_edited("credentials: {service_url: https://svc.example}\n"), False),
    ("flow-style credentials, quoted values", _hand_edited("credentials: {service_url: 'https://svc.example', service_token: \"t,ok\"}\n"), True),
    ("flow-style credentials over several lines", _hand_edited("credentials: {\n  service_url: https://svc.example,\n  service_token: tok,\n}\n"), True),
    ("flow-style credentials after other keys", _hand_edited("verbose: true\ncredentials: {service_url: https://svc.example, service_token: tok}\nother: 1\n"), True),
    ("flow-style empty credentials", _hand_edited("credentials: {}\n"), False),
    ("flow-style credentials with a mint token", _hand_edited("credentials: {service_url: https://svc.example, mint_token: m}\n"), True),
    ("block-style with an inline comment", _hand_edited("credentials:\n  service_url: https://svc.example  # prod\n  service_token: tok # t\n"), True),
]


@pytest.mark.t2_tenant
@pytest.mark.parametrize(("setup", "usable"), [(c[1], c[2]) for c in CASES], ids=[c[0] for c in CASES])
def test_the_plugin_and_the_real_server_give_the_same_answer(
    monkeypatch: pytest.MonkeyPatch, endpoint: ModuleType, nx_setup: ModuleType, notice: ModuleType,
    setup: Case, usable: bool,
) -> None:
    setup(monkeypatch)
    config = _config()
    real = setup_tools.usable_service()
    assert real is usable, "the case's own expectation is wrong, or the real resolver changed"
    assert endpoint.endpoint_usable(config) is real
    assert nx_setup._endpoint_resolves(config) is real, "the setup process decides 'nothing to do' by this"
    assert notice.service_usable(config) is real, "the hook decides 'say nothing' by this"


def test_the_case_table_covers_each_leg_and_none() -> None:
    names = " | ".join(c[0] for c in CASES)
    for needle in ("none", "env url", "saved url", "host and port", "live lease"):
        assert needle in names, needle
    assert any(c[2] for c in CASES) and any(not c[2] for c in CASES), "a table of one answer proves nothing"
    assert len({c[0] for c in CASES}) == len(CASES)


@pytest.mark.t2_tenant
def test_the_old_resolver_still_disagrees_on_a_saved_address_with_no_token(
    monkeypatch: pytest.MonkeyPatch, endpoint: ModuleType,
) -> None:
    """The reason for the new function: ``resolve_base_url`` is the base-URL
    precedence other hooks use with their own credential policy, and it returns the
    address. ``endpoint_usable`` is the one that agrees with the CLI."""
    _save(service_url="https://svc.example")
    assert endpoint.resolve_base_url(_config()) == ("https://svc.example", False)
    assert endpoint.endpoint_usable(_config()) is False
    assert setup_tools.usable_service() is False


# -- the Windows lease read, bounded in the hook ------------------------------------------------


def test_the_hook_budget_is_far_inside_its_100_ms(endpoint: ModuleType) -> None:
    assert 0 < endpoint.HOOK_LEASE_RETRY_BUDGET_S <= 0.03
    assert endpoint.HOOK_LEASE_RETRY_BUDGET_S < endpoint._WINDOWS_SHARING_RETRY_BUDGET_S / 20


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_a_lease_read_that_keeps_failing_gives_up_inside_the_hook_budget(endpoint: ModuleType, tmp_path: Path) -> None:
    clock = _Clock()
    calls = {"n": 0}

    def busy(path: Path) -> str:
        calls["n"] += 1
        raise PermissionError(13, "sharing violation")

    budget = endpoint.HOOK_LEASE_RETRY_BUDGET_S
    with pytest.raises(PermissionError):
        endpoint.read_lease_text(
            tmp_path / "lease", platform="win32", read=busy, sleep=clock.sleep, monotonic=clock.monotonic, budget_s=budget,
        )
    assert calls["n"] > 3, "it must retry within the budget, not give up at once"
    assert sum(clock.sleeps) == pytest.approx(budget, abs=0.011)
    assert sum(clock.sleeps) <= budget, "never sleeps past the budget"
    # The default is still the registry's two seconds: only the hook's bound is short.
    full = _Clock()
    with pytest.raises(PermissionError):
        endpoint.read_lease_text(tmp_path / "lease", platform="win32", read=busy, sleep=full.sleep, monotonic=full.monotonic)
    assert sum(full.sleeps) == pytest.approx(endpoint._WINDOWS_SHARING_RETRY_BUDGET_S, abs=0.011)


@posix_only("a mode-000 lease is how POSIX produces a PermissionError for the Windows branch to retry")
def test_endpoint_usable_reads_the_lease_with_the_hook_budget(
    endpoint: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the whole function, on a lease that is always busy: the answer is
    'not usable' and it comes back inside the budget, not after two seconds."""
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    _lease()
    path = _config() / f"storage_service_addr.{service_identity()}"
    path.chmod(0)
    monkeypatch.setattr(endpoint, "_is_windows", lambda platform=None: True)
    try:
        started = time.monotonic()
        usable = endpoint.endpoint_usable(_config(), retry_budget_s=endpoint.HOOK_LEASE_RETRY_BUDGET_S)
        elapsed = time.monotonic() - started
    finally:
        path.chmod(0o600)
    assert usable is False
    assert elapsed < 0.1, f"the lease read took {elapsed:.3f}s; the hook has 100 ms in all"


def test_the_hook_passes_its_budget(notice: ModuleType, endpoint: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    notice.load_siblings()
    seen: dict[str, object] = {}

    def fake(config: Path, *, retry_budget_s: float | None = None) -> bool:
        seen["budget"] = retry_budget_s
        return True

    monkeypatch.setattr(notice._endpoint_resolve, "endpoint_usable", fake)
    assert notice.service_usable(_config()) is True
    assert seen["budget"] == notice.HOOK_READ_BUDGET_S == endpoint.HOOK_LEASE_RETRY_BUDGET_S
    # With a deadline already spent by the status reads, the lease gets what is left, nothing.
    assert notice.service_usable(_config(), deadline=time.monotonic() - 1.0) is True
    assert seen["budget"] == 0.0


# -- the hand-edited config.yml: what the plugin's scan reads, and the limits it states ----------------


def _scan(endpoint: ModuleType, text: str, keys: tuple[str, ...] = ("service_url", "service_token")) -> dict[str, str]:
    (_config() / "config.yml").write_text(text, encoding="utf-8")
    return endpoint.read_config_yml_credentials(_config(), keys)


def test_the_scan_reads_a_flow_mapping_on_one_line_and_over_several(endpoint: ModuleType) -> None:
    assert _scan(endpoint, "credentials: {service_url: https://a.example, service_token: t1}\n") == {
        "service_url": "https://a.example", "service_token": "t1",
    }
    assert _scan(endpoint, "credentials: {\n service_url: https://a.example,\n service_token: t1\n}\n") == {
        "service_url": "https://a.example", "service_token": "t1",
    }
    assert _scan(endpoint, 'credentials: {"service_url": \'https://a.example\', service_token: "t,1"}\n') == {
        "service_url": "https://a.example", "service_token": "t,1",
    }
    assert _scan(endpoint, "credentials: {service_url: https://a.example, other: x}\n") == {"service_url": "https://a.example"}


def test_the_scan_states_its_limits_by_reading_nothing_where_it_does_not_understand(endpoint: ModuleType) -> None:
    """THE LIMITS (the docstring's list): a key of a document-wide flow mapping, a nested
    mapping and an unterminated mapping read as absent, never as a guess. A machine
    configured that way is read as having no saved address by the plugin and as having
    one by the CLI: the one divergence left in the hand-edited config, and a name for it."""
    assert _scan(endpoint, "{credentials: {service_url: https://a.example, service_token: t}}\n") == {}
    assert _scan(endpoint, "credentials: {service_url: {nested: x}, service_token: t}\n") == {"service_token": "t"}
    assert _scan(endpoint, "credentials: {extra: {a: 1, service_token: x}, service_url: u}\n") == {"service_url": "u"}, "a nested mapping's keys are not credentials"
    assert _scan(endpoint, "credentials: {service_url: https://a.example\n") == {}
    assert _scan(endpoint, "credentials:\n  service_url: https://a.example\n  service_token: t\n") == {
        "service_url": "https://a.example", "service_token": "t",
    }
