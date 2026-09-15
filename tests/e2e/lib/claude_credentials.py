#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared Claude Code OAuth credential picker (nexus-galkv.19).

THE DEFECT THIS CLOSES. Three scripts fetched the Claude Code OAuth
credential with a bare ``security find-generic-password -s 'Claude
Code-credentials' -w`` (no ``-a``): ``tests/e2e/auth-login.sh`` (which then
WRITES the result to ``tests/e2e/.claude-auth/.credentials.json``), and both
the ``--fullstack`` and ``--shakeout-e2e`` legs of
``tests/e2e/migration-rehearsal/run.sh`` (which mount it into a container).
More than one keychain item can carry that service name — on this box an
``acct="unknown"`` item is an empty husk (``accessToken ""``,
``refreshToken ""``, ``expiresAt 0``) sitting alongside the live
``acct=<login user>`` item the CLI actually refreshes — and the bare,
unscoped lookup returns an ARBITRARY match, which on 2026-09-15 was the
husk: ``auth-login.sh`` overwrote its own snapshot with it and interactive
Claude Code showed "Not logged in", while ``claude -p`` failed with "OAuth
session expired and could not be refreshed".

``tests/cc-validation/runner.sh``'s ``_cred_tool`` (nexus-qs1g6, 2026-08-28;
see ``tests/cc-validation/README.md`` § "Auth" /
"More than one keychain item carries the service name") already solved
this by choosing the credential by CONTENT rather than trusting the first
match: enumerate every account under the service (attribute-only
``security dump-keychain``, no secret read, no unlock prompt), fetch each
with ``-a``, reject any item that carries no usable token, and take the
freshest survivor. That fix was never shared with the two callers above —
this module is the one shared home, so a fourth caller finds it here too
instead of re-deriving it a third time.

Modes (argv[1]):
  pick        -- print the freshest usable keychain credential JSON on
                 stdout, exit 0. Exit 1 with a reason on stderr if no
                 keychain item under the service is usable (or `security`
                 is unavailable — non-macOS, missing binary).
  check FILE  -- exit 0 iff FILE holds JSON that `verdict()` below judges
                 usable. Exit 1 with a reason on stderr otherwise. Never
                 writes anything.

Diagnostic ``[auth] ...`` lines go to stderr only; stdout carries the
credential JSON (mode ``pick``) and nothing else, so a caller can safely
capture it via command substitution without capturing diagnostics too, and
a credential is never accidentally echoed into a log via stderr.

`verdict()` and `pick_usable_credential()` are importable directly for unit
tests — see ``tests/test_claude_credentials.py``.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time

SERVICE = "Claude Code-credentials"


def verdict(data: dict | None) -> tuple[bool, str]:
    """(ok, reason). Usable == carries a token we can authenticate or
    refresh with."""
    oauth = (data or {}).get("claudeAiOauth") or {}
    access = oauth.get("accessToken") or ""
    refresh = oauth.get("refreshToken") or ""
    if not access and not refresh:
        return False, "empty husk — accessToken and refreshToken are both blank"
    expires = oauth.get("expiresAt") or 0
    if expires and expires <= int(time.time() * 1000) and not refresh:
        return False, "accessToken expired and no refreshToken to renew it"
    return True, ""


def expiry(data: dict | None) -> int:
    return ((data or {}).get("claudeAiOauth") or {}).get("expiresAt") or 0


def _fetch(acct: str | None) -> dict | None:
    cmd = ["security", "find-generic-password", "-s", SERVICE]
    if acct is not None:
        cmd += ["-a", acct]
    proc = subprocess.run(cmd + ["-w"], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except Exception:
        return None


def _accounts() -> list[str]:
    """Enumerate keychain accounts under SERVICE via an ATTRIBUTE-ONLY dump
    (no ``-d``, so no secret is read and no unlock prompt fires)."""
    accounts: list[str] = []
    block: list[str] = []
    dump = subprocess.run(["security", "dump-keychain"], capture_output=True, text=True).stdout
    for line in dump.splitlines() + ["keychain: <eof>"]:
        if line.startswith("keychain: "):
            text = "\n".join(block)
            svce = re.search(r'"svce"<blob>="([^"]*)"', text)
            acct = re.search(r'"acct"<blob>="([^"]*)"', text)
            if svce and acct and svce.group(1) == SERVICE:
                accounts.append(acct.group(1))
            block = [line]
        else:
            block.append(line)
    return accounts


def pick_usable_credential() -> dict | None:
    """The freshest usable credential across every keychain account under
    SERVICE, or None if none is usable (including when `security` itself
    is unavailable). Emits ``[auth]`` progress/skip lines to stderr;
    NEVER prints a credential to stderr."""
    try:
        accounts = _accounts()
    except FileNotFoundError:
        print("[auth] `security` not found — not macOS or no Keychain access", file=sys.stderr)
        return None
    usable: list[tuple[int, str, dict]] = []
    seen: set[str | None] = set()
    for acct in accounts + [None]:  # None == the old first-match form, tried last
        if acct in seen:
            continue
        seen.add(acct)
        payload = _fetch(acct)
        if payload is None:
            continue
        label = acct if acct is not None else "<first-match>"
        ok, why = verdict(payload)
        if ok:
            usable.append((expiry(payload), label, payload))
        else:
            print(f"[auth] skipping keychain item acct={label!r}: {why}", file=sys.stderr)
    if not usable:
        return None
    usable.sort(key=lambda row: row[0], reverse=True)
    exp, label, payload = usable[0]
    print(f"[auth] keychain item acct={label!r} selected (expiresAt={exp})", file=sys.stderr)
    return payload


def _cmd_pick() -> int:
    payload = pick_usable_credential()
    if payload is None:
        print("[auth] no usable keychain credential found", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(payload))
    return 0


def _cmd_check(path: str) -> int:
    try:
        with open(path) as fh:
            payload = json.load(fh)
    except Exception as exc:
        print(f"[auth] unreadable: {exc}", file=sys.stderr)
        return 1
    ok, why = verdict(payload)
    if not ok:
        print(f"[auth] {why}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: claude_credentials.py pick | check FILE", file=sys.stderr)
        return 2
    mode = argv[1]
    if mode == "pick":
        return _cmd_pick()
    if mode == "check":
        if len(argv) < 3:
            print("usage: claude_credentials.py check FILE", file=sys.stderr)
            return 2
        return _cmd_check(argv[2])
    print(f"unknown mode: {mode!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
