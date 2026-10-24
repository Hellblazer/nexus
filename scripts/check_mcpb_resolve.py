#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Pre-tag resolve gate for the Claude Desktop .mcpb bundle (nexus-2k66t.4.6).

Resolve only, nothing installed: does the bundle's pyproject, with the staged
release lock written into it exactly as the launcher writes it, RESOLVE?

The release workflow stages ``constraints.txt`` into the bundle and the launcher
(``mcpb/src/bootstrap.py``) turns it into ``[tool.uv] constraint-dependencies``
before ``uv sync``. Until now nothing ran that resolution before publish: the
tests parse the TOML, they do not resolve it, and a lock whose pins cannot
satisfy conexus's own dependency floors only showed up on a Desktop user's
machine. This is that resolution, once, in CI: a scratch copy of ``mcpb/`` is
staged with ``scripts/stage_mcpb_constraints.py`` (the ``conexus==<release>``
line included), pinned with the launcher's own ``_apply_pin``, conexus is
pointed at the checkout (or at the freshly built wheel with ``--conexus``: the
release is not on PyPI before the tag), and ``uv lock`` runs. The produced
``uv.lock`` is then read back: conexus must be locked at the release version,
at least ``MIN_LOCKED`` packages must be present, and every unconditional pin
whose package was locked must be locked at the pinned version.

    python3 scripts/check_mcpb_resolve.py [--conexus PATH] [--conexus-version X.Y.Z] [--uv UV]

Needs the network (package index metadata). Prints one verdict line, either
``MCPB RESOLVE GATE PASSED ...`` or ``MCPB RESOLVE GATE FAILED ...``, and exits
0 only for the first; the counts it examined are in the line, so a pass over
nothing cannot read as a pass.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUNDLE = REPO / "mcpb"
STAGER = REPO / "scripts" / "stage_mcpb_constraints.py"

#: A resolve of bare conexus locks well over this many packages (measured on the
#: first live run, recorded in the bead); a lock that came back near-empty is a failure.
MIN_LOCKED = 100
#: ... and at least this many of the staged pins must be the locked version of a locked package.
MIN_HELD = 80
UV_TIMEOUT_S = 900

_IGNORE = shutil.ignore_patterns(".venv", "uv.lock", "__pycache__", "*.pyc", "constraints.txt", "*.mcpb")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _launcher():
    spec = importlib.util.spec_from_file_location("mcpb_bootstrap_gate", BUNDLE / "src" / "bootstrap.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fail(msg: str) -> int:
    print(f"MCPB RESOLVE GATE FAILED: {msg}")
    return 1


def run(args: argparse.Namespace) -> int:
    scratch = Path(tempfile.mkdtemp(prefix="nx-mcpb-resolve-"))
    try:
        return _run(args, scratch)
    finally:
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)
        else:
            print(f"kept scratch bundle: {scratch}")


def _run(args: argparse.Namespace, scratch: Path) -> int:
    bundle = scratch / "bundle"
    shutil.copytree(BUNDLE, bundle, ignore=_IGNORE)

    stage = [sys.executable, "-I", str(STAGER), "--dest", str(bundle)]
    if args.conexus_version:
        stage += ["--conexus-version", args.conexus_version]
    staged = subprocess.run(stage, capture_output=True, text=True, encoding="utf-8", timeout=120)
    if staged.returncode != 0:
        return _fail(f"staging the lock: {staged.stderr.strip() or staged.stdout.strip()}")
    sys.stdout.write(staged.stdout)

    launcher = _launcher()
    line = launcher._apply_pin(str(bundle), environ={})
    print(line)
    if "pinned to the release's lock" not in line:
        return _fail("the launcher did not pin the staged lock into the bundle's pyproject")
    pins, _ = launcher._read_constraints(str(bundle / "constraints.txt"))
    conexus_pin = [p for p in pins if p.startswith("conexus==")]
    if len(conexus_pin) != 1:
        return _fail("the staged lock does not carry exactly one conexus pin")
    release = conexus_pin[0].split("==", 1)[1]

    source = Path(args.conexus).resolve() if args.conexus else REPO
    if not source.exists():
        return _fail(f"--conexus {source} does not exist")
    pyproject = bundle / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    # conexus is not on the index before the tag: resolve it from where it is built.
    text += f'\n[tool.uv.sources]\nconexus = {{ path = {launcher._toml_string(str(source))} }}\n'
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return _fail(f"the scratch pyproject does not parse ({exc})")
    pyproject.write_text(text, encoding="utf-8")

    cmd = [args.uv, "lock", "--no-config", "--directory", str(bundle)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=UV_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _fail(f"could not run {args.uv} lock ({type(exc).__name__})")
    if proc.returncode != 0:
        sys.stdout.write(proc.stdout)
        sys.stdout.write(proc.stderr)
        return _fail(f"uv lock exited {proc.returncode}: the staged lock and the bundle's dependencies do not resolve together")

    try:
        lock = tomllib.loads((bundle / "uv.lock").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return _fail(f"uv wrote no readable uv.lock ({type(exc).__name__})")
    locked: dict[str, set[str]] = {}
    for pkg in lock.get("package", []):
        locked.setdefault(_norm(pkg["name"]), set()).add(str(pkg.get("version", "")))
    if release not in locked.get("conexus", set()):
        return _fail(f"conexus is not locked at {release} (locked: {sorted(locked.get('conexus', ()))})")
    if len(locked) < MIN_LOCKED:
        return _fail(f"only {len(locked)} packages locked (floor {MIN_LOCKED}); the resolve examined almost nothing")

    held, unheld, absent = 0, [], 0
    for pin in pins:
        if ";" in pin:
            continue  # conditional on a marker: the lock may carry other versions for other targets
        name, version = pin.split("==", 1)
        versions = locked.get(_norm(name))
        if versions is None:
            absent += 1
        elif version in versions:
            held += 1
        else:
            unheld.append(f"{name}=={version} (locked {sorted(versions)})")
    if unheld:
        return _fail(f"{len(unheld)} pins not honoured: {'; '.join(unheld[:5])}")
    if held < MIN_HELD:
        return _fail(f"only {held} pins held (floor {MIN_HELD}); the pins reached almost nothing")
    print(
        f"MCPB RESOLVE GATE PASSED: conexus {release}; {len(locked)} packages locked; "
        f"{held} of {len(pins)} pins held, {absent} pinned packages not needed by this bundle, 0 pins moved"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--conexus", default=None, help="conexus to resolve: a checkout directory or a built wheel (default: this checkout)")
    ap.add_argument("--conexus-version", default=None, help="the release to pin (default: the manifest's version)")
    ap.add_argument("--uv", default="uv", help="the uv executable")
    ap.add_argument("--keep", action="store_true", help="keep the scratch bundle and print where it is")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
