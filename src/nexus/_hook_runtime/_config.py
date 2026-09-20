# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""One resolver for the orchestration stop guard (RDR-215).

Four bash scripts read ``NX_ORCH_STOP_GUARD`` inline with no shared helper:
``agent-dispatch-expect.sh``:63, ``subagent-start-stamp.sh``:25,
``subagent-stop.sh``:74 and ``stop_verification_hook.sh``:49. Consolidating
closes a place where their defaults could drift apart unnoticed.

Measured at develop ``9c730421f`` before consolidating, since the port preserves
each script's current effective default rather than picking one: all four spell
it ``${NX_ORCH_STOP_GUARD:-block}``, and all four treat exactly ``observe`` and
``block`` as active — three as a negated guard, the fourth as the positive form,
which is the same predicate. So there is no rival default to choose between.
(The contract map hedges that some script might default to ``off``; none does.)
"""
from __future__ import annotations

import os

__all__ = ["ACTIVE_MODES", "DEFAULT_MODE", "stop_guard_active", "stop_guard_mode"]

#: What the guard resolves to when the variable is unset or empty. DEFAULT-ON
#: since P1.G (bead .15, 2026-07-17) — the guard is not opt-in.
DEFAULT_MODE = "block"

#: The two modes that leave the guard standing. Every other value stands it
#: down, which is the documented opt-out (``subagent-stop.sh``:15, "off/unknown
#: -> exit 0").
ACTIVE_MODES = frozenset({"observe", "block"})


def stop_guard_mode() -> str:
    """Return the guard mode, defaulting to ``block``.

    Read at call time, never cached. In the tool tier the server imports once
    and serves many events, so a module-level read would pin whatever the
    environment was at server boot.

    Empty substitutes the default, matching bash's ``${VAR:-default}`` colon
    form, which the four scripts all use.
    """
    return os.environ.get("NX_ORCH_STOP_GUARD") or DEFAULT_MODE


def stop_guard_active() -> bool:
    """Whether the guard should act.

    The comparison is exact and case-sensitive, as in bash. Being lenient about
    case or surrounding whitespace here would silently re-enable a guard an
    operator had turned off, which is a worse failure than ignoring a typo.
    """
    return stop_guard_mode() in ACTIVE_MODES
