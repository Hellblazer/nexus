# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/claude-restart-sessions: ``--group <name>`` on ``--ask-ready`` and ``--status`` (RDR-252 critique S1,
nexus-igea9; the RDR's "the restart script's ready check becomes a group operation").

The round asks and reports only the sessions named by the group's roster (``nx group list <name> --json``, the member
names, matched to sessions by name). It never restarts anything. Nothing here reaches a tmux server, the real
``~/.claude``, a real ``nx`` or a real ``claude``: sessions are files in a scratch sessions directory, the server check
is faked, the ask is a recorded call, and ``nx`` is either a recorded function or a stand-in executable.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._module_seam import setattr_in
from tests._restart_fake_clock import fake_clock

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude-restart-sessions"
FAKE_SERVER = ("/tmp/fake-tmux/default", 1)
READY = {"ready": True, "reason": "idle", "eta_min": None}


class Env:
    """The scratch world one test runs in: the script module, its fake clock, and what it did."""

    def __init__(self, crs, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.crs, self.root, self.monkeypatch = crs, root, monkeypatch
        self.mailed: list[str] = []
        self.nx_calls: list[tuple[str, ...]] = []
        self.restarted: list[str] = []
        self.tmux: list[list[str]] = []
        self.groups: dict[str, subprocess.CompletedProcess] = {}

    def live(self, name: str, status: str = "idle") -> None:
        (self.root / "sessions" / f"{name}.json").write_text(json.dumps(
            {"kind": "interactive", "tmux": "rs:@1.%5", "pid": os.getpid(), "sessionId": f"sid-{name}", "name": name,
             "cwd": "/w", "status": status}), encoding="utf-8")

    def group(self, name: str, members: list[str | None], **record: object) -> None:
        body = {"group": {"name": name, "live": True, **record},
                "members": [{"identity": f"i{n}", "name": m, "role": "member", "state": "live"}
                            for n, m in enumerate(members, 1)]}
        self.groups[name] = subprocess.CompletedProcess([], 0, json.dumps(body), "")

    def run(self, *argv: str) -> int:
        self.monkeypatch.setattr(sys, "argv", ["claude-restart-sessions", *argv])
        return self.crs.main()

    def answer(self, *names: str) -> None:
        """Each named session writes a ready reply to the file the last round named for it."""
        manifest = json.loads((self.root / "restart-ready.json").read_text(encoding="utf-8"))
        for t in manifest["targets"]:
            if t["name"] in names:
                Path(t["path"]).write_text(json.dumps(READY), encoding="utf-8")

    def manifest(self) -> dict:
        return json.loads((self.root / "restart-ready.json").read_text(encoding="utf-8"))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions_group", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    crs = importlib.util.module_from_spec(spec)
    loader.exec_module(crs)
    (tmp_path / "sessions").mkdir()
    (tmp_path / "ask").mkdir()
    for attr, value in {
        "SESSIONS": tmp_path / "sessions", "LOG": tmp_path / "restart.log", "READY_MANIFEST": tmp_path / "restart-ready.json",
        "LAST_RUN": tmp_path / "restart-last.json", "ASK_DIR": tmp_path / "ask", "PLAN_DIR": tmp_path / "plans",
        "ASK_MARKERS": tmp_path / "restart-asks", "NO_SLASH": True, "KEEP_DEV_CHANNEL": False,
        "SENT_ASKS": {}, "SENT_MARKERS": {}, "MADE_MARKERS": set(), "UNWITHDRAWN": [],
    }.items():
        monkeypatch.setattr(crs, attr, value)
    monkeypatch.setenv("NX_RESTART_PRUNE_DIR", str(tmp_path / "prune"))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    fake_clock(crs, monkeypatch)
    e = Env(crs, tmp_path, monkeypatch)

    def nx_group(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
        e.nx_calls.append(args)
        name = args[1] if len(args) > 1 else ""
        return e.groups.get(name) or subprocess.CompletedProcess([], 1, "", f"Error: no group named {name!r}")

    monkeypatch.setattr(crs, "_nx_group", nx_group)
    # The reply files a round names default to /tmp: keep them in the scratch dir.
    monkeypatch.setattr(crs, "ready_path", lambda s, stamp: str(tmp_path / f"ready-{s['name']}-{stamp}.json"))
    monkeypatch.setattr(crs, "check_server", lambda found, quiet=False: (FAKE_SERVER, list(found), ""))
    monkeypatch.setattr(crs, "session_tasks", lambda _s: [])
    monkeypatch.setattr(crs, "session_work", lambda _s: [])
    monkeypatch.setattr(crs, "ensure_seat", lambda _s: "")
    monkeypatch.setattr(crs, "has_channel", lambda _s: True)
    monkeypatch.setattr(crs, "mail_ask", lambda s, *_a, **_k: e.mailed.append(s["name"]) or True)
    # Anything that would restart a session or reach a tmux server is a recorded failure, never a call.
    monkeypatch.setattr(crs, "restart", lambda s, *_a, **_k: e.restarted.append(s["name"]) or False)
    monkeypatch.setattr(crs, "tmux", lambda *a: e.tmux.append(["tmux", *a]))
    monkeypatch.setattr(crs, "tmux_run", lambda *a: e.tmux.append(["tmux", *a]) or subprocess.CompletedProcess(a, 0, "", ""))
    setattr_in(monkeypatch, crs, "signal.signal", lambda *_a: None)
    return e


# ── --ask-ready --group ──────────────────────────────────────────────────────────────────


def test_ask_ready_with_a_group_asks_only_its_live_members(env: Env, capsys) -> None:
    for n in ("alpha", "beta", "gamma", "delta"):
        env.live(n)
    env.group("push-train", ["alpha", "gamma", "offline"])
    assert env.run("--ask-ready", "--group", "push-train") == 0
    assert sorted(env.mailed) == ["alpha", "gamma"]
    assert sorted(t["name"] for t in env.manifest()["targets"]) == ["alpha", "gamma"]
    assert ("list", "push-train", "--json") == env.nx_calls[0], "the roster is read with `nx group list <name> --json`"
    assert env.restarted == [] and env.tmux == []


def test_a_group_round_does_not_list_the_sessions_outside_the_group(env: Env) -> None:
    env.live("alpha")
    env.live("beta")
    (env.root / "sessions" / "desk.json").write_text(json.dumps(
        {"kind": "interactive", "pid": os.getpid(), "sessionId": "sid-desk", "name": "desk", "cwd": "/w",
         "status": "idle", "entrypoint": "claude-desktop"}), encoding="utf-8")
    env.group("g", ["alpha", "desk"])
    assert env.run("--ask-ready", "--group", "g") == 0
    excluded = {e["name"] for e in env.manifest()["excluded"]}
    assert excluded == {"desk"}, "a group member that cannot be asked is listed; sessions outside the group are not"


def test_without_a_group_ask_ready_asks_every_session_as_before(env: Env) -> None:
    for n in ("alpha", "beta"):
        env.live(n)
    assert env.run("--ask-ready") == 0
    assert sorted(env.mailed) == ["alpha", "beta"] and env.nx_calls == []


# ── --status --group ─────────────────────────────────────────────────────────────────────


def test_status_with_a_group_reports_only_its_members_and_is_go_on_theirs(env: Env, capsys) -> None:
    for n in ("alpha", "beta", "gamma"):
        env.live(n)
    assert env.run("--ask-ready") == 0  # a full round
    env.answer("alpha", "beta")  # gamma never answers
    capsys.readouterr()
    env.group("pair", ["alpha", "beta"])
    assert env.run("--status", "--group", "pair") == 0, "gamma is not a member, so its silence blocks nothing here"
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out and "gamma" not in out
    assert "not live now" not in out
    # and the table without a group still reports gamma as the blocker
    assert env.run("--status") == 1
    assert "gamma" in capsys.readouterr().out


def test_status_with_a_group_blocks_on_a_member_that_has_not_answered(env: Env, capsys) -> None:
    for n in ("alpha", "beta"):
        env.live(n)
    env.group("pair", ["alpha", "beta"])
    assert env.run("--ask-ready", "--group", "pair") == 0
    env.answer("alpha")
    capsys.readouterr()
    assert env.run("--status", "--group", "pair") == 1
    out = capsys.readouterr().out
    rows = {ln.split()[0]: ln for ln in out.splitlines() if ln.split() and ln.split()[0] in ("alpha", "beta")}
    assert "ready" in rows["alpha"] and "no reply" in rows["beta"]


def test_status_with_a_group_keeps_the_other_sessions_in_the_round_file(env: Env) -> None:
    """--status writes the round file back when it withdraws an answered ask; a group view must not drop the rest."""
    for n in ("alpha", "beta", "gamma"):
        env.live(n)
    assert env.run("--ask-ready") == 0
    env.answer("alpha", "beta", "gamma")
    env.group("pair", ["alpha", "beta"])
    assert env.run("--status", "--group", "pair") == 0
    assert sorted(t["name"] for t in env.manifest()["targets"]) == ["alpha", "beta", "gamma"]


def test_status_with_a_group_names_a_member_that_left_the_round(env: Env, capsys) -> None:
    env.live("alpha")
    env.live("beta")
    env.group("pair", ["alpha", "beta"])
    assert env.run("--ask-ready", "--group", "pair") == 0
    env.answer("alpha", "beta")
    (env.root / "sessions" / "beta.json").unlink()
    capsys.readouterr()
    env.run("--status", "--group", "pair")
    assert "in the round but not live now: beta" in capsys.readouterr().out


# ── refusals: one line, nothing asked, nothing written ───────────────────────────────────


def _refused(env: Env, capsys, *argv: str) -> str:
    env.live("alpha")
    rc = env.run(*argv)
    out = capsys.readouterr().out
    assert rc != 0
    assert len(out.strip().splitlines()) == 1, out
    assert env.mailed == [] and env.restarted == [] and env.tmux == []
    assert not (env.root / "restart-ready.json").exists(), "a refused group round writes no round"
    return out


@pytest.mark.parametrize("flag", ["--ask-ready", "--status"])
def test_an_unknown_group_refuses_in_one_line(env: Env, capsys, flag: str) -> None:
    out = _refused(env, capsys, flag, "--group", "nope")
    assert "nope" in out


@pytest.mark.parametrize("flag", ["--ask-ready", "--status"])
def test_an_empty_group_refuses_in_one_line(env: Env, capsys, flag: str) -> None:
    env.group("empty", [])
    assert "empty" in _refused(env, capsys, flag, "--group", "empty")


def test_a_disbanded_group_refuses_in_one_line(env: Env, capsys) -> None:
    env.group("old", ["alpha"], live=False, ended_at="2026-11-01T00:00:00Z")
    assert "disbanded" in _refused(env, capsys, "--ask-ready", "--group", "old")


def test_a_group_none_of_whose_members_is_a_live_session_here_refuses_in_one_line(env: Env, capsys) -> None:
    env.group("away", ["omega", "sigma"])
    out = _refused(env, capsys, "--ask-ready", "--group", "away")
    assert "away" in out and "omega" in out


def test_an_nx_that_cannot_run_refuses_in_one_line(env: Env, capsys) -> None:
    def boom(*_a: str, **_k: object):
        raise OSError("no nx")

    env.monkeypatch.setattr(env.crs, "_nx_group", boom)
    assert "cannot read" in _refused(env, capsys, "--ask-ready", "--group", "any")


def test_a_roster_that_is_not_json_refuses_in_one_line(env: Env, capsys) -> None:
    env.groups["junk"] = subprocess.CompletedProcess([], 0, "not json", "")
    assert "junk" in _refused(env, capsys, "--ask-ready", "--group", "junk")


def test_group_goes_with_ask_ready_or_status_only(env: Env, capsys) -> None:
    env.live("alpha")
    env.group("g", ["alpha"])
    for argv in (["--group", "g"], ["--group", "g", "--dry-run"], ["--group", "g", "--nx", "1.2.3"], ["--revive", "--group", "g"]):
        with pytest.raises(SystemExit) as stop:
            env.run(*argv)
        assert stop.value.code == 2
        assert "--group goes with --ask-ready or --status" in capsys.readouterr().err, argv
    assert env.mailed == [] and env.restarted == [] and env.tmux == []


def test_a_group_round_restarts_nothing(env: Env) -> None:
    for n in ("alpha", "beta"):
        env.live(n)
    env.group("pair", ["alpha", "beta"])
    assert env.run("--ask-ready", "--group", "pair") == 0
    env.answer("alpha", "beta")
    assert env.run("--status", "--group", "pair") == 0
    assert env.restarted == [] and env.tmux == []
    assert not (env.root / "restart-last.json").exists(), "no restart run was recorded"


def test_a_restart_after_a_group_round_still_wants_every_session_asked(env: Env) -> None:
    """The group round is not a round for the whole fleet: the restart's gate names the sessions it never asked."""
    for n in ("alpha", "beta", "gamma"):
        env.live(n)
    env.group("pair", ["alpha", "beta"])
    assert env.run("--ask-ready", "--group", "pair") == 0
    env.answer("alpha", "beta")
    gate = env.crs.go_gate([s for s in env.crs.sessions()], False, 900)
    assert gate == 3


# ── the roster call itself, against a stand-in nx ────────────────────────────────────────


def test_the_roster_is_read_through_the_nx_the_script_resolves(env: Env, tmp_path: Path) -> None:
    """The real subprocess call: argv, the JSON read and the names. The stand-in logs what it was asked."""
    log = tmp_path / "nx-calls.txt"
    fake = tmp_path / "nx"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {log}\n'
        'echo \'{"group": {"name": "g", "live": true}, "members": [{"identity": "i1", "name": "alpha"}, '
        '{"identity": "i2", "name": null}, {"identity": "i3", "name": "beta"}]}\'\n',
        encoding="utf-8")
    fake.chmod(0o755)
    env.monkeypatch.setenv("NX_RESTART_NX", str(fake))
    real = importlib.machinery.SourceFileLoader("claude_restart_sessions_group_real", str(SCRIPT))
    spec = importlib.util.spec_from_loader(real.name, real)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    real.exec_module(module)
    names, problem = module.group_roster("g")
    assert problem == "" and names == frozenset({"alpha", "beta"})
    assert log.read_text(encoding="utf-8").split() == ["group", "list", "g", "--json"]
