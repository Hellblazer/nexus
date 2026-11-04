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
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN_DIR = Path(__file__).resolve().parent
#: The reason given for a module the fallback ran out of time before trying.
_NOT_TRIED = "not tried, over the time budget"
#: The module name the census plugin is loaded under, copied from PLUGIN_DIR; no tree carries this name.
_PLUGIN_AS = "_tenant_delta_census_now"


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
    """A census run gave nothing to attribute: no census file, or the session itself failed to collect.
    ``reason`` is the first error line."""

    def __init__(self, root: Path, returncode: int, reason: str) -> None:
        self.reason = reason
        super().__init__(f"collection under {root} failed (rc {returncode}): {reason}")


def _first_error(output: str) -> str:
    """The first ``E   `` line pytest printed (the exception), else the last line that names an error, else the
    tail. Never a banner ahead of the real error, and never a clipped line."""
    lines = output.splitlines()
    for line in lines:
        if line.startswith("E   "):
            return line[4:].strip()[:300]
    for line in reversed(lines):
        head = line.strip().split(":", 1)[0]
        if ":" in line and head.endswith(("Error", "Exception")):
            return line.strip()[:300]
    return output.strip()[-300:]


def _owner(module: str, failed: dict[str, str]) -> str | None:
    """The failed collector that covers *module* (the module itself, or a package or directory above it)."""
    for nodeid, reason in failed.items():
        node = nodeid.split("::", 1)[0].rstrip("/")
        if node and (module == node or module.startswith(node + "/")):
            return reason
    return None


def census(root: Path, modules: list[str], *, deadline: float,
           own_src: bool = False) -> tuple[dict[str, int], dict[str, str]]:
    """One collection under *root* (only the modules that exist there), with collection errors allowed:
    tenant-minting tests per module, and each module that failed to collect with its first error line.
    *own_src* puts *root*'s ``src/`` first on the path, so its tests import its own code rather than the
    installed package. Finishes before *deadline* (a time.monotonic() value) or raises; CollectionFailed when
    nothing can be attributed (no census, or the session itself failed)."""
    present = [m for m in modules if (root / m).is_file()]
    if not present:
        return {}, {}
    with tempfile.TemporaryDirectory(prefix="tenant-census-") as tmp:
        out = Path(tmp) / "census.json"
        # The plugin is loaded under a name no tree has: a tree's pyproject puts its own scripts/ (and its own,
        # perhaps older, _tenant_census_plugin) ahead of PYTHONPATH, which once made the base write the old format.
        shutil.copyfile(PLUGIN_DIR / "_tenant_census_plugin.py", Path(tmp) / f"{_PLUGIN_AS}.py")
        path = [str(root / "src")] if own_src and (root / "src").is_dir() else []
        env = dict(os.environ, TENANT_CENSUS_OUT=str(out), NX_TEST_T2_SUBSTRATE="none", NX_NO_TELEMETRY="1",
                   PYTHONPATH=os.pathsep.join([*path, tmp, str(root), str(root / "scripts")]))
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            raise subprocess.TimeoutExpired("pytest --collect-only", 0)
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
             "--continue-on-collection-errors", "-p", _PLUGIN_AS,
             "--rootdir", str(root), "-c", str(root / "pyproject.toml"), *present],
            cwd=root, env=env, capture_output=True, text=True, timeout=remaining,
        )
        if not out.is_file():
            raise CollectionFailed(root, proc.returncode, _first_error(proc.stdout + proc.stderr))
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CollectionFailed(root, proc.returncode, f"the census file could not be read ({exc})") from exc
        if not (isinstance(data, dict) and data.get("complete") is True
                and isinstance(data.get("counts"), dict) and isinstance(data.get("failed"), dict)):
            # Never read an incomplete or unexpected census as "no tenant tests" (an older plugin wrote a bare
            # mapping; a collection cut short before the census hook writes nothing).
            raise CollectionFailed(root, proc.returncode,
                                   _first_error(proc.stdout + proc.stderr) or "the census is incomplete")
        failed_nodes: dict[str, str] = data["failed"]
        if "" in failed_nodes:
            raise CollectionFailed(root, proc.returncode, failed_nodes[""])
        failed = {m: r for m in present if (r := _owner(m, failed_nodes)) is not None}
        counts = {m: n for m, n in data["counts"].items() if m not in failed}
        return counts, failed


def _one_by_one(root: Path, modules: list[str], *, deadline: float,
                own_src: bool) -> tuple[dict[str, int], dict[str, str]]:
    """Each module alone, for a run whose failure could not be attributed: the counts it can take, and the
    rest with a reason. Out of budget, the modules not yet tried are named as such and the counts kept."""
    counts: dict[str, int] = {}
    failed: dict[str, str] = {}
    for i, module in enumerate(modules):
        try:
            c, f = census(root, [module], deadline=deadline, own_src=own_src)
        except subprocess.TimeoutExpired:
            for rest in modules[i:]:
                failed[rest] = _NOT_TRIED
            break
        except CollectionFailed as exc:
            f, c = {module: exc.reason}, {}
        counts.update(c)
        failed.update(f)
    return counts, failed


def _attempt(root: Path, modules: list[str], *, deadline: float,
             own_src: bool) -> tuple[dict[str, int], dict[str, str]]:
    try:
        return census(root, modules, deadline=deadline, own_src=own_src)
    except CollectionFailed:
        return _one_by_one(root, modules, deadline=deadline, own_src=own_src)


def base_census(repo: Path, base: str, modules: list[str], *,
                deadline: float) -> tuple[dict[str, int], dict[str, str]]:
    """The same count at *base*, over its whole tree unpacked from git archive (a test may read any file of it
    while collecting). First against the installed (HEAD's) code; a module that fails there is tried again
    against the base's own src/ (an API HEAD has since changed); one that fails both is returned with its
    reason. Measured at 64e2fe6da against its parent, the mcp 2.3 move: against HEAD's code 5 of 36 changed
    modules could not be counted, against the base's src/ first 16 (nexus-l35bq review r1)."""
    with tempfile.TemporaryDirectory(prefix="tenant-base-") as tmp:
        # Resolved: on macOS the temp dir is under /var, a symlink to /private/var. pytest resolves its rootdir
        # and then saw the module arguments outside it, failing the whole session ("ERROR collecting test
        # session", the ../../var/folders paths in the original report) instead of collecting.
        root = Path(tmp).resolve()
        archive = subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", base],
            check=True, capture_output=True,
        ).stdout
        tar_path = root / "base.tar"
        tar_path.write_bytes(archive)
        del archive
        with tarfile.open(tar_path) as tar:
            tar.extractall(root, filter="data")
        tar_path.unlink()
        counts, failed = _attempt(root, modules, deadline=deadline, own_src=False)
        retry = [m for m, why in failed.items() if why != _NOT_TRIED]
        if retry:
            try:
                again, still = _attempt(root, retry, deadline=deadline, own_src=True)
            except subprocess.TimeoutExpired:
                # Pass 1's counts stand; the modules it could not count are left uncounted, not dropped.
                again, still = {}, {m: failed[m] for m in retry}
            counts.update(again)
            for module in retry:
                if module not in still:
                    failed.pop(module, None)
                else:
                    failed[module] = still[module]
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
        # HEAD's own src/ is the installed code (an editable install); first on the path all the same.
        head, head_failed = census(repo, modules, deadline=deadline, own_src=True)
        if head_failed:
            module, reason = next(iter(head_failed.items()))
            # A module the branch itself cannot collect would read as zero tenant tests: refuse instead.
            raise CollectionFailed(repo, 2, f"{module}: {reason}")
        base, uncounted = base_census(repo, merge_base, modules, deadline=deadline)
    except subprocess.TimeoutExpired:
        _say(f"WARNING: tenant delta: not counted, over the {args.budget_s:g} s budget "
             f"({len(modules)} changed test module(s)); run scripts/tenant_delta.py by hand", err=True)
        return 2
    except (RuntimeError, subprocess.SubprocessError, OSError, tarfile.TarError) as exc:
        _say(f"WARNING: tenant delta: could not count ({str(exc)[:400]})", err=True)
        return 2
    rows = [(m, base.get(m, 0), head.get(m, 0)) for m in modules if m not in uncounted]
    delta = sum(h - b for _m, b, h in rows)
    # An uncounted module with no tenant test at head cannot raise the count; only the others are named.
    risky = {m: r for m, r in uncounted.items() if head.get(m, 0) > 0}
    if delta == 0 and not risky and args.quiet_if_zero:
        return 0
    for module, before, after in rows:
        if after != before:
            _say(f"  {module}: {before} -> {after} ({after - before:+d})")
    for module, reason in risky.items():
        _say(f"  WARNING: {module}: not counted at the merge base, {head[module]} at head ({reason})")
    limit = ceiling(repo)
    not_counted = f", {len(uncounted)} not counted at the merge base" if uncounted else ""
    _say(f"tenant delta: {delta:+d} test(s) that mint a tenant across {len(modules)} changed module(s){not_counted}; "
          f"python_unit ceiling {limit}, tolerance 0 (tests/storage_touch_ceiling.toml)")
    if delta > 0:
        _say("  develop CI's count usually sits at or just under the ceiling, so a rise will likely fail "
              "pytest-gate's storage-touch fan-in: make the new test tenant-free, or raise the ceiling in "
              "the same commit with a reason (tests/AGENTS.md)")
        return 1 if args.strict else 0
    if risky:
        return 1 if args.strict else 0  # a rise cannot be ruled out where the base could not be counted
    return 0


if __name__ == "__main__":
    sys.exit(main())
