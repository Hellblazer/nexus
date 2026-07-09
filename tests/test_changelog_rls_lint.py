# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1wjmq / nexus-php10: FORCE-RLS changelog DML static-lint tripwire.

Provenance: nexus-1wjmq (2026-07-08 v0.1.33 cloud deploy incident). Migration-time
``<sql>`` row-DML against a FORCE ROW LEVEL SECURITY table silently no-ops for the
non-BYPASSRLS Liquibase owner role (``nexus_admin``): FORCE applies even to the table
owner, and with no ``nexus.tenant`` GUC set during migration every RLS predicate
evaluates false, so the DML's row-security-filtered candidate set is empty. CI never
caught the class because Testcontainers runs Liquibase as the Postgres superuser
(implicit BYPASSRLS). catalog-013-0 hit this and took down the v0.1.33 deploy;
five other historical members were vacuously harmless (their own downstream
constraint proves the data was already clean). This lint statically enumerates the
class so it cannot grow silently.

Two approved-safe shapes (design nx memory get -p nexus -t
design-php10-force-rls-changelog-lint.md, locked 2026-07-09):

1. **Toggle-wrapped** (the catalog-013-1b fix pattern): every FORCE table the DML
   touches (target OR a table it reads via JOIN/subquery) is toggled
   ``NO FORCE ROW LEVEL SECURITY`` earlier in the SAME changeset, the DML runs while
   visible, and ``FORCE ROW LEVEL SECURITY`` is restored before the changeset ends.
   A toggle that is never restored is its own distinct finding (an isolation-window
   leak, not a no-op) — reported separately from naked DML.

2. **Same-changeset backstop**: the DML's TARGET table gets an RLS-bypassing
   integrity proof in the SAME changeset — an immediately-valid (no ``NOT VALID``)
   ``ADD CONSTRAINT ... FOREIGN KEY``/``UNIQUE``, a ``CREATE UNIQUE INDEX``, or a
   ``VALIDATE CONSTRAINT``. Postgres referential-integrity / uniqueness checks always
   bypass row security (full scan, table-owner semantics), so this is "safe-or-loud":
   either the DML actually ran (RLS was somehow visible) and the backstop trivially
   passes, or the DML silently no-op'd, stale/orphaned rows remain, and the backstop
   FAILS the migration LOUDLY. A backstop is only sound on the DML's own target table:
   the argument relies on the target table's own row-security policy gating the whole
   statement's candidate row set to all-or-nothing, which does not extend to tables
   only read via JOIN/subquery — a backstop in a LATER, separate changeset does
   **NOT** count (this is exactly catalog-013-0's bug shape: naked DML in one
   changeset, ``VALIDATE CONSTRAINT`` in a later one, silently a no-op with no proof
   until the later VALIDATE happened to fail).

Function/trigger bodies (``CREATE [OR REPLACE] FUNCTION/TRIGGER ... AS $$...$$``,
any dollar-quote tag) are EXEMPT: they define code that runs later, at CALL time,
under the caller's own SECURITY INVOKER context, not under the Liquibase migration
role. An anonymous ``DO $$ ... $$`` block is DIFFERENT and is **NOT** exempt: it
executes IMMEDIATELY at migration time under the Liquibase role's own session,
so DML inside it has the identical no-op exposure as top-level SQL
(ground-truth finding: taxonomy-004-1's entire dangerous DML sequence lives inside
a ``DO $$ ... $$`` block). The stripper below special-cases this.

Documented static blind spot: ``SELECT some_fn()`` invoking a function that performs
DML internally is invisible to this analyzer (it only recognizes literal top-level
``INSERT``/``UPDATE``/``DELETE`` keywords, not arbitrary function calls). The real
corpus contains exactly one instance (catalog-014-0's
``SELECT nexus.manifest_backfill();``), and it happens to be safe by explicit
toggle-wrap discipline around the SELECT itself, not because the analyzer sees
inside the function body.

Why Python-side and not the Java/Testcontainers suite: the Java ``service-ci``
workflow is advisory only (does not gate auto-merge — see AGENTS.md "Java schema
changes verify with FULL mvn suite"), and Testcontainers itself runs Liquibase as
the Postgres superuser, which structurally cannot reproduce the RLS-owner no-op this
lint exists to catch. This test has no such blind spot: it is required, always-on
Python CI (``pyproject.toml`` addopts only excludes ``integration``/``slow``/
``stress`` markers; this file carries none of them).

Additional documented static blind spots (substantive-critic review, nexus-fqnii /
nexus-vtgeq, 2026-07-09):

- **CTE-prefixed DML** (``WITH x AS (DELETE FROM nexus.t ...) INSERT INTO ...``) is
  invisible to ``_DML_TARGET_RE``: it only recognizes a literal top-level
  ``INSERT``/``UPDATE``/``DELETE`` keyword at the start of a statement fragment, not
  a DML statement nested inside a ``WITH`` clause. Absent from the real corpus
  (grep-verified 2026-07-09); would need its own detector if ever used.
- **Comment/string-literal stripping order**: ``_strip_comments`` and the ``;``
  split in ``_split_statements`` both run BEFORE single-quoted string literals are
  blanked. A hypothetical string literal containing ``--``, ``/*``, or ``;`` would
  therefore be mis-parsed (a false statement boundary or a swallowed comment
  delimiter). Grep-verified absent from every ``<sql>`` body in the real corpus
  today; not a live bug, but a latent one if such a literal is ever introduced.
- **Schema scope is hardcoded** to ``nexus`` and ``t1`` in every regex (the only two
  schemas this codebase's changelogs use today). A third schema introduced by a
  future changelog would be completely invisible to every rule in this file, not
  just silently missed but with no signal that it happened. The
  ``CREATE SCHEMA ...`` tripwire below (any schema name other than ``nexus``/``t1``)
  exists specifically to catch that moment and force this file to be extended
  rather than silently blind past it.

Same-changeset-backstop ORDER SENSITIVITY (code-review-expert Important #1,
2026-07-09): a same-changeset backstop is only sound if it runs AFTER the DML it is
meant to prove-or-fail — a ``VALIDATE CONSTRAINT`` (or immediately-valid
``ADD CONSTRAINT`` / ``CREATE UNIQUE INDEX``) that appears BEFORE the DML in
statement order proves nothing about rows the DML has not yet touched. The analyzer
tracks each statement's index within its changeset and requires
``backstop_index > dml_index`` on the DML's own target table.

Same-changeset-backstop TARGET-ONLY scope, corrected (substantive-critic Critical,
nexus-fqnii, 2026-07-09): the target-only-backstop exemption from Approved Shape 2
above is sound ONLY when the target table itself is the gating risk (its own
FORCE-RLS state zeroes the whole statement's candidate row set) — it must NEVER be
allowed to excuse a *referenced* (JOIN/subquery) table that is independently FORCE
and untoggled, because a backstop on the target proves nothing about a table it
never touches. The evaluator therefore branches on which table actually gates the
statement: if the target itself is FORCE-and-untoggled, only a later same-changeset
backstop on the target can excuse it (reads are irrelevant in this branch — the
target's own row-security already gates the whole statement to zero-or-full effect,
exactly the fk-001-2..5 shape); if the target is NOT FORCE (the statement executes
for real), ANY referenced table that is independently FORCE-and-untoggled is a
finding on its own — no backstop on the (non-gating) target can excuse it. This is
the asymmetric mirror of catalog-014-0's own documented incident ("toggling only the
target table leaves the join yielding zero rows for the non-bypass owner"), caught
there only by a Java restricted-role replay test, never previously by a Python-side
static check.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent
CHANGELOG_DIR = REPO_ROOT / "service" / "src" / "main" / "resources" / "db" / "changelog"
MASTER_CHANGELOG = CHANGELOG_DIR / "db.changelog-master.xml"

_XSD_NS = "{http://www.liquibase.org/xml/ns/dbchangelog}"


# ---------------------------------------------------------------------------
# Regexes (all operate on already comment-stripped, dollar-body-stripped,
# single-statement text — see _split_statements)
# ---------------------------------------------------------------------------

_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_DOLLAR_QUOTE_RE = re.compile(r"\$(\w*)\$(.*?)\$\1\$", re.DOTALL)
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")

_FORCE_TOGGLE_RE = re.compile(
    r"ALTER\s+TABLE\s+(nexus|t1)\.(\w+)\s+(NO\s+)?FORCE\s+ROW\s+LEVEL\s+SECURITY",
    re.IGNORECASE,
)
_DML_TARGET_RE = re.compile(
    r"^(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(nexus|t1)\.(\w+)",
    re.IGNORECASE,
)
_ADD_CONSTRAINT_RE = re.compile(
    r"ALTER\s+TABLE\s+(nexus|t1)\.(\w+)\s+ADD\s+CONSTRAINT\s+\w+\s+"
    r"(?:FOREIGN\s+KEY|UNIQUE)",
    re.IGNORECASE,
)
_UNIQUE_INDEX_RE = re.compile(
    r"CREATE\s+UNIQUE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?\S+\s+ON\s+(nexus|t1)\.(\w+)",
    re.IGNORECASE,
)
_VALIDATE_CONSTRAINT_RE = re.compile(
    r"ALTER\s+TABLE\s+(nexus|t1)\.(\w+)\s+VALIDATE\s+CONSTRAINT",
    re.IGNORECASE,
)
_TABLE_MENTION_RE = re.compile(r"\b(nexus|t1)\.(\w+)\b(?!\s*\()", re.IGNORECASE)

# FIX 5(c): CREATE SCHEMA tripwire. The analyzer's schema scope is hardcoded to
# nexus/t1 everywhere; a third schema would otherwise be silently invisible. The
# real corpus has exactly two CREATE SCHEMA statements (nexus, t1) — both exempt.
_CREATE_SCHEMA_RE = re.compile(
    r"CREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE
)
_KNOWN_SCHEMAS = frozenset({"nexus", "t1"})

# FIX 3: Liquibase element kinds this analyzer cannot see into at all (only
# <sql> children of <changeSet> are scanned). Zero current usage in the real
# corpus (grep-verified 2026-07-09) — this is purely defensive.
_UNSCANNED_ELEMENT_TAGS = ("sqlFile", "customChange", "createProcedure")

# A DO $$ block's FIRST inner statement shares a ``;``-split fragment with the
# "DO $$\nBEGIN" wrapper tokens (there is no statement-terminating ``;``
# between BEGIN and the first inner statement). Strip that leading wrapper
# before anchoring the DML-target match at position 0, so
# "DO $$\nBEGIN\n    DELETE FROM nexus.widgets ..." still resolves its target
# — otherwise a DO block's very first statement would silently escape
# detection while every later inner statement (naturally split by the prior
# statement's ``;``) is caught, an inconsistency that would be worse than no
# detection at all (looks caught, isn't, for a corpus-shape-dependent subset).
_LEADING_DO_BEGIN_RE = re.compile(
    r"^\s*(?:DO\s+\$\w*\$\s*|BEGIN\s*)+", re.IGNORECASE
)


def _table_key(schema: str, table: str) -> str:
    # PG folds unquoted identifiers to lowercase; normalize here so every
    # regex's captured groups (all six funnel through this helper) key
    # consistently regardless of the source SQL's casing.
    return f"{schema.lower()}.{table.lower()}"


# ---------------------------------------------------------------------------
# Text preprocessing: comments, exempt dollar-quoted bodies, statement split
# ---------------------------------------------------------------------------


def _strip_comments(sql: str) -> str:
    """Remove ``--`` line comments and ``/* */`` block comments."""
    sql = _BLOCK_COMMENT_RE.sub(" ", sql)
    sql = _LINE_COMMENT_RE.sub("", sql)
    return sql


def _strip_exempt_dollar_bodies(sql: str) -> str:
    """Remove ``CREATE [OR REPLACE] FUNCTION/TRIGGER ... AS $$...$$`` bodies.

    A ``DO $$ ... $$`` anonymous block is left INTACT: unlike a function/trigger
    definition (which only executes later, at CALL time, under the caller's own
    SECURITY INVOKER context), a DO block executes immediately at migration time
    under the Liquibase role's own session — its DML has the exact same no-op
    exposure as top-level SQL and must be scanned, not stripped as exempt.
    """
    out: list[str] = []
    pos = 0
    for m in _DOLLAR_QUOTE_RE.finditer(sql):
        pre = sql[pos : m.start()]
        pre_stripped = pre.rstrip()
        is_do_block = bool(
            re.search(r"(?<![A-Za-z0-9_])DO\s*$", pre_stripped, re.IGNORECASE)
        )
        out.append(pre)
        out.append(m.group(0) if is_do_block else " ")
        pos = m.end()
    out.append(sql[pos:])
    return "".join(out)


def _split_statements(sql: str) -> list[str]:
    """Comment-strip, exempt-body-strip, then split into ``;``-terminated
    statement fragments, each stripped of leading/trailing whitespace and with
    single-quoted string literals blanked out (so a literal like
    ``current_setting('nexus.tenant', true)`` cannot false-positive as a table
    reference)."""
    cleaned = _strip_exempt_dollar_bodies(_strip_comments(sql))
    fragments = []
    for raw in cleaned.split(";"):
        frag = raw.strip()
        if not frag:
            continue
        frag = _STRING_LITERAL_RE.sub("''", frag)
        fragments.append(frag)
    return fragments


# ---------------------------------------------------------------------------
# Master include-order walk + per-changeset <sql> extraction
# ---------------------------------------------------------------------------


def parse_master_include_order(master_path: Path) -> list[str]:
    """Return the ``<include file="...">`` basenames in document order."""
    tree = ET.parse(master_path)
    root = tree.getroot()
    includes = []
    for el in root.iter(f"{_XSD_NS}include"):
        file_attr = el.get("file")
        if file_attr:
            includes.append(Path(file_attr).name)
    return includes


def iter_changesets(changelog_dir: Path, basename: str):
    """Yield (changeset_id, sql_text, unscanned_tags) for every ``<changeSet>``
    in *basename*, in document order. ``<rollback>`` bodies are structurally
    excluded (they are sibling elements of ``<sql>``, never visited) —
    rollback SQL never runs at migration time.

    *unscanned_tags* is the list of direct-child tag names matching
    ``_UNSCANNED_ELEMENT_TAGS`` (``sqlFile`` / ``customChange`` /
    ``createProcedure``) — Liquibase change-type elements this analyzer has
    no ability to look inside at all. A changeset may have neither ``<sql>``
    nor any unscanned element (pure DDL via other Liquibase change types this
    lint doesn't need to worry about, e.g. ``<createTable>`` — not currently
    used in this corpus but not itself a blind spot since it carries no DML).
    """
    path = changelog_dir / basename
    tree = ET.parse(path)
    root = tree.getroot()
    for cs in root.iter(f"{_XSD_NS}changeSet"):
        cs_id = cs.get("id", "")
        sql_texts = [
            el.text or "" for el in cs.findall(f"{_XSD_NS}sql") if el.text
        ]
        unscanned = [
            tag
            for tag in _UNSCANNED_ELEMENT_TAGS
            if cs.find(f"{_XSD_NS}{tag}") is not None
        ]
        yield cs_id, "\n".join(sql_texts), unscanned


# ---------------------------------------------------------------------------
# Findings + result shape
# ---------------------------------------------------------------------------

NAKED_DML = "naked_dml"
MISSING_RESTORE = "missing_restore"
UNSCANNED_ELEMENT = "unscanned_element"
UNSCANNED_SCHEMA = "unscanned_schema"


@dataclass(frozen=True)
class Finding:
    changeset_id: str
    file: str
    table: str
    kind: str
    detail: str = ""


@dataclass
class AnalysisResult:
    walked_files: list[str] = field(default_factory=list)
    violations: list[Finding] = field(default_factory=list)
    unused_allowlist: set[tuple[str, str]] = field(default_factory=set)

    @property
    def total_violations(self) -> int:
        return len(self.violations)


# ---------------------------------------------------------------------------
# Allowlist — the exact grandfathered dangerous-shape members (nexus-php10
# ground truth, nx memory get -p nexus -t php10-ground-truth-classification.md).
# Keyed (changeset_id, "schema.table"). An UNUSED entry fails the real-changelog
# test (rot detection) — every entry here must be independently re-derivable
# from a live analyzer run, never hand-waved.
# ---------------------------------------------------------------------------

ALLOWLIST: tuple[tuple[str, str, str], ...] = (
    (
        "catalog-013-0",
        "nexus.chash_index",
        "nexus-1wjmq: burned in the v0.1.33 outage. Naked DML, backstop "
        "(VALIDATE CONSTRAINT) lands in the later, separate catalog-013-2 "
        "changeset — the exact class this lint exists to catch. Fixed "
        "forward by catalog-013-1b (toggle-wrapped re-run); 013-0 itself is "
        "checksum-immutable and already executed on affected tenants.",
    ),
    (
        "taxonomy-004-1",
        "nexus.topic_assignments",
        "nexus-slcn7: dedup-root-topics DML lives inside a DO $$ block (not "
        "toggled), backstop (CREATE UNIQUE INDEX) lands in the later, "
        "separate taxonomy-004-2 changeset. Checksum-immutable, already "
        "executed; nexus-php10 bead description's 2026-07-08 audit note "
        "confirms no production divergence.",
    ),
    (
        "taxonomy-004-1",
        "nexus.topics",
        "Same DO $$ block as topic_assignments above (nexus-slcn7); the "
        "UPDATE/DELETE targeting nexus.topics shares the identical "
        "later-changeset-backstop shape and disposition.",
    ),
    (
        "taxonomy-004-1",
        "nexus.topic_links",
        "Same DO $$ block as topic_assignments above (nexus-slcn7); the "
        "DELETE targeting nexus.topic_links shares the identical "
        "later-changeset-backstop shape and disposition.",
    ),
    (
        "fk-002-0-backfill-stubs",
        "nexus.catalog_collections",
        "nexus-70r3c.2: stub-register backfill INSERT, no toggle. The FKs "
        "added in the same file (fk-002-1..5) are deliberately NOT VALID "
        "(don't count as a backstop) and the real VALIDATE lives in a "
        "wholly separate later file (fk-002-validate.xml). "
        "Checksum-immutable; RDR-153 world-block lifted 2026-06-21; "
        "nexus-php10 bead description's 2026-07-08 audit note confirms no "
        "production divergence.",
    ),
    (
        "fk-003-0-backfill-stubs",
        "nexus.catalog_collections",
        "nexus-dcqml: mirrors fk-002-0-backfill-stubs exactly (five-table "
        "stub-register INSERT, no toggle, VALIDATE deferred to a separate "
        "later file fk-003-validate.xml). Same disposition rationale.",
    ),
    (
        "fk-002-6-reconcile",
        "nexus.catalog_collections",
        "nexus-70r3c.3: gap-window reconcile re-run of the fk-002-0 "
        "stub-register INSERT shape. NOT in the original bead's historical "
        "six-member list — surfaced by the nexus-php10 plan-audit as a "
        "genuine additional member: its backstop (VALIDATE, fk-002-7..11) "
        "is in the SAME FILE but LATER, SEPARATE changesets, the literal "
        "later-changeset-backstop shape. Checksum-immutable, already "
        "executed on any deployed tenant; same nexus-php10 bead description "
        "2026-07-08 audit-note rationale.",
    ),
    (
        "fk-003-6-reconcile",
        "nexus.catalog_collections",
        "nexus-p9aw6: mirrors fk-002-6-reconcile exactly (gap-window "
        "reconcile re-run, backstop in later same-file changesets "
        "fk-003-7..11). Same NEW-finding / disposition rationale.",
    ),
)


def _allowlist_keys(allowlist: tuple[tuple[str, str, str], ...]) -> set[tuple[str, str]]:
    return {(cs_id, table) for cs_id, table, _reason in allowlist}


# ---------------------------------------------------------------------------
# The analyzer
# ---------------------------------------------------------------------------


def analyze_changelog(
    changelog_dir: Path = CHANGELOG_DIR,
    master_path: Path = MASTER_CHANGELOG,
    allowlist: tuple[tuple[str, str, str], ...] = ALLOWLIST,
) -> AnalysisResult:
    """Walk *master_path*'s include order, classify every DML statement in
    every changeset's ``<sql>`` body against the per-table FORCE-RLS state at
    that point, and return the (post-allowlist) violation set.

    Non-vacuity: the walked-file set is asserted equal to the live
    ``<include>`` list AND to the live glob of ``*.xml`` files (minus the
    master itself) in *changelog_dir* — self-verifying against the actual
    directory contents rather than a hardcoded file count, so this cannot
    silently drift as new changelogs land (nx_plan_audit correction,
    nexus-php10 2026-07-09: an earlier draft hardcoded 31, the live count
    is 39).
    """
    include_order = parse_master_include_order(master_path)

    on_disk = {
        p.name for p in changelog_dir.glob("*.xml") if p.name != master_path.name
    }
    included = set(include_order)
    assert included == on_disk, (
        "db.changelog-master.xml include list drifted from the changelog "
        f"directory contents: included-not-on-disk={included - on_disk}, "
        f"on-disk-not-included={on_disk - included}"
    )
    assert len(include_order) == len(on_disk), (
        f"duplicate <include> entries in db.changelog-master.xml: "
        f"{len(include_order)} includes vs {len(on_disk)} unique files"
    )

    global_force: dict[str, bool] = {}
    allow_keys = _allowlist_keys(allowlist)
    raw_findings: dict[tuple[str, str, str], Finding] = {}
    # UNSCANNED_ELEMENT / UNSCANNED_SCHEMA findings are never allowlist-
    # suppressible (they are a defensive "extend the lint" category, not the
    # DML classification the allowlist is scoped to) — collected separately
    # so they always survive into violations regardless of allow_keys.
    hard_findings: list[Finding] = []

    for basename in include_order:
        for cs_id, sql_text, unscanned_tags in iter_changesets(
            changelog_dir, basename
        ):
            for tag in unscanned_tags:
                hard_findings.append(
                    Finding(
                        changeset_id=cs_id,
                        file=basename,
                        table="",
                        kind=UNSCANNED_ELEMENT,
                        detail=(
                            f"changeset {cs_id} contains a <{tag}> element — "
                            "unscanned Liquibase element, extend the lint "
                            "before relying on it for FORCE-RLS DML safety."
                        ),
                    )
                )

            live_force = dict(global_force)
            toggled_off: set[str] = set()
            # table -> list of statement indices where a valid same-changeset
            # backstop (immediately-valid ADD CONSTRAINT / CREATE UNIQUE
            # INDEX / VALIDATE CONSTRAINT) fired on that table.
            backstop_indices: dict[str, list[int]] = {}
            # (target, referenced, live_force snapshot AT this statement,
            #  this statement's index)
            dml_events: list[tuple[str, set[str], dict[str, bool], int]] = []

            stmt_index = -1
            for stmt in _split_statements(sql_text):
                stmt_index += 1

                m = _FORCE_TOGGLE_RE.search(stmt)
                if m and stmt.upper().lstrip().startswith("ALTER TABLE"):
                    key = _table_key(m.group(1), m.group(2))
                    is_no_force = bool(m.group(3))
                    live_force[key] = not is_no_force
                    if is_no_force:
                        toggled_off.add(key)
                    continue

                stmt_for_dml = _LEADING_DO_BEGIN_RE.sub("", stmt)
                m = _DML_TARGET_RE.match(stmt_for_dml)
                if m:
                    target = _table_key(m.group(1), m.group(2))
                    referenced = {
                        _table_key(g[0], g[1])
                        for g in _TABLE_MENTION_RE.findall(stmt)
                    }
                    dml_events.append(
                        (target, referenced, dict(live_force), stmt_index)
                    )
                    continue

                m = _ADD_CONSTRAINT_RE.search(stmt)
                if m:
                    if "NOT VALID" not in stmt.upper():
                        backstop_indices.setdefault(
                            _table_key(m.group(1), m.group(2)), []
                        ).append(stmt_index)
                    continue

                m = _UNIQUE_INDEX_RE.search(stmt)
                if m:
                    backstop_indices.setdefault(
                        _table_key(m.group(1), m.group(2)), []
                    ).append(stmt_index)
                    continue

                m = _VALIDATE_CONSTRAINT_RE.search(stmt)
                if m:
                    backstop_indices.setdefault(
                        _table_key(m.group(1), m.group(2)), []
                    ).append(stmt_index)
                    continue

                m = _CREATE_SCHEMA_RE.search(stmt)
                if m and m.group(1).lower() not in _KNOWN_SCHEMAS:
                    hard_findings.append(
                        Finding(
                            changeset_id=cs_id,
                            file=basename,
                            table=m.group(1).lower(),
                            kind=UNSCANNED_SCHEMA,
                            detail=(
                                f"changeset {cs_id} creates schema "
                                f"{m.group(1)!r}, outside the analyzer's "
                                "hardcoded nexus/t1 scope — extend the lint "
                                "before trusting it for this schema."
                            ),
                        )
                    )
                    continue
                # else: CREATE TABLE / CREATE INDEX (non-unique) / GRANT /
                # COMMENT ON / etc. — irrelevant to this lint, ignored.

            for table in toggled_off:
                if not live_force.get(table):
                    key = (cs_id, table, MISSING_RESTORE)
                    raw_findings[key] = Finding(
                        changeset_id=cs_id,
                        file=basename,
                        table=table,
                        kind=MISSING_RESTORE,
                        detail=(
                            f"{table} toggled NO FORCE in changeset {cs_id} "
                            "but never restored to FORCE before the "
                            "changeset ended (isolation-window leak)"
                        ),
                    )

            for target, referenced, snapshot, dml_idx in dml_events:
                gated_target = bool(snapshot.get(target))

                if gated_target:
                    # Only a LATER same-changeset backstop on the TARGET can
                    # excuse this — the target's own row-security gates the
                    # whole statement to zero-or-full effect, so read-table
                    # FORCE state is irrelevant in this branch (fk-001-2..5
                    # shape: FORCE'd subquery-read parent, sound anyway).
                    later_backstop = any(
                        idx > dml_idx
                        for idx in backstop_indices.get(target, [])
                    )
                    if later_backstop:
                        continue
                    key = (cs_id, target, NAKED_DML)
                    raw_findings[key] = Finding(
                        changeset_id=cs_id,
                        file=basename,
                        table=target,
                        kind=NAKED_DML,
                        detail=(
                            f"changeset {cs_id}: DML targeting FORCE-RLS "
                            f"table {target} has no toggle and no LATER "
                            "same-changeset backstop (immediately-valid ADD "
                            "CONSTRAINT / CREATE UNIQUE INDEX / VALIDATE "
                            f"CONSTRAINT, statement index > {dml_idx}) on "
                            f"{target}. Wrap with NO FORCE / FORCE "
                            "(catalog-013-1b pattern) or add a same-"
                            "changeset backstop AFTER this statement; see "
                            "nexus-1wjmq."
                        ),
                    )
                else:
                    # Target itself is not currently FORCE — the statement
                    # executes for real. ANY referenced (JOIN/subquery) table
                    # that is independently FORCE-and-untoggled is its own
                    # finding; a backstop on the (non-gating) target CANNOT
                    # excuse it (nexus-fqnii: the catalog-014-0-mirror gap).
                    gated_reads = {
                        t for t in referenced - {target} if snapshot.get(t)
                    }
                    if not gated_reads:
                        continue
                    key = (cs_id, target, NAKED_DML)
                    raw_findings[key] = Finding(
                        changeset_id=cs_id,
                        file=basename,
                        table=target,
                        kind=NAKED_DML,
                        detail=(
                            f"changeset {cs_id}: DML targeting "
                            f"non-FORCE table {target} reads FORCE-RLS "
                            f"table(s) {sorted(gated_reads)} via JOIN/"
                            "subquery with no toggle. A same-changeset "
                            f"backstop on {target} does NOT excuse this — "
                            "the read table's own visibility, not the "
                            "target's, gates this statement's correctness "
                            "(the catalog-014-0 both-tables lesson); see "
                            "nexus-1wjmq / nexus-fqnii."
                        ),
                    )

            global_force = live_force

    violations = [
        f
        for key, f in raw_findings.items()
        if (f.changeset_id, f.table) not in allow_keys
    ]
    violations.extend(hard_findings)
    consumed = {
        (f.changeset_id, f.table)
        for f in raw_findings.values()
        if (f.changeset_id, f.table) in allow_keys
    }
    unused = allow_keys - consumed

    return AnalysisResult(
        walked_files=include_order,
        violations=violations,
        unused_allowlist=unused,
    )


# ===========================================================================
# Tests
# ===========================================================================


def _write_changelog(tmp_path: Path, changesets_xml: str) -> tuple[Path, Path]:
    """Write a synthetic single-file changelog + a master that includes it.
    Returns (changelog_dir, master_path)."""
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    child = changelog_dir / "synthetic-001.xml"
    child.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<databaseChangeLog\n'
        '    xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
        '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '    xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog '
        'http://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-4.4.xsd">\n'
        f"{changesets_xml}\n"
        "</databaseChangeLog>\n"
    )
    master = changelog_dir / "db.changelog-master.xml"
    master.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<databaseChangeLog\n'
        '    xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
        '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '    xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog '
        'http://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-4.4.xsd">\n'
        '    <include file="synthetic-001.xml"/>\n'
        "</databaseChangeLog>\n"
    )
    return changelog_dir, master


def _analyze_synthetic(tmp_path: Path, changesets_xml: str) -> AnalysisResult:
    changelog_dir, master = _write_changelog(tmp_path, changesets_xml)
    return analyze_changelog(
        changelog_dir=changelog_dir, master_path=master, allowlist=()
    )


# ---------------------------------------------------------------------------
# FLAG cases — must be caught
# ---------------------------------------------------------------------------


def test_naked_dml_on_force_table_is_flagged(tmp_path):
    """DML against a FORCE table with no toggle and no backstop anywhere."""
    xml = """
    <changeSet id="cs-force" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
    <changeSet id="cs-naked" author="t">
        <sql splitStatements="true">
DELETE FROM nexus.widgets WHERE stale = true;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-naked" and v.table == "nexus.widgets"
        and v.kind == NAKED_DML
        for v in result.violations
    ), result.violations


def test_missing_restore_is_flagged_even_though_dml_ran(tmp_path):
    """NO FORCE issued, DML runs (visible), but FORCE never restored before
    changeset end — flagged as a DISTINCT finding from naked DML, per the
    design's isolation-window-leak framing."""
    xml = """
    <changeSet id="cs-force" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
    <changeSet id="cs-leak" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets NO FORCE ROW LEVEL SECURITY;
DELETE FROM nexus.widgets WHERE stale = true;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    missing = [
        v for v in result.violations
        if v.changeset_id == "cs-leak" and v.kind == MISSING_RESTORE
    ]
    assert missing, result.violations
    assert missing[0].table == "nexus.widgets"
    # The naked-DML rule must NOT ALSO fire for this DML — it ran while
    # visible (toggled off), so it is not itself a no-op; only the
    # missing-restore leak is the finding.
    naked = [
        v for v in result.violations
        if v.changeset_id == "cs-leak" and v.kind == NAKED_DML
    ]
    assert naked == [], naked


def test_later_changeset_backstop_does_not_count(tmp_path):
    """The exact catalog-013-0 shape: naked DML in one changeset, its only
    backstop (VALIDATE CONSTRAINT) lands in a LATER, separate changeset.
    Must still be flagged — a later-changeset backstop does NOT suppress."""
    xml = """
    <changeSet id="cs-force" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
ALTER TABLE nexus.widgets ADD CONSTRAINT widgets_len_check CHECK (length(name) = 8) NOT VALID;
        </sql>
    </changeSet>
    <changeSet id="cs-naked" author="t">
        <sql splitStatements="true">
UPDATE nexus.widgets SET name = substr(name, 1, 8) WHERE length(name) = 16;
        </sql>
    </changeSet>
    <changeSet id="cs-later-backstop" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-naked" and v.table == "nexus.widgets"
        and v.kind == NAKED_DML
        for v in result.violations
    ), result.violations


def test_unwrapped_join_table_read_is_flagged(tmp_path):
    """DML targets a NON-force table but its FROM-clause JOINs a DIFFERENT
    table that IS force and never toggled — must flag on the
    referenced-table rule, not just the target-table rule (the catalog-014
    both-tables lesson, inverted)."""
    xml = """
    <changeSet id="cs-force" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.gadgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.gadgets FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
    <changeSet id="cs-join-naked" author="t">
        <sql splitStatements="true">
UPDATE nexus.widgets w SET label = g.label FROM nexus.gadgets g WHERE g.id = w.gadget_id;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-join-naked" and v.kind == NAKED_DML
        for v in result.violations
    ), result.violations


# ---------------------------------------------------------------------------
# ACCEPT cases — must NOT be flagged
# ---------------------------------------------------------------------------


def test_toggle_wrapped_single_table_accepted(tmp_path):
    xml = """
    <changeSet id="cs-force" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
    <changeSet id="cs-wrapped" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets NO FORCE ROW LEVEL SECURITY;
DELETE FROM nexus.widgets WHERE stale = true;
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert result.violations == [], result.violations


def test_toggle_wrapped_both_tables_accepted(tmp_path):
    """catalog-014 shape: two tables toggled off / DML (joining both) /
    both restored, in one changeset."""
    xml = """
    <changeSet id="cs-force" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
ALTER TABLE nexus.gadgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.gadgets FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
    <changeSet id="cs-both-wrapped" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets NO FORCE ROW LEVEL SECURITY;
ALTER TABLE nexus.gadgets NO FORCE ROW LEVEL SECURITY;
UPDATE nexus.widgets w SET label = g.label FROM nexus.gadgets g WHERE g.id = w.gadget_id;
ALTER TABLE nexus.gadgets FORCE ROW LEVEL SECURITY;
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert result.violations == [], result.violations


def test_same_changeset_backstop_accepted(tmp_path):
    xml = """
    <changeSet id="cs-force" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
ALTER TABLE nexus.owners (id) ADD COLUMN dummy int;
        </sql>
    </changeSet>
    <changeSet id="cs-backstopped" author="t">
        <sql splitStatements="true">
DELETE FROM nexus.widgets WHERE owner_id NOT IN (SELECT id FROM nexus.owners);
ALTER TABLE nexus.widgets ADD CONSTRAINT widgets_owner_fk FOREIGN KEY (owner_id) REFERENCES nexus.owners (id);
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert result.violations == [], result.violations


def test_function_body_dml_exempt(tmp_path):
    """A CREATE FUNCTION ... $$ ... $$ body's internal DML is exempt
    (SECURITY INVOKER, call-time) regardless of FORCE state — mirrors
    catalog-003's document_trash/purge_trash class."""
    xml = """
    <changeSet id="cs-force" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
    <changeSet id="cs-fn" author="t">
        <sql splitStatements="false">
CREATE OR REPLACE FUNCTION nexus.widget_trash(wid text)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
    UPDATE nexus.widgets SET deleted_at = NOW() WHERE id = wid;
END;
$$
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert result.violations == [], result.violations


def test_do_block_dml_is_not_exempt(tmp_path):
    """The critical negative case: a DO $$ ... $$ anonymous block executes
    immediately at migration time and must NOT be treated as an exempt
    function body — its naked DML is flagged exactly like top-level SQL
    (taxonomy-004-1 ground-truth shape)."""
    xml = """
    <changeSet id="cs-force" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
    <changeSet id="cs-do-naked" author="t">
        <sql splitStatements="false">
DO $$
BEGIN
    DELETE FROM nexus.widgets WHERE stale = true;
END $$;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-do-naked" and v.table == "nexus.widgets"
        and v.kind == NAKED_DML
        for v in result.violations
    ), (
        "DO $$ block DML must be flagged, not silently exempted as a "
        f"function body: {result.violations}"
    )


def test_dml_on_never_force_table_accepted(tmp_path):
    """A table that never had FORCE ROW LEVEL SECURITY established is never
    flagged, regardless of DML shape (service_tokens ground-truth case)."""
    xml = """
    <changeSet id="cs-plain" author="t">
        <sql splitStatements="true">
UPDATE nexus.service_tokens SET scope = 'root' WHERE label = 'bootstrap-legacy-token';
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert result.violations == [], result.violations


# ---------------------------------------------------------------------------
# Regression pins for the code-review-expert / substantive-critic fix round
# (nexus-fqnii Critical + Important #1): corrected same-changeset-backstop
# rule, gating-aware and order-sensitive.
# ---------------------------------------------------------------------------


def test_unrelated_target_backstop_does_not_excuse_a_gated_read_table(tmp_path):
    """nexus-fqnii Critical, corrected: a non-gated target (never FORCE) that
    JOINs a gated (FORCE, untoggled) read table must be FLAGGED even when the
    target carries an UNRELATED same-changeset backstop — a backstop on a
    table the DML doesn't need to prove anything about cannot excuse a
    different table's visibility risk (the catalog-014-0-mirror gap the
    critic repro'd: widgets never-FORCE target, gadgets FORCE-and-untoggled
    referenced via FROM, unrelated ADD CONSTRAINT on widgets)."""
    xml = """
    <changeSet id="cs-force" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.gadgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.gadgets FORCE ROW LEVEL SECURITY;
ALTER TABLE nexus.widgets (id) ADD COLUMN dummy int;
        </sql>
    </changeSet>
    <changeSet id="cs-join-unrelated-backstop" author="t">
        <sql splitStatements="true">
UPDATE nexus.widgets w SET label = g.label FROM nexus.gadgets g WHERE g.id = w.gadget_id;
ALTER TABLE nexus.widgets ADD CONSTRAINT widgets_dummy_unique UNIQUE (dummy);
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-join-unrelated-backstop" and v.kind == NAKED_DML
        for v in result.violations
    ), (
        "an unrelated backstop on the (non-gating) target must NOT suppress "
        f"a gated referenced-table finding: {result.violations}"
    )


def test_backstop_before_dml_does_not_count(tmp_path):
    """code-review-expert Important #1: a same-changeset backstop that runs
    BEFORE the DML it is meant to prove-or-fail proves nothing — order
    matters. A gated target with only an earlier backstop must still be
    flagged."""
    xml = """
    <changeSet id="cs-force" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
    <changeSet id="cs-backstop-before-dml" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets ADD CONSTRAINT widgets_owner_fk FOREIGN KEY (owner_id) REFERENCES nexus.owners (id);
DELETE FROM nexus.widgets WHERE stale = true;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-backstop-before-dml" and v.kind == NAKED_DML
        for v in result.violations
    ), (
        "a backstop preceding the DML must NOT count as a safety proof for "
        f"it: {result.violations}"
    )


def test_gated_target_with_gated_subquery_read_and_later_backstop_accepted(tmp_path):
    """The fk-001 shape, pinned precisely: a GATED target (FORCE, untoggled)
    whose DML reads a DIFFERENT, ALSO-gated table only via a WHERE-subquery
    (not a FROM/USING join), backstopped by a LATER same-changeset
    immediately-valid ADD CONSTRAINT on the TARGET. Must be ACCEPTED — the
    read table's FORCE state is irrelevant once the target itself gates the
    whole statement to zero-or-full effect (verified against fk-001-2..5:
    document_aspects UPDATE reading FORCE'd catalog_documents via NOT
    EXISTS, backstopped by an immediately-valid ADD CONSTRAINT FK on
    document_aspects itself)."""
    xml = """
    <changeSet id="cs-force" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.widgets FORCE ROW LEVEL SECURITY;
ALTER TABLE nexus.owners ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.owners FORCE ROW LEVEL SECURITY;
        </sql>
    </changeSet>
    <changeSet id="cs-subquery-backstopped" author="t">
        <sql splitStatements="true">
DELETE FROM nexus.widgets WHERE owner_id NOT IN (SELECT id FROM nexus.owners);
ALTER TABLE nexus.widgets ADD CONSTRAINT widgets_owner_fk FOREIGN KEY (owner_id) REFERENCES nexus.owners (id);
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert result.violations == [], result.violations


def test_unused_allowlist_entry_is_detected_when_no_matching_finding(tmp_path):
    """code-review-expert Important #2: a direct test of the unused-allowlist
    DETECTION MECHANISM itself, not just the real-corpus ``== set()``
    assertion (which only proves the 8 real entries ARE reproduced, not that
    a genuinely stale/wrong entry would be caught). A bogus allowlist key
    that matches no live finding must land in ``unused_allowlist``."""
    xml = """
    <changeSet id="cs-plain" author="t">
        <sql splitStatements="true">
CREATE TABLE nexus.widgets (id int);
        </sql>
    </changeSet>
    """
    changelog_dir, master = _write_changelog(tmp_path, xml)
    bogus_allowlist = (
        ("nonexistent-changeset", "nexus.nonexistent_table", "stale entry"),
    )
    result = analyze_changelog(
        changelog_dir=changelog_dir, master_path=master, allowlist=bogus_allowlist
    )
    assert result.unused_allowlist == {
        ("nonexistent-changeset", "nexus.nonexistent_table")
    }
    assert result.violations == []


@pytest.mark.parametrize("tag", ["sqlFile", "customChange", "createProcedure"])
def test_unscanned_liquibase_element_is_flagged(tmp_path, tag):
    """code-review-expert / critic Significant 3: a Liquibase change-type
    element this analyzer cannot see into at all must be flagged as its own
    finding ("extend the lint"), not silently skipped. Zero current usage in
    the real corpus (grep-verified) — purely defensive."""
    changelog_dir = tmp_path / "changelog"
    changelog_dir.mkdir()
    if tag == "sqlFile":
        el = '<sqlFile path="some.sql"/>'
    elif tag == "customChange":
        el = '<customChange class="com.example.Thing"/>'
    else:
        el = '<createProcedure>CREATE PROCEDURE nexus.p() ...</createProcedure>'
    child = changelog_dir / "synthetic-001.xml"
    child.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<databaseChangeLog\n'
        '    xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
        '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '    xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog '
        'http://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-4.4.xsd">\n'
        f'    <changeSet id="cs-unscanned" author="t">\n'
        f"        {el}\n"
        "    </changeSet>\n"
        "</databaseChangeLog>\n"
    )
    master = changelog_dir / "db.changelog-master.xml"
    master.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<databaseChangeLog\n'
        '    xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
        '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '    xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog '
        'http://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-4.4.xsd">\n'
        '    <include file="synthetic-001.xml"/>\n'
        "</databaseChangeLog>\n"
    )
    result = analyze_changelog(
        changelog_dir=changelog_dir, master_path=master, allowlist=()
    )
    assert any(
        v.changeset_id == "cs-unscanned" and v.kind == UNSCANNED_ELEMENT
        for v in result.violations
    ), result.violations


def test_create_schema_outside_known_scope_is_flagged(tmp_path):
    """FIX 5(c): a CREATE SCHEMA for anything other than nexus/t1 is
    completely invisible to every other rule in this file (hardcoded schema
    scope) — this tripwire is the one thing that catches that moment. The
    real corpus's two CREATE SCHEMA statements (nexus, t1) must NOT trip
    this (see test_real_changelog_zero_violations_and_full_allowlist_consumption)."""
    xml = """
    <changeSet id="cs-schema" author="t">
        <sql splitStatements="true">
CREATE SCHEMA analytics;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-schema" and v.kind == UNSCANNED_SCHEMA
        for v in result.violations
    ), result.violations


def test_create_schema_nexus_and_t1_do_not_trip_the_tripwire(tmp_path):
    xml = """
    <changeSet id="cs-schema-known" author="t">
        <sql splitStatements="true">
CREATE SCHEMA IF NOT EXISTS nexus;
CREATE SCHEMA IF NOT EXISTS t1;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert result.violations == [], result.violations


# ---------------------------------------------------------------------------
# Non-vacuity self-checks
# ---------------------------------------------------------------------------


def test_master_include_order_matches_directory_contents():
    """Self-verifying floor: every non-master ``*.xml`` file in the real
    changelog directory is included exactly once in
    ``db.changelog-master.xml``, and vice versa. This is the live version of
    the 39-file non-vacuity assertion — it is derived from the directory,
    never a hardcoded constant, so it cannot silently drift (nx_plan_audit
    correction, nexus-php10 2026-07-09)."""
    include_order = parse_master_include_order(MASTER_CHANGELOG)
    on_disk = {
        p.name for p in CHANGELOG_DIR.glob("*.xml")
        if p.name != MASTER_CHANGELOG.name
    }
    assert set(include_order) == on_disk
    assert len(include_order) == len(on_disk)
    # Floor value recorded for human legibility; the assertions above are
    # what actually guards non-vacuity (a silently-empty glob would make
    # both sides 0 and vacuously "match" without this explicit floor).
    assert len(on_disk) >= 30, (
        f"changelog directory glob returned suspiciously few files "
        f"({len(on_disk)}) — possible empty/misconfigured CHANGELOG_DIR"
    )


def test_allowlist_has_no_duplicate_keys():
    keys = [(cs_id, table) for cs_id, table, _ in ALLOWLIST]
    assert len(keys) == len(set(keys)), "duplicate ALLOWLIST (changeset, table) key"


# ---------------------------------------------------------------------------
# The real changelog — exact-set assertion
# ---------------------------------------------------------------------------


def test_real_changelog_zero_violations_and_full_allowlist_consumption():
    """The tripwire itself: run the analyzer against the ACTUAL
    ``service/src/main/resources/db/changelog/`` tree.

    Two exact (``==``, never ``>=``) assertions:
      1. Zero violations survive the allowlist — every dangerous-shape DML
         statement in the real changelog is either safe (toggle-wrapped /
         same-changeset backstop) or an explicitly grandfathered historical
         member.
      2. Zero UNUSED allowlist entries — every grandfathered member is
         independently re-derived by a live analyzer run against the real
         changelog, not hand-waved. An unused entry means either the
         changeset was removed/rewritten (checksum-immutable migrations
         never are) or the analyzer's classification logic drifted and no
         longer reproduces a known historical finding — either way, a
         signal worth investigating, not silently dropping.

    Ground truth (nx memory get -p nexus -t
    php10-ground-truth-classification.md): exactly 8 (changeset, table)
    dangerous-shape pairs across 6 changesets (catalog-013-0, taxonomy-004-1
    ×3 tables, fk-002-0-backfill-stubs, fk-003-0-backfill-stubs,
    fk-002-6-reconcile, fk-003-6-reconcile) — fk-001-2..5 are explicitly
    NOT grandfathered (verified same-changeset backstop via an
    immediately-valid ADD CONSTRAINT FK on their own DML target table).
    """
    result = analyze_changelog()
    assert result.total_violations == 0, (
        "FORCE-RLS migration DML tripwire fired — see nexus-1wjmq for the "
        f"failure class and the toggle-wrap / same-changeset-backstop "
        f"remedies: {[(v.changeset_id, v.table, v.kind, v.detail) for v in result.violations]}"
    )
    assert result.unused_allowlist == set(), (
        "ALLOWLIST entries not reproduced by a live analyzer run (rot — "
        "either the migration changed, which should be impossible for a "
        "checksum-immutable changeset, or the analyzer's classification "
        f"logic regressed): {result.unused_allowlist}"
    )


def test_real_changelog_walks_all_39_files():
    """Non-vacuity floor for the real-changelog run specifically (not just
    the master/directory parity check above): the analyzer must actually
    have walked every included file, not silently short-circuited."""
    result = analyze_changelog()
    on_disk = {
        p.name for p in CHANGELOG_DIR.glob("*.xml")
        if p.name != MASTER_CHANGELOG.name
    }
    assert len(result.walked_files) == len(on_disk)
    assert set(result.walked_files) == on_disk


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
