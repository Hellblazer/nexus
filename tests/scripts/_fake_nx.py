# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stateful fake ``nx`` for the develop-freeze tests (nexus-eusu6).

The freeze lives on the live tuple space, and these tests must never touch
it, so an ``nx`` stub goes first on PATH. It models only what the freeze
scripts call: ``tuple out|rd|in|release`` plus the two scope-label reads.
Posts to ``board/develop-freeze`` are kept in a JSONL file next to the stub,
newest by ``created_at`` decides, exactly as the engine's ``rd --newest``
would return them.

Knobs (environment of the process under test):
  FAKE_NX_DIR        directory holding rows.jsonl and calls.log (required)
  FAKE_NX_FAIL_RD    non-empty: ``tuple rd board/...`` exits 1 (board unreadable)
  FAKE_NX_FAIL_OUT   non-empty: ``tuple out board/...`` exits 1
  FAKE_NX_RD_STDERR  text: ``tuple rd board/...`` prints it on stderr and still succeeds
  FAKE_NX_RD_SLEEP   seconds: ``tuple rd board/...`` sleeps first (a hung tuple space)

A board ``tuple out`` whose body exceeds 1024 UTF-8 bytes is refused, as the
real board template's max_body_bytes does (TooLarge).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_STUB = r'''#!/usr/bin/env python3
import json, os, sys, time, uuid

d = os.environ["FAKE_NX_DIR"]
args = sys.argv[1:]
with open(os.path.join(d, "calls.log"), "a") as f:
    f.write(" ".join(args) + "\n")
rows_path = os.path.join(d, "rows.jsonl")

def rows():
    if not os.path.exists(rows_path):
        return []
    with open(rows_path) as f:
        return [json.loads(l) for l in f if l.strip()]

def opts(rest):
    out = {"key": {}, "dim": {}}
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("--key", "--dim"):
            k, _, v = rest[i + 1].partition("=")
            out[a[2:]][k] = v
            i += 2
        elif a in ("--body", "--nonce", "-n", "--claimant", "--lease-s", "--timeout-s", "--pattern", "--ttl-seconds"):
            out[a] = rest[i + 1]
            i += 2
        else:
            out[a] = True
            i += 1
    return out

if args[:2] == ["tuple", "out"]:
    sub, o = args[2], opts(args[3:])
    if sub.startswith("board/"):
        if os.environ.get("FAKE_NX_FAIL_OUT"):
            print("fake-nx: board out refused", file=sys.stderr)
            sys.exit(1)
        if len((o.get("--body") or "").encode("utf-8")) > 1024:
            print("TooLarge: field 'body' exceeds the limit of 1024 bytes", file=sys.stderr)
            sys.exit(1)
        row = {"id": uuid.uuid4().hex, "subspace": sub, "keys": o["key"], "dims": o["dim"],
               "body": o.get("--body"), "nonce": o.get("--nonce"),
               # ONE clock read for both halves: seconds from gmtime() and
               # microseconds from a later time_ns() straddled a second boundary
               # in CI, so a clear posted after a set sorted as OLDER than it.
               "created_at": (lambda t: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t // 10**9))
                              + ".%06dZ" % (t // 1000 % 1000000))(time.time_ns())}
        with open(rows_path, "a") as f:
            f.write(json.dumps(row) + "\n")
        print(row["id"])
    else:
        print("ab" * 32)
elif args[:2] == ["tuple", "rd"]:
    sub, o = args[2], opts(args[3:])
    if sub.startswith("board/"):
        if os.environ.get("FAKE_NX_FAIL_RD"):
            print("fake-nx: tuple space unreachable", file=sys.stderr)
            sys.exit(1)
        if os.environ.get("FAKE_NX_RD_SLEEP"):
            time.sleep(float(os.environ["FAKE_NX_RD_SLEEP"]))
        if os.environ.get("FAKE_NX_RD_STDERR"):
            print(os.environ["FAKE_NX_RD_STDERR"], file=sys.stderr)
        rs = sorted((r for r in rows() if r["subspace"] == sub), key=lambda r: r["created_at"])
        n = int(o.get("-n", 1))
        rs = rs[::-1][:n] if o.get("--newest") else rs[:n]
        print(json.dumps(rs))
    else:
        print("[]")
elif args[:2] == ["tuple", "in"]:
    print(json.dumps({"claim_id": "fakeclaim", "tuple": {}}))
elif args[:2] == ["tuple", "release"]:
    print("released")
elif args[:2] == ["config", "get"]:
    print("not set")
elif args[:2] == ["daemon", "service"]:
    print("no lease", file=sys.stderr)
    sys.exit(1)
else:
    print("fake-nx: unhandled invocation: %s" % " ".join(args), file=sys.stderr)
    sys.exit(1)
'''


def install_fake_nx(tmp_path: Path) -> tuple[Path, Path]:
    """Write the stub as ``<tmp>/fake-nx-bin/nx``. Returns (bin dir, state dir)."""
    bindir = tmp_path / "fake-nx-bin"
    bindir.mkdir(exist_ok=True)
    state = tmp_path / "fake-nx-state"
    state.mkdir(exist_ok=True)
    nx = bindir / "nx"
    nx.write_text(_STUB)
    nx.chmod(0o755)
    return bindir, state


def fake_env(bindir: Path, state: Path, base: dict | None = None) -> dict:
    env = {**(base if base is not None else os.environ)}
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_NX_DIR"] = str(state)
    return env


def post(state: Path, *, body: dict | str, created_at: str | None = None,
         subspace: str = "board/develop-freeze") -> None:
    """Seed a board post. ``created_at`` defaults to now; pass an explicit
    ISO string (the engine's own shape) to fix its age."""
    import datetime
    row = {
        "id": os.urandom(8).hex(), "subspace": subspace,
        "keys": {"topic": subspace.split("/", 1)[1]}, "dims": {"from": "test"},
        "body": body if isinstance(body, str) else json.dumps(body),
        "created_at": created_at or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    with open(state / "rows.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")


def calls(state: Path) -> str:
    p = state / "calls.log"
    return p.read_text() if p.exists() else ""


def run(cmd: list[str], *, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
