# SPDX-License-Identifier: AGPL-3.0-or-later
"""NO-SQLITE tripwire (Hal directive 2026-07-18, bead nexus-mx635).

Nexus is MIGRATING from SQLite TO PG, in EVERY mode. There is NO SQLite
hybrid mode. SQLite is a migration SOURCE only, never a destination:
new persistent state goes to PG through Liquibase via the engine (the PG
bundle ships with every install — local mode's endpoint is the bundled
local PG, same shape as service mode). Directive of record: T2
``nexus/directive-no-sqlite-pg-everywhere``; AGENTS.md hot rule;
retirement epic nexus-146xx.

This suite FREEZES the 2026-07-18 census of client-side SQLite so the
debt can only shrink:

* per-file counts of inline SQLite DDL (``CREATE [VIRTUAL] TABLE``),
* per-file counts of ``ALTER TABLE`` statements (RDR-186 P0 harden
  decision, Hal 2026-07-18 — closes the schema-GROWTH blind spot the
  CREATE-only regex cannot see), and
* per-file counts of ``# epsilon-allow:`` overrides — the
  storage-boundary lint (RDR-120) accepts any >=8-char reason, which
  made exemptions self-service; freezing the population makes each NEW
  one a failing test instead of a comment.

PER-FILE COUNTS, not file presence (final-critique High, 2026-07-18):
the reverted ``leg_convergence`` near-miss would have added a second
``CREATE TABLE`` to ``wire_reid.py`` — a file already in the census — so
a file-granularity freeze would never have seen the exact incident that
spawned the directive. A count going UP in any file fails; going DOWN
fails until the census entry is updated (exact-census discipline: a
stale entry is a lie about the debt). Growth in a NEW file fails
likewise.

Exemptions are Hal's decisions, never code comments: an increment to
any count below requires an explicit Hal decision recorded on a bead,
referenced next to the entry. NOTE the honest limit: nothing here
mechanically verifies that bead reference — enforcement of *that* rule
is review of any diff touching this file (one census file to watch,
instead of comments scattered across the tree).

Scanner is deliberately dumb (regex count, per file): the goal is a
tripwire that cannot be silently satisfied, not a precise linter —
``storage_boundary_lint.py`` remains the AST-precise boundary check
(its numeric ratchet covers ``sqlite3.connect`` call sites, a different
axis than DDL statements).
"""
from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC = REPO_ROOT / "src" / "nexus"

_DDL_RE = re.compile(r"CREATE\s+(?:VIRTUAL\s+)?TABLE", re.IGNORECASE)
_ALTER_RE = re.compile(r"ALTER\s+TABLE", re.IGNORECASE)
_EPSILON_RE = re.compile(r"#\s*epsilon-allow\s*:")

_DIRECTIVE = (
    "NO new SQLite (Hal directive 2026-07-18): nexus is migrating SQLite -> PG "
    "in EVERY mode; there is no SQLite hybrid mode. New persistent state goes "
    "to PG through Liquibase via the engine. Raising a count in this census "
    "requires an explicit Hal decision recorded on a bead, referenced inline. "
    "See T2 nexus/directive-no-sqlite-pg-everywhere and epic nexus-146xx."
)

#: 2026-07-18 census — per-file counts of inline SQLite DDL statements.
#: Retirement targets (epic nexus-146xx), never precedents.
DDL_CENSUS: dict[str, int] = {
    "src/nexus/aspect_promotion.py": 1,           # aspect_promotion_log stray (dodges migrations.py)
    "src/nexus/db/migrations.py": 25,             # the sanctioned-until-retired T2 registry
    "src/nexus/db/t2/aspect_extraction_queue.py": 3,
    "src/nexus/db/t2/catalog.py": 9,
    "src/nexus/db/t2/catalog_taxonomy.py": 4,
    "src/nexus/db/t2/chash_index.py": 2,
    "src/nexus/db/t2/document_aspects.py": 1,
    "src/nexus/db/t2/document_highlights.py": 1,
    "src/nexus/db/t2/memory_store.py": 3,
    "src/nexus/db/t2/plan_library.py": 3,
    "src/nexus/db/t2/telemetry.py": 4,
    "src/nexus/migration/wire_reid.py": 1,        # chash_remap.db (PG twin = RDR-185 .16 Liquibase work)
}

#: 2026-07-18 census — per-file counts of ``ALTER TABLE`` statements
#: (RDR-186 P0 adjudication, Hal 2026-07-18: HARDEN — the CREATE-only DDL
#: regex is blind to schema GROWTH on an already-censused store, which is
#: exactly how the reverted ``leg_convergence`` near-miss would have
#: landed on a second attempt; bead nexus-146xx.2).
ALTER_CENSUS: dict[str, int] = {
    "src/nexus/aspect_promotion.py": 5,           # 1 real + 4 prose self-mentions
    "src/nexus/commands/enrich.py": 1,            # docstring mirror of aspect_promotion's DDL (censused there) — not own debt
    "src/nexus/db/migrations.py": 36,             # 27 real + 9 prose self-mentions
    "src/nexus/db/t2/aspect_extraction_queue.py": 2,
    "src/nexus/db/t2/catalog.py": 9,              # 8 real + 1 self-referential SQL comment
    "src/nexus/db/t2/chash_index.py": 1,
    "src/nexus/db/t2/memory_store.py": 1,         # comment mirror of migrations.py DDL (censused there) — not own debt
    "src/nexus/db/t2/plan_library.py": 1,         # comment mirror of migrations.py DDL (censused there) — not own debt
    "src/nexus/health.py": 1,                     # PG/Liquibase RLS syntax in a comment (health.py:1673) — not SQLite debt
    "src/nexus/plans/repair.py": 1,               # docstring mention only; module runs NO DDL — not own debt
}

#: 2026-07-18 census — per-file counts of ``# epsilon-allow:`` overrides.
#: Each is standing debt: the override was self-granted by comment, which
#: the directive retires going forward.
EPSILON_CENSUS: dict[str, int] = {
    "src/nexus/_session_end_launcher.py": 1,
    "src/nexus/aspect_promotion.py": 6,
    "src/nexus/catalog/catalog_owners.py": 1,
    "src/nexus/collection_audit.py": 3,
    "src/nexus/collection_health.py": 3,
    "src/nexus/commands/_helpers.py": 1,
    "src/nexus/commands/aspects.py": 7,
    "src/nexus/commands/catalog.py": 1,
    "src/nexus/commands/catalog_cmds/backfill.py": 3,
    "src/nexus/commands/catalog_cmds/report.py": 3,
    "src/nexus/commands/collection.py": 1,
    "src/nexus/commands/daemon.py": 1,
    "src/nexus/commands/doc.py": 3,
    "src/nexus/commands/doctor.py": 6,
    "src/nexus/commands/enrich.py": 9,
    "src/nexus/commands/index.py": 3,
    "src/nexus/commands/plan.py": 2,
    "src/nexus/commands/rdr.py": 1,
    "src/nexus/commands/search_cmd.py": 1,
    "src/nexus/commands/storage_cmd.py": 1,
    "src/nexus/commands/taxonomy_cmd.py": 17,
    "src/nexus/commands/tier_status.py": 1,
    "src/nexus/commands/upgrade.py": 3,
    "src/nexus/console/routes/health.py": 1,
    "src/nexus/context.py": 1,
    "src/nexus/db/t2/chash_etl.py": 1,
    "src/nexus/doc_indexer.py": 1,
    "src/nexus/health.py": 2,
    "src/nexus/indexer.py": 1,
    "src/nexus/mcp_infra.py": 4,
    "src/nexus/merge_candidates.py": 2,
    "src/nexus/migration/chroma_read.py": 2,
    "src/nexus/migration/guided_upgrade.py": 1,
    "src/nexus/migration/orchestrator.py": 1,
    "src/nexus/migration/remap_cascade.py": 1,
    "src/nexus/migration/vector_etl.py": 1,
    "src/nexus/migration/wire_reid.py": 1,
    "src/nexus/operators/aspect_sql.py": 6,
    "src/nexus/storage_boundary_lint.py": 10,     # defines the token; matches its own docs
    "src/nexus/taxonomy.py": 1,
    "src/nexus/upgrade_ladder/rungs/t2_schema.py": 1,
}


def _count_matches(pattern: re.Pattern[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        n = len(pattern.findall(path.read_text(encoding="utf-8")))
        if n:
            counts[path.relative_to(REPO_ROOT).as_posix()] = n
    return counts


def _census_delta(live: dict[str, int], census: dict[str, int]) -> tuple[list[str], list[str]]:
    """(grown, shrunk): files whose live count exceeds the census (new files
    included), and files whose live count fell below it (removed files
    included) — the census must be updated DOWNWARD to match reality."""
    grown = sorted(
        f"{f}: {live.get(f, 0)} > {census.get(f, 0)}"
        for f in live.keys() | census.keys()
        if live.get(f, 0) > census.get(f, 0)
    )
    shrunk = sorted(
        f"{f}: {live.get(f, 0)} < {census.get(f, 0)}"
        for f in live.keys() | census.keys()
        if live.get(f, 0) < census.get(f, 0)
    )
    return grown, shrunk


def test_no_new_inline_ddl() -> None:
    grown, shrunk = _census_delta(_count_matches(_DDL_RE), DDL_CENSUS)
    assert not grown, f"inline SQLite DDL GREW at {grown}: {_DIRECTIVE}"
    assert not shrunk, (
        f"stale DDL_CENSUS count(s) {shrunk}: DDL was removed (good!) — "
        "lower the census entry so the frozen debt ledger stays exact."
    )


def test_no_new_alter_table() -> None:
    grown, shrunk = _census_delta(_count_matches(_ALTER_RE), ALTER_CENSUS)
    assert not grown, f"ALTER TABLE statements GREW at {grown}: {_DIRECTIVE}"
    assert not shrunk, (
        f"stale ALTER_CENSUS count(s) {shrunk}: ALTER DDL was removed (good!) — "
        "lower the census entry so the frozen debt ledger stays exact."
    )


def test_no_new_epsilon_allows() -> None:
    grown, shrunk = _census_delta(_count_matches(_EPSILON_RE), EPSILON_CENSUS)
    assert not grown, (
        f"'# epsilon-allow:' population GREW at {grown}: self-service "
        f"exemptions are retired. {_DIRECTIVE}"
    )
    assert not shrunk, (
        f"stale EPSILON_CENSUS count(s) {shrunk}: an override was removed "
        "(good!) — lower the census entry so the frozen debt ledger stays exact."
    )


def test_census_is_nonvacuous() -> None:
    """The tripwire must actually see the debt it freezes: if the scanner
    ever reads zero sites while the census is non-empty, the scan itself
    broke (path drift, encoding) and the `grown` half above would go
    vacuously green."""
    assert _count_matches(_DDL_RE)
    assert _count_matches(_ALTER_RE)
    assert _count_matches(_EPSILON_RE)
    assert sum(DDL_CENSUS.values()) >= 15
    # Loose sanity floor like its siblings (critic 2026-07-18): legitimate
    # D6 shrink-side retirements must not trip it — it only proves the
    # scanner still sees a substantial census.
    assert sum(ALTER_CENSUS.values()) >= 20
    assert sum(EPSILON_CENSUS.values()) >= 40
