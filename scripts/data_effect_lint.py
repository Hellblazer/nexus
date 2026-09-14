#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Liquibase changeset DATA EFFECT disclosure classifier (nexus-f7dwp).

THE GAP. A changeset that changes or deletes EXISTING rows (tuples-003-2's
DELETE of oversized tuple bodies, tuples-004-1's irreversible NULL of
consumed tuple bodies) was, before this module, disclosed only in its own
``<comment>`` prose, a commit message, and ``docs/tuple-space.md`` — no
record a DEPLOYER reads carries it mechanically. ``docs/wire-contract-
pending.md`` rightly excludes these (no wire change) and
``conexus/PENDING_RELEASE.md`` is plugin-only, so there was no ledger this
class of change belonged in at all.

THE CONVENTION. Every changeset whose forward-running SQL modifies or
removes rows that could already exist on a deployed cluster must carry a
``DATA EFFECT: ...`` sentence inside its ``<comment>`` element — one line
naming what rows it touches and whether the effect is reversible. A purely
additive changeset (CREATE, ADD COLUMN, ADD CONSTRAINT, CREATE INDEX,
GRANT, VALIDATE CONSTRAINT, ...) needs nothing.

WHY THE COMMENT, NOT AN XML COMMENT ABOVE THE CHANGESET. Liquibase's
changeset checksum (``ChangeSet.generateCheckSum()``, liquibase-core
4.29.0) is computed ONLY from the ``Change``/``SqlVisitor`` objects — the
``<sql>``/structured-change body. It does NOT include ``<comment>``,
``<preConditions>``, ``<rollback>``, or ``failOnError`` (verified against
liquibase-core 4.29.0 source; see T3 knowledge doc
``liquibase-failonerror-false-vs-preconditions-mark-ran-databasechangelog``,
corroborated by this repo's own
``SchemaRollbackRoundTripIntegrationTest`` comment: "editing rollbacks
needs no validCheckSum ceremony"). Editing ``<comment>`` text on an
ALREADY-SHIPPED, checksum-locked changeset is therefore safe — it will
never trigger a checksum mismatch on a box that already ran it. Putting
the line INSIDE ``<comment>`` (rather than an XML comment floating above
the ``<changeSet>`` element) also keeps it inside the one element
Liquibase itself surfaces at runtime (``DATABASECHANGELOG.COMMENTS``) and
the one element this module's own parser (and any other tool reading
changeset metadata) already visits — an XML comment above the element is
invisible to anything that isn't scanning raw file text.

WHAT COUNTS AS DATA-EFFECTING. Scoped to the bead's explicit list:
DELETE, UPDATE, TRUNCATE, DROP TABLE, DROP COLUMN, and
``ALTER ... TYPE ... USING`` (a column rewrite touching every existing
row). Detected two ways:

  1. Raw SQL in forward ``<sql>`` elements (see _classify_sql_text).
  2. Structured Liquibase change-type elements ``<delete>``, ``<update>``,
     ``<dropTable>``, ``<dropColumn>`` as direct children of a
     ``<changeSet>`` (zero current usage in this corpus — grep-verified
     2026-09-13, every changeset here uses raw ``<sql>`` — but the bead
     names these explicitly and a future changeset could use them).

``<rollback>`` bodies are STRUCTURALLY EXCLUDED, mirroring
``test_changelog_rls_lint.py``'s own established, reviewed convention:
rollback SQL never runs at migration/deploy time, so it carries no
deploy-facing disclosure need. A DROP TABLE that appears only inside
``<rollback>`` (the overwhelmingly common shape in this corpus — a
baseline changeset's rollback tears down what it just created) is not a
data effect on the deploy path at all.

DELIBERATE SCOPE DECISION: ``UPDATE`` is detected as a STATEMENT-INITIAL
keyword (after stripping a leading ``DO $$ BEGIN`` wrapper, same helper as
the RLS lint), not as a bare substring search. This means
``INSERT INTO ... ON CONFLICT (...) DO UPDATE SET ...`` — an upsert that
inserts NEW/backfill rows and only conditionally touches an existing
conflicting row — does NOT trigger this convention. That statement is
grammatically an INSERT (its statement-initial keyword is INSERT, not
UPDATE); a bare substring match on "UPDATE" would explode this lint across
30+ backfill/stub-register changesets whose actual data-effect profile
(idempotent upsert of catalog/registry metadata) is already the subject of
``test_changelog_rls_lint.py``'s own historical allowlist and a
fundamentally different disclosure need than an unconditional UPDATE or
DELETE against rows that could already be populated with user data.

Function/trigger bodies (``CREATE [OR REPLACE] FUNCTION/TRIGGER ... AS
$$...$$``) are exempt — they define code that runs later, at CALL time,
under the caller's own context, not at migration time. An anonymous
``DO $$ ... $$`` block is NOT exempt — it executes immediately at
migration time (the taxonomy-004-1 ground-truth shape), so DML inside it
is scanned exactly like top-level SQL. Both rules mirror
``test_changelog_rls_lint.py`` verbatim (same preprocessing helpers,
independently re-derived here rather than imported, since this module has
no FORCE-RLS toggle-tracking need and importing a `tests/` module from a
`scripts/` module would invert the dependency direction the pythonpath
convention establishes).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG_DIR = _REPO_ROOT / "service" / "src" / "main" / "resources" / "db" / "changelog"
MASTER_CHANGELOG = CHANGELOG_DIR / "db.changelog-master.xml"

_XSD_NS = "{http://www.liquibase.org/xml/ns/dbchangelog}"

DATA_EFFECT_MARKER = "DATA EFFECT:"

# ---------------------------------------------------------------------------
# Text preprocessing — independently re-derived from
# tests/test_changelog_rls_lint.py's own reviewed methodology (comment-strip,
# exempt dollar-body-strip, statement split, leading DO/BEGIN strip). This
# module has no FORCE-RLS toggle-tracking need, so it does not import that
# test file; it keeps the identical preprocessing contract instead.
# ---------------------------------------------------------------------------

_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_DOLLAR_QUOTE_RE = re.compile(r"\$(\w*)\$(.*?)\$\1\$", re.DOTALL)
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_LEADING_DO_BEGIN_RE = re.compile(r"^\s*(?:DO\s+\$\w*\$\s*|BEGIN\s*)+", re.IGNORECASE)


def _strip_comments(sql: str) -> str:
    sql = _BLOCK_COMMENT_RE.sub(" ", sql)
    sql = _LINE_COMMENT_RE.sub("", sql)
    return sql


def _strip_exempt_dollar_bodies(sql: str) -> str:
    """Remove ``CREATE [OR REPLACE] FUNCTION/TRIGGER ... AS $$...$$`` bodies.

    A ``DO $$ ... $$`` anonymous block is left INTACT (see module docstring).
    """
    out: list[str] = []
    pos = 0
    for m in _DOLLAR_QUOTE_RE.finditer(sql):
        pre = sql[pos : m.start()]
        pre_stripped = pre.rstrip()
        is_do_block = bool(re.search(r"(?<![A-Za-z0-9_])DO\s*$", pre_stripped, re.IGNORECASE))
        out.append(pre)
        out.append(m.group(0) if is_do_block else " ")
        pos = m.end()
    out.append(sql[pos:])
    return "".join(out)


def _split_statements(sql: str) -> list[str]:
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
# Classification
# ---------------------------------------------------------------------------

_DELETE_RE = re.compile(r"^DELETE\s+FROM\b", re.IGNORECASE)
_UPDATE_RE = re.compile(r"^UPDATE\s+\S+.*?\bSET\b", re.IGNORECASE | re.DOTALL)
_TRUNCATE_RE = re.compile(r"^TRUNCATE\b", re.IGNORECASE)
_DROP_TABLE_RE = re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE)
_DROP_COLUMN_RE = re.compile(r"\bDROP\s+COLUMN\b", re.IGNORECASE)
# ALTER COLUMN ... TYPE ... USING <expr> — a table rewrite of every existing
# row's value in that column. Bounded to one statement (already split on
# ';'), DOTALL because the TYPE/USING pair can span a formatted multi-line
# ALTER TABLE statement.
_ALTER_TYPE_USING_RE = re.compile(
    r"\bALTER\s+COLUMN\s+\S+\s+TYPE\b.*?\bUSING\b", re.IGNORECASE | re.DOTALL
)

# Liquibase structured change-type elements that are themselves data effects
# regardless of what a raw <sql> scan would find (zero usage in this corpus
# today — see module docstring).
_STRUCTURED_DATA_EFFECT_TAGS = ("delete", "update", "dropTable", "dropColumn")


@dataclass(frozen=True)
class DataEffectReason:
    kind: str  # "delete" | "update" | "truncate" | "drop_table" | "drop_column" | "alter_type_using" | "structured:<tag>"
    detail: str
    # The exact matched SQL statement (comment-stripped, single-quoted
    # literals blanked), empty for a structured-tag reason with no <sql>
    # text of its own. Handed to scripts/list_data_effects.py verbatim as
    # the starting point for the census predicate a deployer runs on their
    # own fork before trusting a predicted row count — it is literally the
    # statement that will run, not a re-derived summary of it.
    statement: str = ""


def _classify_sql_text(sql_text: str) -> list[DataEffectReason]:
    """Classify one changeset's concatenated forward ``<sql>`` text.

    Returns the list of distinct reasons this changeset is data-effecting
    (empty if none — a purely additive changeset).
    """
    reasons: list[DataEffectReason] = []
    seen_kinds: set[str] = set()

    for stmt in _split_statements(sql_text):
        stmt_for_leading = _LEADING_DO_BEGIN_RE.sub("", stmt)

        if _DELETE_RE.match(stmt_for_leading) and "delete" not in seen_kinds:
            seen_kinds.add("delete")
            reasons.append(DataEffectReason("delete", "DELETE FROM statement", stmt))

        if _UPDATE_RE.match(stmt_for_leading) and "update" not in seen_kinds:
            seen_kinds.add("update")
            reasons.append(DataEffectReason("update", "UPDATE ... SET statement", stmt))

        if _TRUNCATE_RE.match(stmt_for_leading) and "truncate" not in seen_kinds:
            seen_kinds.add("truncate")
            reasons.append(DataEffectReason("truncate", "TRUNCATE statement", stmt))

        if _DROP_TABLE_RE.search(stmt) and "drop_table" not in seen_kinds:
            seen_kinds.add("drop_table")
            reasons.append(DataEffectReason("drop_table", "DROP TABLE statement", stmt))

        if _DROP_COLUMN_RE.search(stmt) and "drop_column" not in seen_kinds:
            seen_kinds.add("drop_column")
            reasons.append(DataEffectReason("drop_column", "DROP COLUMN clause", stmt))

        if _ALTER_TYPE_USING_RE.search(stmt) and "alter_type_using" not in seen_kinds:
            seen_kinds.add("alter_type_using")
            reasons.append(
                DataEffectReason(
                    "alter_type_using", "ALTER COLUMN ... TYPE ... USING clause", stmt
                )
            )

    return reasons


def classify_changeset(sql_text: str, structured_tags: list[str]) -> list[DataEffectReason]:
    """Classify a changeset given its forward ``<sql>`` text and any
    structured data-effect change-type tags found as direct children."""
    reasons = _classify_sql_text(sql_text)
    seen_kinds = {r.kind for r in reasons}
    for tag in structured_tags:
        kind = f"structured:{tag}"
        if kind not in seen_kinds:
            seen_kinds.add(kind)
            reasons.append(DataEffectReason(kind, f"structured <{tag}> element"))
    return reasons


def has_data_effect_line(comment_text: str | None) -> bool:
    """Whether *comment_text* (a changeset's raw ``<comment>`` text) carries
    the ``DATA EFFECT:`` marker on some line."""
    if not comment_text:
        return False
    return DATA_EFFECT_MARKER in comment_text


def extract_data_effect_lines(comment_text: str | None) -> list[str]:
    """Return every line (stripped) containing the marker, in document order."""
    if not comment_text:
        return []
    return [
        line.strip()
        for line in comment_text.splitlines()
        if DATA_EFFECT_MARKER in line
    ]


# ---------------------------------------------------------------------------
# Master include-order walk + per-changeset extraction
# ---------------------------------------------------------------------------


def parse_master_include_order(master_path: Path) -> list[str]:
    tree = ET.parse(master_path)
    root = tree.getroot()
    return [
        Path(el.get("file")).name
        for el in root.iter(f"{_XSD_NS}include")
        if el.get("file")
    ]


@dataclass(frozen=True)
class ChangesetInfo:
    changeset_id: str
    file: str
    author: str
    sql_text: str
    comment_text: str | None
    structured_tags: list[str]


def iter_changesets_from_text(xml_text: str, basename: str):
    """Yield :class:`ChangesetInfo` for every ``<changeSet>`` in *xml_text*
    (a changelog file's raw content, from disk or from ``git show``), in
    document order. ``<rollback>`` bodies are structurally excluded.

    Text-based (not path-based) so a caller can parse a changelog file's
    content AT A GIT REF without checking that revision out — the shape
    ``scripts/list_data_effects.py`` needs to compare two refs' changeset
    sets without touching the working tree.
    """
    root = ET.fromstring(xml_text)
    for cs in root.iter(f"{_XSD_NS}changeSet"):
        cs_id = cs.get("id", "")
        author = cs.get("author", "")
        sql_texts = [el.text or "" for el in cs.findall(f"{_XSD_NS}sql") if el.text]
        comment_el = cs.find(f"{_XSD_NS}comment")
        comment_text = comment_el.text if comment_el is not None else None
        structured_tags = [
            tag for tag in _STRUCTURED_DATA_EFFECT_TAGS if cs.find(f"{_XSD_NS}{tag}") is not None
        ]
        yield ChangesetInfo(
            changeset_id=cs_id,
            file=basename,
            author=author,
            sql_text="\n".join(sql_texts),
            comment_text=comment_text,
            structured_tags=structured_tags,
        )


def iter_changesets(changelog_dir: Path, basename: str):
    """Yield :class:`ChangesetInfo` for every ``<changeSet>`` in *basename*
    on disk, in document order. Thin wrapper over
    :func:`iter_changesets_from_text`."""
    path = changelog_dir / basename
    yield from iter_changesets_from_text(path.read_text(), basename)


@dataclass
class DataEffectFinding:
    changeset: ChangesetInfo
    reasons: list[DataEffectReason]


@dataclass
class AnalysisResult:
    walked_files: list[str] = field(default_factory=list)
    data_effecting: list[DataEffectFinding] = field(default_factory=list)
    missing_disclosure: list[DataEffectFinding] = field(default_factory=list)


def analyze_changelog(
    changelog_dir: Path = CHANGELOG_DIR,
    master_path: Path = MASTER_CHANGELOG,
) -> AnalysisResult:
    """Walk *master_path*'s include order and classify every changeset.

    Returns every data-effecting changeset found, plus the subset still
    missing a ``DATA EFFECT:`` line in its ``<comment>``.
    """
    include_order = parse_master_include_order(master_path)

    on_disk = {p.name for p in changelog_dir.glob("*.xml") if p.name != master_path.name}
    included = set(include_order)
    assert included == on_disk, (
        "db.changelog-master.xml include list drifted from the changelog "
        f"directory contents: included-not-on-disk={included - on_disk}, "
        f"on-disk-not-included={on_disk - included}"
    )

    result = AnalysisResult(walked_files=include_order)

    for basename in include_order:
        for cs in iter_changesets(changelog_dir, basename):
            reasons = classify_changeset(cs.sql_text, cs.structured_tags)
            if not reasons:
                continue
            finding = DataEffectFinding(changeset=cs, reasons=reasons)
            result.data_effecting.append(finding)
            if not has_data_effect_line(cs.comment_text):
                result.missing_disclosure.append(finding)

    return result


if __name__ == "__main__":
    res = analyze_changelog()
    print(f"walked {len(res.walked_files)} files")
    print(f"{len(res.data_effecting)} data-effecting changeset(s)")
    for f in res.data_effecting:
        kinds = ",".join(r.kind for r in f.reasons)
        flag = "MISSING" if f in res.missing_disclosure else "ok"
        print(f"  [{flag}] {f.changeset.file}:{f.changeset.changeset_id} ({kinds})")
