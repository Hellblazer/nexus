# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o: the plugin's stdlib mirror of the credential-origin binding agrees with the CLI's.

``nexus.db.service_endpoint`` and ``conexus/hooks/scripts/_endpoint_resolve.py`` each carry the
pure functions (origin normaliser, equality, loopback test, candidate choice, the two messages).
The hook cannot import ``nexus``, so the text is copied; this test holds the two together twice:
the function bodies must be the same AST (docstrings aside), and one table of cases must give the
same answers from both. ``select_service_token`` is IO on each side and is run against the same
config directory and environment.
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from nexus.db import service_endpoint as cli

_SCRIPTS = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"

PURE = (
    "normalize_origin", "origins_equal", "is_loopback_host", "bound_origin_for_saved",
    "choose_bound_token", "mismatch_message", "insecure_message", "bearer_proxy_for", "no_redirect_handler", "host_port_url",
)

MANAGED = "https://api.conexus-nexus.com"


@pytest.fixture(scope="module")
def hook() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("_endpoint_resolve_origin_parity", _SCRIPTS / "_endpoint_resolve.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("NX_SERVICE_URL", "NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN", "NX_MINT_TOKEN"):
        monkeypatch.delenv(k, raising=False)


def _body_dump(fn) -> str:
    node = ast.parse(inspect.getsource(fn).lstrip()).body[0]
    assert isinstance(node, ast.FunctionDef)
    if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
        node.body = node.body[1:]  # the docstring
    return ast.dump(node)


@pytest.mark.parametrize("name", PURE)
def test_pure_function_bodies_are_the_same_ast(hook, name):
    assert _body_dump(getattr(cli, name)) == _body_dump(getattr(hook, name)), name


def test_constants_match(hook):
    assert hook.DEFAULT_MANAGED_SERVICE_URL == cli.DEFAULT_MANAGED_SERVICE_URL
    assert hook._DEFAULT_PORTS == cli._DEFAULT_PORTS


URLS = [
    "https://API.Example.com", "https://api.example.com:443/", "http://api.example.com:80/x?y#z",
    "https://api.example.com./", "http://[::1]:8080", "HTTP://[::1]", "http://localhost:9",
    "http://127.0.0.1:9", "  https://h  ", "http://evil.com\\@good.com", "http://user:pw@host",
    "http://ho st", "http://host:", "http://host:0", "http://host:65536", "http://host:١٢",
    "http://h%6fst", "http://hôst", "http://[::1", "http://[abc]:1", "http://a:b:c", "ftp://host",
    "host:80", "", "https://", "http://10.0.0.5:8080", "https://10.0.0.5",
]


@pytest.mark.parametrize("url", URLS)
def test_normalize_and_loopback_agree(hook, url):
    assert cli.normalize_origin(url) == hook.normalize_origin(url)
    origin = cli.normalize_origin(url)
    if origin is not None:
        assert cli.is_loopback_host(origin[1]) == hook.is_loopback_host(origin[1])


@pytest.mark.parametrize("a", URLS[:12])
@pytest.mark.parametrize("b", URLS[:12])
def test_origins_equal_agree(hook, a, b):
    assert cli.origins_equal(a, b) == hook.origins_equal(a, b)


@pytest.mark.parametrize(
    "saved", [None, "", "  ", "https://staging.example.com/", MANAGED],
)
def test_bound_origin_for_saved_agrees(hook, saved):
    assert cli.bound_origin_for_saved(saved) == hook.bound_origin_for_saved(saved)


CANDIDATES = [
    [],
    [("", "empty", None)],
    [("env", "NX_SERVICE_TOKEN", None), ("saved", "the saved service_token", MANAGED)],
    [("saved", "the saved service_token", MANAGED)],
    [("saved", "the saved service_token", "https://other.example.com"), ("lease", "lease", "http://127.0.0.1:1")],
    [("lease", "the supervisor lease token", "http://localhost:1")],
    [("a", "a", "https://x"), ("b", "b", "https://y")],
]


@pytest.mark.parametrize("url", ["https://api.conexus-nexus.com", "http://127.0.0.1:1", "http://127.0.0.1:2", "https://x:443"])
@pytest.mark.parametrize("candidates", CANDIDATES)
def test_choose_bound_token_agrees(hook, url, candidates):
    assert cli.choose_bound_token(url, candidates) == hook.choose_bound_token(url, candidates)


@pytest.mark.parametrize("refused", [["the saved service_token"], ["a", "b"]])
def test_messages_agree_and_never_carry_a_url(hook, refused):
    for label in ("NX_SERVICE_URL", "NX_SERVICE_HOST / NX_SERVICE_PORT", "the saved service_url"):
        assert cli.mismatch_message(label, refused) == hook.mismatch_message(label, refused)
        assert cli.insecure_message(label) == hook.insecure_message(label)
        assert "://" not in cli.mismatch_message(label, refused)
    assert "nx config set service_url" in cli.mismatch_message("NX_SERVICE_URL", refused)


def test_mismatch_message_names_each_source_once_without_a_doubled_article(hook):
    """The sources already start with "the": the builder adds no second one (a "the the" read)."""
    refused = ["the saved service_token", "the supervisor lease token"]
    for fn in (cli.mismatch_message, hook.mismatch_message):
        text = fn("NX_SERVICE_URL", refused)
        assert "the the" not in text
        assert "that the saved service_token and the supervisor lease token was not issued for" in text


@pytest.mark.parametrize("url", URLS)
@pytest.mark.parametrize("proxy", ["", "http://proxy.corp:3128"])
def test_bearer_proxy_for_agrees_and_is_https_only(hook, url, proxy):
    assert cli.bearer_proxy_for(url, proxy) == hook.bearer_proxy_for(url, proxy)
    origin = cli.normalize_origin(url)
    assert cli.bearer_proxy_for(url, proxy) == (proxy if proxy and origin is not None and origin[0] == "https" else None)


# ── the IO wrappers, against one config directory and one environment ────────

def _save(credentials: dict[str, str]) -> Path:
    d = Path(os.environ["NEXUS_CONFIG_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yml").write_text(yaml.safe_dump({"credentials": credentials}), encoding="utf-8")
    return d


def _outcome(call):
    try:
        return {"returned": call()}
    except Exception as exc:  # noqa: BLE001 — the text is what is compared (the classes differ by design)
        return {"raised": str(exc)}


SCENARIOS = [
    # (saved credentials, env, lease, url)
    ({"service_url": MANAGED, "service_token": "saved"}, {}, None, MANAGED),
    ({"service_url": MANAGED, "service_token": "saved"}, {}, None, "http://127.0.0.1:41999"),
    ({"service_token": "saved"}, {}, None, MANAGED),
    ({"service_token": "saved"}, {}, None, "https://other.example.com"),
    ({"service_url": MANAGED, "service_token": "saved"}, {"NX_SERVICE_TOKEN": "env"}, None, "http://127.0.0.1:41999"),
    ({}, {}, ("http://127.0.0.1:40123", "lease"), "http://127.0.0.1:40123"),
    ({}, {}, ("http://127.0.0.1:40123", "lease"), "http://127.0.0.1:40124"),
    ({}, {}, ("http://127.0.0.1:40123", "lease"), "http://localhost:40123"),
    ({"service_url": MANAGED, "mint_token": "mint"}, {}, None, MANAGED),
    ({"service_url": MANAGED, "mint_token": "mint"}, {}, None, "http://127.0.0.1:41999"),
    ({}, {"NX_SERVICE_TOKEN": "env"}, None, "http://10.0.0.5:8080"),
    ({}, {"NX_SERVICE_TOKEN": "env"}, None, "https://10.0.0.5"),
    ({}, {}, None, "http://127.0.0.1:41999"),
]


# The scenarios that end in a refusal: the storage-touch guard counts a refusal as an endpoint
# failure, so those (and only those) carry the marker.
_REFUSED = {1, 3, 6, 7, 9, 10}
_PARAMS = [
    pytest.param(*sc, marks=[pytest.mark.expects_unresolvable_endpoint] if i in _REFUSED else [])
    for i, sc in enumerate(SCENARIOS)
]


@pytest.mark.parametrize(("saved", "env", "lease", "url"), _PARAMS)
def test_select_service_token_agrees(hook, monkeypatch, saved, env, lease, url):
    config_dir = _save(saved)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    hook_lease = None
    if lease is not None:
        lease_url, lease_token = lease
        host_port = lease_url.removeprefix("http://")
        host, _, port = host_port.rpartition(":")
        hook_lease = {"host": host, "port": int(port), "token": lease_token}

    got_cli = _outcome(lambda: cli.select_service_token(url, lease=lambda: lease or (None, None)))
    got_hook = _outcome(
        lambda: hook.select_service_token(config_dir, url, lease=hook_lease, allow_mint=True)
    )
    assert got_hook == got_cli


HOST_PORT_CASES = [
    ("127.0.0.1", 8080), ("localhost", 9), ("::1", 8080), ("fe80::1", 1), ("[::1]", 80), ("example.com", "443"), ("2001:db8::7", 5),
]


@pytest.mark.parametrize(("host", "port"), HOST_PORT_CASES)
def test_host_port_url_brackets_an_ipv6_host_the_same_way_on_both_sides(hook, host, port):
    """The plugin's lease_origin / env URL and the CLI's agree on an IPv6 host (nexus-ec37o hardening)."""
    assert hook.host_port_url(host, port) == cli.host_port_url(host, port)
    url = cli.host_port_url(host, port)
    assert cli.normalize_origin(url) == hook.normalize_origin(url) is not None


def test_a_lease_origin_with_an_ipv6_host_is_a_url_the_normaliser_reads(hook):
    url = hook.lease_origin({"host": "::1", "port": 4321})
    assert url == "http://[::1]:4321" and hook.normalize_origin(url) == ("http", "::1", 4321)


@pytest.mark.parametrize("env", [{"NX_SERVICE_HOST": "::1", "NX_SERVICE_PORT": "4321"}])
def test_the_cli_env_url_brackets_an_ipv6_host(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert cli.env_host_port_url() == "http://[::1]:4321"


def test_an_idn_host_is_refused_by_both_copies_and_its_punycode_form_is_read(hook):
    """Non-ASCII hosts are unsupported on purpose (two stacks can map one IDN to two hosts): a saved
    service_url must carry the ``xn--`` form, which both copies read alike."""
    assert cli.normalize_origin("https://b\u00fccher.example") is None and hook.normalize_origin("https://b\u00fccher.example") is None
    puny = "https://xn--bcher-kva.example"
    assert cli.normalize_origin(puny) == hook.normalize_origin(puny) == ("https", "xn--bcher-kva.example", 443)
