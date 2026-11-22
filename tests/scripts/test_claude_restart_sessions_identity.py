# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/claude-restart-sessions: an ask is mailed to the target's identity mailbox, and a target with no identity
is not mailed (RDR-253 Phase 2, nexus-hczdo.2.1).

An 8.6 session drains only ``mailbox/<its identity id>``, so an ask addressed to its session id was never seen. The
script finds the identity with `nx identity show --session <sid> --json` (once per session id) and sends
``address_kind=identity``; a session that holds none is not mailed, and each caller falls back (the handoff loop types
its ask, the ready round types or records "not asked", the wake is typed).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests._module_seam import setattr_in
from tests._restart_fake_clock import fake_clock
from tests._restart_identity import identity_stub

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude-restart-sessions"


@pytest.fixture
def crs(monkeypatch, tmp_path):
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions_identity", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module, "LOG", tmp_path / "restart.log")
    monkeypatch.setattr(module, "IDENTITIES", {})
    for name, empty in (("SENT_ASKS", {}), ("SENT_MARKERS", {}), ("MADE_MARKERS", set()), ("UNWITHDRAWN", [])):
        monkeypatch.setattr(module, name, empty)
    monkeypatch.setattr(module, "ASK_MARKERS", tmp_path / "restart-asks")
    monkeypatch.setattr(module, "_nx_identity", identity_stub())
    return module


def test_the_target_s_identity_is_asked_once_per_session(crs, monkeypatch, tmp_path) -> None:
    asked: list = []
    monkeypatch.setattr(crs, "_nx_identity", identity_stub(calls=asked))
    sent: list[list[str]] = []
    setattr_in(monkeypatch, crs, "subprocess.run",
               lambda argv, **_kw: sent.append(list(argv)) or subprocess.CompletedProcess(argv, 0, "t\n", ""))
    s = {"sessionId": "sid-9", "name": "tamsin"}
    assert crs.mail_ask(s, "/tmp/a.md", "x") and crs.mail_ask(s, "/tmp/b.md", "x", kind="restart-ready")
    assert asked == [("show", "--session", "sid-9", "--json")]
    assert [a[3] for a in sent] == ["mailbox/i9", "mailbox/i9"]


def test_an_unbound_target_is_not_mailed(crs, monkeypatch, tmp_path) -> None:
    """A session with no identity drains no mailbox an 8.6 script could address: no marker, no send, False."""
    monkeypatch.setattr(crs, "_nx_identity", identity_stub(unbound={"sid-9"}))
    setattr_in(monkeypatch, crs, "subprocess.run", lambda argv, **_kw: pytest.fail(f"sent {argv}"))
    s = {"sessionId": "sid-9", "name": "tamsin"}
    assert crs.mail_ask(s, "/tmp/h.md", "x") is False
    assert not crs.ask_marker(s, "/tmp/h.md").exists() and crs.MADE_MARKERS == set() and crs.SENT_ASKS == {}
    assert "not mailed: the session holds no identity (unknown_session)" in crs.LOG.read_text()


@pytest.mark.parametrize(("rc", "out", "err"), [
    (0, "not json", ""), (0, json.dumps({"kind": "seat"}), ""), (0, json.dumps({"id": "../x"}), ""), (3, "", "engine down")],
    ids=["not-json", "no-id", "malformed-id", "nx-error"])
def test_an_identity_that_cannot_be_read_is_not_mailed(crs, monkeypatch, tmp_path, rc, out, err) -> None:
    monkeypatch.setattr(crs, "_nx_identity", lambda *a, **k: subprocess.CompletedProcess(a, rc, out, err))
    setattr_in(monkeypatch, crs, "subprocess.run", lambda argv, **_kw: pytest.fail(f"sent {argv}"))
    assert crs.mail_ask({"sessionId": "sid-9", "name": "t"}, "/tmp/h.md", "x") is False
    assert "not mailed:" in crs.LOG.read_text()


def test_the_handoff_ask_of_an_unbound_target_is_typed(crs, monkeypatch, tmp_path) -> None:
    """mail_ask's False sends the handoff loop to its typed fallback; nothing is mailed or left to withdraw."""
    monkeypatch.setattr(crs, "_nx_identity", identity_stub(unbound={"sid-1"}))
    setattr_in(monkeypatch, crs, "subprocess.run", lambda argv, **_kw: pytest.fail(f"sent {argv}"))
    s = {"sessionId": "sid-1", "name": "t", "pid": os.getpid(), "pane": "%9", "file": tmp_path / "s.json"}
    monkeypatch.setattr(crs, "handoff_path", lambda _s, _stamp: str(tmp_path / "h.md"))
    typed: list[str] = []
    monkeypatch.setattr(crs, "typed_ask", lambda _s, p: typed.append(p) or False)
    monkeypatch.setattr(crs, "has_channel", lambda _s: True)
    monkeypatch.setattr(crs, "prompt_count", lambda _sid: 1)
    monkeypatch.setattr(crs, "status", lambda _s: "idle")
    fake_clock(crs, monkeypatch)
    assert crs._write_handoffs([s], {"idle"}, 60, 10_000, "x") == {}
    assert typed == [str(tmp_path / "h.md")] and crs.SENT_ASKS == {}
