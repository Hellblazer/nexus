# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4m6i0.2: Liquibase VALIDATE CONSTRAINT precondition-guard CI lint.

Provenance: nexus-ms57z (2026-07-09, GH #1390) — engine-service v0.1.36
crash-looped on boot because changeset ``catalog-013-2`` ran five bare
``ALTER TABLE ... VALIDATE CONSTRAINT ...`` statements as one atomic
changeset; on an aged/divergent box missing one of the five constraints
(``chunks_384_chash_len_check`` on an empty/never-populated bge-768-only
box), Postgres raised a hard ERROR and the migration — hence the whole
service — crash-looped every restart. Sibling bead nexus-4m6i0.1 fixed the
direct incident by retrofitting ``catalog-013-2``/``catalog-013-3`` with two
approved-safe guard shapes (see below). This lint statically enumerates
every ``VALIDATE CONSTRAINT`` statement in the Liquibase chain and fails CI
if a NEW one lands without one of those two shapes — so the next instance
of the class cannot silently ship (nexus-4m6i0 epic: "validate fail-loud
mechanisms against aged real-world fleet state, not fresh synthetic DBs").

This module deliberately REUSES the parsing machinery from the sibling
FORCE-RLS lint (``tests/test_changelog_rls_lint.py``, born from
nexus-1wjmq) rather than re-deriving it: ``parse_master_include_order`` (the
``<include>`` walk), ``_strip_comments``/``_strip_exempt_dollar_bodies``
(comment stripping; ``DO $$ ... $$`` anonymous blocks are correctly left
INTACT by that helper — they execute immediately at migration time, unlike
a ``CREATE FUNCTION ... $$...$$`` body which only runs later, at CALL time),
``_table_key`` (schema.table casing normalization), and ``CHANGELOG_DIR`` /
``MASTER_CHANGELOG`` (the two path constants the changelog walk needs).
Kept SEPARATE as its own file (per the
bead's stated preference) because the two lints check unrelated properties
of the same changelog tree — a FORCE-RLS DML no-op risk vs. a VALIDATE
CONSTRAINT crash-loop risk — and mixing them would make either harder to
reason about in isolation. Element-level ``<preConditions>`` detection
requires visiting the ``<changeSet>`` element directly (the RLS lint's
``iter_changesets`` only yields already-extracted ``<sql>`` text, not the
element), so this file has its own small ``_iter_changesets_for_validate``
rather than extending the shared one — a deliberate, minimal duplication
that keeps the RLS lint's contract unchanged (bead explicitly: "do not
modify the RLS lint").

One deliberate divergence from the RLS lint's statement-splitting approach:
this file does NOT reuse ``_split_statements``. That helper blanks
single-quoted string literals (``_STRING_LITERAL_RE.sub("''", frag)``) so a
literal like ``current_setting('nexus.tenant', true)`` cannot false-positive
as a table reference — exactly right for the RLS lint's purposes, but fatal
for this one: the per-statement guard shape's constraint name lives INSIDE
a string literal (``WHERE conname = 'chunks_384_chash_len_check'``), and
blanking it would destroy the one piece of information this lint needs to
verify the guard actually names the SAME constraint the VALIDATE targets
(the decoy-guard rejection below). Instead this file regex-scans the
comment-stripped, dollar-body-stripped text directly, tracking each
``DO $$ ... $$`` block's character span and searching for a guard within
that span, BEFORE the VALIDATE position it is meant to cover.

Two approved-safe shapes (nexus-4m6i0.1, already shipped in
``catalog-013-chash-checks-validate.xml``):

1. **Whole-changeset ``<preConditions>``** (any ``onFail`` value — the
   precise value is not this lint's concern) that NAMES the constraint
   being VALIDATEd somewhere in its condition text (e.g. a
   ``<sqlCheck expectedResult="5">`` counting the five chash-length CHECK
   constraints in ``pg_constraint`` by name, matching ``catalog-013-2``'s
   shape exactly; ``onFail="MARK_RAN"`` writes a one-time, non-retrying
   DATABASECHANGELOG row instead of crash-looping when the precondition
   fails). Coverage is evaluated PER CONSTRAINT, not per changeset
   (nexus-d4vy6): a changeset having SOME ``<preConditions>`` does not
   excuse a VALIDATE of a constraint that precondition never names — a
   partial-coverage precondition still leaves every un-named constraint's
   VALIDATE bare and flagged, exactly as if there were no preConditions at
   all for that specific statement.
2. **Per-statement ``DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint
   WHERE conname = '...') THEN ALTER TABLE ... VALIDATE CONSTRAINT ...;
   END IF; END $$;``** — the VALIDATE is nested inside a dollar-quoted
   anonymous block that itself contains an ``IF EXISTS`` guard against
   ``pg_constraint`` for the SAME constraint name, matching
   ``catalog-013-3``'s shape exactly (five independent per-table guards,
   any subset of the five constraints may be missing on a given box and
   every OTHER constraint still gets validated).

A **bare** ``VALIDATE CONSTRAINT`` — neither inside a preConditions-guarded
changeset nor inside a DO-block with a preceding, matching ``IF EXISTS``
check — is a violation. Order matters (mirrors the RLS lint's
same-changeset-backstop order-sensitivity): a guard is only a guard if it
precedes the VALIDATE it is meant to cover, and it must name the SAME
constraint — a ``DO`` block whose ``IF EXISTS`` checks a DIFFERENT
constraint name is a decoy, not a guard, and does not excuse the VALIDATE
inside it.

Retrofitted (nexus-4m6i0.13, 2026-07-10): ``fk-002-validate.xml`` (changesets
``fk-002-7``..``fk-002-11``) and ``fk-003-validate.xml`` (changesets
``fk-003-7``..``fk-003-11``) together used to carry 10 bare
``VALIDATE CONSTRAINT`` statements, one per changeset, each guarding a
collection-registry FK added ``NOT VALID`` earlier in the SAME file's chain
(``fk-002-collection-registry.xml`` / ``fk-003-collection-registry-extra.xml``).
Per nexus-4m6i0.1's precedent, Liquibase checksum-immutability means an
already-executed changeset cannot have its ``<sql>`` body edited in place —
but ``<preConditions>`` IS checksum-neutral (verified against the pinned
liquibase-core 4.29.0 sources: ``ChangeSet.generateCheckSum()`` hashes only
the ``<sql>``/``<changes>`` body and SqlVisitors), so each of the 10
changesets was retrofitted with a whole-changeset ``<preConditions
onFail="MARK_RAN">`` (Shape 1, single-name form — each changeset validates
exactly one constraint, unlike ``catalog-013-2``'s five-constraint IN-list
form). NO ``catalog-013-3``-style defensive re-validate changeset was added:
that changeset rescues collateral damage from catalog-013-2's MONOLITHIC
precondition (one missing constraint MARK_RANs all five VALIDATEs at once),
a coupling the independent fk changesets never had — each skips only its own
VALIDATE, and the other nine validate through their own guards regardless
(nexus-4m6i0.13 review finding: an inert mirror changeset with a misleading
rationale is worse than none). The ALLOWLIST below is now empty — all 10
formerly-bare statements are covered by Shape 1, verified directly against
the real changelog rather than allowlisted.

Documented static blind spots (mirroring the RLS lint's own admissions):

- **``sqlFile`` / ``customChange`` / ``createProcedure``** elements are
  invisible to this lint (only ``<sql>`` children of ``<changeSet>`` are
  scanned) — zero current usage in the real corpus (grep-verified
  2026-07-09), so purely defensive; the RLS lint already carries a
  dedicated ``UNSCANNED_ELEMENT`` tripwire for this class and this file
  does not duplicate it.
- **Schema scope is hardcoded** to ``nexus`` and ``t1`` (the only two
  schemas this codebase's changelogs use today), matching the RLS lint's
  own scope. A third schema would be invisible to the VALIDATE regex here;
  the RLS lint's ``CREATE SCHEMA`` tripwire already covers detecting a new
  schema's introduction, so this file does not re-derive that check.
- **Nested or nonstandard dollar-quote tags** (anything other than a
  single ``DO $$ ... $$`` or ``DO $tag$ ... $tag$`` per statement) are not
  specially handled beyond the same backreference-tag matching the RLS
  lint's own ``_DOLLAR_QUOTE_RE`` uses. Absent from the real corpus today.
- **String literals are never blanked in this file** (unlike the RLS
  lint's ``_split_statements``, which blanks them to prevent a literal
  false-positiving as a table reference — see the divergence note above),
  since the constraint name this lint needs to compare lives INSIDE a
  string literal; this means a hypothetical constraint name containing a
  comment-triggering sequence like ``--`` would be silently mis-tokenized
  by ``_strip_comments`` (which runs first and is unaware of string-literal
  boundaries) before this lint's regexes ever see it — the same latent
  comment-stripping-order blind spot the RLS lint documents for its own
  approach, absent from the real corpus today (grep-verified 2026-07-09)
  but more exposed here since this file leans on literal text directly.
- **``<preConditions>`` boolean grouping (``<and>``/``<or>``/``<not>``) is
  not distinguished.** ``_iter_changesets_for_validate`` collects every
  ``<sqlCheck>`` descendant of a ``<preConditions>`` element regardless of
  which boolean-grouping element (if any) it is nested under, and
  ``_constraint_named_in_preconditions`` treats each one as an independent,
  AND-multiplied gate. Liquibase's own default IS "AND all direct
  children", which is what every ``<preConditions>`` in the real corpus
  uses today — but an explicit ``<or>`` grouping two sqlChecks would be
  mis-evaluated the same way: each constraint named under either branch of
  the OR would be marked "covered" even though only ONE of them is actually
  proven to exist at runtime (a genuinely OR-gated pair, where the
  non-matching branch's named constraint may not exist at all). Zero usage
  of ``<and>``/``<or>``/``<not>`` anywhere in
  ``service/src/main/resources/db/changelog/`` today (grep-verified
  2026-07-09: ``grep -rn "<and>\\|<or>\\|<not>" service/src/main/resources/
  db/changelog/`` returns nothing) — presently inert, would need its own
  detector if this grouping is ever introduced.
- **Shape 2's THEN-branch depth counter (nexus-42cwz fix, see
  ``_then_branch_span``) handles arbitrarily-nested ``IF ... END IF`` pairs
  inside a guard's THEN branch, but nothing beyond that.** It does not
  recognize ``CASE ... WHEN ... END CASE`` branching as an alternative
  conditional form, and if the matched ``IF EXISTS (pg_constraint ...)``
  guard is itself written as a non-first ``ELSIF`` branch of a larger
  ``IF/ELSIF/ELSE`` chain, only THAT branch's own span is computed (correct
  for the guard's own coverage claim, since its THEN branch is unambiguous
  regardless of how many sibling branches precede it, but a differently-
  shaped adversarial construction using ``CASE`` instead of ``IF`` would be
  invisible to this check entirely — no guard match at all, which fails
  CLOSED, i.e. the VALIDATE would be flagged as bare rather than falsely
  accepted). Absent from the real corpus today (grep-verified 2026-07-09:
  no ``CASE`` construct appears in any ``DO $$ ... $$`` block containing a
  VALIDATE CONSTRAINT statement).
- **A trailing semicolon inside a Shape-1 ``<sqlCheck>`` body causes a
  false REJECT, not a false accept.** ``_constraint_named_in_preconditions``
  matches ``_PRECONDITIONS_COVERAGE_RE`` via ``fullmatch`` against the
  comment-stripped, trimmed body; a legitimate guard whose author included
  a trailing ``;`` (not valid inside a Liquibase ``<sqlCheck>`` body, but a
  plausible authoring mistake) would fail ``fullmatch`` and be treated as
  NOT covering the constraint. This is fail-safe (forces stricter,
  corrected authoring rather than silently accepting a malformed guard) and
  therefore not a safety hole, only an authoring-friction cost — documented
  here rather than fixed because loosening the match would reopen the
  substring-decoy gap ``fullmatch`` was introduced to close (nexus-e439y
  Fix 3).
- **General scope boundary**: this lint verifies STRUCTURAL shape only — a
  guard of the right kind exists, in the right direction, covering the
  right branch, naming the right constraint(s) — never SQL SEMANTICS. A
  guard that is structurally correct but semantically wrong (e.g. a
  ``pg_constraint`` existence check for the right ``conname`` that also
  carries an extra ``WHERE`` clause narrowing it to the wrong schema, or a
  ``DO`` block guard whose ``IF EXISTS`` subquery has been altered to check
  an unrelated but confusingly-named table) is out of this lint's reach and
  relies on ordinary human review of migrations, same posture as the
  sibling RLS lint (which makes an equivalent distinction for its own
  toggle-wrap / same-changeset-backstop shapes).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.test_changelog_rls_lint import (
    CHANGELOG_DIR,
    MASTER_CHANGELOG,
    _strip_comments,
    _strip_exempt_dollar_bodies,
    _table_key,
    parse_master_include_order,
)

_XSD_NS = "{http://www.liquibase.org/xml/ns/dbchangelog}"


# ---------------------------------------------------------------------------
# Regexes (operate on comment-stripped, exempt-dollar-body-stripped text —
# see _strip_comments / _strip_exempt_dollar_bodies, reused from the RLS
# lint. Unlike that lint, string literals are NOT blanked here — see the
# module docstring for why.)
# ---------------------------------------------------------------------------

_VALIDATE_RE = re.compile(
    r"ALTER\s+TABLE\s+(nexus|t1)\.(\w+)\s+VALIDATE\s+CONSTRAINT\s+(\w+)",
    re.IGNORECASE,
)

# A DO $$ ... $$ / DO $tag$ ... $tag$ anonymous block, tag-matched via
# backreference exactly like the RLS lint's _DOLLAR_QUOTE_RE (just anchored
# to a leading "DO" since that's the only dollar-quoted construct this lint
# needs to look INSIDE — CREATE FUNCTION/TRIGGER bodies are already blanked
# to a single space by _strip_exempt_dollar_bodies before this regex runs).
_DO_BLOCK_RE = re.compile(r"DO\s+\$(\w*)\$(.*?)\$\1\$", re.IGNORECASE | re.DOTALL)

# The per-statement guard shape: IF EXISTS (SELECT 1 FROM pg_constraint
# WHERE conname = '<name>'). Captures the guarded constraint NAME (a string
# literal here, deliberately not blanked — see module docstring) so it can
# be compared against the VALIDATE statement's own target identifier.
_GUARD_RE = re.compile(
    r"IF\s+EXISTS\s*\(\s*SELECT\s+1\s+FROM\s+pg_constraint\s+WHERE\s+conname\s*=\s*"
    r"'(\w+)'\s*\)",
    re.IGNORECASE | re.DOTALL,
)

# Shape 2's THEN-branch containment check (nexus-42cwz, round-6 critique of
# nexus-4m6i0.2): a matched _GUARD_RE hit previously only proved the
# VALIDATE's position came TEXTUALLY AFTER a name-matching `IF EXISTS (...)`
# somewhere earlier in the same DO block -- never that the VALIDATE was
# actually structurally INSIDE that IF's THEN branch. Two false-accepts
# this closes: a VALIDATE in the guard's ELSE branch ("validate only when
# the constraint is ABSENT" -- the ms57z crash-loop shape expressed via
# Shape 2 instead of Shape 1's expectedResult direction, round 4's
# nexus-e439y bug), and a VALIDATE placed unconditionally AFTER the guard's
# own END IF has already closed (proves nothing).
#
# `_THEN_RE` finds the guard's own THEN keyword; `_IF_BRANCH_TOKEN_RE` is a
# simple depth counter over IF / END IF / ELSE / ELSIF tokens (word-boundary
# matched, case-insensitive, on the same comment/string-stripped text every
# other regex here operates on) used by `_then_branch_span` below to find
# where that THEN branch ends. This is deliberately NOT a full PL/pgSQL
# parser: it correctly handles arbitrarily-nested IF/END IF pairs inside the
# THEN branch (a nested ELSE/ELSIF/END IF does not terminate the OUTER
# branch -- only one at the SAME depth as the branch's own THEN does), which
# covers more than the real corpus currently needs (catalog-013-3 never
# nests). What it does NOT attempt: CASE/WHEN branching, or an ELSIF chain
# where the guard itself is a non-first branch of a larger IF -- see the
# module docstring's "Documented static blind spots" for what remains
# consciously out of scope.
_THEN_RE = re.compile(r"\bTHEN\b", re.IGNORECASE)
_IF_BRANCH_TOKEN_RE = re.compile(
    r"\bEND\s+IF\b|\bELSIF\b|\bELSE\b|\bIF\b", re.IGNORECASE
)

# Round-6 critique (string-literal depth-counter bug): this file deliberately
# never blanks string literals globally (the Shape-2 guard's constraint name
# and Shape 1's conname literals live INSIDE them), but the IF/ELSE/ELSIF/
# END-IF token scan must not see literal CONTENT -- an ordinary
# ``RAISE NOTICE 'skip if missing';`` in a THEN branch injected a phantom
# "if" token, corrupting the depth counter and silently re-opening both
# false-accept classes (ELSE-branch and post-END-IF VALIDATE) the THEN-span
# check exists to close. The blanking below is LENGTH-PRESERVING (contents
# replaced by spaces, quotes kept) so every offset stays aligned with the
# un-blanked ``cleaned`` text the caller's match positions come from. A
# mis-paired apostrophe (e.g. inside a nested dollar-quoted string) can only
# OVER-blank, which fails CLOSED: tokens disappear, the span ends early or
# the THEN is not found, the guard is rejected, and the VALIDATE is flagged.
_SQ_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def _blank_string_literals(text: str) -> str:
    """Replace every single-quoted literal's CONTENT with spaces, keeping the
    quotes and the exact original length (offset-stable)."""
    return _SQ_LITERAL_RE.sub(
        lambda m: "'" + " " * (len(m.group(0)) - 2) + "'", text
    )


def _then_branch_span(cleaned: str, search_start: int, search_end: int):
    """Locate the next ``THEN`` keyword in ``cleaned[search_start:search_end]``
    and return ``(then_end, branch_end)`` -- the half-open span
    ``[then_end, branch_end)`` of the THEN-branch immediately following it,
    i.e. up to (but not including) the matching ``ELSE``/``ELSIF``/``END IF``
    at the SAME nesting depth as that THEN. Returns ``None`` if no ``THEN``
    is found in range. A nested ``IF ... END IF`` (or ``ELSE``/``ELSIF``)
    inside the branch is tracked via a depth counter and does not terminate
    the span early -- only a token at depth 0 (the branch's own level) does.

    The keyword scan runs over a literal-blanked copy of ``cleaned`` (length-
    preserving, so all offsets remain valid against the caller's text) --
    IF/ELSE-shaped English words inside string literals must not perturb the
    depth counter (round-6 critique)."""
    tokens_text = _blank_string_literals(cleaned)
    then_match = _THEN_RE.search(tokens_text, search_start, search_end)
    if then_match is None:
        return None
    then_end = then_match.end()
    depth = 0
    for m in _IF_BRANCH_TOKEN_RE.finditer(tokens_text, then_end, search_end):
        tok = m.group(0).upper()
        if tok.startswith("END"):
            if depth == 0:
                return then_end, m.start()
            depth -= 1
        elif tok in ("ELSE", "ELSIF"):
            if depth == 0:
                return then_end, m.start()
        else:  # bare "IF" opening a nested branch
            depth += 1
    return then_end, search_end


# Shape 1's anchoring regex (nexus-8rgaj, round-3 critique of nexus-4m6i0.2):
# a whole-changeset <preConditions> only "covers" a constraint if its name
# appears INSIDE an actual pg_constraint-referencing existence/count clause,
# not merely anywhere in the <preConditions> element's text blob. Mirrors
# _GUARD_RE's anchoring discipline, generalized to the two forms Liquibase's
# <sqlCheck> bodies actually use in the real corpus (read directly from
# service/src/main/resources/db/changelog/catalog-013-chash-checks-
# validate.xml, 2026-07-09):
#   - single-name form:  ... WHERE conname = 'X'
#   - IN-list form:      ... WHERE conname IN ('X', 'Y', ...)  (catalog-013-2)
# Each match's captured name(s) are what count as covered — a constraint
# name appearing OUTSIDE this clause shape (e.g. in an unrelated table's
# sqlCheck, or only inside a comment) is not coverage. This regex is matched
# with `fullmatch` against a single <sqlCheck>'s ENTIRE (comment-stripped,
# trimmed) body in `_constraint_named_in_preconditions` — not `search`/
# `finditer` over an arbitrary blob — so a clause merely EMBEDDED as a
# substring inside a different query (e.g. inside a dollar-quoted string
# literal) cannot match (nexus-e439y Fix 3).
#
# COUNT(*) and COUNT(1) are both accepted (equally-safe, equally-real
# shapes), and whitespace around `=` is optional (nexus-e439y Fix 2: the
# prior regex falsely flagged `conname='X'` (no space) and `COUNT(1)` as
# violations despite both being semantically identical to the recognized
# shapes).
_PRECONDITIONS_COVERAGE_RE = re.compile(
    r"SELECT\s+COUNT\(\s*[*1]\s*\)\s+FROM\s+pg_constraint\s+WHERE\s+conname\s*"
    r"(?:=\s*'(\w+)'|IN\s*\(\s*('\w+'(?:\s*,\s*'\w+')*)\s*\))",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Master include-order walk + per-changeset <sql> extraction with
# whole-changeset <preConditions> presence (the RLS lint's iter_changesets
# does not expose the element itself, only extracted <sql> text, so this
# lint needs its own minimal walk for that one extra bit of information).
# ---------------------------------------------------------------------------


def _iter_changesets_for_validate(changelog_dir: Path, basename: str):
    """Yield (changeset_id, sql_text, sql_checks) for every ``<changeSet>``
    in *basename*, in document order. ``sql_checks`` is a list of
    ``(expected_result, body_text)`` tuples, one per ``<sqlCheck>`` element
    found (at any nesting depth) inside the changeset's ``<preConditions>``,
    or ``None`` when the changeset has no ``<preConditions>`` element at
    all — callers use it to verify per-constraint COVERAGE, not just
    presence (nexus-d4vy6: presence alone is not sufficient, see
    analyze_changelog).

    Capturing each ``<sqlCheck>``'s ``expectedResult`` XML ATTRIBUTE
    alongside its body text (not just concatenating every ``<preConditions>``
    descendant's TEXT NODES via a bare ``itertext()`` call, the prior
    approach) is the fix for nexus-e439y (round-4 critique of
    nexus-4m6i0.2, Fix 1 CRITICAL): ``expectedResult`` is invisible to
    ``itertext()``, so a backwards guard — ``expectedResult="0"``, "pass
    only if the constraint does NOT exist", the exact inverse of a safe
    VALIDATE guard — was indistinguishable from a correct
    ``expectedResult="1"`` existence assertion. Keeping each ``<sqlCheck>``
    as an independent tuple, rather than one concatenated blob, is also
    what lets ``_constraint_named_in_preconditions`` ``fullmatch`` each
    body in isolation (nexus-e439y Fix 3: closes the dollar-quoted
    substring-decoy gap — see that function's docstring).

    ``<rollback>`` bodies are structurally excluded (sibling elements of
    ``<sql>``, never visited) — rollback SQL never runs at migration time,
    same posture as the RLS lint."""
    path = changelog_dir / basename
    tree = ET.parse(path)
    root = tree.getroot()
    for cs in root.iter(f"{_XSD_NS}changeSet"):
        cs_id = cs.get("id", "")
        pc_el = cs.find(f"{_XSD_NS}preConditions")
        sql_checks = None
        if pc_el is not None:
            sql_checks = [
                (sc.get("expectedResult", ""), "".join(sc.itertext()))
                for sc in pc_el.iter(f"{_XSD_NS}sqlCheck")
            ]
        sql_texts = [
            el.text or "" for el in cs.findall(f"{_XSD_NS}sql") if el.text
        ]
        yield cs_id, "\n".join(sql_texts), sql_checks


def _constraint_named_in_preconditions(constraint: str, sql_checks) -> bool:
    """*constraint* is covered only if some ``<sqlCheck>`` in *sql_checks*
    (a list of ``(expected_result, body_text)`` tuples — see
    ``_iter_changesets_for_validate``) satisfies BOTH of:

    1. **Shape**: its ENTIRE (comment-stripped, whitespace-trimmed) body
       IS — via ``fullmatch``, not ``search``/``finditer`` — an actual
       ``pg_constraint``-referencing existence/count clause naming
       *constraint* (``_PRECONDITIONS_COVERAGE_RE``). Requiring the clause
       be the sqlCheck's WHOLE query (not merely present somewhere within
       a longer one) is what rejects a decoy where an unrelated query
       embeds the clause's literal text inside a dollar-quoted string
       literal (nexus-e439y Fix 3).
    2. **Direction**: its ``expectedResult`` attribute actually asserts
       EXISTENCE of the named constraint(s), not absence (nexus-e439y
       Fix 1 CRITICAL):
         - single-name form (``conname = 'X'``): only
           ``expectedResult="1"`` is a safe existence assertion. Any other
           value — notably ``"0"``, meaning "pass only if X does NOT
           exist", the exact inverse of a safe VALIDATE guard — does not
           cover the constraint.
         - IN-list form (``conname IN ('a', 'b', ..., 'X')`` with N names):
           only ``expectedResult`` equal to N (the count of names in the
           list) correctly asserts ALL N constraints exist. Any other
           value covers NONE of the named constraints, even ones whose
           name appears in the list.

    Fix for nexus-8rgaj (round-3 critique of nexus-4m6i0.2): the prior
    unscoped ``\\bconstraint\\b`` substring match treated a constraint name
    mentioned ANYWHERE — including inside an unrelated table's sqlCheck, or
    inside a SQL comment — as full coverage. Anchoring to the real ``SELECT
    COUNT(*|1) FROM pg_constraint WHERE conname (= | IN (...))`` shape
    (mirroring Shape 2's ``_GUARD_RE`` discipline) closes that gap. Each
    sqlCheck's text is run through ``_strip_comments`` first — same
    treatment ``sql_text`` already gets — so a comment merely naming the
    constraint cannot satisfy the check while the operative query names a
    different one (e.g. a typo'd ``conname`` literal)."""
    if not sql_checks:
        return False
    for expected_result, text in sql_checks:
        cleaned = _strip_comments(text).strip()
        match = _PRECONDITIONS_COVERAGE_RE.fullmatch(cleaned)
        if match is None:
            continue
        single_name, in_list = match.group(1), match.group(2)
        if single_name is not None:
            if single_name == constraint and expected_result == "1":
                return True
        elif in_list is not None:
            names = re.findall(r"'(\w+)'", in_list)
            if constraint not in names:
                continue
            try:
                expected_n = int(expected_result)
            except ValueError:
                continue
            if expected_n == len(names):
                return True
    return False


# ---------------------------------------------------------------------------
# Findings + result shape
# ---------------------------------------------------------------------------

BARE_VALIDATE = "bare_validate"


@dataclass(frozen=True)
class Finding:
    changeset_id: str
    file: str
    table: str
    constraint: str
    kind: str
    detail: str = ""


@dataclass
class AnalysisResult:
    walked_files: list[str] = field(default_factory=list)
    violations: list[Finding] = field(default_factory=list)
    unused_allowlist: set[tuple[str, str, str]] = field(default_factory=set)

    @property
    def total_violations(self) -> int:
        return len(self.violations)


# ---------------------------------------------------------------------------
# Allowlist — EMPTY (nexus-4m6i0.13, 2026-07-10). The 10 formerly-grandfathered
# bare-VALIDATE members (fk-002-7..11 in fk-002-validate.xml, fk-003-7..11 in
# fk-003-validate.xml) were retrofitted with whole-changeset <preConditions>
# (Shape 1) and are now REFERENCE-accepted, verified directly against the
# real changelog, exactly like catalog-013-2/catalog-013-3 already were —
# never allowlisted in the first place. Kept as a typed empty tuple (not
# deleted) so the analyzer signature and every allowlist-consuming test below
# keep working unchanged; a future genuinely-unfixable bare VALIDATE would
# repopulate this. Keyed (changeset_id, "schema.table", constraint) —
# constraint is part of the key (not just changeset+table) so that two
# DIFFERENT constraints VALIDATEd bare on the SAME table in the SAME
# changeset would be tracked as two independent findings, never collapsed
# into one (code-review finding on nexus-4m6i0.2: the coarser (changeset,
# table) key silently swallowed exactly this shape). An UNUSED entry fails
# the real-changelog test (rot detection) — every entry here must be
# independently re-derivable from a live analyzer run, never hand-waved.
# ---------------------------------------------------------------------------

ALLOWLIST: tuple[tuple[str, str, str, str], ...] = ()


def _allowlist_keys(
    allowlist: tuple[tuple[str, str, str, str], ...],
) -> set[tuple[str, str, str]]:
    return {
        (cs_id, table, constraint) for cs_id, table, constraint, _reason in allowlist
    }


# ---------------------------------------------------------------------------
# The analyzer
# ---------------------------------------------------------------------------


def analyze_changelog(
    changelog_dir: Path = CHANGELOG_DIR,
    master_path: Path = MASTER_CHANGELOG,
    allowlist: tuple[tuple[str, str, str, str], ...] = ALLOWLIST,
) -> AnalysisResult:
    """Walk *master_path*'s include order and flag every ``VALIDATE
    CONSTRAINT`` statement in every changeset's ``<sql>`` body that is
    neither covered by a whole-changeset ``<preConditions>`` naming THAT
    SPECIFIC constraint nor by a preceding, name-matching ``DO $$ IF EXISTS
    (pg_constraint) ... END $$`` guard whose THEN branch structurally
    CONTAINS the VALIDATE, in the SAME statement's dollar-quoted block.
    Whole-changeset ``<preConditions>`` coverage is evaluated per
    constraint, not per changeset (nexus-d4vy6): a changeset having SOME
    preConditions does not excuse a VALIDATE of a constraint that
    precondition never names — see ``_constraint_named_in_preconditions``.

    Shape 2's guard is only a guard if the VALIDATE falls inside its THEN
    branch (nexus-42cwz, round-6 critique): a matching ``IF EXISTS (...)``
    appearing textually before the VALIDATE is NOT sufficient on its own —
    a VALIDATE in the guard's ELSE branch or placed after the guard's own
    ``END IF`` has already closed is textually "after" the guard but
    structurally uncovered by it. See ``_then_branch_span``.

    Non-vacuity: the walked-file set is asserted equal to the live
    ``<include>`` list AND to the live glob of ``*.xml`` files (minus the
    master itself) in *changelog_dir*, mirroring the RLS lint's own
    self-verifying floor (cannot silently drift as new changelogs land).
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

    allow_keys = _allowlist_keys(allowlist)
    raw_findings: dict[tuple[str, str, str], Finding] = {}

    for basename in include_order:
        for cs_id, sql_text, sql_checks in _iter_changesets_for_validate(
            changelog_dir, basename
        ):
            if not sql_text:
                continue

            cleaned = _strip_exempt_dollar_bodies(_strip_comments(sql_text))
            do_blocks = [(m.start(), m.end()) for m in _DO_BLOCK_RE.finditer(cleaned)]

            for vm in _VALIDATE_RE.finditer(cleaned):
                table = _table_key(vm.group(1), vm.group(2))
                constraint = vm.group(3)
                pos = vm.start()

                # Shape 1: whole-changeset <preConditions> that names THIS
                # constraint specifically — presence of SOME <preConditions>
                # is not enough (nexus-d4vy6); it must cover the constraint
                # actually being VALIDATEd.
                if sql_checks is not None and _constraint_named_in_preconditions(
                    constraint, sql_checks
                ):
                    continue

                # Shape 2: a preceding, name-matching DO $$ IF EXISTS guard
                # in the same dollar-quoted block, AND the VALIDATE must be
                # structurally INSIDE that guard's THEN branch -- not merely
                # textually after the guard's IF EXISTS match (nexus-42cwz,
                # round-6 critique: textual precedence alone falsely
                # certified an ELSE-branch or after-END-IF VALIDATE safe).
                enclosing = next(
                    (b for b in do_blocks if b[0] <= pos < b[1]), None
                )
                guarded = False
                if enclosing is not None:
                    block_start, block_end = enclosing
                    for gm in _GUARD_RE.finditer(cleaned, block_start, pos):
                        if gm.group(1) != constraint:
                            continue
                        span = _then_branch_span(cleaned, gm.end(), block_end)
                        if span is None:
                            continue
                        then_end, branch_end = span
                        if then_end <= pos < branch_end:
                            guarded = True
                            break

                if guarded:
                    continue

                key = (cs_id, table, constraint)
                raw_findings[key] = Finding(
                    changeset_id=cs_id,
                    file=basename,
                    table=table,
                    constraint=constraint,
                    kind=BARE_VALIDATE,
                    detail=(
                        f"changeset {cs_id}: VALIDATE CONSTRAINT {constraint} "
                        f"on {table} has no whole-changeset <preConditions> "
                        "and no preceding, name-matching DO $$ IF EXISTS "
                        "(SELECT 1 FROM pg_constraint WHERE conname = "
                        f"'{constraint}') guard. Add one of the two "
                        "approved-safe shapes (nexus-4m6i0.1 precedent: "
                        "catalog-013-2 / catalog-013-3) or this VALIDATE "
                        "can hard-ERROR and crash-loop the service on any "
                        "box where the constraint doesn't exist; see "
                        "nexus-ms57z / nexus-4m6i0.2."
                    ),
                )

    violations = [f for key, f in raw_findings.items() if key not in allow_keys]
    consumed = {key for key in raw_findings if key in allow_keys}
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


def test_bare_validate_no_guard_at_all_is_flagged(tmp_path):
    """The base case: a top-level VALIDATE CONSTRAINT with no preConditions
    and no DO block at all."""
    xml = """
    <changeSet id="cs-bare" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-bare"
        and v.table == "nexus.widgets"
        and v.constraint == "widgets_len_check"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), result.violations


def test_do_block_with_no_if_exists_guard_is_still_flagged(tmp_path):
    """A DO $$ ... $$ block alone does NOT make a VALIDATE safe — only a
    matching IF EXISTS guard does. This is the critical negative case."""
    xml = """
    <changeSet id="cs-do-unguarded" author="t">
        <sql splitStatements="false">
DO $$
BEGIN
    ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
END $$;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-do-unguarded"
        and v.table == "nexus.widgets"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), (
        "a DO block with no IF EXISTS guard must still be flagged — the DO "
        f"wrapper alone is not a safety proof: {result.violations}"
    )


def test_decoy_guard_checking_wrong_constraint_name_is_flagged(tmp_path):
    """A DO block's IF EXISTS guard that checks a DIFFERENT constraint name
    than the one actually VALIDATEd is a decoy, not a guard — must still be
    flagged."""
    xml = """
    <changeSet id="cs-decoy" author="t">
        <sql splitStatements="false">
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'some_other_check') THEN
        ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
    END IF;
END $$;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-decoy"
        and v.table == "nexus.widgets"
        and v.constraint == "widgets_len_check"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), (
        "a guard naming the WRONG constraint must not excuse the VALIDATE "
        f"it doesn't actually cover: {result.violations}"
    )


def test_guard_after_validate_does_not_count(tmp_path):
    """Order sensitivity, mirroring the RLS lint's same-changeset-backstop
    rule: an IF EXISTS guard that appears AFTER the VALIDATE it would need
    to cover proves nothing about it."""
    xml = """
    <changeSet id="cs-guard-after" author="t">
        <sql splitStatements="false">
DO $$
BEGIN
    ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'widgets_len_check') THEN
        NULL;
    END IF;
END $$;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-guard-after" and v.kind == BARE_VALIDATE
        for v in result.violations
    ), (
        "a guard appearing after the VALIDATE must not count as covering "
        f"it: {result.violations}"
    )


def test_do_guard_else_branch_validate_is_flagged(tmp_path):
    """Regression for nexus-42cwz (round-6 critique of nexus-4m6i0.2): a
    VALIDATE placed in the ELSE branch of a name-matching IF EXISTS guard
    is semantically "validate only when the constraint does NOT exist" --
    the exact inversion of a safe guard, the Shape-2 analog of round 4's
    expectedResult="0" bug (nexus-e439y). Textual precedence of the guard's
    IF EXISTS match is not enough -- the VALIDATE must be structurally
    INSIDE the guard's THEN branch, not merely somewhere after it."""
    xml = """
    <changeSet id="cs-else-branch" author="t">
        <sql splitStatements="false">
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'widgets_len_check') THEN
        NULL;
    ELSE
        ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
    END IF;
END $$;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-else-branch"
        and v.table == "nexus.widgets"
        and v.constraint == "widgets_len_check"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), (
        "a VALIDATE in the guard's ELSE branch -- \"validate only when "
        "absent\", the ms57z inversion shape expressed via Shape 2 -- must "
        f"be flagged, not falsely certified safe: {result.violations}"
    )


def test_do_guard_validate_after_end_if_is_flagged(tmp_path):
    """Regression for nexus-42cwz: a VALIDATE placed unconditionally AFTER
    the guard's own END IF has already closed proves nothing -- the guard's
    textual precedence alone (the pre-fix check) falsely certified this
    safe because the VALIDATE's position is still "after" the IF EXISTS
    match, just no longer inside its THEN branch."""
    xml = """
    <changeSet id="cs-after-end-if" author="t">
        <sql splitStatements="false">
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'widgets_len_check') THEN
        RAISE NOTICE 'present';
    END IF;
    ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
END $$;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-after-end-if"
        and v.table == "nexus.widgets"
        and v.constraint == "widgets_len_check"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), (
        "a VALIDATE placed AFTER the guard's own END IF must be flagged -- "
        "an unconditionally-reached statement proves nothing about the "
        f"constraint's existence: {result.violations}"
    )


def test_do_guard_then_branch_with_nested_if_and_validate_accepted(tmp_path):
    """A VALIDATE preceded by an unrelated NESTED IF/END IF and another
    statement, all still inside the OUTER guard's THEN branch, must be
    accepted -- confirms the THEN-span depth counter does not mistake the
    nested IF's own END IF for the outer branch's terminator, and that the
    real catalog-013-3 shape (VALIDATE directly inside a THEN branch, no
    ELSE) remains accepted even with intervening structure before it."""
    xml = """
    <changeSet id="cs-nested-then" author="t">
        <sql splitStatements="false">
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'widgets_len_check') THEN
        RAISE NOTICE 'checking dependent object too';
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_class WHERE relname = 'widgets') THEN
            RAISE NOTICE 'table present too';
        END IF;
        ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
    END IF;
END $$;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert result.violations == [], result.violations


def test_do_guard_else_branch_validate_flagged_despite_keyword_in_literal(tmp_path):
    """Regression for the round-6 critique's string-literal depth-counter
    bug: a routine ``RAISE NOTICE 'skip if missing';`` inside the guard's
    THEN branch contains the plain English word "if" -- with string
    literals un-blanked, the depth counter treated it as opening a nested
    IF, so the real ELSE was skipped (evaluated at phantom depth 1) and an
    ELSE-branch VALIDATE was falsely certified safe. The token scan must
    run over literal-blanked text so ordinary log messages cannot corrupt
    branch tracking."""
    xml = """
    <changeSet id="cs-else-literal-if" author="t">
        <sql splitStatements="false">
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'widgets_len_check') THEN
        RAISE NOTICE 'skip if missing';
    ELSE
        ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
    END IF;
END $$;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-else-literal-if"
        and v.constraint == "widgets_len_check"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), (
        "an ELSE-branch VALIDATE must stay flagged even when a THEN-branch "
        "string literal contains the word 'if' -- literal text must not "
        f"corrupt the depth counter: {result.violations}"
    )


def test_do_guard_after_end_if_validate_flagged_despite_keyword_in_literal(tmp_path):
    """Same round-6-critique bug, second confirmed variant: a log message
    containing "if" inside the THEN branch made the depth counter miss the
    real END IF, silently extending the guard's supposed THEN span over an
    unconditional VALIDATE placed after it."""
    xml = """
    <changeSet id="cs-after-endif-literal-if" author="t">
        <sql splitStatements="false">
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'widgets_len_check') THEN
        RAISE NOTICE 'log message mentioning if for humans';
    END IF;
    ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
END $$;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-after-endif-literal-if"
        and v.constraint == "widgets_len_check"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), (
        "a post-END-IF VALIDATE must stay flagged even when a THEN-branch "
        "string literal contains the word 'if' -- literal text must not "
        f"corrupt the depth counter: {result.violations}"
    )


def test_mixed_changeset_bare_one_flagged_guarded_ones_accepted(tmp_path):
    """Multiple VALIDATE CONSTRAINT statements in one changeset: two
    per-statement DO-guarded ones must be accepted, and one bare one must
    be flagged — independently, not as an all-or-nothing changeset
    verdict."""
    xml = """
    <changeSet id="cs-mixed" author="t">
        <sql splitStatements="false">
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'widgets_a_check') THEN
        ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_a_check;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'widgets_b_check') THEN
        ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_b_check;
    END IF;
END $$;

ALTER TABLE nexus.gadgets VALIDATE CONSTRAINT gadgets_len_check;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert result.violations == [
        v for v in result.violations if v.table == "nexus.gadgets"
    ], result.violations
    assert any(
        v.changeset_id == "cs-mixed"
        and v.table == "nexus.gadgets"
        and v.constraint == "gadgets_len_check"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), result.violations
    assert not any(v.table == "nexus.widgets" for v in result.violations), (
        result.violations
    )


def test_preconditions_partial_coverage_missing_constraints_flagged(tmp_path):
    """Regression for nexus-d4vy6: a whole-changeset ``<preConditions>`` only
    guards the constraint(s) it actually names, not every VALIDATE in the
    changeset. A precondition whose sqlCheck names ``widgets_a_check`` must
    NOT excuse two OTHER, unrelated bare VALIDATEs (on different tables) in
    the same changeset — this is the ms57z failure class hiding behind a
    guard that looks present but doesn't cover the risk."""
    xml = """
    <changeSet id="cs-partial-guard" author="t">
        <preConditions onFail="MARK_RAN">
            <sqlCheck expectedResult="1">SELECT COUNT(*) FROM pg_constraint WHERE conname = 'widgets_a_check'</sqlCheck>
        </preConditions>
        <sql splitStatements="true">
ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_a_check;
ALTER TABLE nexus.gadgets VALIDATE CONSTRAINT gadgets_b_check;
ALTER TABLE nexus.sprockets VALIDATE CONSTRAINT sprockets_c_check;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-partial-guard"
        and v.table == "nexus.gadgets"
        and v.constraint == "gadgets_b_check"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), (
        "gadgets_b_check is NOT named anywhere in the preConditions block and "
        f"must be flagged despite the changeset having SOME preConditions: "
        f"{result.violations}"
    )
    assert any(
        v.changeset_id == "cs-partial-guard"
        and v.table == "nexus.sprockets"
        and v.constraint == "sprockets_c_check"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), (
        "sprockets_c_check is NOT named anywhere in the preConditions block "
        f"and must be flagged: {result.violations}"
    )
    assert not any(v.constraint == "widgets_a_check" for v in result.violations), (
        "widgets_a_check IS named in the preConditions sqlCheck and must not "
        f"be flagged: {result.violations}"
    )


def test_preconditions_unrelated_query_merely_mentioning_name_is_flagged(tmp_path):
    """Regression for nexus-8rgaj (round-3 critique of nexus-4m6i0.2): a
    whole-changeset ``<preConditions>`` whose ``<sqlCheck>`` queries an
    UNRELATED table (not ``pg_constraint`` at all) but happens to mention
    the VALIDATEd constraint's name as a word-bounded substring inside a
    string literal must NOT be treated as coverage. Reproduces the
    reviewer's exact false-accept: an ``nexus.audit_log`` existence check
    that merely names ``chunks_384_chash_len_check`` inside a descriptive
    string literal proves nothing about whether that constraint actually
    exists in ``pg_constraint`` — the whole point of Shape 1 coverage."""
    xml = """
    <changeSet id="cs-decoy" author="t">
        <preConditions onFail="MARK_RAN">
            <sqlCheck expectedResult="1">SELECT COUNT(*) FROM nexus.audit_log WHERE event_description = 'ran migration for chunks_384_chash_len_check'</sqlCheck>
        </preConditions>
        <sql splitStatements="true">
ALTER TABLE nexus.chunks_384 VALIDATE CONSTRAINT chunks_384_chash_len_check;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-decoy"
        and v.table == "nexus.chunks_384"
        and v.constraint == "chunks_384_chash_len_check"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), (
        "an nexus.audit_log sqlCheck that merely MENTIONS the constraint "
        "name as a string-literal substring is not a pg_constraint "
        f"existence check and must not excuse the VALIDATE: {result.violations}"
    )


def test_preconditions_comment_only_mention_with_typo_in_real_query_is_flagged(
    tmp_path,
):
    """Regression for nexus-8rgaj: a SQL comment inside a ``<sqlCheck>`` body
    that correctly names the constraint does NOT count as coverage when the
    OPERATIVE query (the actual ``pg_constraint`` count) has a different
    (e.g. typo'd) ``conname`` literal. ``preconditions_text`` must be run
    through ``_strip_comments`` before matching, exactly like ``sql_text``
    already is — otherwise the comment alone would satisfy the check."""
    xml = """
    <changeSet id="cs-comment-decoy" author="t">
        <preConditions onFail="MARK_RAN">
            <sqlCheck expectedResult="1">
-- Checks widgets_len_check exists (superseded query below has a typo)
SELECT COUNT(*) FROM pg_constraint WHERE conname = 'widgets_len_typo'
            </sqlCheck>
        </preConditions>
        <sql splitStatements="true">
ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-comment-decoy"
        and v.table == "nexus.widgets"
        and v.constraint == "widgets_len_check"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), (
        "a SQL comment merely naming the constraint must not count as "
        "coverage when the operative pg_constraint query names a DIFFERENT "
        f"constraint: {result.violations}"
    )


def test_preconditions_expected_result_zero_single_name_inversion_is_flagged(
    tmp_path,
):
    """Regression for nexus-e439y (round-4 critique of nexus-4m6i0.2, Fix 1
    CRITICAL): a whole-changeset ``<preConditions>`` whose ``<sqlCheck
    expectedResult="0">`` asserts the constraint does NOT exist — the exact
    INVERSE of a safe VALIDATE guard — must NOT be treated as coverage.
    Reviewer's reproduction: this shape previously reported ZERO violations
    despite ``expectedResult="0"`` meaning "pass only if the constraint is
    ABSENT", i.e. a backwards guard indistinguishable, to the old
    itertext()-only check, from a correct ``expectedResult="1"``."""
    xml = """
    <changeSet id="cs-inverted" author="t">
        <preConditions onFail="MARK_RAN">
            <sqlCheck expectedResult="0">SELECT COUNT(*) FROM pg_constraint WHERE conname = 'widgets_len_check'</sqlCheck>
        </preConditions>
        <sql splitStatements="true">
ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-inverted"
        and v.table == "nexus.widgets"
        and v.constraint == "widgets_len_check"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), (
        "expectedResult=\"0\" asserts the constraint does NOT exist — the "
        "inverse of a safe existence guard — and must not excuse the "
        f"VALIDATE it superficially appears to cover: {result.violations}"
    )


def test_preconditions_mixed_direction_two_sqlchecks_crash_loop_shape(tmp_path):
    """Regression for nexus-e439y Fix 1 CRITICAL, the exact ms57z-shaped
    crash-loop reproduction: TWO separate ``<sqlCheck>`` elements under one
    ``<preConditions>`` (Liquibase ANDs sibling preconditions) — one
    correctly asserting ``a_check`` EXISTS (``expectedResult="1"``), a
    second asserting ``b_check`` does NOT exist (``expectedResult="0"``) —
    guarding a changeset that VALIDATEs BOTH constraints. On a box where
    ``b_check`` is genuinely missing (the dangerous real-world case), BOTH
    preconditions are simultaneously true (a_check exists=1 AND b_check
    absent=0) -> the changeset executes -> ``VALIDATE CONSTRAINT b_check``
    hard-errors -> crash loop. ``a_check`` (correctly guarded) must NOT be
    flagged; ``b_check`` (guarded backwards) MUST be flagged."""
    xml = """
    <changeSet id="cs-mixed-direction" author="t">
        <preConditions onFail="MARK_RAN">
            <sqlCheck expectedResult="1">SELECT COUNT(*) FROM pg_constraint WHERE conname = 'a_check'</sqlCheck>
            <sqlCheck expectedResult="0">SELECT COUNT(*) FROM pg_constraint WHERE conname = 'b_check'</sqlCheck>
        </preConditions>
        <sql splitStatements="true">
ALTER TABLE nexus.a_tbl VALIDATE CONSTRAINT a_check;
ALTER TABLE nexus.b_tbl VALIDATE CONSTRAINT b_check;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-mixed-direction"
        and v.table == "nexus.b_tbl"
        and v.constraint == "b_check"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), (
        "b_check's guard asserts ABSENCE (expectedResult=0) — the ms57z "
        f"crash-loop shape — and must be flagged: {result.violations}"
    )
    assert not any(v.constraint == "a_check" for v in result.violations), (
        "a_check IS correctly guarded (expectedResult=1, existence "
        f"assertion) and must not be flagged: {result.violations}"
    )


def test_preconditions_in_list_expected_result_not_matching_count_is_flagged(
    tmp_path,
):
    """The IN-list form's expectedResult must equal the count of named
    constraints for it to count as an existence assertion of ALL of them.
    An IN-list of 3 names with ``expectedResult="1"`` (or any value other
    than 3) does not prove all three exist — none of them should be treated
    as covered."""
    xml = """
    <changeSet id="cs-in-list-wrong-count" author="t">
        <preConditions onFail="MARK_RAN">
            <sqlCheck expectedResult="1">SELECT COUNT(*) FROM pg_constraint WHERE conname IN ('a_check', 'b_check', 'c_check')</sqlCheck>
        </preConditions>
        <sql splitStatements="true">
ALTER TABLE nexus.a_tbl VALIDATE CONSTRAINT a_check;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-in-list-wrong-count"
        and v.constraint == "a_check"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), (
        "expectedResult=1 for a 3-name IN-list does not assert all three "
        f"exist and must not excuse a_check's VALIDATE: {result.violations}"
    )


def test_preconditions_single_name_no_space_around_equals_accepted(tmp_path):
    """Regression for nexus-e439y Fix 2 (Significant, fail-safe direction):
    ``conname='X'`` with NO whitespace around ``=`` is an equally-safe,
    equally-real shape and must not be spuriously flagged."""
    xml = """
    <changeSet id="cs-no-space" author="t">
        <preConditions onFail="MARK_RAN">
            <sqlCheck expectedResult="1">SELECT COUNT(*) FROM pg_constraint WHERE conname='widgets_len_check'</sqlCheck>
        </preConditions>
        <sql splitStatements="true">
ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert result.violations == [], result.violations


def test_preconditions_count_one_form_accepted(tmp_path):
    """Regression for nexus-e439y Fix 2: ``SELECT COUNT(1) ...`` is an
    equally-safe, equally-real shape as ``COUNT(*)`` and must not be
    spuriously flagged."""
    xml = """
    <changeSet id="cs-count-one" author="t">
        <preConditions onFail="MARK_RAN">
            <sqlCheck expectedResult="1">SELECT COUNT(1) FROM pg_constraint WHERE conname = 'widgets_len_check'</sqlCheck>
        </preConditions>
        <sql splitStatements="true">
ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert result.violations == [], result.violations


def test_preconditions_dollar_quoted_decoy_substring_is_flagged(tmp_path):
    """Regression for nexus-e439y Fix 3 (Important): a whole-changeset
    ``<preConditions>`` whose ``<sqlCheck>`` queries an UNRELATED table
    (``nexus.audit_log``, not ``pg_constraint``) but embeds the coverage
    clause's literal text INSIDE a dollar-quoted (``$$...$$``) string
    literal must NOT be treated as coverage — the clause must be the
    sqlCheck's ENTIRE query (``fullmatch``), not a fragment merely present
    somewhere within a different one."""
    xml = """
    <changeSet id="cs-dollar-decoy" author="t">
        <preConditions onFail="MARK_RAN">
            <sqlCheck expectedResult="1">SELECT COUNT(*) FROM nexus.audit_log WHERE description = $$ran migration note: SELECT COUNT(*) FROM pg_constraint WHERE conname = 'widgets_len_check'$$</sqlCheck>
        </preConditions>
        <sql splitStatements="true">
ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert any(
        v.changeset_id == "cs-dollar-decoy"
        and v.table == "nexus.widgets"
        and v.constraint == "widgets_len_check"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), (
        "a sqlCheck against an unrelated table that merely EMBEDS the "
        "coverage clause's text inside a dollar-quoted string literal is "
        f"not a real pg_constraint existence check: {result.violations}"
    )


def test_preconditions_in_list_form_covers_all_named_constraints(tmp_path):
    """The real catalog-013-2 shape: a SINGLE sqlCheck whose body is
    ``SELECT COUNT(*) FROM pg_constraint WHERE conname IN (...)`` naming
    several constraints. Every constraint named in the IN-list must be
    treated as covered — the anchoring fix must not regress the already-
    shipped shape by only recognizing the single-name ``=`` form."""
    xml = """
    <changeSet id="cs-in-list" author="t">
        <preConditions onFail="MARK_RAN">
            <sqlCheck expectedResult="3">SELECT COUNT(*) FROM pg_constraint WHERE conname IN ('a_check', 'b_check', 'c_check')</sqlCheck>
        </preConditions>
        <sql splitStatements="true">
ALTER TABLE nexus.a_tbl VALIDATE CONSTRAINT a_check;
ALTER TABLE nexus.b_tbl VALIDATE CONSTRAINT b_check;
ALTER TABLE nexus.c_tbl VALIDATE CONSTRAINT c_check;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert result.violations == [], result.violations


def test_allowlist_keyed_by_constraint_not_just_table(tmp_path):
    """Regression for the code-review finding: the allowlist/violation key
    must include the constraint name, not just (changeset_id, table). Two
    DIFFERENT constraints VALIDATEd bare on the SAME table in the SAME
    changeset must be tracked as two independent findings — an allowlist
    entry for one must never silently swallow the other, genuinely new,
    unallowlisted one."""
    xml = """
    <changeSet id="cs-two-constraints" author="t">
        <sql splitStatements="true">
ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_a_check;
ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_b_check;
        </sql>
    </changeSet>
    """
    changelog_dir, master = _write_changelog(tmp_path, xml)
    allowlist = (
        (
            "cs-two-constraints",
            "nexus.widgets",
            "widgets_a_check",
            "pre-existing, grandfathered for this test only",
        ),
    )
    result = analyze_changelog(
        changelog_dir=changelog_dir, master_path=master, allowlist=allowlist
    )
    assert any(
        v.changeset_id == "cs-two-constraints"
        and v.table == "nexus.widgets"
        and v.constraint == "widgets_b_check"
        and v.kind == BARE_VALIDATE
        for v in result.violations
    ), (
        "widgets_b_check is a genuinely new, unallowlisted bare VALIDATE on "
        "the same table as the allowlisted widgets_a_check — it must not be "
        f"silently swallowed by the coarser (changeset, table) key: "
        f"{result.violations}"
    )
    assert not any(v.constraint == "widgets_a_check" for v in result.violations), (
        f"widgets_a_check IS allowlisted and must not be flagged: {result.violations}"
    )
    assert result.unused_allowlist == set(), result.unused_allowlist


# ---------------------------------------------------------------------------
# ACCEPT cases — must NOT be flagged
# ---------------------------------------------------------------------------


def test_whole_changeset_preconditions_accepted(tmp_path):
    """Approved Shape 1: a whole-changeset <preConditions>, matching
    catalog-013-2's shape exactly — covers every VALIDATE in the
    changeset regardless of onFail value."""
    xml = """
    <changeSet id="cs-pc" author="t">
        <preConditions onFail="MARK_RAN">
            <sqlCheck expectedResult="1">SELECT COUNT(*) FROM pg_constraint WHERE conname = 'widgets_len_check'</sqlCheck>
        </preConditions>
        <sql splitStatements="true">
ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert result.violations == [], result.violations


def test_do_block_if_exists_guard_accepted(tmp_path):
    """Approved Shape 2: a DO $$ ... IF EXISTS (pg_constraint) ... $$ guard
    preceding the VALIDATE it covers, matching catalog-013-3's shape
    exactly."""
    xml = """
    <changeSet id="cs-guarded" author="t">
        <sql splitStatements="false">
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'widgets_len_check') THEN
        ALTER TABLE nexus.widgets VALIDATE CONSTRAINT widgets_len_check;
    END IF;
END $$;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert result.violations == [], result.violations


def test_five_independent_do_guards_all_accepted(tmp_path):
    """The exact catalog-013-3 shape: five INDEPENDENT per-table DO-block
    guards in one changeset, each covering its own VALIDATE."""
    xml = """
    <changeSet id="cs-five" author="t">
        <sql splitStatements="false">
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'a_check') THEN
        ALTER TABLE nexus.a_tbl VALIDATE CONSTRAINT a_check;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'b_check') THEN
        ALTER TABLE nexus.b_tbl VALIDATE CONSTRAINT b_check;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'c_check') THEN
        ALTER TABLE nexus.c_tbl VALIDATE CONSTRAINT c_check;
    END IF;
END $$;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert result.violations == [], result.violations


def test_non_validate_sql_in_changeset_ignored(tmp_path):
    """A changeset with ordinary DDL and no VALIDATE CONSTRAINT at all must
    never be flagged."""
    xml = """
    <changeSet id="cs-plain" author="t">
        <sql splitStatements="true">
CREATE TABLE nexus.widgets (id int);
ALTER TABLE nexus.widgets ADD CONSTRAINT widgets_len_check CHECK (id > 0) NOT VALID;
        </sql>
    </changeSet>
    """
    result = _analyze_synthetic(tmp_path, xml)
    assert result.violations == [], result.violations


# ---------------------------------------------------------------------------
# Real changelog reference-shape pins
# ---------------------------------------------------------------------------


def test_real_changelog_catalog_013_2_preconditions_shape_accepted():
    """catalog-013-2's whole-changeset <preConditions onFail="MARK_RAN">
    shape (nexus-4m6i0.1) must not appear among violations."""
    result = analyze_changelog()
    assert not any(
        v.changeset_id == "catalog-013-2" for v in result.violations
    ), result.violations


def test_real_changelog_catalog_013_3_do_guard_shape_accepted():
    """catalog-013-3's five independent DO $$ IF EXISTS guards
    (nexus-4m6i0.1) must not appear among violations."""
    result = analyze_changelog()
    assert not any(
        v.changeset_id == "catalog-013-3" for v in result.violations
    ), result.violations


# ---------------------------------------------------------------------------
# Non-vacuity: the unused-allowlist-entry detection mechanism itself
# ---------------------------------------------------------------------------


def test_unused_allowlist_entry_is_detected_when_no_matching_finding(tmp_path):
    """A direct test of the unused-allowlist DETECTION MECHANISM itself, not
    just the real-corpus ``== set()`` assertion (which only proves the 10
    real entries ARE reproduced, not that a genuinely stale/wrong entry
    would be caught). A bogus allowlist key that matches no live finding
    must land in ``unused_allowlist``."""
    xml = """
    <changeSet id="cs-plain" author="t">
        <sql splitStatements="true">
CREATE TABLE nexus.widgets (id int);
        </sql>
    </changeSet>
    """
    changelog_dir, master = _write_changelog(tmp_path, xml)
    bogus_allowlist = (
        (
            "nonexistent-changeset",
            "nexus.nonexistent_table",
            "nonexistent_constraint",
            "stale entry",
        ),
    )
    result = analyze_changelog(
        changelog_dir=changelog_dir, master_path=master, allowlist=bogus_allowlist
    )
    assert result.unused_allowlist == {
        ("nonexistent-changeset", "nexus.nonexistent_table", "nonexistent_constraint")
    }
    assert result.violations == []


# ---------------------------------------------------------------------------
# Non-vacuity self-checks (mirror test_changelog_rls_lint.py's floor)
# ---------------------------------------------------------------------------


def test_master_include_order_matches_directory_contents():
    """Self-verifying floor: every non-master ``*.xml`` file in the real
    changelog directory is included exactly once in
    ``db.changelog-master.xml``, and vice versa — derived from the
    directory, never a hardcoded constant, so it cannot silently drift."""
    include_order = parse_master_include_order(MASTER_CHANGELOG)
    on_disk = {
        p.name for p in CHANGELOG_DIR.glob("*.xml")
        if p.name != MASTER_CHANGELOG.name
    }
    assert set(include_order) == on_disk
    assert len(include_order) == len(on_disk)
    assert len(on_disk) >= 30, (
        f"changelog directory glob returned suspiciously few files "
        f"({len(on_disk)}) — possible empty/misconfigured CHANGELOG_DIR"
    )


def test_allowlist_has_no_duplicate_keys():
    keys = [(cs_id, table, constraint) for cs_id, table, constraint, _ in ALLOWLIST]
    assert len(keys) == len(set(keys)), (
        "duplicate ALLOWLIST (changeset, table, constraint) key"
    )


# ---------------------------------------------------------------------------
# The real changelog — exact-set assertion
# ---------------------------------------------------------------------------


def test_real_changelog_zero_violations_and_full_allowlist_consumption():
    """The tripwire itself: run the analyzer against the ACTUAL
    ``service/src/main/resources/db/changelog/`` tree.

    Two exact (``==``, never ``>=``) assertions:
      1. Zero violations survive the allowlist — every bare VALIDATE
         CONSTRAINT statement in the real changelog is either safe
         (whole-changeset preConditions / per-statement DO-IF-EXISTS guard)
         or an explicitly grandfathered historical member.
      2. Zero UNUSED allowlist entries — every grandfathered member is
         independently re-derived by a live analyzer run against the real
         changelog, not hand-waved.

    Ground truth (nexus-4m6i0.13, 2026-07-10): the ALLOWLIST is now EMPTY.
    The 10 (changeset, table) bare-VALIDATE pairs that used to be
    grandfathered across fk-002-validate.xml (fk-002-7..11) and
    fk-003-validate.xml (fk-003-7..11) are retrofitted with whole-changeset
    preConditions (Shape 1) and are REFERENCE shapes now, same posture as
    catalog-013-2 (whole-changeset preConditions) and catalog-013-3 (five
    per-statement DO/IF-EXISTS guards). None of these are allowlisted: they
    are all verified directly against the real changelog by this analyzer.
    """
    result = analyze_changelog()
    assert result.total_violations == 0, (
        "VALIDATE CONSTRAINT precondition-guard tripwire fired — see "
        "nexus-ms57z / nexus-4m6i0.2 for the failure class and the two "
        f"approved-safe guard shapes: "
        f"{[(v.changeset_id, v.table, v.constraint, v.detail) for v in result.violations]}"
    )
    assert result.unused_allowlist == set(), (
        "ALLOWLIST entries not reproduced by a live analyzer run (rot — "
        "either the migration changed, which should be impossible for a "
        "checksum-immutable changeset, or the analyzer's classification "
        f"logic regressed): {result.unused_allowlist}"
    )


def test_real_changelog_walks_all_files():
    """Non-vacuity floor for the real-changelog run specifically: the
    analyzer must actually have walked every included file, not silently
    short-circuited."""
    result = analyze_changelog()
    on_disk = {
        p.name for p in CHANGELOG_DIR.glob("*.xml")
        if p.name != MASTER_CHANGELOG.name
    }
    assert len(result.walked_files) == len(on_disk)
    assert set(result.walked_files) == on_disk


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
