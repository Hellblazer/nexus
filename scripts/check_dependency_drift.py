#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Weekly fresh-resolution drift watch (nexus-l2ku5 class, gap 3 of T2
``nexus/release-protocol-gap-audit-2026-08-14`` [22511]).

THE BUG CLASS this guards. ``tests/test_dependency_bounds_lint.py`` proves
every runtime dependency's *shape* is closed (an upper bound exists), but a
shape check cannot see a break that is still INSIDE the declared bounds --
e.g. a package on a slow major-version cadence bumping from 1.x to 2.x while
our own cap sits at ``<3``, or a transitive dependency (never declared in
``pyproject.toml`` at all, so no bound of ours applies) jumping a major
version underneath us. ``uv.lock`` itself cannot see this either: it pins the
CURRENT resolution and only changes when something re-resolves against it.
The nexus-l2ku5 outage happened at exactly that seam -- a fresh
``uv tool install`` re-resolves ignoring the committed lock, so the first
place a major bump becomes visible is a user's fresh install, days after it
shipped upstream.

THIS SCRIPT closes that visibility gap by asking the same question fresh
installs implicitly ask: "if I resolved this dependency set against PyPI
right now, ignoring the committed lock, what would change?" It runs
``uv lock --upgrade --dry-run`` (never writes ``uv.lock`` -- see the
``--dry-run`` flag; verified empirically to leave the file byte-identical),
parses the ``Update``/``Add``/``Remove`` lines uv prints, and flags any
``Update`` whose leading version component (the closest thing to a semver
"major" that also works for calver/date-based schemes) changed. This is
INFORMATIONAL PRESSURE, not a merge gate -- pyproject.toml's upper bounds are
the actual gate (a bound violation makes ``uv sync`` itself fail); this
script's job is to surface "your bound is about to become the only thing
standing between develop and a major bump" while there is still time to look
at the changelog, not after a user's install breaks.

Usage::

    uv run python scripts/check_dependency_drift.py
    uv run python scripts/check_dependency_drift.py --json

Exit codes: ``0`` no major-version-component drift found; ``1`` one or more
packages would resolve to a new leading version component; ``2`` the ``uv
lock`` dry-run itself could not be run (uv missing, non-zero exit, timeout --
"could not check" is never reported as "clean").

WHERE THIS RUNS. Not wired into any CI workflow by this change -- see the
coordination note in the commit this script ships with. The intended home is
a weekly leg on ``.github/workflows/scheduled-failure-watch.yml`` (the
existing daily silent-failure sweep for ``on.schedule`` workflows), run with
a wider cron interval, opening/updating a tracking issue the same way that
watchdog already does for its three failure modes. Wiring is deliberately
left to a follow-up commit to avoid a concurrent edit collision with the
sibling gap-2 fix landing in the same file around the same time.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field

_UV_TIMEOUT_SECONDS = 300

#: ``Update <name> v<old>[, v<old2>] -> v<new>`` -- uv occasionally lists more
#: than one currently-locked version for a package resolved differently per
#: platform/python marker (e.g. ``torch v2.8.0, v2.10.0 -> v2.13.0``); the
#: comparison only needs the highest old version, so take the last comma-
#: separated ``v...`` token before the arrow as the representative old value.
_UPDATE_RE = re.compile(r"^Update\s+(?P<name>\S+)\s+(?P<old>v[0-9][^-]*?)\s*->\s*v(?P<new>[0-9][^\s]*)\s*$")
_ADD_RE = re.compile(r"^Add\s+(?P<name>\S+)\s+v(?P<version>[0-9][^\s]*)\s*$")
_REMOVE_RE = re.compile(r"^Remove\s+(?P<name>\S+)\s+v(?P<version>[0-9][^\s]*)\s*$")

#: Sentinel distinguishing "ran cleanly, found nothing" from "could not run" --
#: same idiom as the sibling release-gate scripts (check_wire_contract_pairing,
#: check_engine_release_floor): a failure to check must never read as clean.
UV_UNAVAILABLE = object()


@dataclass(frozen=True)
class DriftFinding:
    name: str
    old_version: str
    new_version: str

    @property
    def old_major(self) -> str:
        return _leading_component(self.old_version)

    @property
    def new_major(self) -> str:
        return _leading_component(self.new_version)


@dataclass(frozen=True)
class DriftReport:
    updates: tuple[DriftFinding, ...] = field(default_factory=tuple)
    added: tuple[str, ...] = field(default_factory=tuple)
    removed: tuple[str, ...] = field(default_factory=tuple)

    @property
    def major_bumps(self) -> tuple[DriftFinding, ...]:
        return tuple(f for f in self.updates if f.old_major != f.new_major)

    @property
    def ok(self) -> bool:
        return not self.major_bumps


def _leading_component(version: str) -> str:
    """The version's leading dot-component, e.g. ``"2.13.0"`` -> ``"2"``.
    Works for plain semver majors and for calver/date-based schemes (e.g.
    structlog's ``25.5.0``) alike -- both use the leading component as the
    boundary a `<NEXT_MAJOR>` pyproject.toml cap is meant to hold at."""
    return version.split(".", 1)[0]


def run_uv_dry_run_upgrade(
    *, timeout: int = _UV_TIMEOUT_SECONDS, cwd: str | None = None
) -> str | object:
    """Runs ``uv lock --upgrade --dry-run``. Never writes ``uv.lock`` -- the
    flag is a genuine dry run, empirically verified to leave the committed
    lock byte-identical. Returns the raw ``Update``/``Add``/``Remove`` lines
    (uv prints them to stderr) or :data:`UV_UNAVAILABLE`."""
    try:
        proc = subprocess.run(
            ["uv", "lock", "--color", "never", "--upgrade", "--dry-run"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return UV_UNAVAILABLE
    if proc.returncode != 0:
        return UV_UNAVAILABLE
    return proc.stderr


def parse_dry_run_output(output: str) -> DriftReport:
    updates: list[DriftFinding] = []
    added: list[str] = []
    removed: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if m := _UPDATE_RE.match(line):
            old_versions = [v.strip().lstrip("v") for v in m.group("old").split(",")]
            updates.append(
                DriftFinding(
                    name=m.group("name"),
                    old_version=old_versions[-1],
                    new_version=m.group("new"),
                )
            )
        elif m := _ADD_RE.match(line):
            added.append(m.group("name"))
        elif m := _REMOVE_RE.match(line):
            removed.append(m.group("name"))
    return DriftReport(updates=tuple(updates), added=tuple(added), removed=tuple(removed))


def render_report(report: DriftReport) -> str:
    bumps = report.major_bumps
    if not bumps:
        return (
            "Dependency drift watch: clean. A fresh PyPI resolution of the "
            f"current dependency set would update {len(report.updates)} "
            f"package(s), add {len(report.added)}, remove {len(report.removed)} "
            "-- none crossing a leading version-component boundary."
        )
    lines = [
        f"Dependency drift watch: {len(bumps)} package(s) would resolve to a "
        "new leading version component on a fresh PyPI resolution:",
        "",
    ]
    for f in sorted(bumps, key=lambda x: x.name):
        lines.append(f"  {f.name}: {f.old_version} -> {f.new_version}")
    lines.append("")
    lines.append(
        "This is pressure, not a break by itself -- check whether each "
        "package is a direct pyproject.toml dependency (its cap already "
        "blocks the actual jump; consider whether to raise it deliberately) "
        "or transitive (no cap of ours applies; check the changelog before "
        "the next fresh install picks it up)."
    )
    return "\n".join(lines)


def check(*, cwd: str | None = None) -> tuple[int, str, DriftReport | None]:
    output = run_uv_dry_run_upgrade(cwd=cwd)
    if output is UV_UNAVAILABLE:
        return 2, "dependency drift watch could not run `uv lock --upgrade --dry-run`", None
    report = parse_dry_run_output(output)  # type: ignore[arg-type]
    rc = 0 if report.ok else 1
    return rc, render_report(report), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of prose")
    args = parser.parse_args(argv)

    rc, body, report = check()

    if args.json:
        payload = {
            "rc": rc,
            "ok": report.ok if report else None,
            "major_bumps": (
                [{"name": f.name, "old": f.old_version, "new": f.new_version} for f in report.major_bumps]
                if report
                else []
            ),
            "message": body,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(body)

    return rc


if __name__ == "__main__":
    sys.exit(main())
