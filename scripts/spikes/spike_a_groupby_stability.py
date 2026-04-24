#!/usr/bin/env -S uv run python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-093 Spike A — operator_groupby partition-stability probe.

Bead ``nexus-g8l9``. Measures how often the partition (set of groups,
keyed by item ids) agrees across N independent ``claude -p`` calls
for the same ``(items, key)`` input. Operator_groupby itself does not
exist yet in core.py — this spike validates the prompt + schema shape
proposed in RDR-093 §Technical Design before Phase 1 implementation.

Protocol:
  * Load 20 partition fixtures from
    ``scripts/spikes/spike_a_groupby_fixtures.py``.
  * For each fixture, run the operator_groupby prompt 5 times via
    ``nexus.operators.dispatch.claude_dispatch`` with the RDR-093 schema.
  * Capture each run as a JSONL record in
    ``scripts/spikes/spike_a_groupby_results.jsonl``.
  * Aggregate per-fixture stability:
      - ``modal_partition_loose``: the most-common set-of-frozensets
        (partitions by item ids only, ignoring key labels).
      - ``modal_partition_strict``: the most-common set of
        ``(key_value, frozenset(ids))`` tuples (labels matter).
      - ``stability_loose / stability_strict``: runs matching modal / 5.
  * Aggregate across fixtures: fully-stable rate (loose, strict),
    micro-stability rate, error rate, schema-violation rate.
  * Persist summary JSON to ``scripts/spikes/spike_a_groupby_summary.json``.

Expected runtime: 100 calls at ~25-45s each = 40-75 minutes. Runs
serially.

Usage::

    uv run python scripts/spikes/spike_a_groupby_stability.py

Append-only results file. Delete it to start fresh.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(REPO_ROOT / "src"))

from spike_a_groupby_fixtures import FIXTURES  # noqa: E402

from nexus.operators.dispatch import claude_dispatch  # noqa: E402

REPEATS: int = 5
RESULTS_PATH = SCRIPT_DIR / "spike_a_groupby_results.jsonl"
SUMMARY_PATH = SCRIPT_DIR / "spike_a_groupby_summary.json"

# RDR-093 §Technical Design output schema (verbatim).
GROUPBY_SCHEMA: dict = {
    "type": "object",
    "required": ["groups"],
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["key_value", "items"],
                "properties": {
                    "key_value": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                },
            },
        },
    },
}


def _build_prompt(items: list[dict], key: str) -> str:
    """Construct the operator_groupby prompt per RDR-093 §Technical Design."""
    items_json = json.dumps(items)
    return (
        f"Partition the following items by this key: {key}\n"
        f"Output a list of groups. Each group has a string ``key_value`` "
        f"(the partition label, e.g. a year, a fault model, a system "
        f"property) and an ``items`` array carrying each item's full "
        f"content INLINE — preserve the original ``id`` field and any "
        f"other fields verbatim. Every input item appears in exactly "
        f"one group's ``items``. Items the partition cannot confidently "
        f"assign go in a group with ``key_value`` of ``\"unassigned\"``.\n\n"
        f"Do not reference items by id-only — carry the full item "
        f"dicts in each group's ``items`` array so downstream operators "
        f"see the content without a separate lookup.\n\n"
        f"Items:\n{items_json}"
    )


def _extract_partition(result: dict | None) -> tuple[
    frozenset[frozenset[str]] | None,
    frozenset[tuple[str, frozenset[str]]] | None,
    bool,
]:
    """Convert a groupby result into loose + strict partition signatures.

    Returns ``(partition_loose, partition_strict, schema_ok)``. Both
    signatures are ``None`` when the result is malformed or violates the
    RDR-093 schema (missing groups / items / key_value, or items without
    an ``id`` field). ``schema_ok`` flags whether the result matched the
    expected shape closely enough to extract a partition.
    """
    if not isinstance(result, dict):
        return None, None, False
    groups = result.get("groups")
    if not isinstance(groups, list) or not groups:
        return None, None, False

    loose_sets: list[frozenset[str]] = []
    strict_pairs: list[tuple[str, frozenset[str]]] = []
    schema_ok = True
    for g in groups:
        if not isinstance(g, dict):
            schema_ok = False
            continue
        kv = g.get("key_value")
        items = g.get("items")
        if not isinstance(kv, str) or not isinstance(items, list):
            schema_ok = False
            continue
        ids: list[str] = []
        for it in items:
            if not isinstance(it, dict):
                schema_ok = False
                continue
            iid = it.get("id")
            if isinstance(iid, str):
                ids.append(iid)
            else:
                schema_ok = False
        id_set = frozenset(ids)
        loose_sets.append(id_set)
        strict_pairs.append((kv, id_set))

    if not loose_sets:
        return None, None, False
    return (
        frozenset(loose_sets),
        frozenset(strict_pairs),
        schema_ok,
    )


async def probe_one(fixture: dict, repeat_ix: int) -> dict:
    """Run one operator_groupby invocation and capture a compact record."""
    prompt = _build_prompt(fixture["items_inline"], fixture["key"])
    t0 = time.monotonic()
    result: dict | None
    error: str | None
    try:
        result = await claude_dispatch(
            prompt=prompt,
            json_schema=GROUPBY_SCHEMA,
            timeout=300.0,
        )
        error = None
    except Exception as exc:  # noqa: BLE001 - capture everything
        result = None
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - t0

    partition_loose, partition_strict, schema_ok = _extract_partition(result)
    n_groups = len(result.get("groups", [])) if isinstance(result, dict) else 0

    return {
        "fixture_id": fixture["id"],
        "topic": fixture.get("topic", ""),
        "key": fixture["key"],
        "expected_modal_hint": fixture.get("expected_modal_hint"),
        "repeat_ix": repeat_ix,
        "elapsed_s": round(elapsed, 2),
        "n_groups": n_groups,
        "schema_ok": schema_ok,
        # JSON-serialise the canonical partitions so the JSONL log is
        # human-readable and reloadable for ad-hoc analysis.
        "partition_loose": (
            sorted([sorted(s) for s in partition_loose])
            if partition_loose is not None
            else None
        ),
        "partition_strict": (
            sorted(
                [(kv, sorted(ids)) for kv, ids in partition_strict]
            )
            if partition_strict is not None
            else None
        ),
        "error": error,
    }


def _signature(record: dict, *, strict: bool) -> str | None:
    """Stable string signature for partition equality testing."""
    field = "partition_strict" if strict else "partition_loose"
    val = record.get(field)
    if val is None:
        return None
    return json.dumps(val, sort_keys=True)


def _aggregate(records: list[dict]) -> dict:
    by_fid: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_fid[r["fixture_id"]].append(r)

    per_fixture: list[dict] = []
    fully_loose = 0
    fully_strict = 0
    micro_loose_runs = 0
    micro_strict_runs = 0
    schema_ok_runs = 0
    for fid, recs in by_fid.items():
        loose_sigs = [_signature(r, strict=False) for r in recs]
        strict_sigs = [_signature(r, strict=True) for r in recs]
        valid_loose = [s for s in loose_sigs if s is not None]
        valid_strict = [s for s in strict_sigs if s is not None]
        if valid_loose:
            modal_loose, modal_loose_count = Counter(valid_loose).most_common(1)[0]
        else:
            modal_loose, modal_loose_count = None, 0
        if valid_strict:
            modal_strict, modal_strict_count = Counter(valid_strict).most_common(1)[0]
        else:
            modal_strict, modal_strict_count = None, 0
        agreement_loose = (
            sum(1 for s in loose_sigs if s == modal_loose)
            if modal_loose is not None
            else 0
        )
        agreement_strict = (
            sum(1 for s in strict_sigs if s == modal_strict)
            if modal_strict is not None
            else 0
        )
        if modal_loose is not None and agreement_loose == len(recs):
            fully_loose += 1
        if modal_strict is not None and agreement_strict == len(recs):
            fully_strict += 1
        micro_loose_runs += agreement_loose
        micro_strict_runs += agreement_strict
        schema_ok_runs += sum(1 for r in recs if r.get("schema_ok"))
        per_fixture.append({
            "fixture_id": fid,
            "topic": recs[0]["topic"],
            "expected_modal_hint": recs[0].get("expected_modal_hint"),
            "runs_total": len(recs),
            "runs_with_partition": len(valid_loose),
            "modal_loose": json.loads(modal_loose) if modal_loose else None,
            "modal_strict": json.loads(modal_strict) if modal_strict else None,
            "agreement_loose": agreement_loose,
            "agreement_strict": agreement_strict,
            "stability_loose": (
                round(agreement_loose / len(recs), 3) if recs else None
            ),
            "stability_strict": (
                round(agreement_strict / len(recs), 3) if recs else None
            ),
            "errored_runs": sum(1 for r in recs if r.get("error")),
        })

    total_runs = sum(f["runs_total"] for f in per_fixture)
    fixtures_n = len(per_fixture)
    return {
        "per_fixture": per_fixture,
        "aggregate": {
            "fixtures_total": fixtures_n,
            "fixtures_fully_stable_loose": fully_loose,
            "fixtures_fully_stable_strict": fully_strict,
            "fully_stable_rate_loose": (
                round(fully_loose / fixtures_n, 3) if fixtures_n else None
            ),
            "fully_stable_rate_strict": (
                round(fully_strict / fixtures_n, 3) if fixtures_n else None
            ),
            "micro_stability_rate_loose": (
                round(micro_loose_runs / total_runs, 3) if total_runs else None
            ),
            "micro_stability_rate_strict": (
                round(micro_strict_runs / total_runs, 3) if total_runs else None
            ),
            "total_runs": total_runs,
            "schema_ok_runs": schema_ok_runs,
            "schema_ok_rate": (
                round(schema_ok_runs / total_runs, 3) if total_runs else None
            ),
            "errored_runs": sum(
                1 for r in records if r.get("error")
            ),
            "error_rate": (
                round(
                    sum(1 for r in records if r.get("error")) / total_runs, 3
                )
                if total_runs
                else None
            ),
        },
    }


async def main() -> int:
    records: list[dict] = []
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_PATH.open("a") as out:
        total = len(FIXTURES) * REPEATS
        idx = 0
        t_start = time.monotonic()
        for fixture in FIXTURES:
            for r_ix in range(REPEATS):
                idx += 1
                rec = await probe_one(fixture, r_ix)
                out.write(json.dumps(rec) + "\n")
                out.flush()
                records.append(rec)
                elapsed_total = time.monotonic() - t_start
                rate = idx / elapsed_total if elapsed_total else 0
                eta_s = (total - idx) / rate if rate > 0 else 0
                groups_label = (
                    f"groups={rec['n_groups']}"
                    if rec.get("n_groups") is not None
                    else "groups=?"
                )
                err_label = "" if not rec.get("error") else f" err={rec['error'][:60]}"
                print(
                    f"[{idx}/{total}] {rec['fixture_id']} r{r_ix} "
                    f"{groups_label} schema={rec['schema_ok']} "
                    f"{rec['elapsed_s']:.1f}s ETA {eta_s/60:.1f}m{err_label}",
                    flush=True,
                )

    summary = _aggregate(records)
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    print("\n=== SUMMARY ===")
    print(json.dumps(summary["aggregate"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
