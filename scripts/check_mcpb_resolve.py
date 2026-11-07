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

Both variants the extension can start as are resolved (nexus-dzc61): bare, and
with its ``include_pdf`` setting on, where the launcher's own ``_set_pdf``
rewrites the requirement to ``conexus[pdf]``. The PDF lock must also carry
``PDF_REQUIRED`` and at least ``MIN_LOCKED_PDF`` packages, and the bare lock none of
``PDF_REQUIRED`` (or the setting would decide nothing).

A universal ``uv lock`` needs only ONE compatible wheel per package, so it
cannot see a pinned version that has no wheel on one platform the extension
ships to (macOS arm64 at the macOS 14 floor, Windows x64, Linux x86_64 or
aarch64): that fails later, on the user's ``uv sync`` (nexus-tlt23). So each
variant is also resolved PER TARGET after the lock: ``uv pip compile
--python-platform`` of the bundle's own requirements (conexus pointed at the
same source) under the staged lock and the bundle's overrides, for every
``PLATFORMS`` x ``PYTHONS`` cell, in the shape tests/e2e/constraints-resolve-gate.sh
compiles the install path. A cell that does not resolve, resolves to almost
nothing, or loses the pdf stack (or gains it, bare) fails the gate by name.

    python3 scripts/check_mcpb_resolve.py [--conexus PATH] [--conexus-version X.Y.Z] [--uv UV]

Needs the network (package index metadata). Prints one verdict line, either
``MCPB RESOLVE GATE PASSED ...`` or ``MCPB RESOLVE GATE FAILED ...``, and exits
0 only for the first; the counts it examined are in the line, for each variant,
so a pass over nothing cannot read as a pass.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUNDLE = REPO / "mcpb"
STAGER = REPO / "scripts" / "stage_mcpb_constraints.py"

#: A resolve of bare conexus locks well over this many packages (measured on the
#: first live run, recorded in the bead); a lock that came back near-empty is a failure.
MIN_LOCKED = 100
#: ... and at least this many of the staged pins must be the locked version of a locked package.
MIN_HELD = 80
#: The pdf variant locks the PDF stack on top (MinerU, torch and theirs).
MIN_LOCKED_PDF = 170
PDF_REQUIRED = ("mineru", "torch")
UV_TIMEOUT_S = 900

#: Per-target cells (nexus-tlt23): (tag, ``uv --python-platform``, MACOSX_DEPLOYMENT_TARGET or None). macOS is
#: compiled at 14.0, the declared floor: uv's default for aarch64-apple-darwin is 13.0, where the onnxruntime
#: wheels end. The manifest ships darwin, linux and win32; macOS x64 is not a target (the product's floor is arm64).
PLATFORMS: tuple[tuple[str, str, str | None], ...] = (
    ("linux-x86_64", "x86_64-unknown-linux-gnu", None),
    ("linux-aarch64", "aarch64-unknown-linux-gnu", None),
    ("windows-x64", "x86_64-pc-windows-msvc", None),
    ("macos14-arm64", "aarch64-apple-darwin", "14.0"),
)
PYTHONS = ("3.12", "3.13")  # the manifest's runtimes.python is >=3.12,<3.14
#: The cells per variant, a literal on purpose (the nexus-moht0 doctrine): deleting a row of PLATFORMS or PYTHONS
#: must fail the gate, not shrink it.
EXPECTED_CELLS = 8
#: Pinned-line floors for a compile, the e2e leg's (80-85% of the real lock's compile on each variant).
MIN_COMPILED = 110
MIN_COMPILED_PDF = 180
_COMPILE_WORKERS = 4

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

    summaries = []
    for variant in ("bare", "pdf"):
        if variant == "pdf":
            changed, refusal = launcher._set_pdf(str(bundle), True)
            if not changed:
                return _fail(f"the launcher did not switch the bundle to conexus[pdf] ({refusal or 'no change'})")
            (bundle / "uv.lock").unlink(missing_ok=True)
        rc, summary = _lock_and_check(args, bundle, pins, release, variant)
        if rc:
            return rc
        rc, cells = _platform_check(args, bundle, source, release, variant)
        if rc:
            return rc
        summaries.append(f"{summary}; {cells}")
    print(f"MCPB RESOLVE GATE PASSED: conexus {release}; " + "; ".join(summaries))
    return 0


def _lock_and_check(args: argparse.Namespace, bundle: Path, pins: list[str], release: str,
                    variant: str) -> tuple[int, str]:
    """Lock the scratch bundle as it stands and check the lock: (exit code, the variant's counts)."""
    def fail(msg: str) -> tuple[int, str]:
        return _fail(f"{variant}: {msg}"), ""

    cmd = [args.uv, "lock", "--no-config", "--directory", str(bundle)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=UV_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return fail(f"could not run {args.uv} lock ({type(exc).__name__})")
    if proc.returncode != 0:
        sys.stdout.write(proc.stdout)
        sys.stdout.write(proc.stderr)
        return fail(f"uv lock exited {proc.returncode}: the staged lock and the bundle's dependencies do not resolve together")

    try:
        lock = tomllib.loads((bundle / "uv.lock").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return fail(f"uv wrote no readable uv.lock ({type(exc).__name__})")
    locked: dict[str, set[str]] = {}
    for pkg in lock.get("package", []):
        locked.setdefault(_norm(pkg["name"]), set()).add(str(pkg.get("version", "")))
    if release not in locked.get("conexus", set()):
        return fail(f"conexus is not locked at {release} (locked: {sorted(locked.get('conexus', ()))})")
    floor = MIN_LOCKED_PDF if variant == "pdf" else MIN_LOCKED
    if len(locked) < floor:
        return fail(f"only {len(locked)} packages locked (floor {floor}); the resolve examined almost nothing")
    if variant == "pdf":
        missing = [name for name in PDF_REQUIRED if name not in locked]
        if missing:
            return fail(f"the PDF stack is not in the lock: {', '.join(missing)} missing")
    else:
        present = [name for name in PDF_REQUIRED if name in locked]
        if present:
            return fail(f"the bare bundle locks the PDF stack ({', '.join(present)}): the setting would decide nothing")

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
        return fail(f"{len(unheld)} pins not honoured: {'; '.join(unheld[:5])}")
    if held < MIN_HELD:
        return fail(f"only {held} pins held (floor {MIN_HELD}); the pins reached almost nothing")
    return 0, (f"{variant}: {len(locked)} packages locked, {held} of {len(pins)} pins held, "
               f"{absent} pinned packages not needed, 0 pins moved")


def _bundle_requirements(bundle: Path, source: Path) -> list[str]:
    """The bundle's own ``[project].dependencies`` as plain requirements, conexus taken from ``source``.

    No ``[tool.uv.sources]``: ``uv pip compile`` is handed the conexus location in the requirement itself,
    the way ``uv lock`` got it from the sources table."""
    deps = tomllib.loads((bundle / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    out = []
    for dep in deps:
        m = re.match(r"conexus(\[[^\]]*\])?\s*(?:[<>=!~]|$)", dep)
        out.append(f"conexus{m.group(1) or ''} @ {source.as_uri()}" if m else dep)
    return out


def _compile_cell(args: argparse.Namespace, work: Path, req: Path, overrides: Path, constraints: Path,
                  cell: tuple[str, str, str | None], python: str) -> tuple[str, str | None, str]:
    """One ``uv pip compile``: (cell name, the failure or None, the compiled pins as text)."""
    tag, platform, macos = cell
    name = f"{tag}-py{python}"
    out = work / f"{name}.txt"
    cmd = [args.uv, "pip", "compile", str(req), "--no-config", "--no-header", "--python-platform", platform,
           "--python-version", python, "--overrides", str(overrides), "--constraints", str(constraints),
           "--output-file", str(out)]
    env = {k: v for k, v in os.environ.items() if k != "MACOSX_DEPLOYMENT_TARGET"}
    if macos:
        env["MACOSX_DEPLOYMENT_TARGET"] = macos
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=UV_TIMEOUT_S, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return name, f"could not run {args.uv} pip compile ({type(exc).__name__})", ""
    if proc.returncode != 0:
        tail = " | ".join((proc.stderr or proc.stdout).strip().splitlines()[-4:])
        return name, f"uv pip compile exited {proc.returncode}: {tail[:400]}", ""
    try:
        return name, None, out.read_text(encoding="utf-8")
    except OSError as exc:
        return name, f"uv wrote no output file ({type(exc).__name__})", ""


def _platform_check(args: argparse.Namespace, bundle: Path, source: Path, release: str,
                    variant: str) -> tuple[int, str]:
    """Resolve the variant once per PLATFORMS x PYTHONS cell: (exit code, the variant's counts).

    The universal lock accepts a pin with a wheel on only some platforms; this does not."""
    def fail(msg: str) -> tuple[int, str]:
        return _fail(f"{variant}: {msg}"), ""

    if len(PLATFORMS) * len(PYTHONS) != EXPECTED_CELLS:
        return fail(f"{len(PLATFORMS) * len(PYTHONS)} cells declared, {EXPECTED_CELLS} expected")
    work = Path(tempfile.mkdtemp(prefix="nx-mcpb-cells-", dir=bundle.parent))
    reqs = _bundle_requirements(bundle, source)
    if not any(r.startswith("conexus") for r in reqs):
        return fail("the bundle's dependencies name no conexus")
    req = work / "requirements.in"
    req.write_text("\n".join(reqs) + "\n", encoding="utf-8")
    overrides = work / "overrides.txt"
    uv_table = tomllib.loads((bundle / "pyproject.toml").read_text(encoding="utf-8")).get("tool", {}).get("uv", {})
    overrides.write_text("\n".join(uv_table.get("override-dependencies", [])) + "\n", encoding="utf-8")
    constraints = bundle / "constraints.txt"  # the staged lock, the file the launcher pins from

    cells = [(c, py) for c in PLATFORMS for py in PYTHONS]
    with ThreadPoolExecutor(max_workers=_COMPILE_WORKERS) as pool:
        results = list(pool.map(
            lambda cp: _compile_cell(args, work, req, overrides, constraints, cp[0], cp[1]), cells))
    if len(results) != EXPECTED_CELLS:
        return fail(f"{len(results)} cells examined of {EXPECTED_CELLS} expected")

    floor = MIN_COMPILED_PDF if variant == "pdf" else MIN_COMPILED
    failures: list[str] = []
    smallest: int | None = None
    for name, err, text in results:
        if err:
            failures.append(f"{name}: {err}")
            continue
        # A path or wheel source compiles to ``conexus @ file:///...`` (no version in the line): the staged
        # ``conexus==<release>`` constraint is what ties that source to the release, and uv fails the compile
        # when the source's version differs. A version-pinned line would be ``conexus==...``.
        names = {_norm(m.group(1)) for m in re.finditer(r"^([A-Za-z0-9_.-]+)(?:==| @ )", text, re.M)}
        if "conexus" not in names:
            failures.append(f"{name}: conexus is not in the compile")
        elif len(names) < floor:
            failures.append(f"{name}: only {len(names)} packages compiled (floor {floor}); the compile examined almost nothing")
        elif variant == "pdf" and (gone := [n for n in PDF_REQUIRED if n not in names]):
            failures.append(f"{name}: the PDF stack is not in the compile ({', '.join(gone)} missing)")
        elif variant != "pdf" and (extra := [n for n in PDF_REQUIRED if n in names]):
            failures.append(f"{name}: the bare compile carries the PDF stack ({', '.join(extra)})")
        else:
            smallest = len(names) if smallest is None else min(smallest, len(names))
    if failures:
        return fail(f"{len(failures)} of {len(results)} platform cells do not resolve: " + "; ".join(failures))
    return 0, f"{variant} per platform: {len(results)} of {EXPECTED_CELLS} cells resolved (smallest {smallest} packages)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--conexus", default=None, help="conexus to resolve: a checkout directory or a built wheel (default: this checkout)")
    ap.add_argument("--conexus-version", default=None, help="the release to pin (default: the manifest's version)")
    ap.add_argument("--uv", default="uv", help="the uv executable")
    ap.add_argument("--keep", action="store_true", help="keep the scratch bundle and print where it is")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
