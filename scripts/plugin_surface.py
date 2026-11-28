# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decide whether ci.yml's ``plugin-ledger-gate`` job has an input to look at (RDR-258 Phase 4, nexus-2jomg.5).

The plugin ledger check (``tests/test_plugin_release_drift_ledger.py``, run by ``.github/workflows/plugin-drift-ledger.yml``)
reads the pinned-tag surface of the plugins. ``ci.yml`` calls that workflow as a job so ``pytest-gate`` can read its result
(Sam, 2026-11-27: blocking on the pull request into main, a warning elsewhere). The job costs a runner only when this says
``plugin=true``, or on a pull request into main, which always runs it. This decides once, in the ``changes`` job, from the
same diff base the other ``changes`` steps use: a pull request diffs from its merge base (``base...HEAD``); a push diffs from
the branch's previous tip (``before..HEAD``). Any doubt answers ``true``: an unusable base (empty, all zeros, not in the
checkout). An empty changed-file list between two good commits is a broken range, never "nothing changed": exit 1.

:data:`SURFACE_PATHS` is the list the workflow's own ``paths:`` filter carried before the job moved into ``ci.yml``; a path
ending ``/**`` is a directory prefix and any other entry is an exact path.
tests/test_plugin_release_drift_ledger.py pins that the list covers every surface prefix the ledger test reads, and
tests/test_procedure_preflight.py that preflight classes each of them as ``plugin``.

Standard library only: it runs under the runner's bare python3.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SURFACE_PATHS: tuple[str, ...] = (
    "conexus/hooks/**",
    "conexus/commands/**",
    "conexus/skills/**",
    "conexus/agents/**",
    "conexus/resources/**",
    "conexus/mcp/**",
    "conexus/launch/**",
    "conexus/PENDING_RELEASE.md",
    "sn/hooks/**",
    "sn/.mcp.json",
    "tool-spill/hooks/**",
    "tool-spill/.claude-plugin/plugin.json",
    "prose-edit/skills/**",
    "prose-edit/agents/**",
    "prose-edit/.claude-plugin/plugin.json",
    "conexus/.mcp.json",
    ".claude-plugin/marketplace.json",
    "scripts/plugin_ledger.py",
    "scripts/plugin_channel.py",
    "tests/test_plugin_release_drift_ledger.py",
    ".github/workflows/plugin-drift-ledger.yml",
)
ZERO_SHA = "0" * 40


class MalformedRange(Exception):
    """Two good commits with no changed file between them: the diff is broken, not empty."""


def matches(path: str) -> bool:
    return any(path.startswith(p[:-2]) if p.endswith("/**") else path == p for p in SURFACE_PATHS)


def _usable(repo: Path, sha: str) -> bool:
    return bool(sha) and sha != ZERO_SHA and subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True, check=False).returncode == 0


def decide(repo: Path, event: str, before: str, base: str) -> tuple[bool, str]:
    """(plugin, why)."""
    if event == "pull_request":
        sha, rng = base, [f"{base}...HEAD"]
    else:
        sha, rng = before, [before, "HEAD"]
    if not _usable(repo, sha):
        return True, f"no usable diff base ({sha!r}), so the plugin ledger runs"
    proc = subprocess.run(["git", "-C", str(repo), "diff", "--no-renames", "--name-only", *rng], capture_output=True, text=True,
                          check=False)
    if proc.returncode != 0:
        return True, f"git diff failed ({proc.stderr.strip()[-200:]}), so the plugin ledger runs"
    changed = [p for p in proc.stdout.splitlines() if p.strip()]
    if not changed:
        raise MalformedRange("the changed-file list is EMPTY between two resolved commits: a broken range, not nothing changed")
    hit = next((p for p in changed if matches(p)), None)
    return (hit is not None), (f"{hit} is on the plugin surface" if hit else f"none of {len(changed)} changed paths is on the plugin surface")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="plugin_surface.py")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--event", required=True)
    ap.add_argument("--before", default="")
    ap.add_argument("--base", default="")
    ap.add_argument("--output", required=True, help="file to append plugin=true|false to ($GITHUB_OUTPUT)")
    a = ap.parse_args(argv)
    try:
        plugin, why = decide(Path(a.repo), a.event, a.before, a.base)
    except MalformedRange as exc:
        sys.stdout.write(f"::error::{exc}\n")
        return 1
    sys.stdout.write(f"plugin surface: plugin={'true' if plugin else 'false'}: {why}\n")
    with open(a.output, "a", encoding="utf-8") as f:
        f.write(f"plugin={'true' if plugin else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
