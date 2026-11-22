# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""claude-restart-sessions after Sam's 2026-11-19 restart rethink (T2 nexus/sam-ruling-restart-sandbox-gate-2026-11-19):
B1 the --nx install never runs `uv cache clean`; C1 a repeated --ask-ready round re-asks only the sessions whose last
answer was not a fresh ready, and keeps the others' answers; D1 the ask tells a session to stop its background shells."""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests._module_seam import setattr_in

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude-restart-sessions"
NOW = 1_800_000_000.0
FIRST_AT = NOW - 3000  # when the earlier round began


@pytest.fixture(scope="module")
def crs():
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions_rethink", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def rd(crs, monkeypatch, tmp_path_factory):
    """A scratch ~/.claude: sessions dir, log, round manifest, ask dir; no mailed ask on record."""
    root = tmp_path_factory.mktemp("rethink")
    (root / "sessions").mkdir()
    monkeypatch.setattr(crs, "SESSIONS", root / "sessions")
    monkeypatch.setattr(crs, "LOG", root / "restart.log")
    monkeypatch.setattr(crs, "READY_MANIFEST", root / "restart-ready.json")
    monkeypatch.setattr(crs, "ASK_DIR", root)
    monkeypatch.setattr(crs, "ready_path", lambda s, stamp: str(root / f"ready-{s['name']}-{stamp}.json"))  # never /tmp
    ticks = iter(range(1, 1000))  # a fresh stamp per round, whatever the clock
    setattr_in(monkeypatch, crs, "time.strftime", lambda _fmt, *_a: f"20261119-2{next(ticks):05d}")
    monkeypatch.setattr(crs, "ASK_MARKERS", root / "restart-asks")
    monkeypatch.setattr(crs, "SENT_ASKS", {})
    monkeypatch.setattr(crs, "SENT_MARKERS", {})
    monkeypatch.setattr(crs, "MADE_MARKERS", set())
    monkeypatch.setattr(crs, "UNWITHDRAWN", [])
    monkeypatch.setattr(crs, "session_tasks", lambda _s: [])
    monkeypatch.setattr(crs, "session_work", lambda _s, **k: [])
    monkeypatch.delenv("TMUX_PANE", raising=False)
    setattr_in(monkeypatch, crs, "signal.signal", lambda *_a: None)
    setattr_in(monkeypatch, crs, "time.time", lambda: NOW)
    return root


def _live(rd: Path, name: str, status: str = "idle", pane: str = "%5") -> None:
    (rd / "sessions" / f"{name}.json").write_text(json.dumps(
        {"kind": "interactive", "tmux": f"rs:@1.{pane}", "pid": os.getpid(), "sessionId": f"sid-{name}", "name": name,
         "cwd": "/w", "status": status}), encoding="utf-8")


def _reply(rd: Path, name: str, stamp: str, age: float | None, body: dict | str | None = None) -> str:
    path = rd / f"ready-{name}-{stamp}.json"
    if age is not None:
        path.write_text(body if isinstance(body, str) else json.dumps(
            body if body is not None else {"ready": True, "reason": "idle", "eta_min": None}), encoding="utf-8")
        os.utime(path, (NOW - age, NOW - age))
    return str(path)


def _earlier_round(crs, rd: Path, ages: dict[str, float | None], bodies: dict | None = None, stamp: str = "20261119-100000") -> None:
    """A round that asked every name in `ages`; the value is its reply's age at NOW (None: never answered)."""
    bodies = bodies or {}
    targets = [{"name": n, "sessionId": f"sid-{n}", "path": _reply(rd, n, stamp, age, bodies.get(n)), "how": "mail"}
               for n, age in ages.items()]
    crs.write_manifest(stamp, targets, [], at=FIRST_AT)


def _args(**kw) -> argparse.Namespace:
    return argparse.Namespace(**{"status": False, "dry_run": False, "ready_max_age": 900, "only": [], "skip": [], **kw})


def _asking(crs, monkeypatch) -> list[str]:
    """Stand in for the mail: record whom ask_ready is asked, and make each a target with its path."""
    asked: list[str] = []

    def ask(found, stamp, _sender, out=None):
        out = [] if out is None else out
        for s in found:
            asked.append(s["name"])
            out.append({"name": s["name"], "sessionId": s["sessionId"], "path": crs.ready_path(s, stamp), "how": "mail"})
        return out

    monkeypatch.setattr(crs, "ask_ready", ask)
    return asked


def _round_now(crs, rd: Path, names: list[str]) -> int:
    for n in names:
        _live(rd, n)
    found = crs.sessions()
    return crs.ready_round(_args(), found, found)


# ---- B1: no `uv cache clean` ------------------------------------------------------------------

def test_the_nx_install_never_runs_uv_cache_clean(crs, monkeypatch, tmp_path) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    monkeypatch.setattr(crs, "release_engine_floor", lambda _v, _r: (0, 1, 0))
    setattr_in(monkeypatch, crs, "subprocess.run", lambda cmd, *_a, **_k: ran.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(crs, "nx_version", lambda: "nx, version 7.93.1")
    monkeypatch.setattr(crs, "update_plugins", lambda: None)
    monkeypatch.setattr(crs, "plugin_versions", lambda: {p: "7.93.1" for p in crs.nexus_plugins()})
    assert crs.converge("7.93.1", tmp_path, "http://127.0.0.1:1", gate=False, dry=False) is True
    assert not [c for c in ran if "cache" in c], ran
    assert [c[1:] for c in ran] == [["conexus==7.93.1", "--allow-registry-over-dev"]]  # the pinned install still runs


def test_the_script_has_no_uv_cache_clean_anywhere() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert '"cache", "clean"' not in text and "uv cache clean" not in text.replace("`uv cache clean`", "")


# ---- C1: a repeated round re-asks only the sessions that were not ready --------------------------

def test_a_repeated_round_re_asks_only_the_sessions_without_a_fresh_ready_answer(crs, rd, monkeypatch) -> None:
    _earlier_round(crs, rd, {"alpha": 60, "beta": 60, "gamma": 901, "delta": None, "eps": 60, "zeta": 60},
                   bodies={"beta": {"ready": False, "reason": "mid-edit", "eta_min": 5}, "eps": "{not json"})
    asked = _asking(crs, monkeypatch)
    assert _round_now(crs, rd, ["alpha", "beta", "gamma", "delta", "eps", "zeta", "newcomer"]) == 0
    # alpha and zeta keep their fresh ready answers; not ready, stale, missing, unreadable and unasked are asked again
    assert sorted(asked) == ["beta", "delta", "eps", "gamma", "newcomer"]
    manifest, _ = crs.load_manifest()
    by = {t["name"]: t for t in manifest["targets"]}
    assert set(by) == {"alpha", "beta", "gamma", "delta", "eps", "zeta", "newcomer"}
    assert by["alpha"]["how"] == "kept" and by["alpha"]["kept_from"] == "20261119-100000"
    assert by["alpha"]["path"].endswith("ready-alpha-20261119-100000.json")  # the old file: no second store
    assert by["beta"]["how"] == "mail" and "kept_from" not in by["beta"]


def test_a_ready_answer_from_a_session_no_longer_idle_is_asked_again(crs, rd, monkeypatch) -> None:
    """Review r1: a session that answered ready and has since gone busy or opened a shell is asked again (the ask
    names its shells), not kept until its answer goes stale; the caller's own pane is kept whatever its status."""
    _earlier_round(crs, rd, {"alpha": 60, "beta": 60, "gamma": 60, "me": 60})
    asked = _asking(crs, monkeypatch)
    _live(rd, "alpha")
    _live(rd, "beta", status="busy")
    _live(rd, "gamma", status="shell")
    _live(rd, "me", status="busy", pane="%9")
    monkeypatch.setenv("TMUX_PANE", "%9")
    found = crs.sessions()
    crs.ready_round(_args(), found, found)
    assert sorted(asked) == ["beta", "gamma"]
    manifest, _ = crs.load_manifest()
    assert {t["name"] for t in manifest["targets"] if t["how"] == "kept"} == {"alpha", "me"}


def test_a_target_of_the_earlier_round_that_is_gone_is_not_kept(crs, rd, monkeypatch) -> None:
    """A session that left (not among the live sessions now) is dropped, its fresh answer notwithstanding."""
    _earlier_round(crs, rd, {"alpha": 60, "gone": 60})
    _asking(crs, monkeypatch)
    _round_now(crs, rd, ["alpha"])
    manifest, _ = crs.load_manifest()
    assert {t["name"] for t in manifest["targets"]} == {"alpha"}


def test_a_kept_answer_reads_ready_at_the_gate_and_the_asked_ones_need_their_own_reply(crs, rd, monkeypatch) -> None:
    _earlier_round(crs, rd, {"alpha": 60, "beta": 60}, bodies={"beta": {"ready": False, "reason": "mid-edit", "eta_min": 5}})
    _asking(crs, monkeypatch)
    _round_now(crs, rd, ["alpha", "beta"])
    manifest, _ = crs.load_manifest()
    found = crs.sessions()
    rows = {r["name"]: r for r in crs.readiness(found, manifest, NOW, 900)}
    assert rows["alpha"]["state"] == "ready" and rows["alpha"]["blocker"] == ""  # older than the new round, still counts
    assert rows["beta"]["state"] == "no reply"  # the new ask is unanswered; its old not-ready is not read
    # beta answers the new ask: the round is now complete
    Path(crs.ready_path(next(s for s in found if s["name"] == "beta"), manifest["stamp"])).write_text(
        json.dumps({"ready": True, "reason": "done", "eta_min": None}), encoding="utf-8")
    os.utime(crs.ready_path(next(s for s in found if s["name"] == "beta"), manifest["stamp"]), (NOW, NOW))
    assert crs.go_gate(found, force=False, max_age=900, now=NOW) == 0


def test_a_kept_answer_goes_stale_with_the_window_and_is_asked_again(crs, rd, monkeypatch) -> None:
    _earlier_round(crs, rd, {"alpha": 880})
    asked = _asking(crs, monkeypatch)
    _round_now(crs, rd, ["alpha"])
    assert asked == []  # 880 s old at the second round: inside the 900 s window
    setattr_in(monkeypatch, crs, "time.time", lambda: NOW + 60)  # a third round a minute on: the answer is 940 s old
    _round_now(crs, rd, ["alpha"])
    assert asked == ["alpha"]
    manifest, _ = crs.load_manifest()
    assert manifest["targets"][0]["how"] == "mail" and "kept_from" not in manifest["targets"][0]


def test_a_third_round_keeps_an_answer_from_the_first_and_names_its_round(crs, rd, monkeypatch) -> None:
    _earlier_round(crs, rd, {"alpha": 60, "beta": None})
    asked = _asking(crs, monkeypatch)
    _round_now(crs, rd, ["alpha", "beta"])  # round 2 keeps alpha, asks beta
    _round_now(crs, rd, ["alpha", "beta"])  # round 3 keeps alpha again (the since of its reply is round 1's)
    assert asked == ["beta", "beta"]
    manifest, _ = crs.load_manifest()
    alpha = next(t for t in manifest["targets"] if t["name"] == "alpha")
    assert alpha["kept_from"] == "20261119-100000" and alpha["since"] == FIRST_AT


@pytest.mark.parametrize("record", [None, "{not json", json.dumps({"stamp": "s"})], ids=["none", "garbled", "malformed"])
def test_with_no_usable_earlier_round_every_session_is_asked(crs, rd, monkeypatch, record) -> None:
    if record is not None:
        crs.READY_MANIFEST.write_text(record, encoding="utf-8")
    asked = _asking(crs, monkeypatch)
    _round_now(crs, rd, ["alpha", "beta"])
    assert sorted(asked) == ["alpha", "beta"]


def test_a_kept_entry_carries_no_mailed_ask_to_withdraw(crs, rd, monkeypatch) -> None:
    _earlier_round(crs, rd, {"alpha": 60})
    manifest, _ = crs.load_manifest()
    manifest["targets"][0]["tuple_id"] = "tid-old"
    crs.write_manifest(manifest["stamp"], manifest["targets"], [], at=manifest["at"])
    _asking(crs, monkeypatch)
    withdrawn: list[str] = []
    monkeypatch.setattr(crs, "withdraw_ask", lambda sid, tid="", **_k: withdrawn.append(tid) or crs.GONE[0])
    _round_now(crs, rd, ["alpha"])
    manifest, _ = crs.load_manifest()
    (alpha,) = manifest["targets"]
    assert withdrawn == ["tid-old"]  # the previous round's ask is taken back as before
    assert not alpha.get("tuple_id") and not alpha.get("marker")


def test_status_says_which_sessions_were_re_asked_and_which_answers_were_kept(crs, rd, monkeypatch, capsys) -> None:
    _earlier_round(crs, rd, {"alpha": 60, "beta": None})
    _asking(crs, monkeypatch)
    _round_now(crs, rd, ["alpha", "beta"])
    capsys.readouterr()
    found = crs.sessions()
    assert crs.ready_round(_args(status=True), found, found) in (0, 1)
    out = capsys.readouterr().out
    assert "re-asked: beta" in out and "kept the ready answer of: alpha (from round 20261119-100000)" in out
    alpha_row = next(ln for ln in out.splitlines() if ln.startswith("alpha"))
    assert "ready" in alpha_row and "kept" in alpha_row


def test_a_first_round_prints_no_kept_line(crs, rd, monkeypatch, capsys) -> None:
    _asking(crs, monkeypatch)
    _round_now(crs, rd, ["alpha"])
    found = crs.sessions()
    crs.ready_round(_args(status=True), found, found)
    assert "re-asked" not in capsys.readouterr().out


def test_a_manifest_with_a_bad_since_is_malformed(crs, rd) -> None:
    _earlier_round(crs, rd, {"alpha": 60})
    d = json.loads(crs.READY_MANIFEST.read_text(encoding="utf-8"))
    d["targets"][0]["since"] = "yesterday"
    crs.READY_MANIFEST.write_text(json.dumps(d), encoding="utf-8")
    assert crs.load_manifest() == (None, "the ready round file is malformed")


# ---- D1: the ask tells the session to stop its background shells ----------------------------------

@pytest.mark.parametrize("tasks", [["pid 5000 since t: nx ci wait abc", "pid 5001 since t: sleep 9"], [], None],
                         ids=["tasks-seen", "none-seen", "unreadable"])
def test_the_ask_tells_the_session_to_stop_its_shells_with_taskstop(crs, monkeypatch, tasks) -> None:
    s = {"name": "alpha", "sessionId": "sid-alpha", "pid": 1}
    monkeypatch.setattr(crs, "session_tasks", lambda _s: tasks)
    monkeypatch.setattr(crs, "session_work", lambda _s, **k: [])
    text = " ".join(crs.ready_prompt(s, "/tmp/r.json").split())
    assert "TaskStop" in text and "finished ones included" in text
    assert "before you answer ready" in text
    assert "status `shell`" in text and "the restart skips it" in text
    for t in tasks or []:
        assert t in text  # named, so the session need not look them up


def test_the_ask_names_the_tasks_the_script_sees_ahead_of_the_instruction(crs, monkeypatch) -> None:
    s = {"name": "alpha", "sessionId": "sid-alpha", "pid": 1}
    monkeypatch.setattr(crs, "session_tasks", lambda _s: ["pid 5000 since t: nx ci wait abc"])
    monkeypatch.setattr(crs, "session_work", lambda _s, **k: [])
    text = crs.ready_prompt(s, "/tmp/r.json")
    assert text.index("pid 5000 since t: nx ci wait abc") < text.index("TaskStop") < text.index("Answer by writing the file")
    assert len(text.encode()) <= 4096  # the mailbox template's body ceiling


def test_a_dry_run_round_says_which_answers_it_would_keep(crs, rd, monkeypatch, capsys) -> None:
    """Review r1: --ask-ready --dry-run names the sessions whose ready answer a real round would keep, not "would ask"."""
    _earlier_round(crs, rd, {"alpha": 60, "beta": 901})
    asked = _asking(crs, monkeypatch)
    for n in ("alpha", "beta"):
        _live(rd, n)
    found = crs.sessions()
    assert crs.ready_round(_args(dry_run=True), found, found) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "would keep its ready answer from round 20261119-100000" in out
    assert any("beta" in ln and "would ask" in ln for ln in out.splitlines())
    assert asked == [], "a dry run asks no one"
