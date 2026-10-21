# SPDX-License-Identifier: AGPL-3.0-or-later
"""The plugin's stdlib mirrors refuse a ``service_url`` carrying userinfo (nexus-ujbf8).

The CLI refuses ``user:password@`` in ``service_url`` at set and at read
(``tests/test_service_url_userinfo.py``). The hook scripts under ``conexus/``
cannot import ``nexus``, so they carry their own copy of the predicate and must
refuse the same URLs, with a reason that names the SOURCE and never the URL.
Every test asserts that the secret appears in no message, log line or
uncaught-thread report.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from nexus._log_safe_url import has_userinfo as cli_has_userinfo
from tests._module_seam import setattr_in

PW = "SECRETPW-9f3a"
BAD = f"https://user:{PW}@api.example.test"
GOOD = "https://api.example.test"

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "conexus" / "hooks" / "scripts"

#: Corpus the stdlib copy and the CLI predicate must agree on.
CORPUS = [
    BAD, f"https://user:{PW}#x@api.example.test", f"https://user:{PW}?x@api.example.test",
    f"https://user:{PW}\\x@api.example.test", "http://tok@host:80", f"https://:{PW}@h", "https://u@h/x",
    "tok@host:80", GOOD, "http://127.0.0.1:9999", "https://h.example/path@x", "https://h.example?q=a@b",
    "", "/", "https://", "host:80",
]


def _load(path: Path, name: str) -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ep() -> ModuleType:
    return _load(_SCRIPTS / "_endpoint_resolve.py", "_endpoint_resolve_userinfo")


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    for key in (
        "NX_SERVICE_URL", "NX_SERVICE_TOKEN", "NX_MINT_TOKEN", "NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_CONFIG_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


def _save_raw(config_dir: Path, **credentials: str) -> None:
    """Write ``config.yml`` directly: ``set_credential`` now refuses userinfo,
    and a hand-edited file is exactly the case under test."""
    body = "credentials:\n" + "".join(f"  {k}: {v}\n" for k, v in credentials.items())
    (config_dir / "config.yml").write_text(body, encoding="utf-8")


# -- the predicate ---------------------------------------------------------------------------------


@pytest.mark.parametrize("url", CORPUS)
def test_stdlib_predicate_agrees_with_the_cli(ep: ModuleType, url: str) -> None:
    assert ep.has_userinfo(url) is cli_has_userinfo(url)


# -- the resolvers ---------------------------------------------------------------------------------


def test_resolve_base_url_refuses_env_userinfo_naming_the_source(ep: ModuleType, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NX_SERVICE_URL", BAD)
    with pytest.raises(ep.EndpointUnresolvable) as ei:
        ep.resolve_base_url(tmp_path)
    assert PW not in str(ei.value) and "NX_SERVICE_URL" in str(ei.value)


def test_resolve_base_url_refuses_saved_userinfo_naming_the_source(ep: ModuleType, tmp_path: Path) -> None:
    _save_raw(tmp_path, service_url=BAD)
    with pytest.raises(ep.EndpointUnresolvable) as ei:
        ep.resolve_base_url(tmp_path)
    assert PW not in str(ei.value) and "service_url" in str(ei.value)


def test_resolve_base_url_still_resolves_a_clean_url(ep: ModuleType, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NX_SERVICE_URL", GOOD + "/")
    assert ep.resolve_base_url(tmp_path) == (GOOD, False)


def test_endpoint_usable_is_false_for_env_userinfo_even_with_a_token(ep: ModuleType, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NX_SERVICE_URL", BAD)
    monkeypatch.setenv("NX_SERVICE_TOKEN", "t")
    assert ep.endpoint_usable(tmp_path) is False


def test_endpoint_usable_is_false_for_saved_userinfo_even_with_a_token(ep: ModuleType, tmp_path: Path) -> None:
    _save_raw(tmp_path, service_url=BAD, service_token="t")
    assert ep.endpoint_usable(tmp_path) is False


def test_routing_engine_endpoint_is_unresolved_for_userinfo(tmp_path: Path, monkeypatch) -> None:
    lib = _load(_SCRIPTS / "routing" / "_lib.py", "nx_routing_lib_userinfo")
    monkeypatch.setenv("NX_SERVICE_URL", BAD)
    monkeypatch.setenv("NX_SERVICE_TOKEN", "t")
    assert lib._engine_endpoint() == (None, None)
    monkeypatch.setenv("NX_SERVICE_URL", GOOD)
    assert lib._engine_endpoint() == (GOOD, "t")


# -- the mailbox drain hook: the plugin copy and the wheel copy ------------------------------------


@pytest.fixture(params=["plugin", "wheel"])
def drain(request: pytest.FixtureRequest) -> ModuleType:
    if request.param == "plugin":
        return _load(_SCRIPTS / "mailbox_drain.py", "mailbox_drain_userinfo_plugin")
    return importlib.import_module("nexus.hooks.mailbox_drain")


def test_post_refuses_a_userinfo_base_url_before_any_request(drain: ModuleType, monkeypatch) -> None:
    def boom(*a, **k):
        raise AssertionError("a request was built for a userinfo URL")

    setattr_in(monkeypatch, drain, "urllib.request.build_opener", boom)
    with pytest.raises(drain._Skip) as ei:
        drain._post(BAD, "tok", "/v1/tuples/rd", {}, is_local=False)
    assert PW not in str(ei.value)


def test_post_turns_an_invalid_url_error_into_a_skip_not_a_dead_thread(drain: ModuleType, monkeypatch) -> None:
    """``http.client.InvalidURL`` is not an OSError or a ValueError: it used to
    escape the worker thread, so ``_post`` returned None, which callers read as
    a confirmed negative (it clears the pending-ack record)."""
    with pytest.raises(drain._Skip) as ei:
        drain._post("http://127.0.0.1:notaport", "tok", "/v1/tuples/ack", {}, is_local=True)
    assert "InvalidURL" in str(ei.value)


def test_post_transport_error_text_does_not_echo_a_userinfo_url(drain: ModuleType, monkeypatch) -> None:
    import urllib.error

    class Opener:
        def open(self, *a, **k):
            raise urllib.error.URLError(f"redirect to {BAD}/x refused")

    setattr_in(monkeypatch, drain, "urllib.request.build_opener", lambda *a: Opener())
    with pytest.raises(drain._Skip) as ei:
        drain._post(GOOD, "tok", "/v1/tuples/rd", {}, is_local=False)
    assert PW not in str(ei.value) and "user:" not in str(ei.value)
    assert "URLError" in str(ei.value)


def test_post_transport_error_text_keeps_a_harmless_reason(drain: ModuleType, monkeypatch) -> None:
    import urllib.error

    class Opener:
        def open(self, *a, **k):
            raise urllib.error.URLError("connection refused")

    setattr_in(monkeypatch, drain, "urllib.request.build_opener", lambda *a: Opener())
    with pytest.raises(drain._Skip) as ei:
        drain._post(GOOD, "tok", "/v1/tuples/rd", {}, is_local=False)
    assert "connection refused" in str(ei.value)


def test_resolve_endpoint_no_credential_skip_names_host_not_userinfo(monkeypatch, tmp_path: Path) -> None:
    drain = _load(_SCRIPTS / "mailbox_drain.py", "mailbox_drain_userinfo_skip")
    monkeypatch.setattr(drain._ep, "resolve_base_url", lambda config_dir: (f"https://user:{PW}@h.example:8443/x", False))
    monkeypatch.setattr(drain._ep, "read_data_token_lease", lambda config_dir, base_url: None)
    with pytest.raises(drain._Skip) as ei:
        drain._resolve_endpoint(tmp_path)
    assert PW not in str(ei.value) and "user:" not in str(ei.value)
    assert "h.example:8443" in str(ei.value)


def test_resolve_endpoint_skips_for_userinfo_naming_the_source(monkeypatch, tmp_path: Path) -> None:
    drain = _load(_SCRIPTS / "mailbox_drain.py", "mailbox_drain_userinfo_resolve")
    monkeypatch.setenv("NX_SERVICE_URL", BAD)
    with pytest.raises(drain._Skip) as ei:
        drain._resolve_endpoint(tmp_path)
    assert PW not in str(ei.value) and "NX_SERVICE_URL" in str(ei.value)

