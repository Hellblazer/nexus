#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
judge_aspect_diffs — DEPRECATED shim.

Superseded by ``judge_parity_diffs.py``, which handles both spike-C
(aspect_extractor parity) and spike-D (tier-B tool-use parity) JSONL
output. This shim forces ``--schema spike-c`` for backward compatibility
with existing spike-C invocations and re-exports the original public
symbols.

New invocations should call ``judge_parity_diffs.py`` directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import judge_parity_diffs as _jpd  # noqa: E402

# Re-export the moved primitives so any external importer keeps working.
# Note: ``_judge`` and ``_match_pairs`` are wrapped (rather than directly
# re-exported) so that tests can monkey-patch ``judge_aspect_diffs._judge``
# and have ``judge_aspect_diffs._match_pairs`` honour the patch — preserving
# pre-generalisation test behaviour.
_rescore_row = _jpd._rescore_row  # noqa: F401
main_async = _jpd.main_async  # noqa: F401
_main_parity = _jpd.main

# Preserved for back-compat: callers that imported SET_FIELDS from this
# module (spike-C's two fields).
SET_FIELDS = _jpd.SPIKE_C_SET_FIELDS  # noqa: F401


async def _judge(backend, a, b, *, prose=False):
    """Shim wrapper. Tests monkey-patch this name."""
    return await _jpd._judge(backend, a, b, prose=prose)


async def _match_pairs(backend, only_c, only_q):
    """Shim ``_match_pairs`` that calls this module's ``_judge`` so
    tests patching ``judge_aspect_diffs._judge`` see their stub honoured.
    Logic mirrors ``judge_parity_diffs._match_pairs``."""
    matched: set = set()
    matched_q: set = set()
    trace: list = []
    for a in only_c:
        for b in only_q:
            if b in matched_q:
                continue
            try:
                verdict = await _judge(backend, a, b)
            except Exception as exc:
                trace.append({"a": a, "b": b, "error": str(exc)})
                continue
            trace.append({
                "a": a, "b": b,
                "equivalent": bool(verdict.get("equivalent")),
                "reason": str(verdict.get("reason", "")),
            })
            if verdict.get("equivalent"):
                matched.add((a, b))
                matched_q.add(b)
                break
    return matched, trace


def main(argv: list[str] | None = None) -> int:
    """Dispatch to the generalised judge, forcing the spike-C schema.

    Any caller-supplied ``--schema`` is overridden so behaviour matches
    the pre-generalisation script exactly.
    """
    if argv is None:
        argv = sys.argv[1:]
    # Strip any caller --schema flag (both forms) and force spike-c.
    cleaned: list[str] = []
    skip_next = False
    for tok in argv:
        if skip_next:
            skip_next = False
            continue
        if tok == "--schema":
            skip_next = True
            continue
        if tok.startswith("--schema="):
            continue
        cleaned.append(tok)
    cleaned += ["--schema", "spike-c"]
    return _main_parity(cleaned)


if __name__ == "__main__":
    sys.exit(main())
