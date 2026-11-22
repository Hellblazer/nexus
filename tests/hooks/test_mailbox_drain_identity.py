# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Step 5: the drain hooks send the identity token on every request made while draining ``mailbox/<id>``.

The engine fences ``in`` by the request's subspace and ``ack``/``nack``/``renew``/``release`` by the claim's
(``IdentityFence.mailboxClaimById``): an ack body names only a claim id. So the token is chosen per drained address.
These drive both copies of the hook (the plugin script and the wheel port) with a fake opener that records headers.
"""
from __future__ import annotations

import importlib.util
import io
import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from tests._module_seam import setattr_in

SCRIPT = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "mailbox_drain.py"
AGENT = "a0123456789abcdef"


def _plugin() -> Any:
    spec = importlib.util.spec_from_file_location("_plugin_mailbox_drain_identity", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _wheel() -> Any:
    import nexus.hooks.mailbox_drain as mod  # noqa: PLC0415

    return mod


@pytest.fixture(params=["plugin", "wheel"])
def hook(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    mod = _plugin() if request.param == "plugin" else _wheel()
    sent: list[tuple[str, dict[str, str]]] = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class _Opener:
        def open(self, req: urllib.request.Request, timeout: float = 0):
            sent.append((req.full_url, {k.lower(): v for k, v in req.header_items()}))
            return _Resp(json.dumps({"ok": True}).encode())

    setattr_in(monkeypatch, mod, "urllib.request.build_opener", lambda *_h: _Opener())  # local to the hook module
    monkeypatch.setattr(mod, "_IDENTITY_TOKEN", "tok-i12")
    return mod, sent


def _ack_while_draining(mod: Any, address: str) -> None:
    def inner(base_url: str, token: str, addr: str, **_kw: Any) -> str:
        mod._post(base_url, token, "/v1/tuples/ack", {"claim_id": "c1", "claimant": "x"}, is_local=True)
        return "empty"

    original = mod._drain_address_inner
    mod._drain_address_inner = inner
    try:
        mod._drain_address("http://127.0.0.1:1", "bearer", address, is_local=True)
    finally:
        mod._drain_address_inner = original


def test_an_ack_while_draining_the_identity_mailbox_carries_the_token(hook) -> None:
    mod, sent = hook
    _ack_while_draining(mod, "i12")
    (_url, headers), = sent
    assert headers.get("x-nexus-identity-token") == "tok-i12", "the ack body has no subspace; the address decides"


def test_no_request_outside_the_identity_drain_carries_the_token(hook) -> None:
    mod, sent = hook
    _ack_while_draining(mod, AGENT)
    mod._post("http://127.0.0.1:1", "bearer", "/v1/tuples/in", {"subspace": "mailbox/i12"}, is_local=True)
    assert all("x-nexus-identity-token" not in h for _u, h in sent), "outside the identity drain, never sent"
    assert mod._DRAIN_ADDRESS == "", "the drained address is cleared after the pass"
