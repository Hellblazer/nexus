# SPDX-License-Identifier: AGPL-3.0-or-later
"""BUG-0148 invariant lint: a changeset that REWRITES a table must ANALYZE it.

WHY THIS EXISTS — the invariant was adopted, written down, and then missed
three more times by two more authors.

  2026-07-19 (conexus-xpg7 / BUG-0148, v0.1.48): an ``ALTER TYPE`` conversion
  rewrote its tables and reset planner statistics. The stale-stats planner
  flipped sparse-text-gate hybrid queries off the GIN-bitmap plan onto the
  budget-bounded HNSW plan, and hybrid-search returned ZERO rows in production
  while /health, /version, the smoke round-trip AND the aggregate STEP-6 exit
  code all stayed green. Remediation was a manual ANALYZE.
  ``rdr180-16-analyze-rewritten-tables`` fixed it and recorded the rule:
  "a migration that rewrites a table ANALYZEs it in the same changelog pass."

  2026-07-27: ``memory-002-1`` and ``catalog-017-0`` each rewrote a table by
  re-adding a STORED generated column, and neither ANALYZEd. Caught by conexus
  on the LIVE cluster after deploy (T2 [21164]) — ``catalog_documents`` stats
  were 5757 minutes stale, ``memory`` 715. It did not bite only because both
  tables are small and off the hybrid path.

  Same day: writing THIS lint immediately surfaced two more that had never been
  noticed — ``catalog-015-0`` (2026-07-13) and
  ``catalog-002-1-temporal-typing``. Both are now covered by ``analyze-002``.

Four misses across three authors, with the rule already documented in the tree,
is what makes this mechanical rather than another comment nobody reads.

WHY STALE STATS DO NOT SELF-HEAL: a rewritten table looks FRESH to autovacuum,
so autoanalyze may never re-trigger on a read-mostly store. The bad plan can
persist indefinitely.

WHAT COUNTS AS A REWRITE (PostgreSQL, conservative — the forms this corpus uses):
  - ``ADD COLUMN ... GENERATED ALWAYS AS (...) STORED`` — computes the column
    for every existing row.
  - ``ALTER COLUMN ... TYPE`` — the BUG-0148 original.
A bare ``DROP COLUMN`` is metadata-only and does NOT rewrite, so it is not
flagged; it appears here only as the first half of a drop-then-re-add pair, and
the re-add is what triggers.

WHAT THE RULE IS, PRECISELY: an ANALYZE of that table must appear in the
rewriting changeset OR any LATER one in master include order. That is what "the
same changelog pass" means, and getting this wrong in either direction matters —
requiring same-changeset would falsely flag rdr180-3..7 (correctly remediated by
rdr180-16 further down the same file), while allowing an ANALYZE *earlier* than
the rewrite would accept re-stamping the OLD statistics, which is worse than
nothing.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.test_changelog_rls_lint import (
    CHANGELOG_DIR,
    _split_statements,
    iter_changesets,
    parse_master_include_order,
)

_MASTER = "db.changelog-master.xml"

#: ``ALTER TABLE <schema>.<table> ... ADD COLUMN ... GENERATED ... STORED``
_ADD_GENERATED_RE = re.compile(
    r"\bALTER\s+TABLE\s+(?:ONLY\s+)?([a-z_][\w.]*)\b.*?"
    r"\bADD\s+COLUMN\b.*?\bGENERATED\b.*?\bSTORED\b",
    re.IGNORECASE | re.DOTALL,
)
#: ``ALTER TABLE <schema>.<table> ... ALTER COLUMN ... TYPE`` (the BUG-0148 original)
_ALTER_TYPE_RE = re.compile(
    r"\bALTER\s+TABLE\s+(?:ONLY\s+)?([a-z_][\w.]*)\b.*?"
    r"\bALTER\s+(?:COLUMN\s+)?\w+\s+(?:SET\s+DATA\s+)?TYPE\b",
    re.IGNORECASE | re.DOTALL,
)
_ANALYZE_RE = re.compile(r"\bANALYZE\s+(?:VERBOSE\s+)?([a-z_][\w.]*)", re.IGNORECASE)


def _rewrites_in(sql: str) -> set[str]:
    """Tables this SQL rewrites, lowercased and schema-qualified as written."""
    found: set[str] = set()
    for stmt in _split_statements(sql):
        for rx in (_ADD_GENERATED_RE, _ALTER_TYPE_RE):
            for m in rx.finditer(stmt):
                found.add(m.group(1).lower())
    return found


def _analyzes_in(sql: str) -> set[str]:
    return {m.group(1).lower() for m in _ANALYZE_RE.finditer(sql)}


def _ordered_changesets() -> list[tuple[str, str, str]]:
    """(basename, changeset_id, sql) in master include order."""
    out: list[tuple[str, str, str]] = []
    for basename in parse_master_include_order(CHANGELOG_DIR / _MASTER):
        if not (CHANGELOG_DIR / basename).exists():
            continue
        for cs_id, sql, _unscanned in iter_changesets(CHANGELOG_DIR, basename):
            out.append((basename, cs_id, sql))
    return out


def _is_baseline(basename: str) -> bool:
    """Baselines create their tables EMPTY in the same pass.

    A generated column added to a table with no rows has no statistics to
    invalidate, and ANALYZE on an empty table is a no-op — so requiring it there
    would be noise that trains people to add exemptions.
    """
    return basename.endswith("-baseline.xml")


def test_every_table_rewrite_is_analyzed_at_or_after_it() -> None:
    """The BUG-0148 invariant, mechanically.

    Falsify by deleting any ANALYZE line from analyze-002 or rdr180-16 — the
    changesets it covers reappear here immediately.
    """
    ordered = _ordered_changesets()
    violations: list[str] = []

    for i, (basename, cs_id, sql) in enumerate(ordered):
        if _is_baseline(basename):
            continue
        rewritten = _rewrites_in(sql)
        if not rewritten:
            continue
        # ANALYZE in this changeset or ANY later one — "the same changelog pass".
        later = set()
        for _b, _c, later_sql in ordered[i:]:
            later |= _analyzes_in(later_sql)
        missing = rewritten - later
        if missing:
            violations.append(
                f"  {basename}::{cs_id} rewrites {sorted(missing)} and nothing "
                f"ANALYZEs {'it' if len(missing) == 1 else 'them'} afterwards"
            )

    assert not violations, (
        "BUG-0148 invariant: a changeset that rewrites a table must be followed "
        "by an ANALYZE of that table.\n" + "\n".join(violations) + "\n\n"
        "Re-adding a STORED generated column or ALTERing a column TYPE rewrites "
        "the table and leaves planner statistics describing the PRE-rewrite "
        "shape. A rewritten table also looks fresh to autovacuum, so autoanalyze "
        "may never re-trigger — the stale stats persist. At v0.1.48 this "
        "returned ZERO rows from hybrid-search in production while every health "
        "signal stayed green.\n"
        "Put `ANALYZE <schema>.<table>;` in the rewriting changeset itself. It "
        "is transaction-safe, takes no exclusive lock, and is idempotent."
    )


def test_lint_detects_a_synthetic_violation() -> None:
    """NON-VACUITY. Without this, a regex that matched nothing would pass.

    The corpus is (now) clean, so the only way to prove the detector works is to
    feed it a rewrite it must catch and an ANALYZE it must accept.
    """
    rewrite = (
        "ALTER TABLE nexus.widgets ADD COLUMN v tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', name)) STORED;"
    )
    alter_type = "ALTER TABLE nexus.widgets ALTER COLUMN qty TYPE bigint;"

    assert _rewrites_in(rewrite) == {"nexus.widgets"}, "generated-column rewrite not detected"
    assert _rewrites_in(alter_type) == {"nexus.widgets"}, "ALTER TYPE rewrite not detected"
    assert _analyzes_in("ANALYZE nexus.widgets;") == {"nexus.widgets"}
    # A bare DROP COLUMN is metadata-only and must NOT be treated as a rewrite,
    # or every drop-then-re-add pair would double-report.
    assert _rewrites_in("ALTER TABLE nexus.widgets DROP COLUMN v;") == set()


def test_baselines_are_excluded_for_a_stated_reason() -> None:
    """The baseline carve-out is real, not a blanket skip that hides everything.

    If baselines ever stopped containing rewrites, this carve-out would be dead
    code silently weakening the lint — so assert it is actually load-bearing.
    """
    baseline_rewrites = {
        f"{b}::{c}"
        for b, c, sql in _ordered_changesets()
        if _is_baseline(b) and _rewrites_in(sql)
    }
    assert baseline_rewrites, (
        "no baseline changeset rewrites a table any more — the _is_baseline "
        "carve-out is now dead code and should be deleted rather than left to "
        "silently weaken the lint"
    )
