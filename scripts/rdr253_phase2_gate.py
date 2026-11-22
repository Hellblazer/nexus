#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-253 Phase 2's gate as a check (nexus-hczdo.2.1): exit 0 only when every live Claude process on this host, Desktop
included, holds a binding at client 8.5.0 or later; exit 1 naming each one that does not. Run it on the gate day and paste
its output on the bead:

    uv run python scripts/rdr253_phase2_gate.py
"""
from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any


def main(argv: Sequence[str] | None = None, *, env: Any = None) -> int:
    from nexus.identity.ops import IdentityCommandError, IdentityEnv, phase2_gate  # noqa: PLC0415 — the package, at call time

    del argv
    try:
        ok, lines = phase2_gate(env if env is not None else IdentityEnv.real())
    except IdentityCommandError as exc:
        sys.stdout.write(f"PHASE2_GATE_UNKNOWN: {exc}\n")
        return 2
    sys.stdout.write("\n".join(lines) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
