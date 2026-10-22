#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How many tests that mint a test tenant does this branch add? (nexus-g37hn)

pytest-gate's storage-touch fan-in fails a develop run whose python_unit tests mint more
tenants than ``tests/storage_touch_ceiling.toml`` allows (tolerance 0). Develop went red twice
on 2026-10-21/22 because a new test requested a tenant and its author never saw the ceiling.
This compares, for each test module the branch changes, the tests that mint a tenant at HEAD
and at the merge base with origin/develop, and prints the difference beside the ceiling.

Both sides are counted at collection by scripts/_tenant_census_plugin.py, so tests that skip
at run time cancel out; the base is read from ``git archive`` of the merge base into a
temporary directory, never a worktree or a checkout. Advisory: exit 0 unless ``--strict`` and
the branch adds tenant tests, or 2 when the count could not be taken.

    uv run python scripts/tenant_delta.py [--base REF] [--quiet-if-zero] [--strict]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN_DIR = Path(__file__).resolve().parent


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def changed_test_modules(repo: Path, base: str) -> list[str]:
    """Test modules under tests/ that differ between *base* and HEAD (added, changed or deleted)."""
    out = _git(repo, "diff", "--name-only", f"{base}...HEAD", "--", "tests")
    return sorted(
        p for p in out.splitlines()
        if p.endswith(".py") and (Path(p).name.startswith("test_") or p.endswith("_test.py"))
    )


def census(root: Path, modules: list[str], *, deadline: float) -> dict[str, int]:
    """Tenant-minting tests per module, collected under *root* (only modules that exist there),
    finishing before *deadline* (a time.monotonic() value) or raising."""
    present = [m for m in modules if (root / m).is_file()]
    if not present:
        return {}
    with tempfile.TemporaryDirectory(prefix="tenant-census-") as tmp:
        out = Path(tmp) / "census.json"
        env = dict(os.environ, TENANT_CENSUS_OUT=str(out), NX_TEST_T2_SUBSTRATE="none", NX_NO_TELEMETRY="1",
                   PYTHONPATH=os.pathsep.join([str(root), str(root / "scripts"), str(PLUGIN_DIR)]))
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            raise RuntimeError("out of time budget")
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
             "-p", "_tenant_census_plugin", "--rootdir", str(root), "-c", str(root / "pyproject.toml"),
             *present],
            cwd=root, env=env, capture_output=True, text=True, timeout=remaining,
        )
        # rc 0 or 5 (nothing collected) is a census; anything else means a module failed to
        # collect, which would read as zero tenant tests, so the count is refused instead.
        if proc.returncode not in (0, 5) or not out.is_file():
            raise RuntimeError(f"collection under {root} failed (rc {proc.returncode}): "
                               f"{(proc.stdout + proc.stderr)[-800:]}")
        return json.loads(out.read_text(encoding="utf-8"))


def base_census(repo: Path, base: str, modules: list[str], *, deadline: float) -> dict[str, int]:
    """The same count at *base*: its tests/, scripts/ and pyproject.toml, unpacked from git archive."""
    with tempfile.TemporaryDirectory(prefix="tenant-base-") as tmp:
        root = Path(tmp)
        present = _git(repo, "ls-tree", "--name-only", base, "--", "tests", "scripts", "pyproject.toml").split()
        archive = subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", base, *present],
            check=True, capture_output=True,
        ).stdout
        tar_path = root / "base.tar"
        tar_path.write_bytes(archive)
        with tarfile.open(tar_path) as tar:
            tar.extractall(root, filter="data")
        tar_path.unlink()
        return census(root, modules, deadline=deadline)


def _say(text: str, *, err: bool = False) -> None:
    (sys.stderr if err else sys.stdout).write(text + "\n")


def ceiling(repo: Path) -> int | None:
    path = repo / "tests" / "storage_touch_ceiling.toml"
    if not path.is_file():
        return None
    return int(tomllib.loads(path.read_text(encoding="utf-8"))["python_unit"]["provisioned_tests"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="origin/develop", help="compare against the merge base with this ref")
    parser.add_argument("--quiet-if-zero", action="store_true", help="print nothing when the delta is zero")
    parser.add_argument("--strict", action="store_true", help="exit 1 when the branch adds tenant tests")
    parser.add_argument("--budget-s", type=float, default=120.0,
                        help="total seconds for both counts; over it, nothing is counted (exit 2)")
    parser.add_argument("--repo", type=Path, default=REPO, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    repo = args.repo.resolve()

    merge_base = _git(repo, "merge-base", "HEAD", args.base).strip()
    modules = changed_test_modules(repo, merge_base)
    if not modules:
        if not args.quiet_if_zero:
            _say("tenant delta: no test module changed")
        return 0
    try:
        deadline = time.monotonic() + args.budget_s
        head = census(repo, modules, deadline=deadline)
        base = base_census(repo, merge_base, modules, deadline=deadline)
    except subprocess.TimeoutExpired:
        _say(f"tenant delta: not counted, over the {args.budget_s:g} s budget "
             f"({len(modules)} changed test module(s)); run scripts/tenant_delta.py by hand", err=True)
        return 2
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        _say(f"tenant delta: could not count ({str(exc)[:300]})", err=True)
        return 2
    rows = [(m, base.get(m, 0), head.get(m, 0)) for m in modules]
    delta = sum(h - b for _m, b, h in rows)
    if delta == 0 and args.quiet_if_zero:
        return 0
    for module, before, after in rows:
        if after != before:
            _say(f"  {module}: {before} -> {after} ({after - before:+d})")
    limit = ceiling(repo)
    _say(f"tenant delta: {delta:+d} test(s) that mint a tenant across {len(modules)} changed module(s); "
          f"python_unit ceiling {limit}, tolerance 0 (tests/storage_touch_ceiling.toml)")
    if delta > 0:
        _say("  develop CI's count usually sits at or just under the ceiling, so a rise will likely fail "
              "pytest-gate's storage-touch fan-in: make the new test tenant-free, or raise the ceiling in "
              "the same commit with a reason (tests/AGENTS.md)")
        return 1 if args.strict else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
