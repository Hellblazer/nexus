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
        self.withdrawn: list[str] = []
        self.tmux: list[list[str]] = []
        self.groups: dict[str, subprocess.CompletedProcess] = {}

    def live(self, name: str, status: str = "idle") -> None:
        (self.root / "sessions" / f"{name}.json").write_text(json.dumps(
            {"kind": "interactive", "tmux": "rs:@1.%5", "pid": os.getpid(), "sessionId": f"sid-{name}", "name": name,
             "cwd": "/w", "status": status}), encoding="utf-8")

    def desktop(self, name: str) -> None:
        (self.root / "sessions" / f"{name}.json").write_text(json.dumps(
            {"kind": "interactive", "pid": os.getpid(), "sessionId": f"sid-{name}", "name": name, "cwd": "/w",
             "status": "idle", "entrypoint": "claude-desktop"}), encoding="utf-8")

    def group(self, name: str, members: list[str | None | tuple[str | None, str]], **record: object) -> None:
        """Members are names (state live) or (name, state): the states `nx group list` prints (live, lapsed, ended ...)."""
        pairs = [m if isinstance(m, tuple) else (m, "live") for m in members]
        body = {"group": {"name": name, "live": True, **record},
                "members": [{"identity": f"i{n}", "name": m, "role": "member", "state": st}
                            for n, (m, st) in enumerate(pairs, 1)]}
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
    monkeypatch.setattr(crs, "session_work", lambda _s, **k: [])
    monkeypatch.setattr(crs, "ensure_seat", lambda _s: "")
    monkeypatch.setattr(crs, "has_channel", lambda _s: True)

    def mail_ask(s, *_a, **_k) -> bool:
        # A real send records the ask's tuple id for the round file; the status read then withdraws it and rewrites it.
        e.mailed.append(s["name"])
        crs.SENT_ASKS[s["sessionId"]] = f"t-{s['name']}"
        return True

    monkeypatch.setattr(crs, "mail_ask", mail_ask)
    monkeypatch.setattr(crs, "withdraw_ask", lambda sid, tid="", what="": e.withdrawn.append(tid) or crs.GONE[0])

    def never(*a, **k):
        pytest.fail(f"the round reached tmux: {a} {k}")

    monkeypatch.setattr(crs, "pane_capture", never)
    monkeypatch.setattr(crs, "submit", never)
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
    env.desktop("desk")  # a member that cannot be asked
    env.desktop("other-desk")  # a Desktop session outside the group
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
    """--status writes the round file back when it withdraws an answered ask; a group view must not drop the rest.

    Each ask carries a tuple id (the mail stub records one, as a real send does), so the status read withdraws the
    group's answered asks and rewrites the file: the write-back this test is about really happens."""
    for n in ("alpha", "beta", "gamma"):
        env.live(n)
    assert env.run("--ask-ready") == 0
    assert {t["name"]: t.get("tuple_id") for t in env.manifest()["targets"]} == {
        "alpha": "t-alpha", "beta": "t-beta", "gamma": "t-gamma"}
    env.answer("alpha", "beta", "gamma")
    env.group("pair", ["alpha", "beta"])
    assert env.run("--status", "--group", "pair") == 0
    assert sorted(env.withdrawn) == ["t-alpha", "t-beta"], "only the group's asks are withdrawn"
    after = {t["name"]: t.get("tuple_id") for t in env.manifest()["targets"]}
    assert after == {"alpha": "", "beta": "", "gamma": "t-gamma"}, "the file was rewritten and kept every target"


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


# ── who the round does not cover (critic S1) ─────────────────────────────────────────────


def _roster_with_gaps(env: Env) -> None:
    env.live("alpha")
    env.live("outsider")
    env.group("crew", ["alpha", "omega", ("old", "ended"), (None, "live")])


def _uncovered(out: str) -> dict[str, str]:
    """`  <member> (<state>): <why>` lines, by member."""
    rows = {}
    for ln in out.splitlines():
        if ln.startswith("  ") and "): " in ln:
            who, why = ln.strip().split("): ", 1)
            rows[who] = why
    return rows


def test_ask_ready_with_a_group_names_the_members_it_did_not_cover(env: Env, capsys) -> None:
    _roster_with_gaps(env)
    assert env.run("--ask-ready", "--group", "crew") == 0
    out = capsys.readouterr().out
    assert "group crew: 1 of 4 member(s) covered here" in out
    assert _uncovered(out) == {"omega (live": "no live session here", "old (ended": "no live session here",
                               "i4 (live": "no alias"}
    assert "outsider" not in out


def test_status_with_a_group_names_the_members_it_did_not_cover(env: Env, capsys) -> None:
    _roster_with_gaps(env)
    assert env.run("--ask-ready", "--group", "crew") == 0
    env.answer("alpha")
    capsys.readouterr()
    assert env.run("--status", "--group", "crew") == 0, "the covered member is ready; the exit code alone hides the rest"
    out = capsys.readouterr().out
    assert "group crew: 1 of 4 member(s) covered here" in out
    assert _uncovered(out) == {"omega (live": "no live session here", "old (ended": "no live session here",
                               "i4 (live": "no alias"}


def test_a_member_that_is_not_live_whose_alias_a_live_session_carries_says_so(env: Env, capsys) -> None:
    """An ended member's alias reused by a live session that is no member: the session is here, the member is not."""
    env.live("alpha")
    env.live("old")
    env.group("crew", ["alpha", ("old", "ended")])
    assert env.run("--ask-ready", "--group", "crew") == 0
    assert env.mailed == ["alpha"]
    assert _uncovered(capsys.readouterr().out) == {
        "old (ended": "a live session here has this name, but the member is not live, so it is not asked"}


@pytest.mark.parametrize("beta", [None, "desktop", "tmux"])
def test_an_unleased_member_is_a_desktop_binding_and_is_never_asked(env: Env, capsys, beta) -> None:
    """unleased is the lease a Desktop binding gets (binder.py): no tmux pane, so the round cannot ask it, and a tmux
    session carrying its alias is some other session. Asking that one would count the member covered unasked."""
    env.live("alpha")
    if beta == "desktop":
        env.desktop("beta")
    elif beta == "tmux":
        env.live("beta")
    env.group("crew", ["alpha", ("beta", "unleased")])
    assert env.run("--ask-ready", "--group", "crew") == 0
    assert env.mailed == ["alpha"]
    out = capsys.readouterr().out
    assert "group crew: 1 of 2 member(s) covered here" in out
    assert _uncovered(out) == {"beta (unleased": "a Desktop binding (no tmux pane), cannot be asked from here"}


def test_a_live_member_whose_session_has_no_tmux_pane_says_so(env: Env, capsys) -> None:
    env.live("alpha")
    env.desktop("gamma")
    env.group("crew", ["alpha", "gamma"])
    assert env.run("--ask-ready", "--group", "crew") == 0
    assert _uncovered(capsys.readouterr().out) == {
        "gamma (live": "no tmux pane (a Desktop session), cannot be asked from here"}


@pytest.mark.parametrize(("flags", "why"), [(("--skip", "beta"), "left out by --skip"),
                                            (("--skip", "sid-beta"), "left out by --skip"),
                                            (("--only", "alpha"), "not in --only")])
def test_a_member_the_round_left_out_by_flag_names_the_flag(env: Env, capsys, flags, why) -> None:
    env.live("alpha")
    env.live("beta")
    env.group("crew", ["alpha", "beta"])
    assert env.run("--ask-ready", "--group", "crew", *flags) == 0
    assert env.mailed == ["alpha"]
    assert _uncovered(capsys.readouterr().out) == {"beta (live": why}


def test_a_member_on_another_tmux_server_says_so(env: Env, capsys) -> None:
    env.live("alpha")
    env.live("beta")
    env.group("crew", ["alpha", "beta"])
    env.monkeypatch.setattr(env.crs, "check_server", lambda found, quiet=False: (
        FAKE_SERVER, [s for s in found if s["name"] != "beta"], ""))
    assert env.run("--ask-ready", "--group", "crew") == 0
    assert env.mailed == ["alpha"]
    assert _uncovered(capsys.readouterr().out) == {"beta (live": "not on this tmux server"}


def test_status_names_a_target_left_out_by_a_flag_apart_from_one_that_is_gone(env: Env, capsys) -> None:
    for n in ("alpha", "beta", "gamma"):
        env.live(n)
    assert env.run("--ask-ready") == 0
    (env.root / "sessions" / "gamma.json").unlink()
    capsys.readouterr()
    env.run("--status", "--skip", "beta")
    out = capsys.readouterr().out
    assert "in the round but not live now: gamma" in out
    assert "in the round but left out of this read (--only, --skip or another tmux server): beta" in out


def test_status_names_a_target_on_another_tmux_server_as_left_out_not_gone(env: Env, capsys) -> None:
    for n in ("alpha", "beta"):
        env.live(n)
    assert env.run("--ask-ready") == 0
    env.monkeypatch.setattr(env.crs, "check_server", lambda found, quiet=False: (
        FAKE_SERVER, [s for s in found if s["name"] != "beta"], ""))
    capsys.readouterr()
    env.run("--status")
    out = capsys.readouterr().out
    assert "not live now" not in out
    assert "in the round but left out of this read (--only, --skip or another tmux server): beta" in out


def test_skip_by_a_session_id_prefix_names_the_flag(env: Env, capsys) -> None:
    """--skip takes 8 or more leading characters of a session id; the full id alone would not tell startswith from ==."""
    env.live("alpha")
    env.live("betamax")
    env.group("crew", ["alpha", "betamax"])
    assert env.run("--ask-ready", "--group", "crew", "--skip", "sid-beta") == 0
    assert env.mailed == ["alpha"]
    assert _uncovered(capsys.readouterr().out) == {"betamax (live": "left out by --skip"}


def test_a_group_whose_members_are_all_covered_prints_no_gaps(env: Env, capsys) -> None:
    env.live("alpha")
    env.live("beta")
    env.group("pair", ["alpha", "beta"])
    assert env.run("--ask-ready", "--group", "pair") == 0
    out = capsys.readouterr().out
    assert "group pair: 2 of 2 member(s) covered here" in out and _uncovered(out) == {}


def test_the_help_says_what_an_exit_zero_means_with_a_group(env: Env, capsys) -> None:
    with pytest.raises(SystemExit):
        env.run("--help")
    flat = " ".join(capsys.readouterr().out.split())
    assert "with --group: when the group's live members are ready" in flat
    assert "group round replaces the round file" in flat


# ── refusals: one line, nothing asked, nothing written ───────────────────────────────────


def _refused(env: Env, capsys, *argv: str) -> str:
    env.live("alpha")
    rc = env.run(*argv)
    out = capsys.readouterr().out
    assert rc == 6, (rc, out)  # EXIT_REFUSED
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


@pytest.mark.parametrize("name", ["", " ", "\t "])
@pytest.mark.parametrize("flag", ["--ask-ready", "--status"])
def test_a_blank_group_name_refuses_and_never_becomes_a_round_of_everyone(env: Env, capsys, flag: str, name: str) -> None:
    """Review F1: `--group "$GROUP"` with the variable unset passed an empty name, which read as no --group at all."""
    assert "blank" in _refused(env, capsys, flag, "--group", name)


def test_a_blank_group_name_without_a_round_flag_is_a_usage_error_too(env: Env, capsys) -> None:
    env.live("alpha")
    with pytest.raises(SystemExit) as stop:
        env.run("--group", "")
    assert stop.value.code == 2
    assert "--group goes with --ask-ready or --status" in capsys.readouterr().err
    assert env.mailed == [] and env.restarted == []


@pytest.mark.parametrize("state", ["ended", "lapsed", "unheld", "unleased"])
def test_a_member_that_is_not_live_does_not_match_a_session_reusing_its_alias(env: Env, state: str) -> None:
    """Review F2: the roster names members by alias, and an ended member's alias is free to reuse. Only a live member
    matches a session; a live non-member session that took the alias of one that is not live is not asked."""
    env.live("alpha")
    env.live("beta")  # a session that reuses the alias of the member that left
    env.group("g", ["alpha", ("beta", state)])
    assert env.run("--ask-ready", "--group", "g") == 0
    assert env.mailed == ["alpha"]
    assert [t["name"] for t in env.manifest()["targets"]] == ["alpha"]


def test_a_group_with_no_live_member_refuses_in_one_line(env: Env, capsys) -> None:
    env.group("gone", [("beta", "ended"), ("gamma", "lapsed")])
    out = _refused(env, capsys, "--ask-ready", "--group", "gone")
    assert "gone" in out and "live" in out


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
        'echo \'{"group": {"name": "g", "live": true}, "members": ['
        '{"identity": "i1", "name": "alpha", "role": "leader", "state": "live"}, '
        '{"identity": "i2", "name": null, "role": "member", "state": "live"}, '
        '{"identity": "i3", "name": "beta", "role": "member", "state": "live"}, '
        '{"identity": "i4", "name": "gone", "role": "member", "state": "ended"}]}\'\n',
        encoding="utf-8")
    fake.chmod(0o755)
    env.monkeypatch.setenv("NX_RESTART_NX", str(fake))
    real = importlib.machinery.SourceFileLoader("claude_restart_sessions_group_real", str(SCRIPT))
    spec = importlib.util.spec_from_loader(real.name, real)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    real.exec_module(module)
    members, problem = module.group_roster("g")
    assert problem == "" and [(m["name"], m["state"]) for m in members] == [
        ("alpha", "live"), (None, "live"), ("beta", "live"), ("gone", "ended")]
    assert module.group_names(members) == frozenset({"alpha", "beta"}), "the ended member's alias names no session"
    assert log.read_text(encoding="utf-8").split() == ["group", "list", "g", "--json"]
