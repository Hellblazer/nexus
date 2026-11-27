# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The mailbox drains send a stored credential only to the origin it was issued for
(nexus-ec37o step 4).

A repository's ``.claude/settings.json`` env reaches the hooks, so ``NX_SERVICE_URL`` /
``NX_SERVICE_HOST`` / ``NX_SERVICE_PORT`` can name any host. Each case runs a drain as a
real process (the wheel verb and the plugin script, the latter under the suite's
interpreter with a scrubbed env) against two port-0 ``http.server`` engines, a "real"
one the credential belongs to and a "decoy" the env points at, and asserts on the
Authorization headers each one recorded.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nexus._winsec import restrict_to_owner
from tests.hooks.test_mailbox_drain_hook import (
    _PLUGIN_ARGV,
    _VERB_ARGV,
    _row,
    _run,
    _write_storage_lease,
    engine,  # noqa: F401 -- the fixture
)

_BOTH = pytest.mark.parametrize("argv", [_VERB_ARGV, _PLUGIN_ARGV], ids=["wheel", "plugin"])
#: A case the wheel drain refuses through the CLI's resolver, which raises a guarded storage error on
#: purpose (``expects_unresolvable_endpoint``); the plugin script never imports nexus and raises none,
#: so the marker (reported stale when nothing is raised) sits on the wheel variant only.
_REFUSED = pytest.mark.parametrize(
    "argv",
    [pytest.param(_VERB_ARGV, id="wheel", marks=pytest.mark.expects_unresolvable_endpoint),
     pytest.param(_PLUGIN_ARGV, id="plugin")],
)

_SAVED = "saved-service-token"
_LEASE = "local-supervisor-token"  # what _write_storage_lease puts in the lease


def poster_cfg(tmp_path: Path) -> Path:
    """The account's config directory the telemetry poster reads (RDR-259 Decision 9): ``<account home>/.config/nexus``, the account
    home being the in-process test seam ``_audience._ACCOUNT_HOME_OVERRIDE`` that ``_send_event`` sets."""
    return tmp_path / "acct" / ".config" / "nexus"


def _save(tmp_path: Path, *, url: str | None, token: str = _SAVED, cfg: Path | None = None) -> None:
    lines = ["credentials:"]
    if url is not None:
        lines.append(f"  service_url: {url}")
    lines.append(f"  service_token: {token}")
    cfg = cfg if cfg is not None else tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    path = cfg / "config.yml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    restrict_to_owner(path)


def _url(eng) -> str:
    return f"http://127.0.0.1:{eng.port}"


def _drain(tmp_path, argv, env):
    return _run(tmp_path=tmp_path, argv=argv, env_overrides=env)


class TestSavedTokenIsBoundToItsServiceUrl:
    @_REFUSED
    def test_env_url_naming_another_host_gets_no_saved_token(self, tmp_path, engine, argv) -> None:
        real, decoy = engine(), engine()
        _save(tmp_path, url=_url(real))
        res = _drain(tmp_path, argv, {"NX_SERVICE_URL": _url(decoy)})
        assert res.returncode == 0, res.stderr
        assert decoy.auth == [], decoy.auth
        assert real.auth == [], real.auth
        assert str(decoy.port) not in res.stderr + res.stdout, res.stderr

    @_BOTH
    def test_env_url_naming_the_bound_origin_still_sends_it(self, tmp_path, engine, argv) -> None:
        real, decoy = engine(), engine()
        real.rows = [_row("bd11", body="bound mail")]
        _save(tmp_path, url=_url(real))
        res = _drain(tmp_path, argv, {"NX_SERVICE_URL": _url(real)})
        assert res.returncode == 0, res.stderr
        assert "bound mail" in res.stdout, res.stderr
        assert real.auth and set(real.auth) == {f"Bearer {_SAVED}"}, real.auth
        assert decoy.auth == []

    @_REFUSED
    def test_no_saved_service_url_binds_the_token_to_the_default_managed_url(
        self, tmp_path, engine, argv,
    ) -> None:
        real = engine()
        _save(tmp_path, url=None)
        res = _drain(tmp_path, argv, {"NX_SERVICE_URL": _url(real)})
        assert res.returncode == 0, res.stderr
        assert real.auth == [], real.auth


class TestLeaseTokenIsBoundToTheLeaseEndpoint:
    @_REFUSED
    def test_env_url_naming_another_host_gets_no_lease_token(self, tmp_path, engine, argv) -> None:
        real, decoy = engine(), engine()
        _write_storage_lease(tmp_path / "config", host="127.0.0.1", port=real.port)
        res = _drain(tmp_path, argv, {"NX_SERVICE_URL": _url(decoy)})
        assert res.returncode == 0, res.stderr
        assert decoy.auth == [], decoy.auth
        assert str(decoy.port) not in res.stderr + res.stdout, res.stderr

    @_REFUSED
    def test_env_host_and_port_naming_another_port_gets_no_lease_token(
        self, tmp_path, engine, argv,
    ) -> None:
        real, decoy = engine(), engine()
        _write_storage_lease(tmp_path / "config", host="127.0.0.1", port=real.port)
        res = _drain(tmp_path, argv, {
            "NX_SERVICE_HOST": "127.0.0.1", "NX_SERVICE_PORT": str(decoy.port),
        })
        assert res.returncode == 0, res.stderr
        assert decoy.auth == [], decoy.auth

    @_BOTH
    def test_env_port_naming_the_lease_endpoint_still_sends_it(self, tmp_path, engine, argv) -> None:
        real = engine()
        real.rows = [_row("ls11", body="lease mail")]
        _write_storage_lease(tmp_path / "config", host="127.0.0.1", port=real.port)
        res = _drain(tmp_path, argv, {"NX_SERVICE_PORT": str(real.port)})
        assert res.returncode == 0, res.stderr
        assert "lease mail" in res.stdout, res.stderr
        assert real.auth and set(real.auth) == {f"Bearer {_LEASE}"}, real.auth

    @_BOTH
    def test_the_bare_lease_still_drains(self, tmp_path, engine, argv) -> None:
        real = engine()
        real.rows = [_row("ls22", body="bare lease mail")]
        _write_storage_lease(tmp_path / "config", host="127.0.0.1", port=real.port)
        res = _drain(tmp_path, argv, {})
        assert "bare lease mail" in res.stdout, res.stderr
        assert set(real.auth) == {f"Bearer {_LEASE}"}

    @_BOTH
    def test_a_saved_token_bound_elsewhere_does_not_hide_the_lease_token(
        self, tmp_path, engine, argv,
    ) -> None:
        """The saved token is refused for the lease endpoint (it belongs to the managed
        service), and the lease token, which is bound to the lease endpoint, is the one sent."""
        real = engine()
        real.rows = [_row("ls33", body="lease beats mismatched saved")]
        _write_storage_lease(tmp_path / "config", host="127.0.0.1", port=real.port)
        _save(tmp_path, url=None)
        res = _drain(tmp_path, argv, {"NX_SERVICE_PORT": str(real.port)})
        assert "lease beats mismatched saved" in res.stdout, res.stderr
        assert set(real.auth) == {f"Bearer {_LEASE}"}, real.auth


@_BOTH
class TestEnvTokenIsTheKnownLimit:
    def test_an_env_token_travels_with_the_env_url(self, tmp_path, engine, argv) -> None:
        """Accepted residual (Sam 2026-11-26): a token the user exported themselves is
        unbound. The bound-credential rules above do not apply to it."""
        decoy = engine()
        res = _drain(tmp_path, argv, {
            "NX_SERVICE_URL": _url(decoy), "NX_SERVICE_TOKEN": "exported-by-the-user",
        })
        assert res.returncode == 0, res.stderr
        assert set(decoy.auth) == {"Bearer exported-by-the-user"}, decoy.auth


class TestCleartextOffMachine:
    @_REFUSED
    def test_a_bearer_is_not_sent_over_plain_http_to_a_non_loopback_host(
        self, tmp_path, argv,
    ) -> None:
        res = _drain(tmp_path, argv, {
            "NX_SERVICE_URL": "http://192.0.2.1:9", "NX_SERVICE_TOKEN": "exported-by-the-user",
        })
        assert res.returncode == 0, res.stderr
        assert "plain http" in res.stderr, res.stderr
        assert "192.0.2.1" not in res.stderr, res.stderr


# ---------------------------------------------------------------------------
# The routing telemetry poster (conexus/hooks/scripts/routing/_lib.py)
# ---------------------------------------------------------------------------

_ROUTING_DIR = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "routing"

_SEND = (
    "import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + '/..'); "
    "import _audience; _audience._ACCOUNT_HOME_OVERRIDE = sys.argv[2]; import _lib; "
    "_lib._send_routing_event({'rule': 'r', 'outcome': 'allow'})"
)


def _send_event(tmp_path: Path, env: dict[str, str]):
    """Run the hook side of the telemetry write as a real process (it resolves the endpoint, then
    spawns the detached poster child that carries the bearer)."""
    import subprocess  # noqa: PLC0415 -- only these cases spawn
    import sys  # noqa: PLC0415

    # The ambient environment minus the service variables (Windows needs SYSTEMROOT for sockets in
    # the poster child); the drain cases above scrub the same way through ``_env``.
    child_env = {k: v for k, v in os.environ.items() if not k.startswith(("NX_SERVICE_", "NX_MINT_"))}
    child_env["NX_DROPPED_WRITES_LOG_PATH"] = str(tmp_path / "dropped.jsonl")
    child_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", _SEND, str(_ROUTING_DIR), str(tmp_path / "acct")],
        env=child_env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


def _drops(tmp_path: Path) -> str:
    path = tmp_path / "dropped.jsonl"
    return path.read_text(encoding="utf-8") if path.exists() else ""


class TestRoutingTelemetryPoster:
    def test_env_url_naming_another_host_gets_no_saved_token(self, tmp_path, engine) -> None:
        """RDR-259 Decision 9 on top of the origin binding: the environment names no telemetry endpoint at all, so a decoy
        URL in it gets nothing, and the saved token still goes to the saved URL."""
        from tests._settle import settle  # noqa: PLC0415

        real, decoy = engine(), engine()
        _save(tmp_path, url=_url(real), cfg=poster_cfg(tmp_path))
        res = _send_event(tmp_path, {"NX_SERVICE_URL": _url(decoy)})
        assert res.returncode == 0, res.stderr
        settle(lambda: not real.auth, what="the poster child's POST")
        assert decoy.auth == [], decoy.auth
        assert set(real.auth) == {f"Bearer {_SAVED}"}, real.auth

    def test_env_url_naming_the_bound_origin_still_posts_with_it(self, tmp_path, engine) -> None:
        from tests._settle import settle  # noqa: PLC0415

        real = engine()
        _save(tmp_path, url=_url(real), cfg=poster_cfg(tmp_path))
        res = _send_event(tmp_path, {"NX_SERVICE_URL": _url(real)})
        assert res.returncode == 0, res.stderr
        settle(lambda: not real.auth, what="the poster child's POST")
        assert set(real.auth) == {f"Bearer {_SAVED}"}, real.auth

    def test_env_port_naming_another_port_gets_no_lease_token(self, tmp_path, engine) -> None:
        from tests._settle import settle  # noqa: PLC0415

        real, decoy = engine(), engine()
        _write_storage_lease(poster_cfg(tmp_path), host="127.0.0.1", port=real.port)
        res = _send_event(tmp_path, {
            "NX_SERVICE_HOST": "127.0.0.1", "NX_SERVICE_PORT": str(decoy.port),
        })
        assert res.returncode == 0, res.stderr
        settle(lambda: not real.auth, what="the poster child's POST")
        assert decoy.auth == [], decoy.auth
        assert set(real.auth) == {f"Bearer {_LEASE}"}, real.auth

    def test_the_bare_lease_still_posts(self, tmp_path, engine) -> None:
        from tests._settle import settle  # noqa: PLC0415

        real = engine()
        _write_storage_lease(poster_cfg(tmp_path), host="127.0.0.1", port=real.port)
        res = _send_event(tmp_path, {})
        assert res.returncode == 0, res.stderr
        settle(lambda: not real.auth, what="the poster child's POST")
        assert set(real.auth) == {f"Bearer {_LEASE}"}, real.auth


# ---------------------------------------------------------------------------
# endpoint_usable agrees with what the drain would send
# ---------------------------------------------------------------------------


class TestEndpointUsableFollowsTheBinding:
    """``endpoint_usable`` answers "would the CLI's resolver return an endpoint", and a credential
    that is refused for the URL in use is not a credential. The agreement table in
    ``test_usable_service_agreement.py`` holds the cases both sides give the same answer for; the
    refusals below are the plugin's half of the binding (the CLI's half lands with its resolver)."""

    @pytest.fixture
    def usable(self, tmp_path, monkeypatch):
        import importlib.util  # noqa: PLC0415 -- fixture-local
        scripts = _ROUTING_DIR.parent
        monkeypatch.syspath_prepend(str(scripts))
        spec = importlib.util.spec_from_file_location("_endpoint_resolve_binding", scripts / "_endpoint_resolve.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for name in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN", "NX_SERVICE_HOST", "NX_SERVICE_PORT",
                     "NX_MINT_TOKEN", "NX_MINT_TENANT"):
            monkeypatch.delenv(name, raising=False)
        cfg = tmp_path / "config"
        cfg.mkdir()
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
        return lambda: mod.endpoint_usable(cfg)

    def test_a_saved_token_is_not_usable_at_another_origin(self, tmp_path, monkeypatch, usable) -> None:
        _save(tmp_path, url="https://svc.example")
        monkeypatch.setenv("NX_SERVICE_URL", "https://decoy.example")
        assert usable() is False
        monkeypatch.setenv("NX_SERVICE_URL", "https://SVC.example:443/")
        assert usable() is True

    def test_a_saved_token_with_no_saved_url_is_bound_to_the_default_managed_url(
        self, tmp_path, monkeypatch, usable,
    ) -> None:
        _save(tmp_path, url=None)
        monkeypatch.setenv("NX_SERVICE_URL", "https://decoy.example")
        assert usable() is False
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com")
        assert usable() is True

    def test_the_lease_token_is_not_usable_at_another_port(self, tmp_path, monkeypatch, usable) -> None:
        _write_storage_lease(tmp_path / "config", host="127.0.0.1", port=4242)
        monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:4243")
        assert usable() is False
        monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:4242")
        assert usable() is True

    def test_host_and_port_env_naming_another_port_do_not_borrow_the_lease_token(
        self, tmp_path, monkeypatch, usable,
    ) -> None:
        _write_storage_lease(tmp_path / "config", host="127.0.0.1", port=4242)
        monkeypatch.setenv("NX_SERVICE_PORT", "4243")
        assert usable() is False
        monkeypatch.setenv("NX_SERVICE_PORT", "4242")
        assert usable() is True

    def test_an_env_token_is_not_usable_over_plain_http_off_the_machine(
        self, tmp_path, monkeypatch, usable,
    ) -> None:
        monkeypatch.setenv("NX_SERVICE_URL", "http://svc.example")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "t")
        assert usable() is False
        monkeypatch.setenv("NX_SERVICE_URL", "https://svc.example")
        assert usable() is True
