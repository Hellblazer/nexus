# SPDX-License-Identifier: AGPL-3.0-or-later
"""The telemetry endpoint the plugin pins and what ``nx settings show`` reports about it (nexus-sby49.14, RDR-259).

``_endpoint_resolve.pinned_endpoint(config_dir)`` decides where the telemetry senders may post and with which bearer;
``settings_cmd.telemetry_endpoint_missing(config_dir)`` says whether that decision found no engine. They must agree on every
shape of the account's configuration, including the ones the origin binding of nexus-ec37o refuses: a bearer is never sent over
plain http off this machine, and a supervisor lease token goes only to the lease's own endpoint. Each case builds one
configuration directory, asks both, and holds them to one another and to the stated answer.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from nexus._winsec import restrict_to_owner
from nexus.commands import settings_cmd
from tests.db.test_data_token_origin_binding import _write_lease_file
from tests.hooks.test_mailbox_drain_hook import _write_storage_lease

_SCRIPTS = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
_LEASE_TOKEN = "local-supervisor-token"  # what _write_storage_lease puts in the lease
_LEASE_PORT = 4242


@pytest.fixture(scope="module")
def ep() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("_endpoint_resolve_pinned_parity", _SCRIPTS / "_endpoint_resolve.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _no_endpoint_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NX_SERVICE_URL", "NX_SERVICE_HOST", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN", "NX_MINT_TOKEN", "NX_MINT_TENANT"):
        monkeypatch.delenv(name, raising=False)


def _build(cfg: Path, *, url: str | None, token: str | None, lease: bool, data_lease: str | None = None) -> None:
    cfg.mkdir(parents=True, exist_ok=True)
    lines = ["credentials:"]
    if url is not None:
        lines.append(f"  service_url: {url}")
    if token is not None:
        lines.append(f"  service_token: {token}")
    path = cfg / "config.yml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    restrict_to_owner(path)
    if lease:
        _write_storage_lease(cfg, host="127.0.0.1", port=_LEASE_PORT)
    if data_lease is not None:
        _write_lease_file(cfg, url or "", scheme=data_lease, tenant="default")


#: name -> (saved url, saved token, live supervisor lease, data-token lease scheme, the pinned endpoint's url or None)
_CASES = {
    "saved https url with a saved token": ("https://engine.example", "tok", False, None, "https://engine.example"),
    "saved loopback http url with a saved token": ("http://127.0.0.1:9999", "tok", False, None, "http://127.0.0.1:9999"),
    "saved cleartext url off this machine with a saved token": ("http://evil.example:80", "tok", False, None, None),
    "saved cleartext url off this machine with the lease also live": ("http://evil.example", "tok", True, None, None),
    "saved https url with no token and no lease": ("https://engine.example", None, False, None, None),
    "saved https url with no token and a lease for another origin": ("https://other.example", None, True, None, None),
    "saved url that is the lease's own origin, no saved token": (f"http://127.0.0.1:{_LEASE_PORT}", None, True, None, f"http://127.0.0.1:{_LEASE_PORT}"),
    "no saved url, a live lease": (None, None, True, None, f"http://127.0.0.1:{_LEASE_PORT}"),
    "no saved url, nothing else": (None, None, False, None, None),
    "saved url with userinfo": ("https://user:pw@engine.example", "tok", False, None, None),
    "saved https url with a data-token lease and no saved token": ("https://engine.example", None, False, "https", "https://engine.example"),
    "saved cleartext url off this machine with a data-token lease only": ("http://192.0.2.1:9", None, False, "http", None),
}


def _answers(ep: ModuleType, cfg: Path) -> tuple[tuple[str | None, str | None], bool]:
    return ep.pinned_endpoint(cfg), settings_cmd.telemetry_endpoint_missing(cfg)


@pytest.mark.parametrize("name", sorted(_CASES))
def test_settings_agree_with_the_pinned_endpoint_on_every_shape(ep, tmp_path, name) -> None:
    url, token, lease, data_lease, expected_url = _CASES[name]
    cfg = tmp_path / "cfg"
    _build(cfg, url=url, token=token, lease=lease, data_lease=data_lease)
    (pinned_url, pinned_token), missing = _answers(ep, cfg)
    assert pinned_url == expected_url, (name, pinned_url)
    assert (pinned_token is None) == (pinned_url is None), "a url and its bearer come together or not at all"
    assert missing is (expected_url is None), (name, "nx settings show says", missing)


def test_the_cases_cover_both_answers() -> None:
    answers = {c[4] is None for c in _CASES.values()}
    assert answers == {True, False}, "non-vacuity: some shapes find an engine and some refuse"
    assert len(_CASES) >= 12


def test_the_bearer_a_pinned_endpoint_carries_is_the_right_one(ep, tmp_path) -> None:
    cfg = tmp_path / "cfg"
    _build(cfg, url="https://engine.example", token="saved-tok", lease=True)
    assert ep.pinned_endpoint(cfg) == ("https://engine.example", "saved-tok")
    own = tmp_path / "own"
    _build(own, url=f"http://127.0.0.1:{_LEASE_PORT}", token=None, lease=True)
    assert ep.pinned_endpoint(own) == (f"http://127.0.0.1:{_LEASE_PORT}", _LEASE_TOKEN)


@pytest.mark.parametrize("name", [
    "saved cleartext url off this machine with a saved token",
    "saved cleartext url off this machine with the lease also live",
    "saved cleartext url off this machine with a data-token lease only",
])
def test_the_cleartext_rule_is_what_refuses_those_shapes(ep, tmp_path, monkeypatch, name) -> None:
    """Without the rule the same shapes find an engine: the cases above are the rule's test, not a coincidence."""
    url, token, lease, data_lease, _ = _CASES[name]
    cfg = tmp_path / "cfg"
    _build(cfg, url=url, token=token, lease=lease, data_lease=data_lease)
    assert ep.pinned_endpoint(cfg) == (None, None)
    monkeypatch.setattr(ep, "_may_carry_bearer", lambda _url: True)
    assert ep.pinned_endpoint(cfg)[0] is not None, f"{name}: no other check refuses it, so the rule is untested if this fails"


def test_an_expired_lease_whose_owner_is_alive_and_answering_is_no_engine_for_both_and_is_not_reaped(ep, tmp_path) -> None:
    """The plugin reads a lease by plain TTL: no grace for a live owner, no ``/health`` probe, no reap. The wheel's mirror
    must too; a registry ``discover`` would probe the answering port, revive the lease and say an engine exists."""
    import http.server
    import json
    import os
    import threading
    import time

    from nexus.daemon.service_registry import service_identity

    class _Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps({"status": "ok", "db": "up"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cfg = tmp_path / "cfg"
        _build(cfg, url=None, token=None, lease=False)
        record = {
            "scope_key": service_identity(), "generation": 1, "owner_token": "o", "heartbeat_epoch": time.time() - 120.0,
            "ttl": 30.0, "endpoint": {"host": "127.0.0.1", "port": server.server_address[1], "token": "tok"},
            "version": "t", "payload": {"supervisor_pid": os.getpid()}, "status": "live", "format_version": 1,
        }
        path = cfg / f"storage_service_addr.{service_identity()}"
        path.write_text(json.dumps(record), encoding="utf-8")
        restrict_to_owner(path)
        assert _answers(ep, cfg) == ((None, None), True)
        assert path.exists(), "reading the lease for a report must not reap it"
    finally:
        server.shutdown()
        server.server_close()
