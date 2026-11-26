# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The transport rules for a bearer, on the hook side (nexus-ec37o step 5).

(a) The data-token path of both drains and of the routing poster is held to the same cleartext
    rule as a stored credential: a lease is bound to its host, not to a transport, so a data token
    goes only over https or to a loopback host.
(b) A request that carries a bearer over plain http goes direct, whatever ``HTTP_PROXY`` /
    ``HTTPS_PROXY`` / ``ALL_PROXY`` name: the drains (the wheel verb and the plugin script, each a
    real process) and the routing poster child. The supervisor lease token travels that way on
    every path, so a proxy recorder on a port-0 socket must see none of these requests.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.db.test_data_token_origin_binding import _write_lease_file
from tests.db.test_token_transport_rules import _Server
from tests.hooks.test_drain_credential_binding import (
    _BOTH,
    _LEASE,
    _REFUSED,
    _ROUTING_DIR,
    _SAVED,
    _drain,
    _drops,
    _save,
    _send_event,
    _url,
)
from tests.hooks.test_mailbox_drain_hook import _row, _write_storage_lease, engine  # noqa: F401 -- the fixture

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@pytest.fixture
def proxy():
    srv = _Server(proxy=True)
    yield srv
    srv.close()


def _via(proxy: _Server) -> dict[str, str]:
    """The environment a repository's settings could set: every proxy variable, no exemption."""
    env = dict.fromkeys(_PROXY_VARS, proxy.url)
    env.update({"NO_PROXY": "", "no_proxy": ""})
    return env


@_BOTH
class TestDrainsSendABearerDirect:
    def test_the_lease_token_never_goes_through_a_proxy(self, tmp_path, engine, proxy, argv) -> None:
        real = engine()
        real.rows = [_row("px11", body="lease mail")]
        _write_storage_lease(tmp_path / "config", host="127.0.0.1", port=real.port)
        res = _drain(tmp_path, argv, _via(proxy))
        assert res.returncode == 0, res.stderr
        assert "lease mail" in res.stdout, res.stderr
        assert real.auth and set(real.auth) == {f"Bearer {_LEASE}"}, real.auth
        assert proxy.seen == [], proxy.seen

    def test_the_lease_token_reached_by_an_env_port_never_goes_through_a_proxy(
        self, tmp_path, engine, proxy, argv,
    ) -> None:
        """Not the bare-lease leg (``is_local`` is False): the env names the lease's own port."""
        real = engine()
        real.rows = [_row("px12", body="env port mail")]
        _write_storage_lease(tmp_path / "config", host="127.0.0.1", port=real.port)
        res = _drain(tmp_path, argv, {"NX_SERVICE_PORT": str(real.port), **_via(proxy)})
        assert "env port mail" in res.stdout, res.stderr
        assert set(real.auth) == {f"Bearer {_LEASE}"}, real.auth
        assert proxy.seen == [], proxy.seen

    def test_an_env_url_and_token_never_go_through_a_proxy(self, tmp_path, engine, proxy, argv) -> None:
        real = engine()
        real.rows = [_row("px13", body="env url mail")]
        res = _drain(tmp_path, argv, {
            "NX_SERVICE_URL": _url(real), "NX_SERVICE_TOKEN": "exported-by-the-user", **_via(proxy),
        })
        assert "env url mail" in res.stdout, res.stderr
        assert set(real.auth) == {"Bearer exported-by-the-user"}, real.auth
        assert proxy.seen == [], proxy.seen

    def test_a_saved_token_at_its_own_plain_http_url_never_goes_through_a_proxy(
        self, tmp_path, engine, proxy, argv,
    ) -> None:
        real = engine()
        real.rows = [_row("px14", body="saved mail")]
        _save(tmp_path, url=_url(real))
        res = _drain(tmp_path, argv, _via(proxy))
        assert "saved mail" in res.stdout, res.stderr
        assert set(real.auth) == {f"Bearer {_SAVED}"}, real.auth
        assert proxy.seen == [], proxy.seen

    def test_a_data_token_over_loopback_http_never_goes_through_a_proxy(
        self, tmp_path, engine, proxy, argv,
    ) -> None:
        real = engine()
        real.rows = [_row("px15", body="data token mail")]
        _write_lease_file(tmp_path / "config", _url(real), scheme="http", tenant="default")
        res = _drain(tmp_path, argv, {"NX_SERVICE_URL": _url(real), **_via(proxy)})
        assert "data token mail" in res.stdout, res.stderr
        assert set(real.auth) == {"Bearer LEASED"}, real.auth
        assert proxy.seen == [], proxy.seen


class TestDataTokenIsHeldToTheCleartextRule:
    @_REFUSED
    def test_a_data_token_lease_is_not_sent_over_plain_http_off_the_machine(
        self, tmp_path, engine, argv,
    ) -> None:
        decoy = engine()
        url = "http://192.0.2.1:9"
        _write_lease_file(tmp_path / "config", url, scheme="http", tenant="default")
        res = _drain(tmp_path, argv, {"NX_SERVICE_URL": url})
        assert res.returncode == 0, res.stderr
        assert "plain http" in res.stderr, res.stderr
        assert "192.0.2.1" not in res.stderr + res.stdout, res.stderr
        assert decoy.auth == []

    @_BOTH
    def test_an_old_lease_with_no_scheme_field_is_not_served_off_the_machine(self, tmp_path, argv) -> None:
        url = "http://192.0.2.1:9"
        _write_lease_file(tmp_path / "config", url, tenant="default")  # no scheme: never served over cleartext off-machine
        res = _drain(tmp_path, argv, {"NX_SERVICE_URL": url})
        assert res.returncode == 0, res.stderr
        assert "found no credential" in res.stderr, res.stderr


class TestRoutingPoster:
    def test_the_lease_token_never_goes_through_a_proxy(self, tmp_path, engine, proxy) -> None:
        from tests._settle import settle  # noqa: PLC0415

        real = engine()
        _write_storage_lease(tmp_path / "config", host="127.0.0.1", port=real.port)
        res = _send_event(tmp_path, _via(proxy))
        assert res.returncode == 0, res.stderr
        settle(lambda: not real.auth, what="the poster child's POST")
        assert set(real.auth) == {f"Bearer {_LEASE}"}, real.auth
        assert proxy.seen == [], proxy.seen

    def test_a_data_token_lease_is_not_posted_over_plain_http_off_the_machine(self, tmp_path) -> None:
        from tests._settle import settle  # noqa: PLC0415

        url = "http://192.0.2.1:9"
        _write_lease_file(tmp_path / "config", url, scheme="http", tenant="default")
        res = _send_event(tmp_path, {"NX_SERVICE_URL": url})
        assert res.returncode == 0, res.stderr
        settle(lambda: "unresolvable" not in _drops(tmp_path), what="the unresolvable drop record")
        assert "192.0.2.1" not in _drops(tmp_path)


def test_the_plugin_script_and_the_routing_hook_import_the_proxy_rule_from_one_module() -> None:
    """The routing poster and the drain script call ``_endpoint_resolve.bearer_may_use_proxy``, the
    stdlib mirror the parity test holds to the CLI's text; no third copy of the rule exists."""
    scripts = Path(_ROUTING_DIR).parent
    hits = [p.name for p in scripts.rglob("*.py") if "def bearer_may_use_proxy" in p.read_text(encoding="utf-8")]
    assert hits == ["_endpoint_resolve.py"], hits
