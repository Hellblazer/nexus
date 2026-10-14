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

Known difference, not tested for agreement because it needs a process and a socket: a
lease that has just passed its TTL is read as live by the real server when its owner
process is alive and its port answers ``/health`` (reader grace, nexus-wo6sc). The plugin
makes no network request, so it reads that lease as absent.

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


def _lease(*, host: str = "127.0.0.1", port: int = 4242, token: str = "lease-token", ttl: float = 60.0, clock: float | None = None) -> None:
    registry = ServiceRegistry(
        dir=_config(), tier="storage_service", ttl=ttl, **({} if clock is None else {"clock": lambda: clock}),
    )
    registry.publish(
        service_identity(), endpoint={"host": host, "port": port, "token": token}, version="test", owner_token="agreement",
    )


def _save(**credentials: str) -> None:
    from nexus.config import set_credential

    for key, value in credentials.items():
        set_credential(key, value)


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


def _corrupt_lease(m: pytest.MonkeyPatch) -> None:
    (_config() / f"storage_service_addr.{service_identity()}").write_text("{ not json", encoding="utf-8")


def _shutdown_lease(m: pytest.MonkeyPatch) -> None:
    _lease()
    path = _config() / f"storage_service_addr.{service_identity()}"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = "shutting_down"
    path.write_text(json.dumps(data), encoding="utf-8")


CASES += [
    ("corrupt lease", _corrupt_lease, False),
    ("lease marked shutting down", _shutdown_lease, False),
]


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
    assert seen["budget"] == endpoint.HOOK_LEASE_RETRY_BUDGET_S
