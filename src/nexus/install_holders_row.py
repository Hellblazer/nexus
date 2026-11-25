# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ``nx doctor`` Holders row: who still runs from an older generation.

Moved out of ``health.py`` (nexus-utpuw.11's row, unchanged in behaviour) because
that file is at its size ceiling and the generation lease needed to add to it. The
row reports two sources: the process census, live, and the generation lease, from
the LAST ``gc_run`` record in ``install-gc.log`` (``gc_core`` writes it; this row
never takes a lock, so doctor can never make a starting interpreter lose its lease).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from nexus import config as _config

#: How much of the end of install-gc.log is read for the last ``gc_run`` line. A run
#: line with a long ``kept`` list and an ancestry chain is a few KB; this is generous.
_GC_LOG_TAIL_BYTES = 262_144


#: What to do about a lease the gc keeps on error, or one a filesystem cannot hold.
_LEASE_FIX = [
    "see ~/.config/nexus/logs/install-gc.log (`gc_run` lease counts and each kept generation's lease state)",
    "a generation kept on `error:version` was written by a newer release: upgrade, or remove it by hand once nothing runs from it",
]


def check_generation_holders(
    current, generations, *, tools: Path | None = None,
) -> list:
    """Who is still running from an older generation. INFORMATIONAL.

    Holders are a fact, not a fault: they keep running from their own tree and
    converge at their next spawn. A row that failed on them would contradict
    the acceptance criterion this whole arc exists to satisfy, which is that a
    live session stops being an obstacle to installing.
    """
    from nexus import install_census  # noqa: PLC0415 — deferred import
    from nexus.health import HealthResult  # noqa: PLC0415 — health imports this module

    snapshot = install_census.ps_snapshot()
    held = []
    for gen in generations:
        if gen == current:
            continue
        try:
            pids = install_census.generation_holder_pids(gen, snapshot=snapshot)
        except Exception:  # noqa: BLE001 — a census failure is not a layout fault
            continue
        if pids:
            held.append(
                f"{gen.name}: {len(pids)} ({', '.join(str(p) for p in pids[:4])}), "
                f"{_format_held_size(gen)} on disk"
            )
    # The legacy uv tree is an "older generation" too -- the oldest one there
    # is -- and it is receipt-less, so it is never in *generations*. Ask the
    # census for it by structure (nexus-k52g0: 9 processes on the 7.19.0 uv
    # tree rendered as "nothing is still bound to an older generation").
    for legacy in install_census.legacy_tree_candidates(tools=tools):
        try:
            pids = install_census.generation_holder_pids(legacy, snapshot=snapshot)
        except Exception:  # noqa: BLE001 — a census failure is not a layout fault
            continue
        if pids:
            held.append(
                f"legacy uv tree {legacy}: {len(pids)} "
                f"({', '.join(str(p) for p in pids[:4])})"
            )
    note, attention = lease_note(last_gc_run())
    suffix = f" {note}" if note else ""
    if not held:
        return [HealthResult(
            label="Holders", ok=not attention, warn=attention,
            detail="nothing is still bound to an older generation (by the process census)" + suffix,
            fix_suggestions=_LEASE_FIX if attention else [],
        )]
    return [HealthResult(
        label="Holders", ok=not attention, warn=attention,
        detail=(
            "still bound to an older generation, converging at their next "
            f"spawn — {'; '.join(held)}. A held tree is never reaped while "
            "those processes live (nexus-xn84f): end those sessions, then "
            "`nx self gc` (or the next `nx self install`) reclaims it." + suffix
        ),
        fix_suggestions=_LEASE_FIX if attention else [],
    )]


#: Files the Holders row will stat per held generation before it stops and
#: reports a lower bound. A generation is ~50k files; the cap keeps a box
#: with many stranded trees (the case nexus-xn84f fixes) from turning doctor
#: into a disk walk.
_TREE_BYTES_MAX_FILES: int = 200_000


def _tree_bytes(root: Path) -> tuple[int, bool]:
    """``(bytes under root, complete)``, following no symlinks; ``(0, True)``
    when unreadable. ``complete`` is False when the file cap stopped the walk,
    so the caller renders a lower bound."""
    total = 0
    seen = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for name in filenames:
                seen += 1
                if seen > _TREE_BYTES_MAX_FILES:
                    return total, False
                fp = os.path.join(dirpath, name)
                try:
                    if not os.path.islink(fp):
                        total += os.stat(fp).st_size
                except OSError:
                    continue
    except OSError:
        return 0, True
    return total, True


def _format_held_size(gen: Path) -> str:
    size, complete = _tree_bytes(gen)
    return _format_bytes(size) if complete else f"at least {_format_bytes(size)}"


def _format_bytes(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.0f} MB"
    return f"{n} B"


def last_gc_run(log: Path | None = None) -> dict | None:
    """The newest ``gc_run`` record in ``install-gc.log`` (or its rotated ``.1``), or
    None when there is none or the log cannot be read. Never raises."""
    base = log if log is not None else _config.nexus_config_dir() / "logs" / "install-gc.log"
    for path in (base, base.with_name(base.name + ".1")):
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - _GC_LOG_TAIL_BYTES))
                text = fh.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        for line in reversed(text.splitlines()):
            if '"gc_run"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # the first line of a tail read can be cut mid-record
            if isinstance(rec, dict) and rec.get("event") == "gc_run":
                return rec
    return None


def lease_note(record: dict | None) -> tuple[str, bool]:
    """``(sentence, needs_attention)`` for the generation lease, from a ``gc_run`` record.

    Empty sentence when the last run saw no lease at all (no record, or a record from
    before leases). *needs_attention* is True when generations were kept on a lease error
    (kept on EVERY run until fixed, about one generation's disk each) or the filesystem
    has no locks (the lease cannot protect anything there; the census alone does)."""
    if not record or "lease" not in record:
        return "", False
    counts = record.get("lease") or {}
    held = [Path(k.get("generation", "")).name for k in record.get("kept", []) if str(k.get("lease", "")).startswith("held")]
    errors = [
        f"{Path(k.get('generation', '')).name} ({k.get('lease')})"
        for k in record.get("kept", []) if str(k.get("lease", "")).startswith("error")
    ]
    parts = [f"as of the last gc run ({record.get('ts', 'unknown time')})"]
    if held:
        parts.append(f"{len(held)} generation(s) kept by a live interpreter's lease: {', '.join(held)}")
    if errors:
        parts.append(f"{len(errors)} kept on a lease error, and kept on every run until it is fixed: {', '.join(errors)}")
    if counts.get("unsupported") or record.get("lease_unsupported"):
        parts.append(
            f"the filesystem has no locks for {counts.get('unsupported', 0)} generation(s), so only the process "
            "census guards them (the lease cannot)"
        )
    attention = bool(errors) or bool(counts.get("unsupported") or record.get("lease_unsupported"))
    if len(parts) == 1:
        parts.append("no generation needed its lease")
    return "Lease " + "; ".join(parts) + ".", attention
