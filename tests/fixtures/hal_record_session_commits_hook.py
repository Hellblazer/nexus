#!/usr/bin/env python3
"""FIXTURE-OF-RECORD (tests/test_hal_git_policy_hook.py, nexus-9wxu6,
2026-09-07): checked-in COPY of the companion PostToolUse hook installed
at ``~/.claude/hooks/record_session_commits.py``. Same contract as
``hal_git_policy_hook.py``'s fixture: drift from the installed copy is
accepted; this file keeps the behaviour under CI.

--------------------------------------------------------------------------

Record every commit a Claude Code session makes, so the git-policy hook's
rule 3 can tell "my tip" from "a peer's tip" in the shared primary
checkout (nexus-9wxu6).

PostToolUse on Bash. When the command that just ran contains a
``git [-C dir] commit`` segment, append the resulting HEAD sha to
``<dir>/<session_id>`` where ``<dir>`` is ``~/.config/nexus/session_commits``
(override: ``NX_SESSION_COMMITS_DIR``). One sha per line, deduplicated.
Amends are recorded too: the rewritten tip is this session's own.

Prints nothing and always exits 0: a recorder must never fail a tool call.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
from typing import Any

_DEFAULT_DIR = pathlib.Path.home() / ".config" / "nexus" / "session_commits"
_SEGMENT_SPLIT_RE = r"(?:&&|\|\||;|\s\|\s|\bthen\b|\bdo\b)"
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _commit_dirs(command: str) -> list[str | None]:
    """The ``-C`` directory (or None) of every ``git commit`` segment."""
    out: list[str | None] = []
    for segment in re.split(_SEGMENT_SPLIT_RE, command):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            tokens = segment.replace('"', " ").replace("'", " ").split()
        i = 0
        while i < len(tokens) and _ENV_ASSIGN_RE.match(tokens[i]):
            i += 1
        tokens = tokens[i:]
        if len(tokens) < 2 or tokens[0] != "git":
            continue
        c_dir: str | None = None
        j = 1
        while j < len(tokens) and tokens[j].startswith("-"):
            if tokens[j] in {"-C", "-c"}:
                if tokens[j] == "-C" and j + 1 < len(tokens):
                    c_dir = tokens[j + 1]
                j += 2
            else:
                j += 1
        if j < len(tokens) and tokens[j] == "commit":
            out.append(c_dir)
    return out


_PRINTED_SHA_RE = re.compile(r"^\[[^\]\n]+ ([0-9a-f]{7,40})\]", re.MULTILINE)


def _printed_shas(payload: dict[str, Any]) -> list[str]:
    """The short shas ``git commit`` printed (``[develop abc1234] msg``) in
    the tool's own output. Preferred over a fresh ``rev-parse HEAD``: a peer
    commit landing between the command and this hook would otherwise be
    recorded as this session's."""
    resp = payload.get("tool_response")
    texts: list[str] = []
    if isinstance(resp, dict):
        for key in ("stdout", "stderr", "output"):
            v = resp.get(key)
            if isinstance(v, str):
                texts.append(v)
    elif isinstance(resp, str):
        texts.append(resp)
    return [m.group(1) for t in texts for m in _PRINTED_SHA_RE.finditer(t)]


def _resolve(cwd: str, rev: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", rev + "^{commit}"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def _head(cwd: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def record(payload: dict[str, Any]) -> list[str]:
    if payload.get("tool_name") != "Bash":
        return []
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else ""
    session_id = str(payload.get("session_id") or "")
    if not isinstance(command, str) or not command or not session_id:
        return []
    dirs = _commit_dirs(command)
    if not dirs:
        return []
    base = str(payload.get("cwd") or "") or os.getcwd()
    override = os.environ.get("NX_SESSION_COMMITS_DIR")
    store = pathlib.Path(override) if override else _DEFAULT_DIR
    path = store / session_id
    try:
        known = set(path.read_text(encoding="utf-8").split())
    except OSError:
        known = set()
    added: list[str] = []
    printed = _printed_shas(payload)
    for c_dir in dirs:
        cwd = base if c_dir is None else (c_dir if os.path.isabs(c_dir) else os.path.join(base, c_dir))
        shas = [full for short in printed if (full := _resolve(cwd, short))] or [_head(cwd)]
        for sha in shas:
            if sha and sha not in known:
                known.add(sha)
                added.append(sha)
    if added:
        store.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("".join(s + "\n" for s in added))
    return added


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if isinstance(payload, dict):
            record(payload)
    except BaseException:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
