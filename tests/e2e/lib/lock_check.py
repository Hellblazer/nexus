#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Does an installed environment hold the versions its wheel's lock names?
nexus-2k66t.4.1.

Run by the INSTALLED environment's own interpreter, against the
``nexus/_install/constraints.txt`` its wheel shipped::

    <env>/bin/python tests/e2e/lib/lock_check.py <constraints.txt> [witness ...]

Every pin whose marker holds on this box is compared with the installed
version. Exit 0 when all match and the check examined enough to mean it; 1
otherwise; the last stdout line is the JSON report either way. Stdlib plus
``packaging`` (a transitive dependency of the PDF stack, so any conexus
environment carries it; its absence fails loud).

Shared by ``generation_install_probe.py`` (fresh-install MVV leg 9) and
``migration-rehearsal/rehearse_package_upgrade.sh`` (staged into its image).
"""
from __future__ import annotations

import json
import sys

#: The packages whose drift motivated the file; each must be installed AND equal
#: to the lock, or the check has not proved what it claims. These are in the
#: DEFAULT dependency closure, so a slim install (nexus-2k66t.4.2) has them.
LOCK_WITNESSES = ("onnxruntime", "pymupdf", "mcp")

#: Witnesses that only the ``pdf`` extra brings (opencv-python-headless comes with
#: MinerU). A caller checking an install that carries the stack adds these; on a
#: slim install they are absent by design and are not witnesses.
LOCK_PDF_WITNESSES = ("opencv-python-headless",)

#: The lock holds ~220 pins; an environment installs the large majority. A floor
#: far below that and far above "the check examined almost nothing".
LOCK_MATCHED_FLOOR = 100


def build_report(constraints: str, witnesses: tuple[str, ...]) -> dict:
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415
    from packaging.requirements import Requirement  # noqa: PLC0415

    report: dict = {"examined": 0, "matched": 0, "absent": [], "mismatched": [], "marker_skipped": 0, "witness": {}}
    with open(constraints, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            req = Requirement(line)
            if req.marker is not None and not req.marker.evaluate():
                report["marker_skipped"] += 1
                continue
            report["examined"] += 1
            try:
                have = version(req.name)
            except PackageNotFoundError:
                report["absent"].append(req.name)
                continue
            if req.specifier.contains(have, prereleases=True):
                report["matched"] += 1
            else:
                report["mismatched"].append(f"{req.name} installed {have}, locked {req.specifier}")
            if req.name.lower() in witnesses:
                report["witness"][req.name.lower()] = [have, str(req.specifier)]
    return report


def lock_verdict(report: dict, witnesses: tuple[str, ...] = LOCK_WITNESSES) -> list[str]:
    """Why an environment does NOT match its shipped constraints; empty when it does.

    Pure so it is unit-testable without an environment. Three ways to fail: a pin
    whose installed version differs; a witness package that is absent or
    unpinned; and a check that examined too little to mean anything (a pass over
    input it never looked at is not a pass).
    """
    problems = [f"MISMATCH {m}" for m in report.get("mismatched", [])]
    for name in witnesses:
        if name not in report.get("witness", {}):
            problems.append(f"witness {name} is not installed or not pinned: the check did not examine it")
    if report.get("matched", 0) < LOCK_MATCHED_FLOOR:
        problems.append(
            f"only {report.get('matched', 0)} pins matched (floor {LOCK_MATCHED_FLOOR}); "
            "the check examined too little to prove the install follows the lock"
        )
    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: lock_check.py <constraints.txt> [witness ...]", file=sys.stderr)
        return 2
    witnesses = tuple(argv[2:]) or LOCK_WITNESSES
    report = build_report(argv[1], witnesses)
    problems = lock_verdict(report, witnesses)
    for problem in problems:
        print(f"LOCK-CHECK FAIL {problem}")
    print(json.dumps(report))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
