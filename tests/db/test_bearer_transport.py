# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o: a request that carries a bearer reads no proxy or CA variable from the environment.

A repository's ``.claude/settings.json`` env can set ``HTTPS_PROXY`` and ``SSL_CERT_FILE``, and a proxy
that terminates TLS with a CA the repository chose reads the ``Authorization`` header. The proxy and the
extra CA of a bearer-carrying request come from ``network.https_proxy`` / ``network.ca_bundle`` of the
global ``config.yml`` (Sam, 2026-11-26, T2 ``nexus/sam-ruling-ec37o-token-host-binding-2026-11-26`` item
6). These tests hold the primitives (CLI module and the plugin's stdlib mirror) against a real TLS
server and a real CA; ``tests/test_ec37o_netcfg_suite.py`` holds every caller as a process.
"""
from __future__ import annotations

import importlib.util
import socket
import ssl
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from nexus.db.t2 import bearer_transport as bt
from nexus.db import service_endpoint as se
from tests._tls_pki import TlsOrigin, make_ca

_SCRIPTS = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
_TRUST_VARS = (
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSLKEYLOGFILE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)


@pytest.fixture(scope="module")
def hook() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("_endpoint_resolve_bearer_net", _SCRIPTS / "_endpoint_resolve.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory):
    d = tmp_path_factory.mktemp("pki")
    real, other = make_ca(d, "real-ca"), make_ca(d, "other-ca")
    return real, other, real.issue_leaf(d, "real-leaf"), other.issue_leaf(d, "other-leaf")


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    for var in _TRUST_VARS:
        monkeypatch.delenv(var, raising=False)
    bt.reset_for_tests()
    return cfg


def _write_network(cfg: Path, **network: str) -> None:
    (cfg / "config.yml").write_text(yaml.safe_dump({"network": network}), encoding="utf-8")


def _proxies(opener) -> dict[str, str]:
    """The proxy mapping an opener applies: an empty ``ProxyHandler`` is not even registered."""
    import urllib.request

    found = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    return dict(found[0].proxies) if found else {}


def _handshake(ctx: ssl.SSLContext, origin: TlsOrigin) -> None:
    with socket.create_connection(("127.0.0.1", origin.port), timeout=5) as raw, ctx.wrap_socket(raw, server_hostname="127.0.0.1"):
        pass


@pytest.fixture
def origin(pki) -> Iterator[TlsOrigin]:
    server = TlsOrigin(pki[2])
    yield server
    server.close()


# ── the CLI trust store ──────────────────────────────────────────────────────

class TestCliTrust:
    def test_an_unset_trust_store_does_not_trust_the_test_ca(self, config_dir, origin) -> None:
        with pytest.raises(ssl.SSLCertVerificationError):
            _handshake(bt.bearer_ssl_context(), origin)

    def test_the_config_ca_adds_a_root(self, config_dir, origin, pki) -> None:
        _write_network(config_dir, ca_bundle=str(pki[0].pem))
        _handshake(bt.bearer_ssl_context(), origin)

    @pytest.mark.parametrize("var", ["SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"])
    def test_a_ca_file_in_the_environment_adds_no_root(self, config_dir, origin, pki, monkeypatch, var) -> None:
        monkeypatch.setenv(var, str(pki[0].pem))
        with pytest.raises(ssl.SSLCertVerificationError):
            _handshake(bt.bearer_ssl_context(), origin)

    def test_a_ca_directory_in_the_environment_adds_no_root(self, config_dir, origin, pki, tmp_path, monkeypatch) -> None:
        d = tmp_path / "certs"
        d.mkdir()
        monkeypatch.setenv("SSL_CERT_DIR", str(d))
        (d / "real-ca.pem").write_bytes(pki[0].pem.read_bytes())
        with pytest.raises(ssl.SSLCertVerificationError):
            _handshake(bt.bearer_ssl_context(), origin)

    def test_the_key_log_file_is_not_written(self, config_dir, origin, pki, tmp_path, monkeypatch) -> None:
        log = tmp_path / "keys.log"
        monkeypatch.setenv("SSLKEYLOGFILE", str(log))
        _write_network(config_dir, ca_bundle=str(pki[0].pem))
        _handshake(bt.bearer_ssl_context(), origin)
        assert not log.exists(), "the key log file named by the environment was opened"

    def test_the_context_follows_a_changed_config_ca(self, config_dir, origin, pki) -> None:
        _write_network(config_dir, ca_bundle=str(pki[1].pem))
        with pytest.raises(ssl.SSLCertVerificationError):
            _handshake(bt.bearer_ssl_context(), origin)
        _write_network(config_dir, ca_bundle=str(pki[0].pem))
        _handshake(bt.bearer_ssl_context(), origin)

    def test_a_missing_config_bundle_names_it(self, config_dir, tmp_path) -> None:
        _write_network(config_dir, ca_bundle=str(tmp_path / "nope.pem"))
        with pytest.raises(OSError, match="network.ca_bundle"):
            bt.bearer_ssl_context()


class TestCliSettings:
    def test_they_come_from_the_global_config_only(self, config_dir, tmp_path, monkeypatch) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".nexus.yml").write_text(yaml.safe_dump({"network": {"https_proxy": "http://evil.example:3128"}}), encoding="utf-8")
        monkeypatch.chdir(repo)
        monkeypatch.setenv("NEXUS_REPO_ROOT", str(repo))
        assert bt.network_settings() == bt.NetworkSettings("", "")
        _write_network(config_dir, https_proxy="http://proxy.corp:3128")
        assert bt.network_settings() == bt.NetworkSettings("http://proxy.corp:3128", "")

    def test_a_non_string_value_reads_as_unset(self, config_dir) -> None:
        _write_network(config_dir, https_proxy=3128)  # type: ignore[arg-type]
        assert bt.network_settings().https_proxy == ""


class TestCliHttpx:
    def test_the_environment_is_not_read(self, config_dir, monkeypatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://evil.example:3128")
        monkeypatch.setenv("ALL_PROXY", "http://evil.example:3128")
        kwargs = bt.bearer_httpx_kwargs("https://svc.example")
        assert kwargs["trust_env"] is False
        assert "proxy" not in kwargs and "mounts" not in kwargs
        assert isinstance(kwargs["verify"], ssl.SSLContext)

    def test_a_configured_proxy_serves_https_only(self, config_dir) -> None:
        _write_network(config_dir, https_proxy="http://proxy.corp:3128")
        assert bt.bearer_httpx_kwargs("https://svc.example")["proxy"] == "http://proxy.corp:3128"
        assert "proxy" not in bt.bearer_httpx_kwargs("http://127.0.0.1:9")
        assert "proxy" not in bt.bearer_httpx_kwargs("http://svc.example")

    def test_a_client_gets_the_proxy_and_a_direct_mount_for_plain_http(self, config_dir) -> None:
        _write_network(config_dir, https_proxy="http://proxy.corp:3128")
        kwargs = bt.bearer_httpx_kwargs()
        assert kwargs["proxy"] == "http://proxy.corp:3128" and list(kwargs["mounts"]) == ["http://"]

    def test_an_https_proxy_url_is_reached_over_the_same_context(self, config_dir) -> None:
        _write_network(config_dir, https_proxy="https://proxy.corp:3128")
        kwargs = bt.bearer_httpx_kwargs("https://svc.example")
        assert str(kwargs["proxy"].url) == "https://proxy.corp:3128" and kwargs["proxy"].ssl_context is kwargs["verify"]

    def test_a_client_with_no_proxy_has_no_mounts(self, config_dir) -> None:
        assert "mounts" not in bt.bearer_httpx_kwargs()


class TestCliUrllib:
    def test_the_environment_proxy_is_not_read(self, config_dir, monkeypatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://evil.example:3128")
        monkeypatch.setenv("HTTP_PROXY", "http://evil.example:3128")
        assert _proxies(bt.bearer_opener("https://svc.example")) == {}

    def test_a_configured_proxy_serves_https_only(self, config_dir) -> None:
        _write_network(config_dir, https_proxy="http://proxy.corp:3128")
        assert _proxies(bt.bearer_opener("https://svc.example")) == {"https": "http://proxy.corp:3128"}
        assert _proxies(bt.bearer_opener("http://127.0.0.1:9")) == {}

    def test_the_opener_trusts_what_the_context_trusts(self, config_dir, origin, pki) -> None:
        import urllib.error
        import urllib.request

        with pytest.raises(urllib.error.URLError):
            bt.bearer_opener(origin.url).open(origin.url + "/x", timeout=5)
        _write_network(config_dir, ca_bundle=str(pki[0].pem))
        bt.reset_for_tests()
        with bt.bearer_opener(origin.url).open(urllib.request.Request(origin.url + "/x", headers={"Authorization": "Bearer t"}), timeout=5) as resp:
            assert resp.status == 200
        assert origin.auth == ["Bearer t"]


# ── the plugin's stdlib mirror ───────────────────────────────────────────────

class TestPluginTrust:
    def test_an_unset_trust_store_does_not_trust_the_test_ca(self, hook, origin) -> None:
        with pytest.raises(ssl.SSLCertVerificationError):
            _handshake(hook.bearer_ssl_context(), origin)

    def test_the_config_ca_adds_a_root(self, hook, origin, pki) -> None:
        _handshake(hook.bearer_ssl_context(str(pki[0].pem)), origin)

    @pytest.mark.parametrize("var", ["SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"])
    def test_a_ca_file_in_the_environment_adds_no_root(self, hook, origin, pki, monkeypatch, var) -> None:
        monkeypatch.setenv(var, str(pki[0].pem))
        with pytest.raises(ssl.SSLCertVerificationError):
            _handshake(hook.bearer_ssl_context(), origin)

    def test_a_ca_directory_in_the_environment_adds_no_root(self, hook, origin, pki, tmp_path, monkeypatch) -> None:
        d = tmp_path / "certs"
        d.mkdir()
        (d / "real-ca.pem").write_bytes(pki[0].pem.read_bytes())
        monkeypatch.setenv("SSL_CERT_DIR", str(d))
        with pytest.raises(ssl.SSLCertVerificationError):
            _handshake(hook.bearer_ssl_context(), origin)

    def test_the_key_log_file_is_not_written(self, hook, origin, pki, tmp_path, monkeypatch) -> None:
        log = tmp_path / "keys.log"
        monkeypatch.setenv("SSLKEYLOGFILE", str(log))
        _handshake(hook.bearer_ssl_context(str(pki[0].pem)), origin)
        assert not log.exists(), "the key log file named by the environment was opened"

    def test_the_default_roots_load(self) -> None:
        """The interpreter's own bundle (or a system one, or the Windows stores) loads: the context holds roots, so a
        real site's chain can verify. A bare interpreter, as the plugin runs: ``import nexus`` would inject truststore."""
        import subprocess

        code = (
            "import sys, ssl; sys.path.insert(0, sys.argv[1]); import _endpoint_resolve as ep; ctx = ep.bearer_ssl_context(); "
            "print(ctx.verify_mode == ssl.CERT_REQUIRED, ctx.check_hostname, len(ctx.get_ca_certs()))"
        )
        out = subprocess.run([sys.executable, "-I", "-c", code, str(_SCRIPTS)], capture_output=True, text=True, timeout=60)
        verify, hostname, roots = out.stdout.split()
        assert (verify, hostname) == ("True", "True") and int(roots) > 0, (out.stdout, out.stderr[-300:])


class TestPluginOpener:
    def test_the_environment_proxy_is_not_read(self, hook, config_dir, monkeypatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://evil.example:3128")
        assert _proxies(hook.bearer_opener("https://svc.example", config_dir)) == {}

    def test_a_configured_proxy_serves_https_only(self, hook, config_dir) -> None:
        _write_network(config_dir, https_proxy="http://proxy.corp:3128")
        assert _proxies(hook.bearer_opener("https://svc.example", config_dir)) == {"https": "http://proxy.corp:3128"}
        assert _proxies(hook.bearer_opener("http://127.0.0.1:9", config_dir)) == {}


# ── the two copies agree ─────────────────────────────────────────────────────

URLS = [
    "https://api.conexus-nexus.com", "https://10.0.0.5:8443/x", "https://127.0.0.1:9", "http://127.0.0.1:9",
    "http://svc.example", "http://u:p@127.0.0.1", "HTTPS://Svc.Example:443", "ftp://x", "", "https://",
]


@pytest.mark.parametrize("url", URLS)
@pytest.mark.parametrize("proxy", ["", "http://proxy.corp:3128"])
def test_bearer_proxy_for_agrees_and_is_https_only(hook, url, proxy) -> None:
    got = se.bearer_proxy_for(url, proxy)
    assert got == hook.bearer_proxy_for(url, proxy)
    origin = se.normalize_origin(url)
    assert got == (proxy if proxy and origin is not None and origin[0] == "https" else None)


@pytest.mark.parametrize("network", [
    {"https_proxy": "http://proxy.corp:3128"},
    {"ca_bundle": "/etc/corp/root-ca.pem"},
    {"https_proxy": "http://u:p@proxy.corp:3128", "ca_bundle": r"C:\corp\root ca.pem"},
    {"https_proxy": "http://proxy.corp:3128", "ca_bundle": "/with space/and: colon/ca.pem"},
    {},
])
def test_the_plugin_reads_the_network_section_as_the_cli_does(hook, config_dir, network) -> None:
    # written the way ``nexus.config.set_config_value`` dumps it
    (config_dir / "config.yml").write_text(yaml.dump({"network": network}, default_flow_style=False), encoding="utf-8")
    cli = bt.network_settings()
    plugin = hook.read_config_yml_network(config_dir)
    assert plugin.get("https_proxy", "") == cli.https_proxy
    assert plugin.get("ca_bundle", "") == cli.ca_bundle
    assert cli.https_proxy == network.get("https_proxy", "") and cli.ca_bundle == network.get("ca_bundle", "")


def test_the_plugin_reads_a_flow_style_network_section(hook, config_dir) -> None:
    (config_dir / "config.yml").write_text("network: {https_proxy: 'http://proxy.corp:3128', ca_bundle: /etc/ca.pem}\n", encoding="utf-8")
    assert hook.read_config_yml_network(config_dir) == {"https_proxy": "http://proxy.corp:3128", "ca_bundle": "/etc/ca.pem"}
    bt.reset_for_tests()
    assert bt.network_settings() == bt.NetworkSettings("http://proxy.corp:3128", "/etc/ca.pem")


def test_the_credentials_reader_is_unchanged_by_the_section_generalisation(hook, config_dir) -> None:
    (config_dir / "config.yml").write_text(
        yaml.dump({"credentials": {"service_url": "https://x", "service_token": "t"}, "network": {"https_proxy": "http://p:1"}}),
        encoding="utf-8",
    )
    assert hook.read_config_yml_credentials(config_dir) == {"service_url": "https://x", "service_token": "t"}
    assert hook.read_config_yml_network(config_dir) == {"https_proxy": "http://p:1"}


# ── nx config set / get / unset ──────────────────────────────────────────────

class TestConfigCommand:
    def test_set_get_unset_round_trip(self, config_dir, pki) -> None:
        from click.testing import CliRunner

        from nexus.commands.config_cmd import config_group

        run = CliRunner().invoke
        assert run(config_group, ["set", "network.https_proxy=http://proxy.corp:3128"]).exit_code == 0
        assert run(config_group, ["set", f"network.ca_bundle={pki[0].pem}"]).exit_code == 0
        assert bt.network_settings() == bt.NetworkSettings("http://proxy.corp:3128", str(pki[0].pem.resolve()))
        assert run(config_group, ["get", "network.https_proxy"]).output.strip() == "http://proxy.corp:3128"
        assert run(config_group, ["unset", "network.https_proxy"]).exit_code == 0
        assert run(config_group, ["unset", "network.ca_bundle"]).exit_code == 0
        assert bt.network_settings() == bt.NetworkSettings("", "")
        assert "network" not in yaml.safe_load((config_dir / "config.yml").read_text(encoding="utf-8")), "an emptied section is removed"
        assert "not saved" in run(config_group, ["unset", "network.ca_bundle"]).output

    @pytest.mark.parametrize("pair", [
        "network.https_proxy=not a url", "network.https_proxy=ftp://proxy.corp", "network.ca_bundle=/no/such/file.pem",
        "network.nonsense=1", "network.https_proxy=",
    ])
    def test_a_bad_value_is_refused_and_nothing_is_written(self, config_dir, pair) -> None:
        from click.testing import CliRunner

        from nexus.commands.config_cmd import config_group

        result = CliRunner().invoke(config_group, ["set", pair])
        assert result.exit_code != 0, result.output
        assert bt.network_settings() == bt.NetworkSettings("", "")

    def test_get_shows_the_global_file_and_never_a_repository_file(self, config_dir, tmp_path, monkeypatch) -> None:
        from click.testing import CliRunner

        from nexus.commands.config_cmd import config_group

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".nexus.yml").write_text(yaml.safe_dump({"network": {"https_proxy": "http://evil.example:3128"}}), encoding="utf-8")
        monkeypatch.chdir(repo)
        monkeypatch.setenv("NEXUS_REPO_ROOT", str(repo))
        assert "not set" in CliRunner().invoke(config_group, ["get", "network.https_proxy"]).output
