# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-096 spike A3: no-null-row contract preserves partial successes.

Pre-registered acceptance (per nexus_rdr/096-research-1, id=1008):

- A3 PASS if a clean discriminator distinguishes read-failure nulls
  from partial-extraction successes, AND no rows fall into the
  ambiguous category at >5% prevalence.
- A3 FAIL if >20% of partial-looking rows are ambiguous.
- A3 BORDERLINE (5-20% ambiguous) → Phase 2 ships with a manual-review
  queue for the ambiguous tail.

Procedure:

1. Categorize every document_aspects row into one of:
   - all_null      — every aspect field null + extras={} + confidence
                     null (read-failure nulls, the #331 footgun)
   - all_populated — every aspect field non-null/non-empty
                     (full-extraction successes)
   - partial       — some aspect fields populated, others null
                     (extractor read source but didn't fill every
                     field — what we MUST NOT drop)
2. For the partial category, sub-categorize by which fields are
   populated and which are not, and look at extras + confidence
   for evidence of the partial-extraction signal.
3. Discriminator candidate: `confidence IS NULL AND extras = '{}' AND
   ALL aspect fields IS NULL` should match all_null rows and ZERO
   partial rows. Validate.

Outputs:
- spike_rdr096_a3_results.jsonl — per-row category + sub-category
- spike_rdr096_a3_summary.json — counts + discriminator validation

Run with:  uv run python scripts/spikes/spike_rdr096_a3_partial_success.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

OUT_DIR = Path(__file__).parent
RESULTS_PATH = OUT_DIR / "spike_rdr096_a3_results.jsonl"
SUMMARY_PATH = OUT_DIR / "spike_rdr096_a3_summary.json"

# Aspect fields under audit. extras + confidence are tracked
# separately because they're the proposed discriminator components,
# not aspect content per se.
ASPECT_FIELDS = (
    "problem_formulation",
    "proposed_method",
    "experimental_datasets",
    "experimental_baselines",
    "experimental_results",
)


def is_empty_field(name: str, value: Any) -> bool:
    """Return True if a field counts as empty/null for categorization.

    JSON list fields stored as strings (e.g. ``[]``) and empty TEXT
    are both considered empty. ``None`` is empty. ``"null"`` (the
    string) is empty (some upserts may have stringified null).
    """
    if value is None:
        return True
    if isinstance(value, str):
        s = value.strip()
        if s == "" or s == "null":
            return True
        if name in ("experimental_datasets", "experimental_baselines"):
            # JSON-encoded list; empty list is empty.
            try:
                parsed = json.loads(s)
                return parsed in ([], None)
            except json.JSONDecodeError:
                return False
        return False
    return False


def is_empty_extras(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        s = value.strip()
        if s in ("", "null", "{}"):
            return True
        try:
            parsed = json.loads(s)
            return parsed in ({}, None)
        except json.JSONDecodeError:
            return False
    return False


def categorize(row: dict[str, Any]) -> tuple[str, dict[str, bool]]:
    """Return (category, field_emptiness_map).

    Categories: all_null, all_populated, partial.
    """
    field_empty: dict[str, bool] = {
        f: is_empty_field(f, row.get(f)) for f in ASPECT_FIELDS
    }
    n_empty = sum(field_empty.values())
    if n_empty == len(ASPECT_FIELDS):
        return ("all_null", field_empty)
    if n_empty == 0:
        return ("all_populated", field_empty)
    return ("partial", field_empty)


def matches_proposed_discriminator(row: dict[str, Any]) -> bool:
    """Does this row match the proposed read-failure discriminator?

    Discriminator (per pre-reg 096-research-1):
       confidence IS NULL AND extras = '{}' AND ALL aspect fields IS NULL.
    """
    if row.get("confidence") is not None:
        return False
    if not is_empty_extras(row.get("extras")):
        return False
    return all(is_empty_field(f, row.get(f)) for f in ASPECT_FIELDS)


def main() -> int:
    from nexus.db.t2 import T2Database
    from nexus.commands._helpers import default_db_path

    t2 = T2Database(default_db_path())
    cur = t2.document_aspects.conn.execute(
        "SELECT collection, source_path, "
        "problem_formulation, proposed_method, experimental_datasets, "
        "experimental_baselines, experimental_results, extras, "
        "confidence, model_version, extractor_name "
        "FROM document_aspects"
    )

    cat_counts: Counter = Counter()
    discriminator_match: Counter = Counter()  # category → match count
    by_collection: dict[str, dict] = {}
    rows_out: list[dict] = []
    partial_subcategories: Counter = Counter()
    ambiguous: list[dict] = []

    for row_tuple in cur:
        keys = (
            "collection", "source_path",
            "problem_formulation", "proposed_method", "experimental_datasets",
            "experimental_baselines", "experimental_results", "extras",
            "confidence", "model_version", "extractor_name",
        )
        row = dict(zip(keys, row_tuple))

        category, field_empty = categorize(row)
        cat_counts[category] += 1

        matches = matches_proposed_discriminator(row)
        discriminator_match[(category, matches)] += 1

        info = by_collection.setdefault(
            row["collection"],
            {"all_null": 0, "all_populated": 0, "partial": 0, "discriminator_matches": 0},
        )
        info[category] += 1
        if matches:
            info["discriminator_matches"] += 1

        if category == "partial":
            sig = "+".join(
                f for f in ASPECT_FIELDS if not field_empty[f]
            ) or "(none)"
            partial_subcategories[sig] += 1
            if matches:
                # A partial row matching the read-failure discriminator
                # is the failure mode — it would be wrongly dropped.
                ambiguous.append({
                    "collection": row["collection"],
                    "source_path": row["source_path"],
                    "field_emptiness": field_empty,
                    "extras": row["extras"],
                    "confidence": row["confidence"],
                })

        rows_out.append({
            "collection": row["collection"],
            "source_path": row["source_path"],
            "category": category,
            "fields_populated": [f for f in ASPECT_FIELDS if not field_empty[f]],
            "fields_empty": [f for f in ASPECT_FIELDS if field_empty[f]],
            "discriminator_match": matches,
            "confidence": row["confidence"],
            "extras_empty": is_empty_extras(row["extras"]),
        })

    total = sum(cat_counts.values())
    n_partial = cat_counts["partial"]
    n_partial_matching_discriminator = sum(
        1 for r in rows_out
        if r["category"] == "partial" and r["discriminator_match"]
    )
    pct_ambiguous = (
        (n_partial_matching_discriminator / n_partial * 100)
        if n_partial else 0.0
    )

    print(f"=== A3 audit: {total} document_aspects rows ===")
    for cat in ("all_null", "partial", "all_populated"):
        cnt = cat_counts[cat]
        pct = (cnt / total * 100) if total else 0.0
        print(f"  {cat:15s} {cnt:5d}  ({pct:5.1f}%)")
    print()
    print(f"Discriminator: confidence IS NULL AND extras='{{}}' AND all-fields-empty")
    print(f"  Matches all_null      : {discriminator_match[('all_null', True)]:5d} of {cat_counts['all_null']}")
    print(f"  Matches partial       : {discriminator_match[('partial', True)]:5d} of {n_partial}  ← MUST be 0")
    print(f"  Matches all_populated : {discriminator_match[('all_populated', True)]:5d} of {cat_counts['all_populated']}  ← MUST be 0")
    print()
    print(f"Ambiguous rate (partial rows matching discriminator):")
    print(f"  {n_partial_matching_discriminator}/{n_partial} = {pct_ambiguous:.2f}%")
    print()
    print(f"Top partial-row populated-field signatures:")
    for sig, cnt in partial_subcategories.most_common(10):
        print(f"  {cnt:4d}  {sig}")

    # Pre-registered verdict
    print("\n=== Pre-registered A3 verdict ===")
    if (
        pct_ambiguous <= 5.0
        and discriminator_match[("all_null", True)] == cat_counts["all_null"]
        and discriminator_match[("all_populated", True)] == 0
    ):
        verdict = "PASS"
    elif pct_ambiguous > 20.0:
        verdict = "FAIL"
    else:
        verdict = "BORDERLINE"
    print(f"  ambiguous {pct_ambiguous:.2f}% → {verdict}")

    with RESULTS_PATH.open("w") as f:
        for r in rows_out:
            f.write(json.dumps(r) + "\n")
    summary = {
        "total_rows": total,
        "category_counts": dict(cat_counts),
        "discriminator_validation": {
            "all_null_matches": discriminator_match[("all_null", True)],
            "all_null_total": cat_counts["all_null"],
            "partial_matches_discriminator_BAD": discriminator_match[("partial", True)],
            "partial_total": n_partial,
            "all_populated_matches_discriminator_BAD": discriminator_match[("all_populated", True)],
            "all_populated_total": cat_counts["all_populated"],
        },
        "ambiguous_rate_pct": pct_ambiguous,
        "verdict": verdict,
        "partial_subcategories": dict(partial_subcategories),
        "by_collection": by_collection,
        "ambiguous_rows": ambiguous,
    }
    with SUMMARY_PATH.open("w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n  Wrote {RESULTS_PATH.name} ({total} rows)")
    print(f"  Wrote {SUMMARY_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
