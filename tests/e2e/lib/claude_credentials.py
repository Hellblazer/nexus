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
  run [--remote HOST] -- <command> [args...]
                 (RDR-219 P1.1.) Reads the harness's OWN automation token
                 from the keychain item ``nexus-automation-oauth-token``
                 (account ``$USER`` — never ``Claude Code-credentials``,
                 never the operator's interactive login) and execs
                 <command> with ``CLAUDE_CODE_OAUTH_TOKEN`` set in its
                 environment. Exit non-zero naming ``claude setup-token``
                 and the keychain item if the token is absent or expired;
                 the child is never started in that case. Prints nothing
                 else. With --remote, ships <command> and the token to
                 HOST over ssh: the token travels only on the ssh
                 channel's stdin, never on argv and never in the local
                 ssh process's own environment. The REMOTE side of
                 <command> is responsible for reading that first stdin
                 line (``IFS= read -r CLAUDE_CODE_OAUTH_TOKEN; export
                 CLAUDE_CODE_OAUTH_TOKEN``) before doing its real work —
                 this helper does not synthesize a remote wrapper script,
                 by design: RDR-219's Phase 0 spike proved the shape
                 caller-side (``wsl-run.sh``, T2
                 ``nexus_rdr/219-spike-script``), the qwentescence ssh
                 endpoint is PowerShell rather than a POSIX shell so a
                 single generic wrapper cannot cover every remote target,
                 and a caller-owned remote script keeps this helper
                 platform-agnostic (the RDR-219 Phase 1 Step 1 residual:
                 "encoded in the helper, or passed by the caller" —
                 resolved as the latter).
                 A bare ``docker run`` in <command> gets ``--rm`` forced
                 in if not already present (RDR-219 Phase 0 code review
                 requirement 3, T2 ``nexus_rdr/219-review-p0-code``): a
                 container holding the token in its environment must not
                 outlive the run, where ``docker inspect`` could still
                 read it.
                 ``ANTHROPIC_API_KEY``, when present in the caller's own
                 environment, is removed from the child's environment by
                 default — Claude Code's documented authentication
                 precedence puts ``ANTHROPIC_API_KEY`` above
                 ``CLAUDE_CODE_OAUTH_TOKEN``, so a leftover API key would
                 silently steer a harness run onto API billing instead of
                 the automation token. Set
                 ``CLAUDE_CREDENTIALS_KEEP_ANTHROPIC_API_KEY=1`` to opt
                 back in explicitly.
  status         -- print whether the automation token is present, its
                 creation date and days to expiry (creation date + 365
                 days), with a warning line at 30 days or fewer. Never
                 prints token material — it never asks `security` for the
                 secret itself, only its attributes. Exit 0 present and
                 unexpired, 1 absent, 2 expired.

Diagnostic ``[auth] ...`` lines go to stderr only; stdout carries the
credential JSON (mode ``pick``) and nothing else, so a caller can safely
capture it via command substitution without capturing diagnostics too, and
a credential is never accidentally echoed into a log via stderr. ``run``
and ``status`` print no credential material on any path, by construction:
``run`` prints only a named error (or nothing, on success, before the exec
that replaces the process), and ``status`` never issues the keychain read
(`-w`) that would return the secret at all.

`verdict()` and `pick_usable_credential()` are importable directly for unit
tests — see ``tests/test_claude_credentials.py``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SERVICE = "Claude Code-credentials"

#: RDR-219: the harness's own automation identity, never the operator's
#: interactive login. Created once with `claude setup-token` and stored
#: under the invoking user's own account.
AUTOMATION_SERVICE = "nexus-automation-oauth-token"

#: The environment variable `run` sets for the child process.
TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

#: `claude setup-token` mints a one-year token (RDR-219 Technical Design).
TOKEN_TTL_DAYS = 365

#: `status` (and the doctor/battery leg that will call it) warns inside
#: this many days of expiry.
TOKEN_WARN_DAYS = 30

#: Set to "1" to keep a caller's ANTHROPIC_API_KEY in the child environment
#: `run` builds. Unset by default: Claude Code's documented authentication
#: precedence ranks ANTHROPIC_API_KEY above CLAUDE_CODE_OAUTH_TOKEN, so a
#: leftover key would silently move the child off the automation token and
#: onto API billing.
KEEP_ANTHROPIC_API_KEY_ENV = "CLAUDE_CREDENTIALS_KEEP_ANTHROPIC_API_KEY"

_CDAT_RE = re.compile(r'"cdat"<timedate>=0x[0-9A-Fa-f]+\s+"(\d{14})Z')


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


# ===========================================================================
# RDR-219 P1.1: the automation identity (`run`, `status`)
# ===========================================================================


@dataclass(frozen=True)
class AutomationTokenStatus:
    present: bool
    created: "datetime | None"
    days_left: "int | None"


def _automation_account() -> str:
    """The keychain account the automation token is stored under —
    always the invoking user's own account, never a fixed name."""
    import getpass

    return os.environ.get("USER") or getpass.getuser()


def _parse_cdat(attrs_text: str) -> "datetime | None":
    match = _CDAT_RE.search(attrs_text)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def automation_token_status(
    account: str | None = None, now: "datetime | None" = None
) -> AutomationTokenStatus:
    """Present/creation-date/days-to-expiry for the automation token,
    without ever reading the secret itself — this calls `security
    find-generic-password` WITHOUT `-w`, so it is structurally incapable
    of returning token material."""
    account = account or _automation_account()
    now = now or datetime.now(timezone.utc)
    proc = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", AUTOMATION_SERVICE],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return AutomationTokenStatus(present=False, created=None, days_left=None)
    created = _parse_cdat(proc.stdout)
    if created is None:
        return AutomationTokenStatus(present=True, created=None, days_left=None)
    expires = created + timedelta(days=TOKEN_TTL_DAYS)
    days_left = (expires - now).days
    return AutomationTokenStatus(present=True, created=created, days_left=days_left)


def fetch_automation_token(account: str | None = None) -> "str | None":
    """The automation token's actual value, or None if absent/unreadable.
    Only `run` calls this — `status` never does."""
    account = account or _automation_account()
    proc = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", AUTOMATION_SERVICE, "-w"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    token = proc.stdout.strip()
    return token or None


def _remedy(account: str) -> str:
    return (
        f"run `claude setup-token` and store the result in keychain item "
        f"{AUTOMATION_SERVICE!r} (account {account!r})"
    )


def _ensure_docker_rm(command: list[str]) -> list[str]:
    """If `command` invokes `docker run` (any leading docker flags before
    `run` are tolerated), force `--rm` in when absent. A container that
    holds the automation token in its environment must not outlive the
    run: `docker inspect` can still read it while it exists (RDR-219
    Phase 0 code review requirement 3)."""
    if not command:
        return command
    prog = command[0]
    if prog != "docker" and not prog.endswith("/docker"):
        return command
    try:
        run_idx = command.index("run", 1)
    except ValueError:
        return command
    if "--rm" in command[run_idx:]:
        return command
    return command[: run_idx + 1] + ["--rm"] + command[run_idx + 1 :]


def _child_env(token: str) -> dict[str, str]:
    env = dict(os.environ)
    if "ANTHROPIC_API_KEY" in env and os.environ.get(KEEP_ANTHROPIC_API_KEY_ENV) != "1":
        del env["ANTHROPIC_API_KEY"]
    env[TOKEN_ENV_VAR] = token
    return env


def _exec(command: list[str], env: dict[str, str]) -> int:
    """Replaces this process with `command`, `env` and all — the token
    lives only in `env`, never as a `command` element, so it can never
    land on the child's own argv. Does not return on success; the `127`
    below is unreachable and exists only so callers (and mypy) see an
    int."""
    os.execvpe(command[0], command, env)
    return 127  # pragma: no cover — os.execvpe never returns on success


def _run_remote(host: str, command: list[str], token: str) -> int:
    """Ships `command` to `host` over ssh; the token travels ONLY on the
    ssh channel's stdin, never on ssh's own argv and never in the local
    ssh process's environment. The remote side of `command` is the
    caller's responsibility for reading it (see the module docstring)."""
    proc = subprocess.run(["ssh", host] + command, input=token + "\n", text=True)
    return proc.returncode


def _cmd_run(command: list[str], remote: "str | None" = None) -> int:
    account = _automation_account()
    status = automation_token_status(account)
    if not status.present:
        print(f"[auth] automation token absent -- {_remedy(account)}", file=sys.stderr)
        return 1
    if status.days_left is not None and status.days_left < 0:
        print(
            f"[auth] automation token expired {-status.days_left} day(s) ago -- "
            f"{_remedy(account)}",
            file=sys.stderr,
        )
        return 1
    token = fetch_automation_token(account)
    if not token:
        print(f"[auth] automation token unreadable -- {_remedy(account)}", file=sys.stderr)
        return 1
    command = _ensure_docker_rm(command)
    if remote:
        return _run_remote(remote, command, token)
    return _exec(command, _child_env(token))


def _cmd_status(account: "str | None" = None, now: "datetime | None" = None) -> int:
    account = account or _automation_account()
    status = automation_token_status(account, now=now)
    if not status.present:
        print(f"absent -- {_remedy(account)}")
        return 1
    if status.created is None:
        print(f"present (account {account!r}) -- creation date unreadable")
        return 0
    created_str = status.created.date().isoformat()
    if status.days_left is not None and status.days_left < 0:
        print(
            f"expired -- created {created_str}, {-status.days_left} day(s) ago "
            f"(account {account!r})"
        )
        return 2
    print(
        f"present -- created {created_str}, {status.days_left} day(s) to expiry "
        f"(account {account!r})"
    )
    if status.days_left is not None and status.days_left <= TOKEN_WARN_DAYS:
        print(
            f"warning: automation token expires in {status.days_left} day(s) -- "
            "run `claude setup-token` to renew"
        )
    return 0


def _parse_run_args(argv: list[str]) -> "tuple[str | None, list[str]] | None":
    """Parses the argv AFTER `run`: `[--remote HOST] -- <command> [args...]`.
    Returns (remote_host_or_None, command), or None if malformed (no `--`,
    an empty command, or `--remote` with no host)."""
    remote: "str | None" = None
    i = 0
    while i < len(argv):
        if argv[i] == "--remote":
            if i + 1 >= len(argv):
                return None
            remote = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--":
            command = argv[i + 1 :]
            if not command:
                return None
            return remote, command
        return None
    return None


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: claude_credentials.py pick | check FILE | "
            "run [--remote HOST] -- <command> [args...] | status",
            file=sys.stderr,
        )
        return 2
    mode = argv[1]
    if mode == "pick":
        return _cmd_pick()
    if mode == "check":
        if len(argv) < 3:
            print("usage: claude_credentials.py check FILE", file=sys.stderr)
            return 2
        return _cmd_check(argv[2])
    if mode == "run":
        parsed = _parse_run_args(argv[2:])
        if parsed is None:
            print(
                "usage: claude_credentials.py run [--remote HOST] -- <command> [args...]",
                file=sys.stderr,
            )
            return 2
        remote, command = parsed
        return _cmd_run(command, remote=remote)
    if mode == "status":
        return _cmd_status()
    print(f"unknown mode: {mode!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
