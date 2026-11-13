# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ``slash`` scenario of the restart sandbox: what Claude Code's own ``/restart`` does to a session.

claude-restart-sessions restarts a session by exiting it and typing ``claude <its flags> --resume <id>``
back into its pane. Claude Code 2.1.288 added ``/restart`` ("Restart Claude Code and resume this session,
which applies an installed update"; alias ``/update``). One use on the operator's box (2.1.322 -> 2.1.323,
2026-11-13) kept the process id, its argv, the session id and the pane. The docs leave open what the
script would rely on, so this scenario measures it before the script uses it. Two sessions are told a
code word; ``rs_clear`` then runs ``/clear`` and is told a second word, so its current session id is not
the one on its argv. Each is sent ``/restart`` at an empty prompt. The scenario fails unless every one:

- restarts with no newer Claude Code to install (the sandbox runs the operator's installed binary, so no
  update is pending): a new pid, or the same pid with a later ``startedAt`` in its session file;
- comes back as its current session id (rs_clear: the id after ``/clear``, not the one on its argv), in
  the same tmux pane, idle;
- keeps what its launch flags did: the model, bypass permissions (relaunched as
  ``--allow-dangerously-skip-permissions --permission-mode bypassPermissions``), its name (``-n`` is dropped
  from the argv; the session keeps the name) and ``--channels`` where it had it;
- restarts its MCP servers (no child pid of the old process survives as a child of the new one), which
  is what a plugin update needs;
- still answers its current code word and no other (rs_clear: the word told after ``/clear``).

Before the restart, rs_alpha is given an unsent draft and one Ctrl-C, which must leave an empty prompt on the
same, idle process: ``/restart`` typed after a draft would be sent with it, so a script using it clears the
prompt first. The scenario notes whether the draft is back after the restart (2026-11-13, 2.1.323: it is not,
so the draft has to be saved before the clear).

Opt-in (``--scenario slash``), not part of the full pass the script's gate records: it measures Claude
Code, not the script, and adding it to the pass would change that gate's criteria. Not exercised: a
``/restart`` that installs a pending update (the sandbox cannot stage an older binary), a session in WSL
or on a plan without the command (the docs exclude both), and the SessionStart ``source`` value.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

#: The word rs_clear is told after its /clear; its pre-clear word must be gone after the restart.
CLEARED_WORD = "QUARTZITE"
#: How long a session may take to come back from /restart.
RESTART_WAIT_S = 180
#: An unsent draft left in rs_alpha's prompt. A restart through /restart has to clear the prompt first
#: (``/restart`` typed after a draft would be sent as part of it); one Ctrl-C is the clear measured here.
DRAFT = "an unsent draft the restart must clear first"


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _prompt(sb, name: str) -> str:
    """The text on the pane's prompt line (after the last ``❯``), or "" when it is empty."""
    lines = [ln for ln in _plain(sb.capture(name)).splitlines() if ln.lstrip().startswith("❯")]
    return lines[-1].lstrip()[1:].strip() if lines else ""


def _argv(pid: int) -> str:
    r = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True)
    return r.stdout.strip()


def _children(pid: int) -> set[int]:
    r = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
    return {int(x) for x in r.stdout.split() if x.isdigit()}


def _type(sb, name: str, text: str) -> None:
    sb.tmux("send-keys", "-t", sb.pane_of(name), "-l", text, check=True)
    time.sleep(1)
    sb.tmux("send-keys", "-t", sb.pane_of(name), "Enter", check=True)


def _snapshot(sb, name: str) -> dict:
    d = dict(sb.session(name) or {})
    pid = int(d.get("pid", 0) or 0)
    d["_argv"] = _argv(pid) if pid else ""
    d["_children"] = _children(pid) if pid else set()
    return d


def _restarted(was: dict, now: dict | None) -> bool:
    if not now or now.get("status") != "idle":
        return False
    if now.get("pid") != was.get("pid"):
        return True
    return (now.get("startedAt") or 0) > (was.get("startedAt") or 0)


def scenario_slash(sb, crs, a, failures: list[str], notes: list[str], *, boot) -> None:
    spec = sb.spec
    # Sandbox.setup gives every scenario a native install's layout (native_layout), which /restart needs.
    notes.append(f"native layout: {sb.claude} -> {os.path.realpath(sb.claude)}")
    before = boot(sb, failures)
    sb.before = before
    if failures:
        return
    first = str(before["rs_clear"].get("sessionId", ""))
    _type(sb, "rs_clear", "/clear")
    sb.wait_status("rs_clear", {"idle"}, 120, not_sid=first)
    sb.prompt("rs_clear", f"For this task, the project code word is {CLEARED_WORD}. Reply with only: OK")
    words = dict(spec.words, rs_clear=CLEARED_WORD)
    was = {name: _snapshot(sb, name) for name in spec.words}
    if str(was["rs_clear"].get("sessionId", "")) == first:
        failures.append("precondition: rs_clear's session id did not change at /clear")
        return
    if first not in was["rs_clear"]["_argv"]:
        notes.append(f"rs_clear's argv does not carry its first id {first[:8]} ({was['rs_clear']['_argv'][:160]})")
    for name in spec.words:
        if not was[name]["_children"]:
            failures.append(f"precondition: {name} (pid {was[name].get('pid')}) has no child process to compare")
    if failures:
        return

    # The draft: one Ctrl-C must clear it and leave the session running and idle, so the script can save
    # the draft, clear the prompt, and type /restart.
    pid = was["rs_alpha"].get("pid")
    sb.tmux("send-keys", "-t", sb.pane_of("rs_alpha"), "-l", DRAFT, check=True)
    time.sleep(1.5)
    if DRAFT not in _prompt(sb, "rs_alpha"):
        failures.append(f"precondition: the draft did not show on rs_alpha's prompt ({_prompt(sb, 'rs_alpha')!r})")
        return
    sb.tmux("send-keys", "-t", sb.pane_of("rs_alpha"), "C-c", check=True)
    time.sleep(2)
    left, now = _prompt(sb, "rs_alpha"), sb.session("rs_alpha") or {}
    if left or now.get("pid") != pid or now.get("status") != "idle":
        failures.append(f"rs_alpha: one Ctrl-C on a draft did not leave an empty prompt on a running, idle "
                        f"session (prompt {left!r}, pid {pid} -> {now.get('pid')}, status {now.get('status')})")
        return
    notes.append("rs_alpha: one Ctrl-C cleared the draft; same pid, idle")

    t0 = time.time()
    for name in spec.words:
        _type(sb, name, "/restart")
    for name in spec.words:
        old = was[name]
        deadline = time.time() + RESTART_WAIT_S
        now = sb.session(name)
        while time.time() < deadline and not _restarted(old, now):
            time.sleep(2)
            now = sb.session(name)
        if not _restarted(old, now):
            tail = "\n".join(sb.capture(name).rstrip().splitlines()[-12:])
            failures.append(f"{name}: no restart within {RESTART_WAIT_S}s of /restart (pid {old.get('pid')}, "
                            f"startedAt {old.get('startedAt')}; now {now and now.get('pid')}, "
                            f"{now and now.get('startedAt')}, {now and now.get('status')}); pane:\n{tail}")
            continue
        new = _snapshot(sb, name)
        how = "same pid" if new.get("pid") == old.get("pid") else f"new pid {new.get('pid')}"
        notes.append(f"{name}: restarted after {time.time() - t0:.0f}s, {how}, version "
                     f"{old.get('version')} -> {new.get('version')}")
        if new.get("sessionId") != old.get("sessionId"):
            failures.append(f"{name}: came back as session {new.get('sessionId')}, not its current "
                            f"{old.get('sessionId')}" + (f" (its first id was {first})" if name == "rs_clear" else ""))
        if str(new.get("tmux", "")) != str(old.get("tmux", "")):
            failures.append(f"{name}: pane {old.get('tmux')} -> {new.get('tmux')}")
        # /restart rewrites the launch line (2026-11-13, 2.1.323): --dangerously-skip-permissions comes back as
        # --allow-dangerously-skip-permissions --permission-mode bypassPermissions, and -n is dropped while the
        # session keeps its name. So check what each flag does, not its spelling.
        argv = new["_argv"]
        lost = [] if f"--model {sb.model}" in argv else ["model"]
        if not ("--dangerously-skip-permissions" in argv or "--permission-mode bypassPermissions" in argv):
            lost.append("bypass permissions")
        if new.get("name") != name:
            lost.append(f"session name (now {new.get('name')!r})")
        if name in spec.channel and "--channels plugin:conexus@nexus-plugins" not in argv:
            lost.append("channel")
        if lost:
            failures.append(f"{name}: lost across /restart: {lost} (argv {argv[:300]})")
        if new["_argv"] != old["_argv"]:
            notes.append(f"{name}: argv changed: {old['_argv']} -> {new['_argv']}")
        # An argv without the flag does not settle it: the mode and name may come back from the session.
        if name == "rs_alpha":
            notes.append(f"rs_alpha: prompt after /restart {_prompt(sb, name)!r} (the cleared draft "
                         + ("came back)" if DRAFT in _prompt(sb, name) else "did not come back)"))
        foot = " | ".join(ln.strip() for ln in re.sub(r"\x1b\[[0-9;]*m", "", sb.capture(name)).splitlines() if "permissions" in ln or name in ln)
        notes.append(f"{name}: pane after /restart: {foot[-300:]}; session name {new.get('name')!r}")
        kept = old["_children"] & new["_children"]
        if kept or not new["_children"]:
            failures.append(f"{name}: MCP servers not restarted (children before {sorted(old['_children'])}, "
                            f"after {sorted(new['_children'])})")
        reply = sb.prompt(name, "What is the project code word for this task? Reply with only the word.").upper()
        others = [w for n, w in words.items() if n != name] + (["MARZIPAN"] if name == "rs_clear" else [])
        if words[name] not in reply or any(w in reply for w in others):
            failures.append(f"{name}: after /restart it answered {reply[:80]!r}, not its word {words[name]}")
