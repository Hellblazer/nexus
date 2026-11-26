# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o: a repository's proxy and CA environment never meet a bearer; ``config.yml`` is the one place they come from.

A repository's ``.claude/settings.json`` env reaches the plugin hooks, the MCP server and every ``nx``
the model runs, so a repository can set ``HTTPS_PROXY`` and ``SSL_CERT_FILE`` (``SSL_CERT_DIR``,
``REQUESTS_CA_BUNDLE``, ``CURL_CA_BUNDLE``, ``ALL_PROXY`` ...) beside an ``NX_SERVICE_URL`` equal to the
managed origin a saved token is bound to. A proxy that terminates TLS with the repository's own CA then
reads the ``Authorization`` header. Sam's ruling (2026-11-26, T2
``nexus/sam-ruling-ec37o-token-host-binding-2026-11-26`` item 6): a request that carries a bearer ignores
those variables; a user who needs a corporate proxy or CA sets ``network.https_proxy`` /
``network.ca_bundle`` in nexus's own ``config.yml``, which a repository cannot write.

Each caller of ``tests/test_ec37o_decoy_suite.py`` runs here as a real process (the bare-python plugin
drain, the routing poster, the MCP T2 construction, ``bearer_for``, the doctor probe included) against a
real TLS origin the credential belongs to, a real TLS-terminating proxy and a real tunnelling proxy,
in four legs:

* ``env_attack``: the environment names the terminating proxy and its CA; ``config.yml`` names only the
  origin's CA. The proxy never reads a bearer and the origin gets it.
* ``env_trust_only``: the environment names the origin's own CA and ``config.yml`` none. The origin is not
  trusted, so no request is sent: an environment CA adds no root.
* ``config_tunnel``: ``config.yml`` names a tunnelling proxy and the origin's CA, and the environment
  still names the attacker's proxy. The request goes through the configured proxy and the bearer reaches
  the origin; the attacker's proxy never reads a bearer.
* ``config_corporate_proxy``: ``config.yml`` names the terminating proxy and the CA that signed it (a
  corporate TLS-inspecting proxy): the user's choice is honoured, and the proxy reads the bearer.

Only requests that carry a bearer are held to this (Sam's ruling leaves the others alone): the T3 client's
unauthenticated ``/version`` probe, for one, still honours ``HTTPS_PROXY`` and ``SSL_CERT_FILE``, so the
assertions are on what the proxies read (``auth``), never on whether anything touched them.
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from tests._tls_pki import TerminatingProxy, TlsOrigin, TunnelProxy, make_ca
from tests.test_ec37o_decoy_suite import CALLERS, KINDS, _config_dir

LEGS = ("env_attack", "env_trust_only", "config_tunnel", "config_corporate_proxy")


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory):
    d = tmp_path_factory.mktemp("netcfg-pki")
    real, attacker = make_ca(d, "real-ca"), make_ca(d, "attacker-ca")
    return real, attacker, real.issue_leaf(d, "origin"), attacker.issue_leaf(d, "proxy")


@pytest.fixture
def origin(pki) -> Iterator[TlsOrigin]:
    server = TlsOrigin(pki[2])
    yield server
    server.close()


@pytest.fixture
def mitm(pki) -> Iterator[TerminatingProxy]:
    proxy = TerminatingProxy(pki[3])
    yield proxy
    proxy.close()


@pytest.fixture
def tunnel() -> Iterator[TunnelProxy]:
    proxy = TunnelProxy()
    yield proxy
    proxy.close()


def _proxy_env(url: str) -> dict[str, str]:
    return {
        "HTTPS_PROXY": url, "https_proxy": url, "HTTP_PROXY": url, "http_proxy": url, "ALL_PROXY": url, "all_proxy": url,
    }


def _ca_env(pem: Path, directory: Path) -> dict[str, str]:
    directory.mkdir(exist_ok=True)
    (directory / pem.name).write_bytes(pem.read_bytes())
    return {
        "SSL_CERT_FILE": str(pem), "SSL_CERT_DIR": str(directory), "REQUESTS_CA_BUNDLE": str(pem), "CURL_CA_BUNDLE": str(pem),
    }  # no SSLKEYLOGFILE: a request with no bearer still reads it, and on Windows uv's CPython dies opening the file (no OPENSSL_Applink)


def _kind(caller) -> str:
    return "saved_service_token_with_url" if "saved_service_token_with_url" in caller.kinds else "saved_mint_token"


def _add_network(tmp_path: Path, **network: str) -> None:
    path = _config_dir(tmp_path) / "config.yml"
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    if network:
        data["network"] = network
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _matrix() -> list[pytest.param]:
    params = []
    for caller in CALLERS:
        for leg in LEGS:
            # the T3 client reads /version from whatever answers: the terminating proxy standing in for the engine
            # is not a conformant one, and the client raises ManagedServiceIncompatible (a guarded error) in the child
            marks = (pytest.mark.expects_unresolvable_endpoint,) if caller.name == "nx_t3_vector_client" and leg == "config_corporate_proxy" else ()
            params.append(pytest.param(caller, leg, id=f"{caller.name}-{leg}", marks=marks))
    return params


@pytest.mark.parametrize(("caller", "leg"), _matrix())
def test_a_bearer_request_takes_its_proxy_and_ca_from_config_only(tmp_path, pki, origin, mitm, tunnel, caller, leg):
    real_ca, attacker_ca = pki[0], pki[1]
    kind = _kind(caller)
    KINDS[kind].store(tmp_path, origin)  # a saved token bound to the origin's https URL
    env: dict[str, str] = {"NX_SERVICE_URL": origin.url, "_NX_TEST_REAL_URL": origin.url}
    attack = {**_proxy_env(mitm.url), **_ca_env(attacker_ca.pem, tmp_path / "attacker-certs")}
    expects_bearer = True

    if leg == "env_attack":
        _add_network(tmp_path, ca_bundle=str(real_ca.pem))
        env |= attack
    elif leg == "env_trust_only":
        env |= _ca_env(real_ca.pem, tmp_path / "real-certs")
        expects_bearer = False
    elif leg == "config_tunnel":
        _add_network(tmp_path, https_proxy=tunnel.url, ca_bundle=str(real_ca.pem))
        env |= attack
    else:
        _add_network(tmp_path, https_proxy=mitm.url, ca_bundle=str(attacker_ca.pem))
        env |= _proxy_env("http://127.0.0.1:9")  # the environment names a dead proxy: it must not matter

    proc = caller.run(tmp_path, env)
    if caller.after is not None:
        caller.after(tmp_path, origin, mitm, expects_bearer and leg != "config_corporate_proxy")

    header = KINDS[kind].header
    allowed = {header, "Bearer minted-data-token"}
    context = (leg, proc.returncode, proc.stdout[-500:], proc.stderr[-500:], origin.requests, mitm.connects, mitm.requests, tunnel.connects)

    assert mitm.auth == [] or leg == "config_corporate_proxy", context  # the repository's proxy never reads a bearer
    if leg == "env_attack":
        assert not tunnel.connects, context
        assert header in origin.auth, context  # and the request reached the bound origin, over the CA of config.yml
    elif leg == "env_trust_only":
        assert origin.auth == [] and not tunnel.connects, context  # no root was added by the env
    elif leg == "config_tunnel":
        assert any(c.startswith(f"CONNECT 127.0.0.1:{origin.port}") for c in tunnel.connects), context
        assert header in origin.auth, context  # the bearer travelled inside the tunnel to the bound origin
    else:
        assert any(c.startswith(f"CONNECT 127.0.0.1:{origin.port}") for c in mitm.connects), context
        assert header in mitm.auth, context  # the user named this proxy and its CA: it is trusted with the bearer
    assert set(origin.auth) <= allowed, context


def test_the_harness_is_not_vacuous(tmp_path, pki, origin, mitm):
    """The absences above are measured against this presence: with the origin's CA in ``config.yml``, a
    direct request reaches the origin and carries its bearer, and the proxy stays untouched."""
    _config_dir(tmp_path)
    _add_network(tmp_path, ca_bundle=str(pki[0].pem))
    env = {**os.environ, "NEXUS_CONFIG_DIR": str(_config_dir(tmp_path))}
    code = (
        "import httpx, sys; from nexus.db.t2.bearer_transport import bearer_httpx_kwargs; "
        "r = httpx.get(sys.argv[1] + '/x', headers={'Authorization': 'Bearer probe'}, **bearer_httpx_kwargs(sys.argv[1])); print(r.status_code)"
    )
    out = subprocess.run([sys.executable, "-c", code, origin.url], capture_output=True, text=True, env=env, timeout=120)
    assert out.stdout.strip() == "200", out.stderr[-400:]
    assert origin.auth == ["Bearer probe"] and not mitm.touched
