# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o: the token the ``nx setup`` page checks goes to the address typed and nowhere else.

``conexus/launch/nx_setup.py`` ``check_pair`` makes three kinds of request. ``GET /version`` carries no token, and its answer
is trusted, so it takes the transport of ``config.yml`` (``tests/test_ec37o_probe_transport.py``). The two that carry the
token (``TOKEN_ROUTE``, ``MINT_ROUTE``) take no proxy, no redirect and the context with no environment in it: a repository's
env can name ``NEXUS_CONFIG_DIR`` and so plant a ``config.yml`` with a proxy and a CA, and the token the user typed into the
page must not travel through them.

The planted directory here names a tunnelling proxy and the CA of the origin. If the token calls honoured it, the token
would reach the origin through the proxy; they do not, so the origin is never sent a token (its certificate is not trusted
without the planted CA) and the proxy tunnels the one tokenless request only.
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from tests._tls_pki import TlsOrigin, TunnelProxy, make_ca

ROOT = Path(__file__).resolve().parents[1]
LAUNCH = ROOT / "conexus" / "launch"
TOKEN = "setup-page-token-0123456789"


@pytest.fixture
def pki(tmp_path: Path):
    ca = make_ca(tmp_path, "real-ca")
    return ca, ca.issue_leaf(tmp_path, "origin")


@pytest.fixture
def origin(pki) -> Iterator[TlsOrigin]:
    server = TlsOrigin(pki[1])
    yield server
    server.close()


@pytest.fixture
def tunnel() -> Iterator[TunnelProxy]:
    proxy = TunnelProxy()
    yield proxy
    proxy.close()


def test_a_planted_config_proxy_and_ca_never_carry_the_token(tmp_path, pki, origin, tunnel):
    cfg = tmp_path / "planted"
    cfg.mkdir()
    (cfg / "config.yml").write_text(
        yaml.safe_dump({"network": {"https_proxy": tunnel.url, "ca_bundle": str(pki[0].pem)}}), encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k.upper() not in {"SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"}}
    env["NEXUS_CONFIG_DIR"] = str(cfg)
    code = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(LAUNCH)!r})\n"
        "import nx_setup\n"
        "r = nx_setup.check_pair(sys.argv[1], sys.argv[2])\n"
        "print(json.dumps(list(r)[:2]))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, origin.url, TOKEN], capture_output=True, text=True, env=env, timeout=120,
        encoding="utf-8", errors="replace",
    )
    context = (proc.returncode, proc.stdout[-300:], proc.stderr[-300:], origin.requests, tunnel.connects)
    assert any(path == "/version" for path, _auth in origin.requests), context  # the tokenless probe took the config transport: positive control
    assert origin.auth == [], context  # no request that carried a token reached the origin
    assert len(tunnel.connects) == 1, context  # and the proxy tunnelled the /version request only
