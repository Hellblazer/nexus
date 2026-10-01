# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Non-vacuity verdict for the mandatory-pins gate (nexus-z0o2p.41).

``mandatory_pins_check.py <junit.xml> <expected> <skip-budget>``

Reads the junit file of the pins' own pytest run and exits 1 unless exactly ``expected``
`mandatory_regression_pin` testcases were reported, none failed or errored, at most
``skip-budget`` were skipped, and at least one actually ran. pytest exits 0 on an
all-skipped run; conftest's session guard catches that for a run that selects the marker,
and this is the second, independent read: a gate that skip-passes when its dependency is
absent must carry its own max-skip / non-vacuity assert.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def verdict(junit_path: str, expected: int, budget: int) -> tuple[bool, str]:
    try:
        root = ET.parse(junit_path).getroot()
    except (OSError, ET.ParseError) as exc:
        return False, f"cannot read the junit file {junit_path}: {exc}"
    cases = list(root.iter("testcase"))
    skipped = [c for c in cases if c.find("skipped") is not None]
    bad = [c for c in cases if c.find("failure") is not None or c.find("error") is not None]
    ran = len(cases) - len(skipped)
    line = f"reported={len(cases)} ran={ran} skipped={len(skipped)} failed={len(bad)} expected={expected} budget={budget}"
    if len(cases) != expected:
        return False, f"{line}: the run reported {len(cases)} pin test(s), expected exactly {expected} (a new or removed pin must bump MANDATORY_PIN_EXPECTED in tests/e2e/lib/mandatory_pins.sh)"
    if bad:
        return False, f"{line}: {', '.join(c.get('name') or '?' for c in bad)} did not pass"
    if len(skipped) > budget:
        why = "; ".join(
            f"{c.get('name')}: {(c.find('skipped').get('message') or '')[:120]}" for c in skipped  # type: ignore[union-attr]
        )
        return False, f"{line}: skipped over the budget, so the pin proved nothing this run ({why})"
    if ran < 1:
        return False, f"{line}: no pin ran"
    return True, line


def main(argv: list[str]) -> int:
    if len(argv) != 3 or not argv[1].isdigit() or not argv[2].isdigit():
        sys.stderr.write(__doc__ or "")
        return 2
    ok, line = verdict(argv[0], int(argv[1]), int(argv[2]))
    print(f"MANDATORY PINS: {line}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
