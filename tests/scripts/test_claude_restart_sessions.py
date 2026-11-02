# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/claude-restart-sessions: the draft check and the relaunch command (nexus-nuyju).

The pane lines are real ``tmux capture-pane -e`` captures from the 2026-11-01 sessions
whose dim suggested prompt made the 7.92.0 restart skip six of nine handoffs.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from tests._module_seam import setattr_in

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude-restart-sessions"

RULE = "\x1b[38;5;244m" + "─" * 40 + "\x1b[39m"
FOOTER = "  \x1b[2m? for shortcuts\x1b[0m"


@pytest.fixture(scope="module")
def crs():
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def pane(prompt_line: str) -> str:
    return "\n".join(["some transcript text", RULE, prompt_line, RULE, FOOTER])


@pytest.mark.parametrize(
    ("prompt_line", "draft"),
    [
        ("\x1b[39m❯\xa0\x1b[2mgo Step 0\x1b[0m", False),  # elsinore: a suggested prompt
        ("\x1b[39m❯\xa0\x1b[2m`[no suggestion]`\x1b[0m", False),  # labrun_559
        ("\x1b[39m❯\xa0\x1b[2m! bash ~/.local/state/conexus/run/v0174/catalogue-wt.sh\x1b[0m", False),  # bob
        ("\x1b[39m❯\xa0", False),  # empty prompt
        ("\x1b[38;5;241m❯\xa0\x1b[39m", False),  # empty prompt, grey mark
        ("\x1b[39m❯\xa0fix the build", True),  # typed text, default colour
        ("\x1b[39m❯\xa0half a thought\x1b[7m \x1b[27m", True),  # typed text and the cursor cell
        ("\x1b[39m❯\xa0typed \x1b[2mand a ghost tail\x1b[0m", True),  # typed text with a dim completion
        ("\x1b[39m❯\xa0\x1b[2;38;5;245mdim with a colour\x1b[22m", False),  # dim set with a colour in one SGR
        ("\x1b[39m❯\xa0\x1b[38;5;2mtyped in colour 2", True),  # 256-colour index 2 is not dim
        ("\x1b[39m❯\xa0\x1b[38;2;2;2;2mtyped in rgb 2,2,2", True),  # truecolour 2s are not dim
    ],
    ids=["ghost-elsinore", "ghost-no-suggestion", "ghost-command", "empty", "empty-grey",
         "typed", "typed-cursor", "typed-plus-ghost", "dim-combined-sgr", "colour-index-2", "truecolour-2s"],
)
def test_has_draft_counts_only_text_that_is_not_dim(crs, prompt_line: str, draft: bool) -> None:
    assert crs.has_draft(pane(prompt_line)) is draft


def test_a_prompt_mark_outside_the_input_box_is_not_the_prompt(crs) -> None:
    """The footer's agent list can open with the same mark; only the line under a rule counts."""
    captured = "\n".join(["❯ an agent row with text", RULE, "\x1b[39m❯\xa0", RULE])
    assert crs.has_draft(captured) is False


def test_no_prompt_line_is_no_draft(crs) -> None:
    assert crs.has_draft("just output\nmore output") is False


@pytest.mark.parametrize(
    ("argv", "fresh", "want", "unwanted"),
    [
        ("claude --channels plugin:conexus@nexus-plugins -n old_name --resume abc", False,
         ["--channels", "-n", "tamsin", "--resume", "sid-1"], ["old_name", "abc"]),
        ("claude --name=old --continue --permission-mode auto", True,
         ["-n", "tamsin", "--permission-mode", "auto"], ["--resume", "--continue", "--name=old"]),
    ],
    ids=["resume-current-id-and-name", "fresh-drops-resume-keeps-mode"],
)
def test_relaunch_command(crs, argv: str, fresh: bool, want: list[str], unwanted: list[str]) -> None:
    cmd = crs.relaunch_command({"argv": argv, "name": "tamsin", "sessionId": "sid-1", "cwd": "/w d"}, fresh=fresh)
    assert cmd.startswith("cd '/w d' && ")
    args = shlex.split(cmd.split(" && ", 1)[1])[1:]
    for w in want:
        assert w in args, (w, args)
    for u in unwanted:
        assert u not in args, (u, args)


def test_claude_binary_honours_the_override_then_path(crs, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NX_RESTART_CLAUDE", "/opt/x/claude")
    assert crs._claude_binary() == "/opt/x/claude"
    monkeypatch.delenv("NX_RESTART_CLAUDE")
    monkeypatch.setenv("HOME", str(tmp_path))
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake.parent))
    assert crs._claude_binary() == str(fake)  # no ~/.local/bin/claude under this HOME


def test_an_empty_capture_counts_as_a_draft(crs) -> None:
    """tmux failed to read the pane: typing a handoff ask into it is the unsafe direction."""
    assert crs.has_draft("") is True and crs.has_draft("\n  \n") is True


def test_colon_form_sgr_is_parsed(crs) -> None:
    assert crs.has_draft(pane("\x1b[39m❯\xa0\x1b[38:5:2mtyped in colour 2")) is True
    assert crs.has_draft(pane("\x1b[39m❯\xa0\x1b[2mghost\x1b[0m")) is False


def _session_file(directory: Path, name: str, sid: str, pid: int, mtime: float) -> Path:
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"kind": "interactive", "tmux": "rs:@1.%5", "pid": pid, "sessionId": sid,
                                "name": name, "cwd": "/w", "status": "idle"}), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_sessions_skips_a_gone_pid_and_keeps_the_newest_of_a_duplicated_id(crs, monkeypatch, tmp_path) -> None:
    """The two non-draft SKIP kinds in the restart log: 'pid already gone' and 'also live as'."""
    monkeypatch.setattr(crs, "SESSIONS", tmp_path)
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    gone = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    me = os.getpid()
    _session_file(tmp_path, "old_pane", "sid-dup", me, 1000.0)
    _session_file(tmp_path, "new_pane", "sid-dup", me, 2000.0)
    _session_file(tmp_path, "ghost", "sid-gone", int(gone.stdout.strip()), 1500.0)
    got = crs.sessions()
    assert [s["name"] for s in got] == ["new_pane"]
    assert "is also live as new_pane; resuming it twice would fork it" in (tmp_path / "restart.log").read_text()


def test_pane_has_draft_reads_the_pane_with_its_escapes(crs, monkeypatch) -> None:
    """The fix is the -e: without it the dim suggestion arrives as plain text (nexus-nuyju)."""
    seen: list[list[str]] = []

    def fake_run(argv, **_kw):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=pane("\x1b[39m\u276f\xa0\x1b[2mgo Step 0\x1b[0m"), stderr="")

    setattr_in(monkeypatch, crs, "subprocess.run", fake_run)
    assert crs.pane_has_draft("%5") is False
    assert seen and seen[0][:2] == ["tmux", "capture-pane"] and "-e" in seen[0]


def test_an_empty_prompt_under_a_reverse_video_cursor_is_no_draft(crs) -> None:
    assert crs.has_draft(pane("\x1b[39m\u276f\xa0\x1b[7m \x1b[27m")) is False


def test_the_local_claude_wins_over_path(crs, monkeypatch, tmp_path) -> None:
    """The operator's box: ~/.local/bin/claude exists and is used even with another claude on PATH."""
    monkeypatch.delenv("NX_RESTART_CLAUDE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    local = tmp_path / ".local" / "bin" / "claude"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    other = tmp_path / "other" / "claude"
    other.parent.mkdir()
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    monkeypatch.setenv("PATH", str(other.parent))
    assert crs._claude_binary() == str(local)


@pytest.mark.parametrize(
    ("argv", "want"),
    [
        ("claude --channels plugin:conexus@nexus-plugins -n x", True),
        ("claude --channels=plugin:conexus@nexus-plugins", True),
        ("claude --dangerously-load-development-channels server:nexus", True),
        ("claude --channels plugin:other@elsewhere", False),
        ("claude -n x --model sonnet", False),
        ("claude -n 'unbalanced", False),
        ("", False),
        ("claude --channels plugin:other@x plugin:conexus@nexus-plugins -n x", True),
        ("claude -n x --channels", False),
        ("claude -n x -- --channels plugin:conexus@nexus-plugins", False),
        ("claude --channels plugin:conexus@elsewhere", False),
    ],
    ids=["channels", "channels-inline", "dev-channels", "other-plugin", "none", "unparseable", "empty",
         "second-value", "flag-last", "after-double-dash", "other-market"],
)
def test_has_channel(crs, argv: str, want: bool) -> None:
    """Mail reaches a session as a turn only through the conexus channel (nexus-cmqe8)."""
    assert crs.has_channel({"argv": argv}) is want


def test_fresh_relaunch_carries_the_first_prompt_after_a_double_dash(crs) -> None:
    """--channels takes values, so the positional prompt must follow `--` or it is read as a channel."""
    cmd = crs.relaunch_command({"argv": "claude --channels plugin:conexus@nexus-plugins", "name": "tamsin",
                                "sessionId": "sid-1", "cwd": "/w"}, fresh=True, first_prompt="cat /tmp/h f.md")
    args = shlex.split(cmd.split(" && ", 1)[1])[1:]
    assert args[-2:] == ["--", "cat /tmp/h f.md"]
    assert "--resume" not in args


def test_mail_ask_writes_the_session_mailbox_and_types_nothing(crs, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    seen: list[list[str]] = []

    def fake_run(argv, **_kw):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="tupleid\n", stderr="")

    setattr_in(monkeypatch, crs, "subprocess.run", fake_run)
    s = {"sessionId": "sid-9", "name": "tamsin"}
    assert crs.mail_ask(s, "/tmp/h.md", "sid-caller") is True
    assert len(seen) == 1 and seen[0][1:4] == ["tuple", "out", "mailbox/sid-9"]
    argv = seen[0]
    pairs = {(argv[i], argv[i + 1]) for i in range(len(argv) - 1)}
    assert {("--key", "to=sid-9"), ("--dim", "from=sid-caller"), ("--dim", "address_kind=session"),
            ("--dim", "kind=restart-handoff"), ("--ttl-seconds", "1200")} <= pairs
    assert argv[argv.index("--nonce") + 1]
    body = argv[argv.index("--body") + 1]
    assert "/tmp/h.md" in body and body.endswith("reply with only: cat /tmp/h.md")
    assert len(body.encode()) <= 4096  # the mailbox template's body ceiling


def test_mail_ask_reports_a_failed_send(crs, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    setattr_in(monkeypatch, crs, "subprocess.run",
               lambda argv, **_kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="refused\n"))
    assert crs.mail_ask({"sessionId": "sid-9", "name": "t"}, "/tmp/h.md", "x") is False
    assert "mail to mailbox/sid-9 failed (exit 1)" in (tmp_path / "restart.log").read_text()


def test_transcript_has_matches_only_a_user_message(crs, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / ".claude" / "projects" / "p"
    proj.mkdir(parents=True)
    rows = [{"type": "assistant", "message": {"content": "cat /tmp/a.md"}},
            {"type": "user", "message": {"role": "user", "content": "cat /tmp/b.md"}}]
    (proj / "sid.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert crs.transcript_has("sid", "/tmp/b.md") is True
    assert crs.transcript_has("sid", "/tmp/a.md") is False
    assert crs.transcript_has("other", "/tmp/b.md") is False


@pytest.mark.parametrize("fresh", [False, True], ids=["resume", "fresh"])
def test_a_previous_first_prompt_is_not_carried_into_the_next_relaunch(crs, fresh: bool) -> None:
    """After a --fresh roll, ps shows `... -- cat <old handoff>`; the next roll must drop it,
    or -n and --resume land after `--` and claude reads them as prompt text."""
    s = {"argv": "claude --channels plugin:conexus@nexus-plugins -n old -- cat /tmp/old.md",
         "name": "tamsin", "sessionId": "sid-2", "cwd": "/w"}
    cmd = crs.relaunch_command(s, fresh=fresh, first_prompt="cat /tmp/new.md" if fresh else "")
    args = shlex.split(cmd.split(" && ", 1)[1])[1:]
    assert "/tmp/old.md" not in " ".join(args)
    assert args.count("--") == (1 if fresh else 0)
    head = args[:args.index("--")] if fresh else args
    assert ["-n", "tamsin"] == head[head.index("-n"):head.index("-n") + 2]
    if fresh:
        assert args[-1] == "cat /tmp/new.md" and "--resume" not in args
    else:
        assert head[-2:] == ["--resume", "sid-2"]


def test_draft_text_reads_every_line_of_the_input_box(crs) -> None:
    box = ("output\n────\n\x1b[39m❯\xa0first line of the draft\n"
           "  second line\n────\n  footer ❯ agents\n")
    assert crs.draft_text(box) == "first line of the draft\nsecond line"
    assert crs.draft_text(pane("\x1b[39m❯\xa0\x1b[2mghost suggestion\x1b[0m")) == ""
    assert crs.draft_text("") == ""


def _write_transcript(home: Path, sid: str, rows: list[dict]) -> None:
    proj = home / ".claude" / "projects" / "p"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                                       encoding="utf-8")


def test_prompt_count_counts_prompts_channel_and_peer_turns(crs, monkeypatch, tmp_path) -> None:
    """Channel mail and peer messages start turns and are recorded as meta user entries with an
    origin kind ("channel", "peer"; CC 2.1.316); a meta entry with no origin is not a prompt."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_transcript(tmp_path, "sid", [
        {"type": "user", "message": {"content": "typed prompt"}},
        {"type": "user", "isMeta": True, "origin": {"kind": "channel"}, "message": {"content": "<channel>mail"}},
        {"type": "user", "isMeta": True, "origin": {"kind": "peer"}, "message": {"content": "<peer>hi"}},
        {"type": "user", "isMeta": True, "message": {"content": "Caveat: local command output"}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
    ])
    assert crs.prompt_count("sid") == 3


def test_transcript_has_matches_a_non_ascii_path(crs, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = "/tmp/nexus-continuation-restart-café-12345678-20261101-000000.md"
    _write_transcript(tmp_path, "sid", [{"type": "user", "message": {"content": f"cat {path}"}}])
    assert crs.transcript_has("sid", path) is True


def test_mail_ask_survives_a_missing_nx(crs, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    monkeypatch.setenv("NX_RESTART_NX", str(tmp_path / "no-such-nx"))
    assert crs.mail_ask({"sessionId": "sid-9", "name": "t"}, "/tmp/h.md", "x") is False
    assert "FileNotFoundError" in (tmp_path / "restart.log").read_text()


@pytest.mark.parametrize(
    ("channel", "busy", "mail_turn_writes", "draft", "want_typed", "want_done"),
    [
        (True, True, True, False, False, True),     # mail taken: nothing typed
        (True, True, False, False, True, True),     # mail declined: asked once by typing
        (True, True, False, True, False, False),    # mail declined, draft in the prompt: skipped
        (False, True, False, False, True, True),    # no channel: typed from the start
        (True, False, False, False, True, True),    # mail started no turn: typed at once
    ],
    ids=["mail-taken", "mail-declined-typed", "mail-declined-draft-skipped", "no-channel-typed",
         "mail-no-turn-typed"],
)
def test_write_handoffs_routes_mail_then_typing(crs, monkeypatch, tmp_path, channel, busy, mail_turn_writes,
                                               draft, want_typed, want_done) -> None:
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    s = {"sessionId": "sid-1", "name": "t", "pid": os.getpid(), "pane": "%9", "file": tmp_path / "s.json"}
    handoff = tmp_path / "h.md"
    monkeypatch.setattr(crs, "handoff_path", lambda _s, _stamp: str(handoff))
    marker = crs.handoff_marker(s)
    body = "# Handoff\n\n## 1. State\n" + "x" * 300 + f"\n{marker}\n"
    typed: list[str] = []

    now = [1_000_000.0]

    def write_later() -> None:
        handoff.write_text(body, encoding="utf-8")
        os.utime(handoff, (now[0], now[0]))  # on the fake clock: after the ask, then stable

    monkeypatch.setattr(crs, "has_channel", lambda _s: channel)
    monkeypatch.setattr(crs, "mail_ask", lambda *_a, **_k: (write_later() if mail_turn_writes else None) or True)
    monkeypatch.setattr(crs, "went_busy", lambda *_a, **_k: busy)
    monkeypatch.setattr(crs, "prompt_count", lambda _sid: 1)
    monkeypatch.setattr(crs, "status", lambda _s: "idle")
    monkeypatch.setattr(crs, "pane_has_draft", lambda _p: draft)
    monkeypatch.setattr(crs, "ask_line", lambda _s, _path: "Do what the ask file says")  # no /tmp write
    monkeypatch.setattr(crs, "submit", lambda _pane, text, _f: typed.append(text) or write_later() or True)
    setattr_in(monkeypatch, crs, "time.sleep", lambda _s: None)
    def tick() -> float:
        now[0] += 7
        return now[0]

    setattr_in(monkeypatch, crs, "time.time", tick)
    got = crs.write_handoffs([s], {"idle"}, wait=60, timeout=10_000)
    assert bool(typed) is want_typed, typed
    assert (got == {"sid-1": str(handoff)}) is want_done, got
    if want_typed and channel and not busy:
        assert "by typed (the mail started no turn within 60s)" in (tmp_path / "restart.log").read_text()


def _fake_capture(monkeypatch, crs, text: str) -> None:
    setattr_in(monkeypatch, crs, "subprocess.run",
               lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, stdout=text, stderr=""))


def _box(line: str) -> str:
    return f"out\n───\n\x1b[39m❯\xa0{line}\n───\n"


def test_save_draft_writes_a_private_file(crs, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    _fake_capture(monkeypatch, crs, _box("half a thought"))
    path, refused = crs.save_draft({"pane": "%1", "sessionId": "sid", "name": "t"}, str(tmp_path / "h"))
    assert refused == "" and path == str(tmp_path / "h.draft.txt")
    assert "half a thought" in Path(path).read_text(encoding="utf-8")
    assert Path(path).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("line", ["[Pasted text #1 +12 lines]", "see [Image #2] here"], ids=["paste", "image"])
def test_save_draft_refuses_a_placeholder(crs, monkeypatch, tmp_path, line: str) -> None:
    """The pane shows only the placeholder, so saving it would claim a draft kept that is lost."""
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    _fake_capture(monkeypatch, crs, _box(line))
    path, refused = crs.save_draft({"pane": "%1", "sessionId": "sid", "name": "t"}, str(tmp_path / "h"))
    assert path == "" and "placeholder" in refused
    assert not (tmp_path / "h.draft.txt").exists()


def test_save_draft_with_nothing_typed(crs, monkeypatch, tmp_path) -> None:
    _fake_capture(monkeypatch, crs, _box("\x1b[2mghost\x1b[0m"))
    assert crs.save_draft({"pane": "%1", "sessionId": "sid", "name": "t"}, str(tmp_path / "h")) == ("", "")


def test_mail_note_is_logged_before_the_send_and_named_in_the_body(crs, monkeypatch, tmp_path) -> None:
    log = tmp_path / "restart.log"
    monkeypatch.setattr(crs, "LOG", log)
    s = {"sessionId": "sid-9", "name": "tamsin"}
    seen: list[str] = []

    def fake_run(argv, **_kw):
        seen.append(log.read_text(encoding="utf-8") if log.exists() else "")
        return subprocess.CompletedProcess(argv, 0, stdout="id\n", stderr="")

    setattr_in(monkeypatch, crs, "subprocess.run", fake_run)
    assert crs.mail_ask(s, "/tmp/h.md", "x") is True
    note = crs.mail_note(s, "/tmp/h.md")
    assert note in seen[0]  # in the log when the send runs
    assert note in crs.handoff_prompt(s, "/tmp/h.md", mailed=True)
    assert note not in crs.handoff_prompt(s, "/tmp/h.md")  # the typed ask is the user's own prompt


@pytest.mark.parametrize(("headings", "counted"), [(2, True), (1, False)], ids=["two-headings", "one-heading"])
def test_a_handoff_needs_two_headings(crs, monkeypatch, tmp_path, headings: int, counted: bool) -> None:
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    s = {"sessionId": "sid-1", "name": "t", "pid": os.getpid(), "pane": "%9", "file": tmp_path / "s.json"}
    handoff = tmp_path / "h.md"
    monkeypatch.setattr(crs, "handoff_path", lambda _s, _stamp: str(handoff))
    now = [1_000_000.0]
    body = "".join(f"# H{i}\n" for i in range(headings)) + "x" * 300 + f"\n{crs.handoff_marker(s)}\n"

    def write() -> bool:
        handoff.write_text(body, encoding="utf-8")
        os.utime(handoff, (now[0], now[0]))
        return True

    monkeypatch.setattr(crs, "has_channel", lambda _s: True)
    monkeypatch.setattr(crs, "mail_ask", lambda *_a, **_k: write())
    monkeypatch.setattr(crs, "went_busy", lambda *_a, **_k: True)
    monkeypatch.setattr(crs, "prompt_count", lambda _sid: 1)
    monkeypatch.setattr(crs, "status", lambda _s: "idle")
    setattr_in(monkeypatch, crs, "time.sleep", lambda _s: None)

    def tick() -> float:
        now[0] += 7
        return now[0]

    setattr_in(monkeypatch, crs, "time.time", tick)
    got = crs.write_handoffs([s], {"idle"}, wait=60, timeout=200)
    assert (got == {"sid-1": str(handoff)}) is counted


@pytest.mark.parametrize(("reported", "accepted"), [
    ("nx, version 7.93.1", True),
    ("nx, version 7.93.10", False),  # the old substring test read 7.93.1 in 7.93.10 as installed
    ("nx, version 17.93.1", False),
    ("nx, version 7.93.1.", True),  # sentence punctuation after the version
], ids=["exact", "longer-patch", "longer-major", "trailing-period"])
def test_converge_needs_nx_at_exactly_the_release(crs, monkeypatch, tmp_path, reported: str, accepted: bool) -> None:
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    monkeypatch.setattr(crs, "release_engine_floor", lambda _v, _r: (0, 1, 0))
    setattr_in(monkeypatch, crs, "subprocess.run", lambda *_a, **_k: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(crs, "nx_version", lambda: reported)
    monkeypatch.setattr(crs, "update_plugins", lambda: None)
    monkeypatch.setattr(crs, "plugin_versions", lambda: {p: "7.93.1" for p in crs.nexus_plugins()})
    assert crs.converge("7.93.1", tmp_path, "http://127.0.0.1:1", gate=False, dry=False) is accepted
    assert ("ABORT: nx reports" in (tmp_path / "restart.log").read_text(encoding="utf-8")) is not accepted


def test_converge_aborts_on_a_failed_install_even_with_nx_at_the_release(crs, monkeypatch, tmp_path) -> None:
    """A reinstall that exits nonzero aborts although nx already reports the release."""
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    monkeypatch.setattr(crs, "release_engine_floor", lambda _v, _r: (0, 1, 0))
    setattr_in(monkeypatch, crs, "subprocess.run", lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", ""))
    monkeypatch.setattr(crs, "nx_version", lambda: "nx, version 7.93.1")
    monkeypatch.setattr(crs, "update_plugins", lambda: pytest.fail("plugins updated after a failed install"))
    assert crs.converge("7.93.1", tmp_path, "http://127.0.0.1:1", gate=False, dry=False) is False
    assert "ABORT: reinstall-tool exited 1" in (tmp_path / "restart.log").read_text(encoding="utf-8")


@pytest.mark.parametrize(("read", "line"), [
    (OSError("refused"), "ABORT: could not read the cloud engine's version"),
    ((0, 0, 9), "ABORT: the cloud engine is behind v7.93.1's floor"),
], ids=["unreadable", "behind"])
def test_the_engine_gate_names_why_it_refused(crs, monkeypatch, tmp_path, read, line: str) -> None:
    monkeypatch.setattr(crs, "LOG", tmp_path / "restart.log")
    monkeypatch.setattr(crs, "release_engine_floor", lambda _v, _r: (0, 1, 0))

    def cloud(_ep: str) -> tuple[int, ...]:
        if isinstance(read, Exception):
            raise read
        return read

    monkeypatch.setattr(crs, "cloud_engine", cloud)
    assert crs.converge("7.93.1", tmp_path, "http://127.0.0.1:1", gate=True, dry=False, engine_wait=0) is False
    assert line in (tmp_path / "restart.log").read_text(encoding="utf-8")
