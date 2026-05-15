# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-operator routing between Qwen and Claude dispatch backends.

Three modes of operation, controlled by ``NEXUS_DISPATCH_BACKEND``:

* ``claude`` (default when unset): all bundles route to ``claude_dispatch``.
  Preserves existing behavior — no surprise dispatch flips on a fresh
  install.
* ``qwen``: all bundles route to ``qwen_dispatch``.
* ``auto``: per-operator routing per the bench-grounded table below.

Per-operator overrides apply in **all** modes (most-granular wins):

* ``NEXUS_DISPATCH_QWEN_OPERATORS=op1,op2,…``  — pin to qwen
* ``NEXUS_DISPATCH_CLAUDE_OPERATORS=op1,op2,…`` — pin to claude

Bake-in defaults are grounded in measurement —
``qwen-coprocessor-stack/scripts/bench/`` (run 2026-05-09) covered all
ten bundleable operator types with 10/10 schema-conforming output on
both engines and content-tied or near-tied on 9/10 cases.

Initial routing pinned ``extract`` to Claude based on a single bench
sample where Qwen filtered an underscore-prefixed function. A follow-
up bench (2026-05-09 evening, 4 extract case shapes × 5 repeats = 20
dispatches) returned 20/20 oracle-match — including the original
``_normalize``-miss case at 5/5. The miss was sampling variance, not
a precision floor. ``extract`` is now in ``QWEN_OPERATORS_DEFAULT``;
``CLAUDE_OPERATORS_PINNED`` remains as an empty placeholder so future
bench-driven pins have a home without re-introducing the symbol.

Operator-chooses pattern: defaults reflect bench evidence, the operator
flips to ``auto`` (or pins per-operator) when ready, no flag day.
"""
from __future__ import annotations

import os
from typing import Iterable, Literal

__all__ = [
    "DispatchBackend",
    "QWEN_OPERATORS_DEFAULT",
    "CLAUDE_OPERATORS_PINNED",
    "pick_dispatcher",
    "pick_dispatcher_for",
    "pick_dispatcher_for_bundle",
]


DispatchBackend = Literal["qwen", "claude"]


#: Operators routed to Qwen when ``NEXUS_DISPATCH_BACKEND=auto``.
#: Bench: 10/10 schema-conforming; content tied or near-tied. ``extract``
#: was previously Claude-pinned on a single-sample miss, then validated
#: at 20/20 oracle-match (4 case shapes × 5 repeats including URL-
#: from-prose, ISO-date-from-log, CLI-flag-from-help, function-name-
#: from-code) and promoted to default. ``filter`` showed minor
#: formatting drift (kept input enum prefix) but content was correct;
#: operator preference is to favor Qwen pipelines for filter (cleaner)
#: so it lives here despite the cosmetic note.
QWEN_OPERATORS_DEFAULT: frozenset[str] = frozenset({
    "summarize", "compare", "rank", "filter",
    "aggregate", "groupby", "verify", "check", "generate",
    "extract",
})

#: Operators where Qwen has measurably lost precision in the bench.
#: Currently empty — the original ``extract`` pin was retired after a
#: 20/20 oracle-match validation. Kept as a placeholder so future
#: bench-driven pins can land without re-introducing the symbol.
CLAUDE_OPERATORS_PINNED: frozenset[str] = frozenset()


def _bare(tool: str) -> str:
    """Strip the optional ``operator_`` prefix nexus uses on some plan YAMLs."""
    return tool.removeprefix("operator_")


def _parse_env_set(name: str) -> frozenset[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def pick_dispatcher(operator_name: str) -> DispatchBackend:
    """Route a single operator to its dispatcher.

    Resolution order (highest priority first):

    1. ``NEXUS_DISPATCH_CLAUDE_OPERATORS`` env contains this operator → claude
    2. ``NEXUS_DISPATCH_QWEN_OPERATORS`` env contains this operator → qwen
    3. ``NEXUS_DISPATCH_BACKEND=qwen`` → qwen
    4. ``NEXUS_DISPATCH_BACKEND=auto`` → bake-in routing table
       (``CLAUDE_OPERATORS_PINNED`` then ``QWEN_OPERATORS_DEFAULT``)
    5. ``NEXUS_DISPATCH_BACKEND=claude`` or unset → claude

    Per-operator pins always win — they're the most granular surface
    and let the operator opt one operator into qwen on an otherwise-
    claude environment, or force one back to claude when ``auto`` is
    on.
    """
    bare = _bare(operator_name)

    # Per-operator pins win regardless of mode.
    if bare in _parse_env_set("NEXUS_DISPATCH_CLAUDE_OPERATORS"):
        return "claude"
    if bare in _parse_env_set("NEXUS_DISPATCH_QWEN_OPERATORS"):
        return "qwen"

    mode = os.environ.get("NEXUS_DISPATCH_BACKEND", "").strip().lower()
    if mode == "qwen":
        return "qwen"
    if mode == "auto":
        if bare in CLAUDE_OPERATORS_PINNED:
            return "claude"
        if bare in QWEN_OPERATORS_DEFAULT:
            return "qwen"
        # Unknown operator under auto-mode: conservative.
        return "claude"
    # mode == "claude" or unset: default Claude (preserves prior behavior).
    return "claude"


def pick_dispatcher_for(call_site: str) -> DispatchBackend:
    """Route a named non-operator call site through the same machinery.

    Some ``claude_dispatch`` callers aren't bundleable operators —
    e.g. ``taxonomy_cmd._generate_labels_batch`` (topic labeler) and
    the inline plan-miss planner. They still benefit from per-site
    routing so the operator can flip them to Qwen once bench evidence
    lands, without a code change.

    Semantics: identical to :func:`pick_dispatcher`. The two per-call
    env vars (``NEXUS_DISPATCH_QWEN_OPERATORS`` /
    ``NEXUS_DISPATCH_CLAUDE_OPERATORS``) accept call-site names too —
    "what to route" is the same surface whether it's an operator or a
    call site, so a separate env var would be bloat.

    Because call sites are not in :data:`QWEN_OPERATORS_DEFAULT`, the
    auto-mode unknown-name branch routes them to claude — cautious-by-
    default, matching #623's rollout posture. Opting one site into
    Qwen is a one-env-var flip
    (``NEXUS_DISPATCH_QWEN_OPERATORS=<call_site>``).
    """
    return pick_dispatcher(call_site)


def pick_dispatcher_for_bundle(tools: Iterable[str]) -> DispatchBackend:
    """Route an entire bundle.

    Conservative semantics: if **any** step in the bundle would route
    to Claude, the whole bundle goes to Claude. This preserves bundle
    amortization (one HTTP/subprocess call per N operators) without
    risking a Qwen-handled extract step in an otherwise-Qwen bundle.

    Empty input → claude (defensive — should not happen in practice
    since bundles must have ≥2 steps to exist).
    """
    seen = False
    for tool in tools:
        seen = True
        if pick_dispatcher(tool) == "claude":
            return "claude"
    return "qwen" if seen else "claude"
