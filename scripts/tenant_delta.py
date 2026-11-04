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
temporary directory, never a worktree or a checkout, and is collected against that tree's own
``src/`` and files (nexus-l35bq: collected against HEAD's code, a base test that used an API HEAD
had changed failed to import, and the whole count was lost). A base module that still will not
collect is named, with its HEAD count, and the others are counted. Advisory: exit 0 unless
``--strict`` and the branch adds tenant tests, or 2 when the count could not be taken (a HEAD
module that does not collect, or the budget).

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


class CollectionFailed(RuntimeError):
    """Collection under a tree failed; ``reason`` is the first error line pytest printed."""

    def __init__(self, root: Path, returncode: int, output: str) -> None:
        self.reason = _first_error(output)
        super().__init__(f"collection under {root} failed (rc {returncode}): {self.reason}")


def _first_error(output: str) -> str:
    """The first line that says what failed (an ``E   `` line, or an exception line), else the tail."""
    for line in output.splitlines():
        text = line.strip()
        if line.startswith("E   ") or (text.split(":", 1)[0].endswith(("Error", "Exception")) and ":" in text):
            return text.removeprefix("E").strip()[:300]
    return output.strip()[-300:]


def census(root: Path, modules: list[str], *, deadline: float) -> dict[str, int]:
    """Tenant-minting tests per module, collected under *root* (only modules that exist there), against
    *root*'s own ``src/`` first on the path, finishing before *deadline* (a time.monotonic() value) or
    raising (CollectionFailed when a module will not collect)."""
    present = [m for m in modules if (root / m).is_file()]
    if not present:
        return {}
    with tempfile.TemporaryDirectory(prefix="tenant-census-") as tmp:
        out = Path(tmp) / "census.json"
        path = [str(root / "src")] if (root / "src").is_dir() else []
        env = dict(os.environ, TENANT_CENSUS_OUT=str(out), NX_TEST_T2_SUBSTRATE="none", NX_NO_TELEMETRY="1",
                   PYTHONPATH=os.pathsep.join([*path, str(root), str(root / "scripts"), str(PLUGIN_DIR)]))
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
            raise CollectionFailed(root, proc.returncode, proc.stdout + proc.stderr)
        return json.loads(out.read_text(encoding="utf-8"))


def base_census(repo: Path, base: str, modules: list[str], *,
                deadline: float) -> tuple[dict[str, int], dict[str, str]]:
    """The same count at *base*, over its whole tree unpacked from git archive (a test may read any file of
    it while collecting, and imports the base's own src/). Returns the counts and, for each module that will
    not collect there even alone, the first error line: one such module costs its own count, not all."""
    with tempfile.TemporaryDirectory(prefix="tenant-base-") as tmp:
        root = Path(tmp)
        archive = subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", base],
            check=True, capture_output=True,
        ).stdout
        tar_path = root / "base.tar"
        tar_path.write_bytes(archive)
        with tarfile.open(tar_path) as tar:
            tar.extractall(root, filter="data")
        tar_path.unlink()
        try:
            return census(root, modules, deadline=deadline), {}
        except CollectionFailed:
            pass
        counts: dict[str, int] = {}
        failed: dict[str, str] = {}
        for module in modules:
            try:
                counts.update(census(root, [module], deadline=deadline))
            except CollectionFailed as exc:
                failed[module] = exc.reason
        return counts, failed


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
        base, uncounted = base_census(repo, merge_base, modules, deadline=deadline)
    except subprocess.TimeoutExpired:
        _say(f"tenant delta: not counted, over the {args.budget_s:g} s budget "
             f"({len(modules)} changed test module(s)); run scripts/tenant_delta.py by hand", err=True)
        return 2
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        _say(f"tenant delta: could not count ({str(exc)[:300]})", err=True)
        return 2
    rows = [(m, base.get(m, 0), head.get(m, 0)) for m in modules if m not in uncounted]
    delta = sum(h - b for _m, b, h in rows)
    if delta == 0 and not uncounted and args.quiet_if_zero:
        return 0
    for module, before, after in rows:
        if after != before:
            _say(f"  {module}: {before} -> {after} ({after - before:+d})")
    for module, reason in uncounted.items():
        _say(f"  {module}: not counted at the merge base, {head.get(module, 0)} at head ({reason})")
    limit = ceiling(repo)
    not_counted = f", {len(uncounted)} not counted at the merge base" if uncounted else ""
    _say(f"tenant delta: {delta:+d} test(s) that mint a tenant across {len(modules)} changed module(s){not_counted}; "
          f"python_unit ceiling {limit}, tolerance 0 (tests/storage_touch_ceiling.toml)")
    if delta > 0:
        _say("  develop CI's count usually sits at or just under the ceiling, so a rise will likely fail "
              "pytest-gate's storage-touch fan-in: make the new test tenant-free, or raise the ceiling in "
              "the same commit with a reason (tests/AGENTS.md)")
        return 1 if args.strict else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
