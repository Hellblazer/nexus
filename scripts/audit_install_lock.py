#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Weekly vulnerability audit of the shipped install lock (nexus-2k66t.4.5).

THE GAP this closes. Since nexus-2k66t.4.1 every install is pinned to
``src/nexus/_install/constraints.txt`` (generated from ``uv.lock``), so a
security fix in a transitive dependency reaches users only through a nexus
release, and nothing prompted that release: dependabot covers github-actions
only. The first audit (2026-10-24, T2 ``nexus/dependency-audit-first-run-2026-10-24``)
found 258 advisories across 34 of 208 pinned packages, nearly all fixed
upstream (nexus-13z79).

THIS SCRIPT reads the JSON ``pip-audit`` writes for the constraints file and
renders one line per vulnerable package: its pinned version, its advisory
ids, and the highest fixed version. It exits 1 when any advisory has a fixed
version and is not acknowledged in :data:`ACKNOWLEDGED`, so the weekly run is
red exactly when a lock refresh would clear something. An advisory with no
fix is shown and never fails the run (nothing a refresh can do). An
acknowledged advisory carries its reason and is shown, never fails.

Usage::

    uvx pip-audit -r src/nexus/_install/constraints.txt --no-deps --disable-pip \
        --progress-spinner off --format json -o audit.json
    python3 scripts/audit_install_lock.py audit.json

Exit codes: ``0`` nothing actionable; ``1`` an unacknowledged advisory has a
fix; ``2`` the report could not be read or examined nothing (a vacuous audit
is a failure, nexus-moht0).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

#: Advisory id -> why it is accepted for now. Each entry needs a reason a reader
#: can check, and is removed when the lock moves past it.
ACKNOWLEDGED: dict[str, str] = {}

#: Package -> (the exact pinned version, why every advisory against it is accepted).
#: The acknowledgement holds only at that version: when the lock moves the package,
#: the entry stops applying and any advisory then open turns the run red again.
ACKNOWLEDGED_AT_VERSION: dict[str, tuple[str, str]] = {
    "pillow": (
        "12.3.0",
        "every fix is in pillow 13.0.0 (released 2026-10-16); fastembed through 0.9.0 "
        "declares pillow<13, so the lock cannot take it yet (nexus-13z79)",
    ),
    "transformers": (
        "4.57.6",
        "the fixed versions are transformers 5.x; mineru[pipeline] 3.1.x requires transformers<5, "
        "and mineru 4 is a gated major (nexus-13z79)",
    ),
}

#: Fewer packages than this in the report means pip-audit read the wrong file
#: or nothing at all; the lock has about 200.
MIN_EXAMINED: int = 50


def _version_key(text: str) -> tuple:
    """A sort key for version strings without importing packaging (stdlib only)."""
    parts: list[tuple[int, object]] = []
    for piece in text.replace("-", ".").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append((0, int(digits)) if digits and digits == piece else (1, piece))
    return tuple(parts)


def summarise(report: dict) -> tuple[list[str], int, int]:
    """(lines, examined, actionable) for a pip-audit JSON *report*."""
    deps = report.get("dependencies", []) if isinstance(report, dict) else report
    lines: list[str] = []
    actionable = 0
    for dep in sorted(deps, key=lambda d: str(d.get("name", ""))):
        vulns = {v["id"]: v for v in dep.get("vulns") or [] if v.get("id")}
        if not vulns:
            continue
        fixes = [f for v in vulns.values() for f in v.get("fix_versions") or []]
        highest = max(fixes, key=_version_key) if fixes else "-"
        pinned_ack = ACKNOWLEDGED_AT_VERSION.get(str(dep.get("name", "")).lower())
        version_acked = pinned_ack is not None and pinned_ack[0] == str(dep.get("version", ""))
        open_ids = sorted(
            i for i, v in vulns.items() if v.get("fix_versions") and i not in ACKNOWLEDGED and not version_acked
        )
        acked = sorted(i for i in vulns if i in ACKNOWLEDGED)
        nofix = sorted(i for i, v in vulns.items() if not v.get("fix_versions"))
        actionable += len(open_ids)
        mark = "FIX" if open_ids else "ok "
        lines.append(
            f"{mark} {dep['name']} {dep.get('version', '?')}: {len(vulns)} advisories, "
            f"fixed by {highest}; open {len(open_ids)}, acknowledged {len(acked)}, no fix {len(nofix)}"
        )
        for i in acked:
            lines.append(f"      acknowledged {i}: {ACKNOWLEDGED[i]}")
        if version_acked:
            lines.append(f"      acknowledged at {pinned_ack[0]}: {pinned_ack[1]}")
    return lines, len(deps), actionable


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: audit_install_lock.py <pip-audit.json>", file=sys.stderr)
        return 2
    try:
        report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"could not read the pip-audit report {argv[1]}: {exc}", file=sys.stderr)
        return 2
    lines, examined, actionable = summarise(report)
    for line in lines:
        print(line)
    vulnerable = sum(1 for line in lines if not line.startswith(" "))
    print(f"install lock audit: {examined} packages examined, {vulnerable} with advisories, "
          f"{actionable} advisories fixed upstream and not acknowledged")
    if examined < MIN_EXAMINED:
        print(f"FAILED: only {examined} packages examined (expected at least {MIN_EXAMINED}); "
              "the audit read the wrong file or nothing", file=sys.stderr)
        return 2
    if actionable:
        print("FAILED: refresh uv.lock and scripts/regen-install-constraints.sh, "
              "or acknowledge an advisory in scripts/audit_install_lock.py ACKNOWLEDGED with its reason")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
