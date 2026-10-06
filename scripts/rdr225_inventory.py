#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-225 P1.1 (nexus-3wh8d.6): generate and pin the inventory of every object
that names ``nexus.chunks``, ``taxonomy_centroids``, ``catalog_document_chunks``,
``topic_assignments`` or ``chunk_orphaned_at``.

WHY. RDR-225 splits ``nexus.chunks`` and ``taxonomy_centroids`` into one
partitioned table per embedding model and tenant, with a four-column key
``(tenant_id, collection, chash, embedding_model)``. Every FK into the table,
every ``ON CONFLICT`` arbiter on it, every ``INSERT`` column list that feeds it
or the three referencing tables, every function body, view, policy, grant and
isolation check must change with it. The RDR's failure mode "a write site
missed in the rewrite" shows up as ``no unique or exclusion constraint matching
the ON CONFLICT specification`` at runtime. This module is the guard: the
inventory is GENERATED, never hand-kept, and ``tests/test_rdr225_inventory_lint.py``
fails when the tree names one of the five tables somewhere the pinned
inventory does not list.

WHAT IT SCANS.

* Liquibase changelogs (``service/src/main/resources/db/changelog``): the
  ``<sql>`` bodies, ``<rollback>`` bodies and ``<sqlCheck>`` preconditions of
  every changeset, in master include order, with line numbers. Every
  top-level statement that names a table becomes a row of its own kind (function,
  view, trigger, index, policy, table, fk, constraint, rls, column, grant, comment,
  drop, set-constraints, do-block, dml, ...). Embedded statements (function bodies,
  DO blocks) are broken into ``sites``: INSERT (with column list and ON CONFLICT
  arbiter), UPDATE (with the assigned columns), DELETE, TRUNCATE, READ (FROM/JOIN),
  REFERENCES, SET CONSTRAINTS, ALTER/CREATE INDEX/POLICY/TRIGGER, GRANT, COMMENT
  and name literals (``'nexus.chunks'::regclass``).
* Functions, views, triggers and policies carry ``live``: a later definition (or
  drop) of the same name supersedes an earlier one even when the later body does
  not name the tables, because P1.3 redefines the LATEST live body. ``live_in``
  names the changeset that holds it.
* Java under ``service/src/main/java``: per enclosing method, the SQL string
  literals (with ``DimTables.CHUNKS_TABLE_NAME`` / ``CENTROIDS_TABLE_NAME``
  substituted), the jOOQ typed forms (``insertInto(CHUNKS)``, ``.onConflict(...)``,
  ``DimTables.CHUNKS.get(dim)`` accessors), and calls to a SQL routine whose body
  names the tables.
* A short list of Python anchors outside ``service/`` (the doctor RLS canary), and the Python
  registries that list tables (``_RLS_TENANT_TABLES``, ``CHASH_BEARING_TABLES``,
  ``LEGACY_CHASH_BEARING_TABLES``) recorded entry by entry: adding or removing any entry changes
  the inventory.
* Liveness follows renames: ``ALTER TABLE x RENAME TO y`` ends x (and its indexes, constraints,
  triggers and policies) and defines y; ``ALTER INDEX x RENAME TO y`` ends x;
  ``RENAME CONSTRAINT a TO b`` ends a and defines b (RDR steps 7.2, 7.4, 7.5).

WHAT IT CANNOT READ, AND SAYS SO. A changeset whose change element is not ``<sql>`` (``addColumn``,
``createIndex``, ``insert``, ``tableExists`` ...) and that names one of the five tables, and ANY
``sqlFile`` or ``customChange``, is listed under ``unscannable``. A non-empty list fails
``--check`` and the lint, and ``--write`` refuses: rewrite the change as ``<sql>`` or teach the
scanner the element. The coverage check (``crude_locations``) reads the raw changeset text and
does not call the changelog parser, so a blind spot in the parser cannot hide in it.

WHAT IS DELIBERATELY NOT A MATCH. The five names match as whole tokens, so
``pdf_chunks``, the retired per-dimension tables, ``p_floor_min_chunks``,
``live_chunks`` and ``staging.chunks`` are different objects. A quoted spelling
(``nexus."chunks"``, ``"nexus"."chunks"``, ``"chunks"``) is the same table. Comments (SQL, XML, Javadoc) are not
code. A bare ``chunks`` in Java matches only inside a SQL-shaped string
literal, never as an identifier or a JSON key.

DETERMINISM. Rows are ordered by changelog include order then statement order
(changelog) and by path then source order (Java). No timestamps. The file
records the sha it was generated against; ``--check`` ignores that sha and every
``line`` field, so unrelated edits that shift line numbers do not fail it.

USAGE.
    python scripts/rdr225_inventory.py --write            # regenerate the pinned file
    python scripts/rdr225_inventory.py --check            # fresh generation vs pinned file
    python scripts/rdr225_inventory.py --source-sha <sha> # override the recorded sha
"""
from __future__ import annotations

import argparse
import bisect
import html
import json
import re
import subprocess
import sys
import xml.parsers.expat
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1

TABLES: tuple[str, ...] = (
    "catalog_document_chunks",
    "chunk_orphaned_at",
    "chunks",
    "taxonomy_centroids",
    "topic_assignments",
)

#: FK constraints into / out of ``nexus.chunks``. ``SET CONSTRAINTS`` names them,
#: not the table, so they are tracked as names in their own right. Anything else
#: discovered as an FK touching one of the five tables is added at scan time.
STATIC_CONSTRAINTS: dict[str, str] = {
    "fk_catalog_chunks_chunk": "chunks",
    "topic_assignments_chunk_fk": "chunks",
    "chunk_orphaned_at_chunk_fk": "chunks",
    "chunks_collection_fk": "chunks",
}

REPO_ROOT = Path(__file__).resolve().parent.parent
PINNED_PATH = REPO_ROOT / "docs" / "rdr" / "rdr-225-inventory.json"
CHANGELOG_REL = "service/src/main/resources/db/changelog"
JAVA_REL = "service/src/main/java"


@dataclass(frozen=True)
class Anchor:
    """A fixed Python-side site named by the RDR table that cannot be found by
    scanning ``service/``. ``pattern`` must match inside ``file`` or generation
    fails loudly (a moved anchor is a finding, not a silent drop)."""

    file: str
    pattern: str
    name: str
    tables: tuple[str, ...]
    rdr: str = "T11"


@dataclass(frozen=True)
class Registry:
    """A Python tuple/list literal that lists tables, recorded ENTRY BY ENTRY (every entry,
    every one that names a table, and every table an entry names). ``start`` matches the
    line that opens the literal. The container row carries the full entry list, so adding or
    removing ANY entry (not only one naming a table) changes the inventory; each entry that
    names one of the five tables is also a row of its own. ``consts`` maps a module constant
    an entry may use in place of a string (``CHUNKS_TABLE``) to its table. A literal that
    moved fails generation loudly."""

    file: str
    start: str
    name: str
    rdr: str = "T11"
    consts: tuple[tuple[str, str], ...] = ()


DEFAULT_ANCHORS: tuple[Anchor | Registry, ...] = (
    Registry("src/nexus/health.py", r"^_RLS_TENANT_TABLES: tuple\[str, \.\.\.\] = \(", "_RLS_TENANT_TABLES"),
    # the client-side chash-census registries: keyed by table name against diag_chash_conformance
    Anchor("src/nexus/db/chash_tables.py", r'^CHUNKS_TABLE: str = "nexus\.chunks"', "CHUNKS_TABLE", ("chunks",), "X6"),
    Registry("src/nexus/db/chash_tables.py", r"^CHASH_BEARING_TABLES: tuple\[ChashBearingTable, \.\.\.\] = \(",
             "CHASH_BEARING_TABLES", "X6", (("CHUNKS_TABLE", "chunks"),)),
    Registry("src/nexus/db/chash_tables.py", r"^LEGACY_CHASH_BEARING_TABLES: tuple\[ChashBearingTable, \.\.\.\] = \(",
             "LEGACY_CHASH_BEARING_TABLES", "X6", (("CHUNKS_TABLE", "chunks"),)),
)


@dataclass(frozen=True)
class Roots:
    repo: Path
    changelog_dir: Path
    java_dir: Path
    anchors: tuple[Anchor | Registry, ...] = DEFAULT_ANCHORS


def default_roots(repo: Path = REPO_ROOT) -> Roots:
    return Roots(repo=repo, changelog_dir=repo / CHANGELOG_REL, java_dir=repo / JAVA_REL)


# ---------------------------------------------------------------------------
# Regular expressions shared by the SQL and the Java string-literal scans
# ---------------------------------------------------------------------------

_TBL_ALT = "|".join(sorted(TABLES, key=len, reverse=True))
#: A table token: optional ``nexus.`` schema (quoted or not), the whole identifier, quoted
#: or not. A match preceded by another schema (``staging.chunks``) or an identifier
#: character is rejected by the lookbehind; the retired per-dimension tables and
#: ``pdf_chunks`` fail the lookahead / lookbehind on the underscore.
TBL = rf'(?:(?:"nexus"|nexus)\.)?(?P<q>")?(?P<t>{_TBL_ALT})(?(q)"|(?![\w$"]))'
NAMED = re.compile(rf'(?<![\w.$"]){TBL}', re.I)



def _t(m: re.Match[str]) -> str:
    return m.group("t").lower()


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# SQL lexing
# ---------------------------------------------------------------------------

_LEX = re.compile(
    r"--[^\n]*"
    r"|/\*.*?\*/"
    r"|'(?:[^']|'')*'"
    r"|\"(?:[^\"]|\"\")*\""
    r"|\$(?:[A-Za-z_]\w*)?\$",
    re.S,
)


def blank_comments(sql: str) -> str:
    """Replace SQL comments by spaces (newlines kept so line numbers hold). String
    literals and quoted identifiers are kept; dollar quotes are transparent."""

    def sub(m: re.Match[str]) -> str:
        s = m.group(0)
        if s.startswith("--") or s.startswith("/*"):
            return re.sub(r"[^\n]", " ", s)
        return s

    return _LEX.sub(sub, sql)


_PROSE = re.compile(
    r"(\bRAISE\s+(?:NOTICE|WARNING|EXCEPTION|INFO|LOG|DEBUG)\s+|\b(?:MESSAGE|DETAIL|HINT)\s*=\s*|\bCOMMENT\s+ON\b[^';]*?\bIS\s+)"
    r"('(?:[^']|'')*')",
    re.I | re.S | re.M,
)


def mask_prose(sql: str) -> str:
    """Blank the text of RAISE messages and COMMENT ... IS literals: they are prose
    that happens to name tables, not references. Offsets are preserved."""
    return _PROSE.sub(lambda m: m.group(1) + "'" + re.sub(r"[^\n]", " ", m.group(2)[1:-1]) + "'", sql)


def sql_code(raw: str) -> str:
    """The comment-free, prose-free SQL both the generator and the crude coverage
    detector read."""
    return mask_prose(blank_comments(raw))


def split_statements(sql: str) -> list[tuple[int, str]]:
    """Top-level statements of comment-blanked SQL as ``(offset, text)``. ``;`` inside
    a dollar-quoted body or a string does not split."""
    out: list[tuple[int, str]] = []
    stack: list[str] = []
    start = 0
    pos = 0
    for m in _LEX.finditer(sql):
        # a ';' between the previous token and this one, outside any dollar quote
        if not stack:
            for sc in re.finditer(";", sql[pos:m.start()]):
                end = pos + sc.start()
                out.append((start, sql[start:end]))
                start = end + 1
        tok = m.group(0)
        if tok.startswith("$") and len(tok) >= 2 and tok.endswith("$") and tok[0] == "$":
            if stack and stack[-1] == tok:
                stack.pop()
            else:
                stack.append(tok)
        pos = m.end()
    if not stack:
        for sc in re.finditer(";", sql[pos:]):
            end = pos + sc.start()
            out.append((start, sql[start:end]))
            start = end + 1
    if sql[start:].strip():
        out.append((start, sql[start:]))
    return [(o, s) for o, s in out if s.strip()]


def _skip_quoted(text: str, i: int) -> int:
    """Index just past the quoted run that opens at ``text[i]`` (``'...'`` or ``"..."``; a
    doubled quote inside is an escaped quote, the SQL rule). ``len`` if it never closes."""
    q = text[i]
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == q:
            if j + 1 < n and text[j + 1] == q:
                j += 2
                continue
            return j + 1
        j += 1
    return n


def balanced(text: str, i: int) -> int:
    """Index just past the ``)`` matching the ``(`` at ``text[i]``; ``len`` if open.
    A parenthesis inside a quoted string or identifier does not count."""
    depth = 0
    j = i
    n = len(text)
    while j < n:
        c = text[j]
        if c in "'\"":
            j = _skip_quoted(text, j)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def split_top_pos(text: str, sep: str = ",") -> list[tuple[int, str]]:
    """Top-level pieces of ``text`` as ``(offset, piece)``. A separator inside parentheses,
    brackets (``ARRAY['a','b']``) or a quoted string / identifier does not split. Offsets
    point at the start of each piece before it is stripped; empty pieces are dropped."""
    parts: list[tuple[int, str]] = []
    depth = 0
    start = 0
    j = 0
    n = len(text)
    while j < n:
        c = text[j]
        if c in "'\"":
            j = _skip_quoted(text, j)
            continue
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c == sep and depth == 0:
            parts.append((start, text[start:j]))
            start = j + 1
        j += 1
    parts.append((start, text[start:]))
    return [(o, p) for o, p in parts if p.strip()]


def split_top(text: str, sep: str = ",") -> list[str]:
    return [p.strip() for _, p in split_top_pos(text, sep)]


# ---------------------------------------------------------------------------
# Site extraction: shared by changelog statements and Java string literals
# ---------------------------------------------------------------------------

_INSERT = re.compile(rf"\bINSERT\s+INTO\s+(?:ONLY\s+)?{TBL}(?:\s+AS\s+\w+)?", re.I)
_INSERT_ANY = re.compile(r"\bINSERT\s+INTO\b", re.I)
_UPDATE = re.compile(rf"\bUPDATE\s+(?:ONLY\s+)?{TBL}(?:\s+(?:AS\s+)?(?!SET\b)\w+)?\s+SET\b", re.I)
_DELETE = re.compile(rf"\bDELETE\s+FROM\s+(?:ONLY\s+)?{TBL}", re.I)
_TRUNCATE = re.compile(rf"\bTRUNCATE\s+(?:TABLE\s+)?(?:ONLY\s+)?{TBL}", re.I)
_READ = re.compile(rf"(?:\bFROM|\bJOIN|\bUSING|,)\s+(?:ONLY\s+)?{TBL}", re.I)
_READ_STRICT = re.compile(rf"(?:\bFROM|\bJOIN|\bUSING)\s+(?:ONLY\s+)?{TBL}", re.I)  # Java prose has commas
_REFERENCES = re.compile(rf"\bREFERENCES\s+{TBL}", re.I)
_FK_OPTS = re.compile(
    r"\s*(?:MATCH\s+\w+|ON\s+(?:DELETE|UPDATE)\s+(?:NO\s+ACTION|CASCADE|RESTRICT|SET\s+NULL|SET\s+DEFAULT)"
    r"|NOT\s+DEFERRABLE|DEFERRABLE|INITIALLY\s+(?:DEFERRED|IMMEDIATE)|NOT\s+VALID)",
    re.I,
)
_ALTER = re.compile(rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{TBL}", re.I)
_CREATE_INDEX = re.compile(
    rf"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(?P<n>[\w.\"]+)?\s*ON\s+(?:ONLY\s+)?{TBL}",
    re.I,
)
_POLICY = re.compile(rf"\b(?P<v>CREATE|ALTER|DROP)\s+POLICY\s+(?:IF\s+EXISTS\s+)?(?P<n>\"[^\"]+\"|\w+)\s+ON\s+{TBL}", re.I)
_TRIGGER = re.compile(rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?TRIGGER\s+(?P<n>\w+)[^;]*?\sON\s+{TBL}", re.I | re.S)
_CREATE_TABLE = re.compile(rf"\bCREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{TBL}", re.I)
_GRANT = re.compile(rf"\b(?P<v>GRANT|REVOKE)\b[^;]*?\bON\s+(?:TABLE\s+)?(?:[\w.,\s]*?,\s*)?{TBL}", re.I | re.S)
_COMMENT = re.compile(rf"\bCOMMENT\s+ON\s+(?P<o>TABLE|COLUMN|CONSTRAINT|INDEX)\s+(?:[\w.\"]+\s+ON\s+)?{TBL}", re.I)
_NAME_LITERAL = re.compile(rf"'(?:nexus\.)?(?P<t>{_TBL_ALT})'(?:\s*::\s*regclass)?", re.I)
_SET_CONSTRAINTS = re.compile(r"\bSET\s+CONSTRAINTS\s+(?P<names>[^;]*?)\s+(?P<mode>DEFERRED|IMMEDIATE)\b", re.I)
_ON_CONFLICT = re.compile(r"\bON\s+CONFLICT\b", re.I)
_ALL_TABLES_GRANT = re.compile(r"\bGRANT\b[^;]*\bON\s+ALL\s+TABLES\s+IN\s+SCHEMA\s+nexus\b", re.I | re.S)

Site = dict[str, Any]


def _conflict_after(text: str, start: int, stop: int) -> str | None:
    m = _ON_CONFLICT.search(text, start, stop)
    if not m:
        return None
    i = m.end()
    while i < stop and text[i].isspace():
        i += 1
    target = ""
    if i < stop and text[i] == "(":
        j = balanced(text, i)
        target = norm(text[i:j])
        i = j
    else:
        mc = re.compile(r"ON\s+CONSTRAINT\s+(\w+)", re.I).match(text, i)
        if mc:
            target = f"ON CONSTRAINT {mc.group(1)}"
            i = mc.end()
    ma = re.compile(r"(?:\s*WHERE\s+[^;]*?)?\s*DO\s+(NOTHING|UPDATE)", re.I | re.S).match(text, i)
    action = f" DO {ma.group(1).upper()}" if ma else ""
    return (target + action).strip() or None


def extract_sites(
    text: str,
    line_of: Callable[[int], int] | None = None,
    constraints: dict[str, str] | None = None,
    bare_mentions: bool = True,
) -> list[Site]:
    """Every reference to one of the five tables in ``text`` (comment-free SQL), as
    ordered ``sites``. Each ``NAMED`` match is accounted for: anything not
    recognised as a specific verb becomes ``MENTION``, so nothing is dropped
    silently."""
    line_of = line_of or (lambda off: 1 + text.count("\n", 0, off))
    cons = constraints if constraints is not None else STATIC_CONSTRAINTS
    sites: list[Site] = []
    used: list[tuple[int, int]] = []

    def claim(m: re.Match[str]) -> None:
        used.append((m.start("t"), m.end("t")))

    def overlaps(a: int, b: int) -> bool:
        return any(a < e and s < b for s, e in used)

    def add(pos: int, verb: str, table: str, **kw: Any) -> None:
        s: Site = {"verb": verb, "table": table, "line": line_of(pos)}
        s.update({k: v for k, v in kw.items() if v is not None})
        sites.append(s)
        sites[-1]["_pos"] = pos

    inserts_any = [m.start() for m in _INSERT_ANY.finditer(text)]
    for m in _INSERT.finditer(text):
        claim(m)
        end = m.end()
        columns = None
        k = end
        while k < len(text) and text[k].isspace():
            k += 1
        if k < len(text) and text[k] == "(" and not re.match(r"\(\s*SELECT\b", text[k:k + 20], re.I):
            j = balanced(text, k)
            columns = norm(text[k + 1:j - 1])
            end = j
        nxt = [p for p in inserts_any if p > m.start()]
        stop = min([len(text)] + nxt + [i for i in [text.find(";", end)] if i >= 0])
        add(m.start(), "INSERT", _t(m), columns=columns if columns is not None else None,
            conflict=_conflict_after(text, end, stop))
        sites[-1]["columns"] = columns
        sites[-1]["conflict"] = sites[-1].get("conflict")
    for m in _UPDATE.finditer(text):
        claim(m)
        j = m.end()
        stop = len(text)
        for kw in (r"\bFROM\b", r"\bWHERE\b", r"\bRETURNING\b", ";"):
            mm = re.compile(kw, re.I).search(text, j)
            if mm:
                stop = min(stop, mm.start())
        assigned: list[str] = []
        for part in split_top(text[j:stop]):
            ma = re.match(r"(\w+)\s*=", part)
            if ma:
                assigned.append(ma.group(1))
        add(m.start(), "UPDATE", _t(m), columns=", ".join(assigned) or None)
    for m in _DELETE.finditer(text):
        claim(m)
        add(m.start(), "DELETE", _t(m))
    for m in _TRUNCATE.finditer(text):
        claim(m)
        add(m.start(), "TRUNCATE", _t(m))
    for m in _REFERENCES.finditer(text):
        claim(m)
        k = m.end()
        cols = None
        mp = re.compile(r"\s*\(").match(text, k)
        if mp:
            j = balanced(text, mp.end() - 1)
            cols = norm(text[mp.end():j - 1])
            k = j
        opts: list[str] = []
        while True:
            mo = _FK_OPTS.match(text, k)
            if not mo:
                break
            opts.append(norm(mo.group(0)))
            k = mo.end()
        add(m.start(), "REFERENCES", _t(m), columns=cols, detail=" ".join(opts) or None)
    for m in _ALTER.finditer(text):
        claim(m)
        tail = norm(text[m.end():m.end() + 160])
        add(m.start(), "ALTER TABLE", _t(m), detail=tail)
    for m in _CREATE_INDEX.finditer(text):
        claim(m)
        add(m.start(), "CREATE INDEX", _t(m), object=(m.group("n") or "").strip('"') or None)
    for m in _POLICY.finditer(text):
        claim(m)
        add(m.start(), f"{m.group('v').upper()} POLICY", _t(m), object=m.group("n").strip('"'))
    for m in _TRIGGER.finditer(text):
        claim(m)
        add(m.start(), "CREATE TRIGGER", _t(m), object=m.group("n"))
    for m in _CREATE_TABLE.finditer(text):
        claim(m)
        add(m.start(), "CREATE TABLE", _t(m))
    for m in _GRANT.finditer(text):
        claim(m)
        add(m.start(), m.group("v").upper(), _t(m), detail=norm(text[m.start():m.end()])[:160])
    for m in _COMMENT.finditer(text):
        claim(m)
        add(m.start(), f"COMMENT ON {m.group('o').upper()}", _t(m))
    for m in _SET_CONSTRAINTS.finditer(text):
        names = [n.strip().split(".")[-1].strip('"') for n in m.group("names").split(",")]
        hit = [n for n in names if n in cons]
        if hit or any(n.upper() == "ALL" for n in names):
            tbl = cons[hit[0]] if hit else "chunks"
            add(m.start(), "SET CONSTRAINTS", tbl, object=", ".join(names), detail=m.group("mode").upper())
    for m in _NAME_LITERAL.finditer(text):
        if overlaps(m.start("t"), m.end("t")):
            continue
        if not bare_mentions:
            # Java literal text: a quoted name counts only where SQL looks a table up by it
            # (an error message that quotes 'chunks' is prose)
            before = text[max(0, m.start() - 24):m.start()]
            if not (m.group(0).lower().endswith("::regclass")
                    or re.search(r"(?:to_regclass\s*\(|=|\bIN\s*\(|,)\s*$", before, re.I)):
                continue
        claim(m)
        ctx = norm(text[max(0, m.start() - 40):m.start()])[-30:]
        add(m.start(), "NAME-LITERAL", _t(m), detail=f"{ctx} {m.group(0)}".strip())
    # READ last: FROM/JOIN/USING/comma lists, minus spans the specific verbs already took
    for m in (_READ if bare_mentions else _READ_STRICT).finditer(text):
        a, b = m.start("t"), m.end("t")
        if overlaps(a, b):
            continue
        claim(m)
        add(m.start(), "READ", _t(m))
    for m in NAMED.finditer(text):
        if overlaps(m.start("t"), m.end("t")):
            continue
        if not bare_mentions and not re.match(r'"?nexus"?\.', m.group(0), re.I):
            # Java prose / JSON keys: a bare word is a table only after a SQL keyword
            if not re.search(r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE|ONLY|ON|USING|REFERENCES|TRUNCATE)\s+$",
                             text[max(0, m.start() - 14):m.start()], re.I):
                continue
        ctx = norm(text[max(0, m.start() - 40):m.start()])[-28:]
        if len(ctx) >= 28 and " " in ctx:
            ctx = ctx.split(" ", 1)[1]  # drop the word the 28-character cut landed in
        add(m.start(), "MENTION", _t(m), detail=ctx)
    sites.sort(key=lambda s: (s["_pos"], s["verb"], s["table"]))
    for s in sites:
        del s["_pos"]
    return sites


def dedupe_sites(sites: list[Site]) -> list[Site]:
    """Drop exact repeats ignoring ``line`` (a function body reading a table five
    times is one fact), keeping the first line."""
    seen: set[str] = set()
    out: list[Site] = []
    for s in sites:
        key = json.dumps({k: v for k, v in s.items() if k != "line"}, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# Changelog parsing
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    kind: str  # "sql" | "rollback" | "precondition"
    text: str
    line0: int


@dataclass
class Native:
    """A change element the SQL scanner cannot read: anything inside a changeset that is not
    ``sql`` / ``sqlCheck`` / ``comment`` / ``rollback`` / ``preConditions`` or the boolean
    wrappers of preconditions (``addColumn``, ``createIndex``, ``insert``, ``sqlFile``,
    ``customChange``, ``tableExists`` ...)."""

    element: str
    attrs: dict[str, str]
    line: int
    text: str = ""


@dataclass
class Changeset:
    file: str
    id: str
    run_always: bool
    line: int
    segments: list[Segment] = field(default_factory=list)
    native: list[Native] = field(default_factory=list)


#: Elements whose content the generator reads (or that only wrap what it reads). Every other
#: element inside a changeset is a ``Native`` one.
SCANNED_ELEMENTS = frozenset({"databaseChangeLog", "changeSet", "comment", "sql", "sqlCheck", "rollback",
                              "preConditions", "and", "or", "not"})
#: Elements the scanner can never read, whatever they name: the SQL is in another file or in
#: Java, or is computed.
UNREADABLE_ELEMENTS = frozenset({"sqlFile", "customChange"})


def master_order(changelog_dir: Path) -> list[str]:
    master = changelog_dir / "db.changelog-master.xml"
    names: list[str] = []
    handler = xml.parsers.expat.ParserCreate()

    def start(name: str, attrs: dict[str, str]) -> None:
        if name == "include" and attrs.get("file"):
            names.append(attrs["file"].split("/")[-1])

    handler.StartElementHandler = start
    handler.Parse(master.read_bytes(), True)
    return names


def parse_changelog(path: Path) -> list[Changeset]:
    raw = path.read_bytes()
    parser = xml.parsers.expat.ParserCreate()
    parser.buffer_text = False
    stack: list[str] = []
    out: list[Changeset] = []
    state: dict[str, Any] = {"seg": None, "native": []}

    def line_after_tag(byte_index: int) -> int:
        gt = raw.find(b">", byte_index)
        return raw.count(b"\n", 0, gt + 1) + 1

    def start(name: str, attrs: dict[str, str]) -> None:
        stack.append(name)
        if name == "changeSet":
            out.append(Changeset(path.name, attrs.get("id", ""), attrs.get("runAlways") == "true",
                                 parser.CurrentLineNumber))
        elif name not in SCANNED_ELEMENTS and out and "changeSet" in stack:
            nat = Native(name, dict(attrs), parser.CurrentLineNumber)
            out[-1].native.append(nat)
            state["native"].append(nat)
        elif name in ("sql", "sqlCheck") and out:
            if "rollback" in stack:
                kind = "rollback"
            elif "preConditions" in stack:
                kind = "precondition"
            elif name == "sql":
                kind = "sql"
            else:
                return
            if name == "sqlCheck" and kind != "precondition":
                return
            state["seg"] = Segment(kind, "", line_after_tag(parser.CurrentByteIndex))

    def data(text: str) -> None:
        if state["seg"] is not None:
            state["seg"].text += text
        elif state["native"]:
            state["native"][-1].text += text

    def end(name: str) -> None:
        stack.pop()
        if name not in SCANNED_ELEMENTS and state["native"] and "changeSet" in stack:
            state["native"].pop()
        if name in ("sql", "sqlCheck") and state["seg"] is not None:
            out[-1].segments.append(state["seg"])
            state["seg"] = None

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = data
    parser.Parse(raw, True)
    return out


def unscannable(roots: Roots, constraints: dict[str, str] | None = None) -> list[str]:
    """Changesets the SQL scanner cannot read, one line each. Empty means every changeset's
    content is ``sql`` / ``sqlCheck`` / rollback / precondition SQL the generator parses.

    * ``sqlFile`` and ``customChange`` are listed WHATEVER they name: the statements live in
      another file or in Java, so the scanner cannot know what they touch.
    * Any other change element (``addColumn``, ``createIndex``, ``addForeignKeyConstraint``,
      ``insert``, ``update``, ``tableExists`` ...) is listed when an attribute or its text
      names one of the five tables or a tracked FK constraint, because its table is not SQL
      the scanner reads. One that names none of them is not a reference.

    A non-empty list is a failure of the guard, never a row: teach the scanner the element,
    or write the change as ``<sql>``."""
    cons = tuple(constraints or STATIC_CONSTRAINTS)
    cons_re = re.compile(r"(?<![\w$])(?:" + "|".join(map(re.escape, cons)) + r")(?![\w$])") if cons else None
    out: list[str] = []
    for fname in master_order(roots.changelog_dir):
        path = roots.changelog_dir / fname
        if not path.exists():
            continue
        for cs in parse_changelog(path):
            loc = f"changelog:{cs.file}#{cs.id}"
            for nat in cs.native:
                if nat.element in UNREADABLE_ELEMENTS:
                    out.append(f"{loc}: <{nat.element}> (line {nat.line}) runs SQL the scanner cannot read")
                    continue
                values = [*nat.attrs.values(), nat.text]
                tables = sorted({_t(m) for v in values for m in NAMED.finditer(v)})
                names = sorted({m.group(0) for v in values for m in (cons_re.finditer(v) if cons_re else [])})
                if tables or names:
                    out.append(f"{loc}: <{nat.element}> (line {nat.line}) names {', '.join(tables + names)} "
                               f"outside <sql>; the scanner cannot read it")
    return sorted(out)


def line_fn(text: str, line0: int) -> Callable[[int], int]:
    starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
    return lambda off: line0 + bisect.bisect_right(starts, off) - 1


# ---------------------------------------------------------------------------
# Statement classification
# ---------------------------------------------------------------------------

_FN = re.compile(r"^CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\s+(?P<n>[\w.\"]+)\s*\(", re.I)
_VIEW = re.compile(r"^CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+|MATERIALIZED\s+|RECURSIVE\s+)*VIEW\s+(?P<n>[\w.\"]+)", re.I)
_TRG = re.compile(r"^CREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?TRIGGER\s+(?P<n>\w+)\s+(?P<ev>.*?)\s+ON\s+(?P<t>[\w.\"]+)", re.I | re.S)
_TRG_FN = re.compile(r"EXECUTE\s+(?:FUNCTION|PROCEDURE)\s+(?P<f>[\w.\"]+)", re.I)
_IDX = re.compile(r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(?P<n>[\w.\"]+)?\s*ON\s+(?:ONLY\s+)?(?P<t>[\w.\"]+)(?:\s+USING\s+(?P<m>\w+))?", re.I)
_POL = re.compile(r"^(?P<v>CREATE|ALTER|DROP)\s+POLICY\s+(?:IF\s+EXISTS\s+)?(?P<n>\"[^\"]+\"|\w+)\s+ON\s+(?P<t>[\w.\"]+)", re.I)
_CT = re.compile(r"^CREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<n>[\w.\"]+)", re.I)
_AT = re.compile(r"^ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(?P<n>[\w.\"]+)\s+(?P<rest>.*)$", re.I | re.S)
_DROP = re.compile(r"^DROP\s+(?P<k>MATERIALIZED\s+VIEW|FUNCTION|PROCEDURE|VIEW|TRIGGER|POLICY|INDEX|TABLE|CONSTRAINT)\s+(?:IF\s+EXISTS\s+)?(?:CONCURRENTLY\s+)?(?P<rest>.*)$", re.I | re.S)
_GR = re.compile(r"^(?:GRANT|REVOKE)\b", re.I)
_CM = re.compile(r"^COMMENT\s+ON\b", re.I)
_SC = re.compile(r"^SET\s+CONSTRAINTS\b", re.I)
_DO = re.compile(r"^DO\b", re.I)
_ALTER_INDEX_RENAME = re.compile(
    r"^ALTER\s+INDEX\s+(?:IF\s+EXISTS\s+)?(?P<n>[\w.\"]+)\s+RENAME\s+TO\s+(?P<new>[\w.\"]+)\s*$", re.I)
_RENAME_TABLE = re.compile(r"^RENAME\s+TO\s+(?P<new>[\w.\"]+)\s*$", re.I)
_RENAME_CONSTRAINT = re.compile(r"^RENAME\s+CONSTRAINT\s+(?P<n>[\w\"]+)\s+TO\s+(?P<new>[\w\"]+)\s*$", re.I)


def bare(name: str) -> str:
    """Identifier without quotes. The ``nexus.`` schema is dropped; any other
    schema is kept, so ``staging.topic_assignments`` never collides with
    ``topic_assignments`` when liveness is tracked."""
    n = name.replace('"', "").lower()
    return n[len("nexus."):] if n.startswith("nexus.") else n


def split_actions(rest: str) -> list[str]:
    return split_top(rest, ",")


@dataclass
class Stmt:
    """One top-level statement that names a table, or an event that changes
    liveness of a named object."""

    kind: str
    name: str
    host: str  # table the object hangs off ("" for functions / views)
    text: str  # normalised statement text
    sites: list[Site]
    tables: list[str]
    line: int
    extra: dict[str, Any] = field(default_factory=dict)


def fn_argc(stmt: str, m: re.Match[str]) -> int:
    i = m.end() - 1
    j = balanced(stmt, i)
    inner = stmt[i + 1:j - 1].strip()
    params = [p for p in split_top(inner) if not re.match(r"OUT\b", p, re.I)] if inner else []
    return len(params)


def _tables_in(text: str) -> list[str]:
    return sorted({_t(m) for m in NAMED.finditer(text)})


def classify(stmt: str, line_of: Callable[[int], int], base: int, cons: dict[str, str]) -> list[Stmt]:
    """Rows for one top-level statement. A statement that does not name a table
    still yields an event-only ``Stmt`` when it defines or drops a liveness-tracked
    object; ``tables == []`` marks those."""
    s = stmt.strip()
    lead = len(stmt) - len(stmt.lstrip())
    line = line_of(base + lead)
    body_sites = lambda text, off: dedupe_sites(  # noqa: E731
        extract_sites(text, lambda o: line_of(base + off + o), cons))

    m = _FN.match(s)
    if m:
        argc = fn_argc(s, m)
        tabs = _tables_in(s)
        sig = norm(s[:balanced(s, m.end() - 1)])
        return [Stmt("function", bare(m.group("n")), "", sig, body_sites(s, lead) if tabs else [], tabs, line,
                     {"argc": argc})]
    m = _VIEW.match(s)
    if m:
        tabs = _tables_in(s)
        return [Stmt("view", bare(m.group("n")), "", norm(s)[:300], body_sites(s, lead) if tabs else [], tabs, line)]
    m = _TRG.match(s)
    if m:
        tabs = _tables_in(s)
        mf = _TRG_FN.search(s)
        return [Stmt("trigger", m.group("n").lower(), bare(m.group("t")), norm(s)[:300], [], tabs, line,
                     {"function": bare(mf.group("f")) if mf else None, "event": norm(m.group("ev"))[:120]})]
    m = _IDX.match(s)
    if m:
        tabs = _tables_in(s)
        return [Stmt("index", bare(m.group("n") or "(unnamed)"), bare(m.group("t")), norm(s)[:300], [], tabs, line,
                     {"method": (m.group("m") or "btree").lower()})]
    m = _POL.match(s)
    if m:
        tabs = _tables_in(s)
        verb = m.group("v").upper()
        return [Stmt("policy" if verb != "DROP" else "drop", m.group("n").strip('"').lower(), bare(m.group("t")),
                     norm(s)[:300], [], tabs, line, {"drop_of": "policy"} if verb == "DROP" else {})]
    m = _CT.match(s)
    if m:
        tabs = _tables_in(s)
        host = bare(m.group("n"))
        out_t: list[Stmt] = [Stmt("table", host, host, norm(s)[:300], [], tabs, line)]
        po = s.find("(", m.end())
        if po >= 0:
            body = s[po + 1:balanced(s, po) - 1]
            for el in split_top(body):
                e = norm(el)
                mk = re.match(r"CONSTRAINT\s+(?P<n>\w+)\s+(?P<k>FOREIGN\s+KEY|CHECK|PRIMARY\s+KEY|UNIQUE|EXCLUDE)", e, re.I)
                if mk:
                    name_c, ctype = mk.group("n").lower(), re.sub(r"\s+", " ", mk.group("k").upper())
                elif re.match(r"PRIMARY\s+KEY\s*\(", e, re.I):
                    name_c, ctype = f"{host}_pkey(inline)", "PRIMARY KEY"
                elif re.match(r"UNIQUE\s*\(", e, re.I):
                    name_c, ctype = f"{host}_key(inline)", "UNIQUE"
                elif re.match(r"FOREIGN\s+KEY\b", e, re.I):
                    name_c, ctype = f"{host}_fkey(inline)", "FOREIGN KEY"
                elif re.search(r"\bREFERENCES\b", e, re.I):
                    name_c, ctype = f"{e.split(' ')[0].lower()}_fkey(inline)", "FOREIGN KEY"
                elif re.search(r"\bPRIMARY\s+KEY\b", e, re.I):
                    name_c, ctype = f"{host}_pkey(inline)", "PRIMARY KEY"
                else:
                    continue
                etabs = sorted(({host} & set(TABLES)) | set(_tables_in(e)))
                kind_c = "fk" if ctype == "FOREIGN KEY" else "constraint"
                eline = line_of(base + po + 1 + max(body.find(el), 0))
                esites = dedupe_sites(extract_sites(e, lambda o, b=base + po + 1 + max(body.find(el), 0): line_of(b + o), cons)) if kind_c == "fk" else []
                out_t.append(Stmt(kind_c, name_c, host, f"CREATE TABLE {host} ... {e}"[:400], esites, etabs, eline,
                                  {"op": "add", "ctype": ctype}))
        return out_t
    m = _AT.match(s)
    if m:
        host = bare(m.group("n"))
        rest = m.group("rest")
        off0 = lead + m.start("rest")
        out: list[Stmt] = []
        for act in split_actions(rest):
            a = norm(act)
            tabs = sorted({host} & set(TABLES) | set(_tables_in(act)))
            aline = line
            mk = re.match(r"ADD\s+CONSTRAINT\s+(?P<n>\w+)\s+(?P<k>FOREIGN\s+KEY|CHECK|PRIMARY\s+KEY|UNIQUE|EXCLUDE)", a, re.I)
            if mk:
                kind = "fk" if mk.group("k").upper().startswith("FOREIGN") else "constraint"
                out.append(Stmt(kind, mk.group("n").lower(), host, f"ALTER TABLE {host} {a}"[:400],
                                dedupe_sites(extract_sites(act, lambda o, b=base + off0: line_of(b + o), cons)) if kind == "fk" else [],
                                tabs, aline, {"op": "add", "ctype": mk.group("k").upper()}))
                continue
            mk = _RENAME_TABLE.match(a)
            if mk:
                # ALTER TABLE x RENAME TO y: x is gone (its indexes, constraints and triggers go
                # with it, as the retired table), y is a definition. RDR steps 7.2 / 7.4 / 7.5.
                new = bare(mk.group("new"))
                out.append(Stmt("rename-table", f"{host}->{new}", host, f"ALTER TABLE {host} {a}"[:300], [],
                                sorted({host, new} & set(TABLES)), aline, {"op": "rename", "renamed_to": new}))
                continue
            mk = _RENAME_CONSTRAINT.match(a)
            if mk:
                # RENAME CONSTRAINT a TO b: a is dropped, b is defined on the same table
                old, new = mk.group("n").strip('"').lower(), mk.group("new").strip('"').lower()
                out.append(Stmt("constraint", old, host, f"ALTER TABLE {host} {a}"[:300], [], tabs, aline,
                                {"op": "rename", "renamed_to": new}))
                out.append(Stmt("constraint", new, host, f"ALTER TABLE {host} {a}"[:300], [], tabs, aline,
                                {"op": "rename-to", "renamed_from": old}))
                continue
            mk = re.match(r"(?P<op>DROP|VALIDATE|RENAME)\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?(?P<n>\w+)", a, re.I)
            if mk:
                out.append(Stmt("constraint", mk.group("n").lower(), host, f"ALTER TABLE {host} {a}"[:300], [], tabs, aline,
                                {"op": mk.group("op").lower()}))
                continue
            mk = re.match(r"(?P<op>ENABLE|FORCE|DISABLE|NO\s+FORCE)\s+ROW\s+LEVEL\s+SECURITY", a, re.I)
            if mk:
                out.append(Stmt("rls", norm(mk.group("op")).lower(), host, f"ALTER TABLE {host} {a}", [], tabs, aline))
                continue
            mk = re.match(r"(?P<op>ADD|DROP|ALTER|RENAME)\s+(?:COLUMN\s+)?(?:IF\s+(?:NOT\s+)?EXISTS\s+)?(?P<n>\w+)", a, re.I)
            if mk and not re.match(r"(ADD|DROP|ALTER)\s+(CONSTRAINT|TRIGGER)", a, re.I):
                out.append(Stmt("column", mk.group("n").lower(), host, f"ALTER TABLE {host} {a}"[:300], [], tabs, aline,
                                {"op": mk.group("op").lower()}))
                continue
            out.append(Stmt("alter-other", a.split(" ")[0].lower(), host, f"ALTER TABLE {host} {a}"[:300], [], tabs, aline))
        return out
    m = _ALTER_INDEX_RENAME.match(s)
    if m:
        # the old index name is gone from here on; the statement names none of the five tables,
        # so it is an event for liveness, never a row
        return [Stmt("drop", bare(m.group("n")), "", norm(s)[:200], [], [], line,
                     {"drop_of": "index", "renamed_to": bare(m.group("new"))})]
    m = _DROP.match(s)
    if m:
        k = m.group("k").upper()
        rest = norm(m.group("rest"))
        tabs = _tables_in(s)
        if k in ("FUNCTION", "PROCEDURE"):
            out_d: list[Stmt] = []
            for part in split_top(re.sub(r"\s+(?:CASCADE|RESTRICT)\s*$", "", rest, flags=re.I)):
                mf = re.match(r"([\w.\"]+)\s*(\(.*\))?\s*$", part.strip(), re.S)
                if not mf:
                    continue
                extra: dict[str, Any] = {"drop_of": "function"}
                if mf.group(2) is not None:
                    inner = mf.group(2)[1:-1].strip()
                    extra["argc"] = len([x for x in split_top(inner) if not re.match(r"OUT\b", x, re.I)]) if inner else 0
                out_d.append(Stmt("drop", bare(mf.group(1)), "", norm(s)[:200], [], tabs, line, extra))
            return out_d
        if k in ("VIEW", "MATERIALIZED VIEW", "TABLE", "INDEX"):
            names = [bare(re.split(r"[\s(]", p.strip())[0]) for p in split_top(re.sub(r"\(.*?\)", "", rest))] or [""]
            return [Stmt("drop", n, "", norm(s)[:200], [], tabs, line,
                         {"drop_of": k.lower().replace("materialized view", "view")})
                    for n in names]
        if k == "TRIGGER":
            mt = re.match(r"(\w+)\s+ON\s+([\w.\"]+)", rest, re.I)
            return [Stmt("drop", mt.group(1).lower() if mt else rest, bare(mt.group(2)) if mt else "", norm(s)[:200],
                         [], tabs, line, {"drop_of": "trigger"})]
        return [Stmt("drop", rest, "", norm(s)[:200], [], tabs, line, {"drop_of": k.lower()})]
    if _SC.match(s):
        sites = body_sites(s, lead)
        return [Stmt("set-constraints", "SET CONSTRAINTS", "", norm(s)[:200], sites, sorted({x["table"] for x in sites}), line)] if sites else []
    if _GR.match(s):
        tabs = _tables_in(s)
        if tabs:
            return [Stmt("grant", norm(s)[:60], "", norm(s)[:300], [], tabs, line)]
        if _ALL_TABLES_GRANT.search(s):
            return [Stmt("grant-schema-wide", norm(s)[:60], "", norm(s)[:300], [], [], line)]
        return []
    if _CM.match(s):
        tabs = _tables_in(s)
        return [Stmt("comment", norm(s)[:60], "", norm(s)[:200], [], tabs, line)] if tabs else []
    tabs = _tables_in(s)
    bulk = _DO.match(s) and re.search(r"\bGRANT\b", s, re.I) and (
        _ALL_TABLES_GRANT.search(s) or (re.search(r"\bpg_class\b", s) and re.search(r"nspname\s+IN\s*\(\s*'nexus'", s)))
    if bulk:
        extra_b = [Stmt("grant-schema-wide", "bulk GRANT over every nexus relation", "", norm(s)[:300], [], [], line)]
        if not tabs:
            return extra_b
    else:
        extra_b = []
    if not tabs:
        return []
    if _DO.match(s):
        made = sorted({f"{k.lower()}:{bare(n)}" for k, n in re.findall(
            r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:UNIQUE\s+)?(VIEW|POLICY|INDEX|TRIGGER|FUNCTION|TABLE)\s+"
            r"(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)", s, re.I)})
        return extra_b + [Stmt("do-block", "DO", "", norm(s)[:200], body_sites(s, lead), tabs, line,
                               {"defines": made} if made else {})]
    return [Stmt("dml", "statement", "", norm(s)[:200], body_sites(s, lead), tabs, line)]


# ---------------------------------------------------------------------------
# Changelog pass: events + rows
# ---------------------------------------------------------------------------



def _key(st: Stmt) -> tuple[str, str, str] | None:
    if st.kind == "function":
        return ("function", st.name, str(st.extra.get("argc", "")))
    if st.kind == "view":
        return ("view", st.name, "")
    if st.kind in ("trigger", "policy"):
        return (st.kind, st.name, st.host)
    if st.kind == "index":
        return ("index", st.name, "")
    if st.kind in ("fk", "constraint") and st.extra.get("op") in ("add", "rename-to"):
        return ("constraint", st.name, st.host)
    if st.kind == "rename-table":
        return ("table", st.extra["renamed_to"], "")
    if st.kind == "table":
        return ("table", st.name, "")
    if st.kind == "rls" and st.name in ("enable", "force"):
        return (f"rls-{st.name}", st.host, "")
    return None


def _drop_keys(st: Stmt) -> list[tuple[str, str, str]]:
    if st.kind == "drop":
        of = st.extra.get("drop_of", "")
        if of == "function":
            return [("function", st.name, str(st.extra.get("argc", "*")))]
        if of in ("view", "index", "table"):
            return [(of, st.name, "")]
        if of in ("trigger", "policy"):
            return [(of, st.name, st.host)]
    if st.kind == "constraint" and st.extra.get("op") in ("drop", "rename"):
        return [("constraint", st.name, st.host)]
    if st.kind == "rename-table":
        return [("table", st.host, "")]
    if st.kind == "rls" and st.name == "no force":
        return [("rls-force", st.host, "")]
    if st.kind == "rls" and st.name == "disable":
        return [("rls-enable", st.host, "")]
    return []


@dataclass
class Event:
    seq: int
    key: tuple[str, str, str]
    op: str  # def | drop
    loc: str
    host: str = ""


def changelog_pass(roots: Roots) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, int]]:
    order = master_order(roots.changelog_dir)
    cons = dict(STATIC_CONSTRAINTS)
    parsed: list[tuple[Changeset, list[tuple[Segment, list[Stmt]]]]] = []
    # pass 1: discover FK names touching the five tables so SET CONSTRAINTS is data-driven
    for fname in order:
        path = roots.changelog_dir / fname
        if not path.exists():
            continue
        for cs in parse_changelog(path):
            segs: list[tuple[Segment, list[Stmt]]] = []
            for seg in cs.segments:
                clean = sql_code(seg.text)
                lf = line_fn(seg.text, seg.line0)
                stmts: list[Stmt] = []
                for off, st in split_statements(clean):
                    stmts.extend(classify(st, lf, off, cons))
                segs.append((seg, stmts))
            parsed.append((cs, segs))
    for cs, segs in parsed:
        for _, stmts in segs:
            for st in stmts:
                if st.kind == "fk" and st.tables:
                    cons.setdefault(st.name, st.tables[0])
    # pass 2: re-classify with the discovered constraint names
    parsed2: list[tuple[Changeset, list[tuple[Segment, list[Stmt]]]]] = []
    for cs, segs in parsed:
        segs2: list[tuple[Segment, list[Stmt]]] = []
        for seg, _ in segs:
            clean = sql_code(seg.text)
            lf = line_fn(seg.text, seg.line0)
            stmts = []
            for off, st in split_statements(clean):
                stmts.extend(classify(st, lf, off, cons))
            segs2.append((seg, stmts))
        parsed2.append((cs, segs2))

    # events, in order, over ALL statements (liveness)
    events: list[Event] = []
    seq = 0
    for cs, segs in parsed2:
        for seg, stmts in segs:
            if seg.kind != "sql":
                continue
            for st in stmts:
                k = _key(st)
                loc = f"{cs.file}#{cs.id}"
                if k:
                    seq += 1
                    events.append(Event(seq, k, "def", loc, st.host))
                    st.extra["_seq"] = seq
                for dk in _drop_keys(st):
                    seq += 1
                    events.append(Event(seq, dk, "drop", loc, st.host))
    last: dict[tuple[str, str, str], Event] = {}
    wildcard_drop: dict[str, Event] = {}  # DROP FUNCTION name (no argument list) kills every overload
    table_drops: dict[str, list[Event]] = {}
    for ev in events:
        last[ev.key] = ev
        if ev.op == "drop" and ev.key[0] == "function" and ev.key[2] == "*":
            wildcard_drop[ev.key[1]] = ev
        if ev.op == "drop" and ev.key[0] == "table":
            table_drops.setdefault(ev.key[1], []).append(ev)

    def liveness(st: Stmt) -> tuple[bool | None, str | None]:
        k = _key(st)
        if k is None or "_seq" not in st.extra:
            return None, None
        ev = last[k]
        wd = wildcard_drop.get(k[1]) if k[0] == "function" else None
        if wd is not None and wd.seq > ev.seq:
            ev = wd
        mine = st.extra["_seq"]
        host_gone = any(d.seq > mine for d in table_drops.get(st.host, [])) if st.host and k[0] != "table" else False
        if ev.op == "drop" or host_gone:
            gone = ev if ev.op == "drop" else next(d for d in table_drops[st.host] if d.seq > mine)
            return False, f"DROPPED in {gone.loc}"
        return (ev.seq == mine), ev.loc

    rows: list[dict[str, Any]] = []
    order_idx = {n: i for i, n in enumerate(order)}
    for cs, segs in parsed2:
        run_always = cs.run_always
        loc = f"{cs.file}#{cs.id}"
        dml_sites: list[Site] = []
        dml_tables: set[str] = set()
        dml_line = 0
        dml_n = 0
        rb_sites: list[Site] = []
        rb_tables: set[str] = set()
        rb_line = 0
        pc_sites: list[Site] = []
        pc_tables: set[str] = set()
        pc_line = 0
        ordinal = Counter()
        for seg, stmts in segs:
            for st in stmts:
                if seg.kind == "rollback":
                    if st.tables:
                        rb_sites += st.sites or [{"verb": st.kind.upper(), "table": t, "line": st.line} for t in st.tables]
                        rb_tables |= set(st.tables)
                        rb_line = rb_line or st.line
                    continue
                if seg.kind == "precondition":
                    if st.tables:
                        pc_sites += st.sites
                        pc_tables |= set(st.tables)
                        pc_line = pc_line or st.line
                    continue
                if st.kind == "dml":
                    dml_sites += st.sites
                    dml_tables |= set(st.tables)
                    dml_line = dml_line or st.line
                    dml_n += 1
                    continue
                if st.kind == "grant-schema-wide":
                    rows.append(_mk_row(order_idx, cs, st, "grant-schema-wide", ordinal, run_always, None, None, implicit=True))
                    continue
                if not st.tables:
                    continue
                live, live_in = liveness(st)
                rows.append(_mk_row(order_idx, cs, st, st.kind, ordinal, run_always, live, live_in))
        if dml_n:
            rows.append({
                "id": f"changelog:{loc}:dml", "kind": "dml", "name": f"{dml_n} statement(s)", "source": "changelog",
                "file": f"{CHANGELOG_REL}/{cs.file}", "changeset": cs.id, "line": dml_line, "run_always": run_always,
                "live": None, "live_in": None, "tables": sorted(dml_tables), "sites": dedupe_sites(dml_sites), "current": "",
                "_o": (0, order_idx.get(cs.file, 10**6), cs.line, 9, 0),
            })
        if rb_sites or rb_tables:
            rows.append({
                "id": f"changelog:{loc}:rollback", "kind": "rollback", "name": "rollback", "source": "changelog",
                "file": f"{CHANGELOG_REL}/{cs.file}", "changeset": cs.id, "line": rb_line, "run_always": run_always,
                "live": None, "live_in": None, "tables": sorted(rb_tables), "sites": dedupe_sites(rb_sites), "current": "",
                "_o": (0, order_idx.get(cs.file, 10**6), cs.line, 10, 0),
            })
        if pc_tables:
            rows.append({
                "id": f"changelog:{loc}:precondition", "kind": "precondition", "name": "sqlCheck", "source": "changelog",
                "file": f"{CHANGELOG_REL}/{cs.file}", "changeset": cs.id, "line": pc_line, "run_always": run_always,
                "live": None, "live_in": None, "tables": sorted(pc_tables), "sites": dedupe_sites(pc_sites), "current": "",
                "_o": (0, order_idx.get(cs.file, 10**6), cs.line, 11, 0),
            })
    # per-function history: earlier defs listed on the live row
    history: dict[str, list[str]] = {}
    for ev in events:
        if ev.key[0] == "function" and ev.op == "def":
            history.setdefault(ev.key[1], []).append(ev.loc)
    for r in rows:
        if r["kind"] == "function":
            r["definitions"] = history.get(r["name"], [])
    routine_names = sorted({r["name"] for r in rows if r["kind"] == "function" and r.get("live")})
    return rows, cons, {"changesets": len(parsed2), "events": len(events), "routines": len(routine_names)}


def _mk_row(order_idx: dict[str, int], cs: Changeset, st: Stmt, kind: str, ordinal: Counter,
            run_always: bool, live: bool | None, live_in: str | None, implicit: bool = False) -> dict[str, Any]:
    loc = f"{cs.file}#{cs.id}"
    ordinal[(kind, st.name)] += 1
    n = ordinal[(kind, st.name)]
    suffix = f"{kind}:{st.name}" + (f"@{n}" if n > 1 else "")
    row: dict[str, Any] = {
        "id": f"changelog:{loc}:{suffix}", "kind": kind, "name": st.name, "source": "changelog",
        "file": f"{CHANGELOG_REL}/{cs.file}", "changeset": cs.id, "line": st.line, "run_always": run_always,
        "live": live, "live_in": live_in, "tables": list(st.tables), "sites": st.sites, "current": st.text,
        "_o": (0, order_idx.get(cs.file, 10**6), cs.line, 5, st.line),
    }
    if st.host:
        row["host"] = st.host
    for k, v in st.extra.items():
        if not k.startswith("_") and k not in ("op",) and v is not None:
            row[k] = v
    if "op" in st.extra:
        row["op"] = st.extra["op"]
    if implicit:
        row["implicit"] = True
    return row


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------

JAVA_CONSTS = {
    "CHUNKS_TABLE_NAME": "nexus.chunks",
    "CENTROIDS_TABLE_NAME": "nexus.taxonomy_centroids",
}
JOOQ_TABLE_CONSTS = {
    "CHUNKS": "chunks",
    "TAXONOMY_CENTROIDS": "taxonomy_centroids",
    "CATALOG_DOCUMENT_CHUNKS": "catalog_document_chunks",
    "TOPIC_ASSIGNMENTS": "topic_assignments",
    "CHUNK_ORPHANED_AT": "chunk_orphaned_at",
    "CENTROIDS": "taxonomy_centroids",  # DimTables.CENTROIDS accessor
}
#: The jOOQ record classes generated for the five tables (``ChunksRecord``,
#: ``CatalogDocumentChunksRecord``, ``TopicAssignmentsRecord``, ``TaxonomyCentroidsRecord``,
#: ``ChunkOrphanedAtRecord``): the typed record API names a table without ever spelling
#: ``CHUNKS`` or a SQL literal (``ctx.fetch...``, ``record.store()``, ``newRecord``).
JOOQ_RECORD_CLASSES: dict[str, str] = {"".join(w.capitalize() for w in t.split("_")) + "Record": t for t in TABLES}
_RECORD_RE = re.compile(r"(?<![\w$])(" + "|".join(sorted(JOOQ_RECORD_CLASSES, key=len, reverse=True)) + r")(?![\w$])")
_JT = "|".join(sorted(JOOQ_TABLE_CONSTS, key=len, reverse=True))

_JTOK = re.compile(
    r"//[^\n]*"
    r"|/\*.*?\*/"
    r'|"""(?:\\.|[^\\])*?"""'
    r'|"(?:\\.|[^"\\\n])*"'
    r"|'(?:\\.|[^'\\\n])+'"
    r"|[A-Za-z_$][\w$]*"
    r"|->|[{}();.=@,<>]"
    r"|\S",
    re.S,
)
_KEYWORDS = {"if", "for", "while", "switch", "catch", "try", "else", "synchronized", "return", "new", "do", "throw", "assert"}


@dataclass
class JTok:
    kind: str  # id | str | punct
    text: str
    pos: int
    end: int


def _unescape(s: str) -> str:
    s = re.sub(r"\\n|\\t|\\r", " ", s)
    return s.replace('\\"', '"').replace("\\\\", "\\")


def java_tokens(src: str) -> list[JTok]:
    toks: list[JTok] = []
    for m in _JTOK.finditer(src):
        t = m.group(0)
        if t.startswith("//") or t.startswith("/*"):
            continue
        if t.startswith('"""'):
            toks.append(JTok("str", _unescape(t[3:-3]), m.start(), m.end()))
        elif t.startswith('"'):
            toks.append(JTok("str", _unescape(t[1:-1]), m.start(), m.end()))
        elif t.startswith("'"):
            toks.append(JTok("char", t, m.start(), m.end()))
        elif re.match(r"[A-Za-z_$]", t):
            toks.append(JTok("id", t, m.start(), m.end()))
        else:
            toks.append(JTok("punct", t, m.start(), m.end()))
    return toks


def _scope_name(head: list[JTok]) -> str | None:
    texts = [t.text for t in head]
    if not texts:
        return None
    # strip annotations
    clean: list[JTok] = []
    i = 0
    while i < len(head):
        if head[i].text == "@" and i + 1 < len(head) and head[i + 1].kind == "id":
            i += 2
            if i < len(head) and head[i].text == "(":
                depth = 0
                while i < len(head):
                    if head[i].text == "(":
                        depth += 1
                    elif head[i].text == ")":
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                    i += 1
            continue
        clean.append(head[i])
        i += 1
    texts = [t.text for t in clean]
    if "new" in texts[:texts.index("(")] if "(" in texts else "new" in texts:
        return None
    for kw in ("class", "interface", "enum", "record"):
        if kw in texts:
            j = texts.index(kw)
            if j + 1 < len(clean) and clean[j + 1].kind == "id":
                return clean[j + 1].text
    if "(" not in texts:
        return None
    first_paren = texts.index("(")
    if first_paren == 0:
        return None
    if any(t in ("=", "->") for t in texts[:first_paren]):
        return None
    name = clean[first_paren - 1]
    if name.kind != "id" or name.text in _KEYWORDS:
        return None
    # head must close its parens (method declaration), allow `throws X`
    depth = 0
    for t in texts[first_paren:]:
        if t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
    if depth != 0:
        return None
    if "->" in texts:
        return None
    return name.text


def _strip_imports(toks: list[JTok]) -> tuple[list[JTok], set[str]]:
    """Remove ``package`` / ``import`` statements (they are not usage); return the
    simple names static-imported from the jOOQ ``Routines`` / ``Tables`` classes."""
    kept: list[JTok] = []
    static_names: set[str] = set()
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.kind == "id" and t.text in ("import", "package") and (i == 0 or toks[i - 1].text in (";", "}")):
            j = i
            while j < len(toks) and toks[j].text != ";":
                j += 1
            stmt = [x.text for x in toks[i:j]]
            if t.text == "import" and "static" in stmt and ("Routines" in stmt or "Tables" in stmt):
                static_names.add(stmt[-1])
            i = j + 1
            continue
        kept.append(t)
        i += 1
    return kept, static_names


def java_owners(src: str) -> tuple[list[tuple[str, list[JTok]]], set[str]]:
    """Tokens grouped by enclosing named scope (innermost wins). A method's own
    declaration (annotations, signature, parameters) belongs to the method, so a
    ``DimTables.ChunkTable ch`` parameter attributes its uses to it. Overloads and
    repeats are disambiguated with ``@n`` in source order. Returns ``(owners,
    static-imported jOOQ Routines/Tables names)``."""
    toks, static_names = _strip_imports(java_tokens(src))
    stack: list[tuple[str | None, int]] = []
    depth = 0
    head: list[JTok] = []
    owners: dict[str, list[JTok]] = {}
    seen_names: Counter[str] = Counter()
    scope_instances: list[str] = []  # instance label of each named scope on the stack
    for t in toks:
        if t.kind == "punct" and t.text == "{":
            nm = _scope_name(head)
            depth += 1
            stack.append((nm, depth))
            if nm:
                outer = scope_instances[-1] if scope_instances else "<top>"
                if head and owners.get(outer, [])[-len(head):] == head:
                    del owners[outer][-len(head):]
                base = "/".join([n for n, _ in stack if n])
                seen_names[base] += 1
                label = base if seen_names[base] == 1 else f"{base}@{seen_names[base]}"
                scope_instances.append(label)
                owners.setdefault(label, []).extend(head)
            head = []
            continue
        if t.kind == "punct" and t.text == "}":
            if stack and stack[-1][1] == depth:
                nm, _ = stack.pop()
                if nm:
                    scope_instances.pop()
            depth -= 1
            head = []
            continue
        owner = scope_instances[-1] if scope_instances else "<top>"
        owners.setdefault(owner, []).append(t)
        if t.kind == "punct" and t.text == ";":
            head = []
            continue
        head.append(t)
    return [(o, owners[o]) for o in sorted((k for k in owners if owners[k]), key=lambda x: (min(tok.pos for tok in owners[x]), x))], static_names


def _camel(n: str) -> str:
    return re.sub(r"_([a-z0-9])", lambda m: m.group(1).upper(), n)


def _snake(n: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", lambda m: "_" + m.group(1).lower(), n).lower()


def routine_forms(name: str) -> set[str]:
    c = _camel(name)
    return {name, name.upper(), c, c[0].upper() + c[1:]}


_DSL_WRITE = {"insertInto": "INSERT", "batchInsert": "INSERT", "deleteFrom": "DELETE", "update": "UPDATE",
              "mergeInto": "MERGE", "truncateTable": "TRUNCATE", "truncate": "TRUNCATE"}
_DSL_READ = ("selectFrom", "from", "join", "leftJoin", "rightJoin", "innerJoin", "crossJoin", "fullJoin", "using")
_DSL_CALL = re.compile(
    r"(?<![\w$])(" + "|".join(sorted(list(_DSL_WRITE) + list(_DSL_READ), key=len, reverse=True)) + r")\s*\("
)
_DSL_WRITE_CALL = re.compile(r"(?<![\w$])(" + "|".join(sorted(_DSL_WRITE, key=len, reverse=True)) + r")\s*\(")


def _field(expr: str) -> str:
    """Column name of a jOOQ field expression: ``CHUNKS.TENANT_ID`` and
    ``ch.tenantId()`` both become ``tenant_id``, so a typed site compares with a raw
    one."""
    e = norm(expr)
    m = re.fullmatch(r"(?:\w+\.)*([A-Z][A-Z0-9_]*)", e)
    if m:
        return m.group(1).lower()
    m = re.fullmatch(r"\w+\.(\w+)\(\)", e)
    if m:
        return _snake(m.group(1))
    return e


def _table_of(expr: str, accessors: dict[str, str]) -> str | None:
    e = norm(expr)
    m = re.fullmatch(rf"(?:\w+\.)?({_JT})", e)
    if m and m.group(1) not in ("CENTROIDS",):
        return JOOQ_TABLE_CONSTS[m.group(1)]
    m = re.fullmatch(r"(\w+)\.table\(\)", e)
    if m and m.group(1) in accessors:
        return accessors[m.group(1)]
    m = re.fullmatch(r"(?:\w+\.)?(CHUNKS|CENTROIDS)\.get\(.*\)\.table\(\)", e)
    if m:
        return "chunks" if m.group(1) == "CHUNKS" else "taxonomy_centroids"
    return None


def _jooq_sites(code: str, line_of: Callable[[int], int], accessors: dict[str, str]) -> list[Site]:
    """Typed jOOQ sites: a DSL call (``insertInto``, ``deleteFrom``, ``update``,
    ``from`` ...) whose first argument is one of the five tables, or an accessor-held
    table (``DimTables.CHUNKS.get(dim)`` bound to a local)."""
    sites: list[Site] = []
    calls = list(_DSL_CALL.finditer(code))
    write_starts = [m.start() for m in _DSL_WRITE_CALL.finditer(code)]
    for m in calls:
        j = balanced(code, m.end() - 1)
        args = split_top(code[m.end():j - 1])
        if not args:
            continue
        table = _table_of(args[0], accessors)
        if table is None:
            continue
        fn = m.group(1)
        verb = _DSL_WRITE.get(fn, "READ")
        site: Site = {"verb": verb, "table": table, "line": line_of(m.start()), "detail": "jOOQ"}
        if verb in ("INSERT", "UPDATE"):
            nxt = min([p for p in write_starts if p > m.start()] + [m.start() + 6000, len(code)])
            window = code[j:nxt]
            if verb == "INSERT":
                cols = [_field(a) for a in args[1:]]
                if not cols:
                    mc = re.search(r"\.\s*columns\s*\(", window)
                    if mc:
                        k = balanced(window, mc.end() - 1)
                        cols = [_field(a) for a in split_top(window[mc.end():k - 1])]
                site["columns"] = ", ".join(cols) if cols else None
                confs: list[str] = []
                for mo in re.finditer(r"\.\s*onConflict\s*\(", window):
                    k = balanced(window, mo.end() - 1)
                    tgt = "(" + ", ".join(_field(a) for a in split_top(window[mo.end():k - 1])) + ")"
                    ma = re.match(r"\s*\.\s*(doNothing|doUpdate)\s*\(", window[k:])
                    act = {"doNothing": " DO NOTHING", "doUpdate": " DO UPDATE"}[ma.group(1)] if ma else ""
                    confs.append(tgt + act)
                site["conflict"] = " | ".join(confs) if confs else None
            else:
                sets = [_field(split_top(window[mo.end():balanced(window, mo.end() - 1) - 1])[0])
                        for mo in re.finditer(r"\.\s*set\s*\(", window)
                        if split_top(window[mo.end():balanced(window, mo.end() - 1) - 1])]
                site["columns"] = ", ".join(dict.fromkeys(sets)) if sets else None
        sites.append(site)
    return sites


def _table_name_helpers(src: str) -> set[str]:
    """Simple names of methods that return ``DimTables.CHUNKS_TABLE_NAME`` /
    ``CENTROIDS_TABLE_NAME`` (``PgVectorRepository#chunksTable``): a caller's SQL
    names the table through them."""
    owners, _ = java_owners(src)
    out: set[str] = set()
    for owner, toks in owners:
        if "/" not in owner:
            continue
        texts = [t.text for t in toks]
        if "return" in texts and any(t in JAVA_CONSTS for t in texts) and len(toks) < 40:
            out.add(owner.split("/")[-1].split("@")[0])
    return out


def java_rows(roots: Roots, routine_names: list[str], cons: dict[str, str]) -> list[dict[str, Any]]:
    routine_re = None
    forms: dict[str, str] = {}
    for n in routine_names:
        for f in routine_forms(n):
            forms.setdefault(f, n)
    if forms:
        routine_re = re.compile(r"(?<![\w$])(" + "|".join(sorted(map(re.escape, forms), key=len, reverse=True)) + r")(?![\w$])")
    routine_set = set(routine_names)
    family_prefixes: dict[str, list[str]] = {}
    for n in routine_names:
        m_dim = re.fullmatch(r"(.+_)(?:384|768|1024)", n)
        if m_dim:
            family_prefixes.setdefault(m_dim.group(1), []).append(n)
    rows: list[dict[str, Any]] = []
    for path in sorted(roots.java_dir.rglob("*.java")):
        src = path.read_text(encoding="utf-8")
        rel = path.relative_to(roots.java_dir).as_posix()
        starts = [0] + [i + 1 for i, c in enumerate(src) if c == "\n"]

        def line_at(off: int, _s: list[int] = starts) -> int:
            return bisect.bisect_right(_s, off)

        owners, static_names = java_owners(src)
        helpers = _table_name_helpers(src)
        for owner, toks in owners:
            pieces: list[tuple[str, int]] = []
            code_offs: list[tuple[int, int]] = []
            prev_end: int | None = None
            code_text = ""
            for i, t in enumerate(toks):
                if t.kind == "str":
                    pieces.append((t.text, t.pos))
                    seg = '""'
                elif t.kind == "char":
                    seg = "'x'"
                elif t.kind == "id" and t.text in JAVA_CONSTS:
                    pieces.append((JAVA_CONSTS[t.text], t.pos))
                    seg = t.text
                elif t.kind == "id" and t.text in helpers and i + 1 < len(toks) and toks[i + 1].text == "(":
                    pieces.append((JAVA_CONSTS["CHUNKS_TABLE_NAME"], t.pos))
                    seg = t.text
                else:
                    seg = t.text
                gap = " " if (prev_end is not None and t.pos > prev_end) else ""
                code_offs.append((len(code_text), t.pos))
                code_text += gap + seg
                prev_end = t.end
            sql_text = ""
            sql_offs: list[tuple[int, int]] = []
            for text, pos in pieces:
                sql_offs.append((len(sql_text), pos))
                sql_text += text + " "

            def mk_line(offs: list[tuple[int, int]]) -> Callable[[int], int]:
                idx = [o for o, _ in offs]

                def f(o: int) -> int:
                    if not offs:
                        return 1
                    return line_at(offs[max(0, bisect.bisect_right(idx, o) - 1)][1])

                return f

            sites = extract_sites(sql_text, mk_line(sql_offs), cons, bare_mentions=False)
            accessors: dict[str, str] = {}
            for m in re.finditer(r"\b(\w+)\s*=\s*(?:\w+\.)?(CHUNKS|CENTROIDS)\s*\.\s*get\s*\(", code_text):
                accessors[m.group(1)] = "chunks" if m.group(2) == "CHUNKS" else "taxonomy_centroids"
            for m in re.finditer(r"\b(ChunkTable|CentroidTable)\s+(\w+)\b", code_text):
                accessors.setdefault(m.group(2), "chunks" if m.group(1) == "ChunkTable" else "taxonomy_centroids")
            lf = mk_line(code_offs)
            sites += _jooq_sites(code_text, lf, accessors)
            # typed references that no verb above accounts for
            tref: dict[str, int] = {}
            for m in re.finditer(rf"(?<![\w$])(?:(?:Tables|DimTables)\s*\.\s*)?(?P<c>{_JT})(?![\w$])(?!\s*\.\s*get\b)", code_text):
                c = m.group("c")
                if c == "CENTROIDS":
                    continue
                tref.setdefault(JOOQ_TABLE_CONSTS[c], m.start())
            for m in re.finditer(r"(?<![\w$])(?:DimTables\s*\.\s*)?(CHUNKS_TABLE_NAME|CENTROIDS_TABLE_NAME)(?![\w$])", code_text):
                tref.setdefault("chunks" if m.group(1) == "CHUNKS_TABLE_NAME" else "taxonomy_centroids", m.start())
            for m in _RECORD_RE.finditer(code_text):
                tref.setdefault(JOOQ_RECORD_CLASSES[m.group(1)], m.start())
            for m in re.finditer(r"(?<![\w$])(?:\w+\.)?(CHUNKS|CENTROIDS)\s*\.\s*get\b", code_text):
                tref.setdefault("chunks" if m.group(1) == "CHUNKS" else "taxonomy_centroids", m.start())
            for var, table in accessors.items():
                mu = re.search(rf"(?<![\w$.]){re.escape(var)}\s*\.\s*\w+\s*\(", code_text)
                if mu:
                    tref.setdefault(table, mu.start())
            have = {s["table"] for s in sites}
            for t, pos in sorted(tref.items()):
                if t not in have:
                    sites.append({"verb": "TYPED-REF", "table": t, "line": lf(pos)})
            for tname, ctx_s, pos in java_name_literals(toks):
                sites.append({"verb": "NAME-LITERAL", "table": tname, "line": line_at(pos), "detail": ctx_s})
            calls: set[str] = set()
            if routine_re:
                for m in routine_re.finditer(code_text):
                    form = m.group(1)
                    before = code_text[max(0, m.start() - 10):m.start()]
                    if re.search(r"(?:Routines|Tables)\s*\.\s*$", before) or form in static_names or form.isupper():
                        calls.add(forms[form])
                for m in re.finditer(r"(?<![\w$])(?:nexus\.)?(\w+)\s*\(", sql_text):
                    if m.group(1) in routine_set:
                        calls.add(m.group(1))
                # a per-dimension family named by concatenation: "nexus.assign_from_chashes_" + dim
                for prefix, members in family_prefixes.items():
                    if re.search(rf"(?<![\w$])(?:nexus\.)?{re.escape(prefix)}(?![\w$])", sql_text):
                        calls.update(members)
            if not sites and not calls:
                continue
            for c in sorted(calls):
                sites.append({"verb": "CALLS", "table": "", "object": c, "line": 0})
            sites = dedupe_sites(sites)
            tables = sorted({s["table"] for s in sites if s["table"]})
            first = min([s["line"] for s in sites if s.get("line")] or [0])
            rows.append({
                "id": f"java:{rel}#{owner}", "kind": "java-method", "name": owner, "source": "java",
                "file": f"{JAVA_REL}/{rel}", "changeset": None, "line": first, "run_always": False,
                "live": None, "live_in": None, "tables": tables, "sites": sites, "current": "",
                "_o": (1, rel, min(t.pos for t in toks), 0, 0),
            })
    return rows


_EXACT_NAME = re.compile(rf"(?:nexus\.)?(?P<t>{_TBL_ALT})", re.I)
#: Callees whose string argument is a JSON / map key or an alias, not a table name
#: (``body.get("chunks")``, ``Map.of("chunks", rows)``, ``count().as("chunks")``).
_KEY_CALLEES = {"get", "put", "putIfAbsent", "getOrDefault", "containsKey", "as", "computeIfAbsent", "merge",
                "remove", "has", "optString", "path", "getString", "getJSONArray", "getValue", "entry"}


def java_name_literals(toks: list[JTok]) -> list[tuple[str, str, int]]:
    """String literals that are exactly a table name: ``.eq("chunks")`` against pg_policies,
    ``new CollectionScopedTable("chunks", ...)``, ``Set.of("catalog_document_chunks")``.
    Returns ``(table, "qualifier.callee(", pos)``. A JSON / map key or an alias in first
    position (``_KEY_CALLEES``, ``Map.of``) is not a table reference; a name in value
    position (``CHASH_LEN_CONSTRAINTS.put("..._check", "catalog_document_chunks")``) is."""
    out: list[tuple[str, str, int]] = []
    for i, t in enumerate(toks):
        if t.kind != "str":
            continue
        m = _EXACT_NAME.fullmatch(t.text.strip())
        if not m:
            continue
        depth = 0
        j = i - 1
        while j >= 0:
            x = toks[j].text
            if toks[j].kind == "punct" and x == ")":
                depth += 1
            elif toks[j].kind == "punct" and x == "(":
                if depth == 0:
                    break
                depth -= 1
            elif toks[j].kind == "punct" and x in (";", "{", "}"):
                j = -1
                break
            j -= 1
        if j <= 0:
            callee, qual = "", ""
        else:
            callee = toks[j - 1].text if toks[j - 1].kind == "id" else ""
            qual = toks[j - 3].text if j >= 3 and toks[j - 2].text == "." and toks[j - 3].kind == "id" else ""
            if j >= 2 and toks[j - 2].text == "new":
                qual = "new"
        first_arg = j >= 0 and i == j + 1
        if first_arg and (callee in _KEY_CALLEES or (callee == "of" and qual == "Map")):
            continue  # a JSON / map KEY; a table name in VALUE position (put("x_check", "chunks")) still counts
        if qual == "new":
            detail = f"new {callee}("
        elif callee:
            detail = f"{qual + '.' if qual else ''}{callee}("
        else:
            detail = "literal"
        out.append((m.group("t").lower(), detail, t.pos))
    return out


_PY_LEX = re.compile(r'#[^\n]*|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')


def _py_code(text: str) -> str:
    """Python source with ``#`` comments blanked (offsets kept) and strings left alone."""
    return _PY_LEX.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)) if m.group(0).startswith("#") else m.group(0), text)


def _entry_key(entry: str) -> str:
    """What names a registry entry: its first call argument (``ChashBearingTable("nexus.x", ...)``)
    or the entry itself (``"nexus.x"``), quotes removed."""
    mc = re.match(r"^[\w.]+\s*\((?P<a>.*)$", entry, re.S)
    first = split_top(mc.group("a").rstrip(")"))[0] if mc and split_top(mc.group("a").rstrip(")")) else entry
    return first.strip().strip("\"'")


def _registry_rows(roots: Roots, reg: Registry) -> list[dict[str, Any]]:
    path = roots.repo / reg.file
    if not path.exists():
        raise SystemExit(f"anchor file missing: {reg.file}")
    text = path.read_text(encoding="utf-8")
    code = _py_code(text)
    mstart = re.search(reg.start, code, re.M)
    if mstart is None:
        raise SystemExit(f"anchor not found: {reg.file} /{reg.start}/ ({reg.name})")
    eq = code.find("=", mstart.start())
    po = code.find("(", eq) if eq >= 0 else -1
    if po < 0:
        raise SystemExit(f"registry {reg.name} in {reg.file} has no parenthesised literal")
    body = code[po + 1:balanced(code, po) - 1]
    start_line = 1 + code.count("\n", 0, mstart.start())
    consts = dict(reg.consts)
    entries: list[tuple[str, int, list[str]]] = []
    for off, piece in split_top_pos(body):
        lead = len(piece) - len(piece.lstrip())
        entry = norm(piece)
        line = 1 + code.count("\n", 0, po + 1 + off + lead)
        named: set[str] = set()
        for lit in re.findall(r"\"([^\"]*)\"|\'([^\']*)\'", entry):
            for one in lit:
                mx = _EXACT_NAME.fullmatch(one.strip()) if one else None
                if mx:
                    named.add(mx.group("t").lower())
        for const, table in consts.items():
            if re.search(rf"(?<![\w.]){re.escape(const)}(?![\w])", entry):
                named.add(table)
        entries.append((entry, line, sorted(named)))
    rows: list[dict[str, Any]] = [{
        "id": f"python:{reg.file}#{reg.name}", "kind": "python-anchor", "name": reg.name, "source": "python",
        "file": reg.file, "changeset": None, "line": start_line, "run_always": False, "live": None, "live_in": None,
        "tables": sorted({t for _, _, ts in entries for t in ts}), "sites": [],
        "current": norm(code[mstart.start():po + 1]), "entries": [e for e, _, _ in entries],
        "_o": (2, reg.file, start_line, 0, 0), "rdr_hint": reg.rdr,
    }]
    seen: Counter[str] = Counter()
    for entry, line, tables in entries:
        if not tables:
            continue
        key = _entry_key(entry)
        seen[key] += 1
        suffix = f"{key}@{seen[key]}" if seen[key] > 1 else key
        rows.append({
            "id": f"python:{reg.file}#{reg.name}:{suffix}", "kind": "python-anchor", "name": f"{reg.name}:{suffix}",
            "source": "python", "file": reg.file, "changeset": None, "line": line, "run_always": False,
            "live": None, "live_in": None, "tables": tables, "sites": [], "current": entry[:200],
            "_o": (2, reg.file, line, 1, 0), "rdr_hint": reg.rdr,
        })
    return rows


def python_rows(roots: Roots) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for a in roots.anchors:
        if isinstance(a, Registry):
            rows += _registry_rows(roots, a)
            continue
        path = roots.repo / a.file
        if not path.exists():
            raise SystemExit(f"anchor file missing: {a.file}")
        lines = path.read_text(encoding="utf-8").splitlines()
        hit = next((i for i, line in enumerate(lines, 1) if re.search(a.pattern, line)), None)
        if hit is None:
            raise SystemExit(f"anchor not found: {a.file} /{a.pattern}/")
        rows.append({
            "id": f"python:{a.file}#{a.name}", "kind": "python-anchor", "name": a.name, "source": "python",
            "file": a.file, "changeset": None, "line": hit, "run_always": False, "live": None, "live_in": None,
            "tables": list(a.tables), "sites": [], "current": lines[hit - 1].strip(), "_o": (2, a.file, hit, 0, 0),
            "rdr_hint": a.rdr,
        })
    return rows


# ---------------------------------------------------------------------------
# RDR mapping
# ---------------------------------------------------------------------------
# The Technical Design table of docs/rdr/rdr-225-vector-tables-per-embedding-model.md,
# row by row, plus the prose sections that name code the table does not. ``new_form`` is
# the design; ``fallback_form`` is the "If step 3 is not PASS" layout (LIST-partitioned by
# tenant_id only, three-column PK kept).

#
# TD_ROWS is a HAND COPY of the RDR's Technical Design table as it stood at c2415c3c8 (the
# commit that accepted the RDR), not generated from the RDR text. When the RDR's Technical
# Design changes (nexus-3wh8d.24 rewrites the write-site list, the SET CONSTRAINTS wording and
# the file:line citations), update these rows to match and regenerate; ``compare`` reports a
# changed TD_ROWS as a difference, so a forgotten update shows up as a stale pin, never silently.

TD_ROWS: list[dict[str, str]] = [
    {"id": "T01", "title": "fk_catalog_chunks_chunk (catalog-029), FK into chunks, and every SET CONSTRAINTS site on it",
     "new_form": "4-column FK (tenant_id, collection, chash, embedding_model); ON UPDATE CASCADE, NO ACTION, DEFERRABLE INITIALLY IMMEDIATE; SAME name. Every SET CONSTRAINTS site is rewritten schema-qualified (nexus.fk_catalog_chunks_chunk): the bare name fails without the schema on search_path (step 2). Java CatalogRepository SET CONSTRAINTS, plpgsql sites in catalog-029, catalog-033, vectors-017.",
     "fallback_form": "Unchanged 3-column FK, re-added on the swapped table at step 7.6 with the same name; SET CONSTRAINTS sites still become schema-qualified."},
    {"id": "T02", "title": "topic_assignments_chunk_fk (taxonomy-012), FK into chunks",
     "new_form": "4-column; ON UPDATE CASCADE, ON DELETE CASCADE; same name. topic_assignments gains embedding_model NOT NULL.",
     "fallback_form": "Unchanged 3-column FK, re-added at step 7.6."},
    {"id": "T03", "title": "chunk_orphaned_at_chunk_fk (vectors-021), FK into chunks",
     "new_form": "4-column; ON UPDATE CASCADE, ON DELETE CASCADE; same name. chunk_orphaned_at gains embedding_model NOT NULL; its own PK arbiter stays three-column.",
     "fallback_form": "Unchanged 3-column FK, re-added at step 7.6."},
    {"id": "T04", "title": "chunks_collection_fk (fk-004), FK out of chunks",
     "new_form": "Replaced by the composite model FK (tenant_id, collection, embedding_model) -> catalog_collections UNIQUE (tenant_id, name, embedding_model), ON DELETE RESTRICT. Dropped on the old table at step 7.3, re-added validated directly on the partitioned table (NOT VALID is refused for a partitioned referencing table).",
     "fallback_form": "Step 7.3 still drops the old chunks_collection_fk and step 7.6 re-adds it on the new table; no composite model FK."},
    {"id": "T05", "title": "Write sites: ON CONFLICT targets on chunks, INSERT column lists, writers of the three referencing tables",
     "new_form": "Rewritten: the conflict target becomes the four-column PK (tenant_id, collection, chash, embedding_model) and the INSERT supplies embedding_model. Writers of catalog_document_chunks, topic_assignments and chunk_orphaned_at supply embedding_model. chunk_orphaned_at keeps its own three-column arbiter. Functions are redefined once, at migration step 7.8, against the swapped table (the LATEST live body, see live_in).",
     "fallback_form": "No change to conflict targets or column lists: the three-column PK already includes the partition key tenant_id."},
    {"id": "T06", "title": "stamp_chunks_on_manifest_delete / _update (vectors-021-3) and the manifest triggers",
     "new_form": "Rewritten to carry embedding_model from the manifest row and to join chunks on all four columns; redefined at step 7.8.",
     "fallback_form": "Unchanged; the triggers keep their current text."},
    {"id": "T07", "title": "Views over chunks: live_chunks, collection_vector_stats (vectors-019), diag_chash_conformance (taxonomy-011-8)",
     "new_form": "Dropped before the swap (step 7.1) and recreated over the new table in the same transaction (7.7); they bind by OID and would otherwise follow the retired table. diag_chash_conformance may be owned by a superuser, so its drop and recreate runs as that owner or is a documented operator step.",
     "fallback_form": "Same: dropped before the swap and recreated after it."},
    {"id": "T08", "title": "RLS: chunks_gate_probe_owner_read (vectors-029), tenant_isolation policy, FORCE RLS",
     "new_form": "On the parent chunks AND on every model partition and leaf (PostgreSQL inherits neither RLS state nor policies, and a leaf can be queried directly). FORCE restored on every table at step 7.7.",
     "fallback_form": "On the parent and on every tenant leaf; same reasoning."},
    {"id": "T09", "title": "Constraints and indexes on chunks: chunks_content_retention_consistent, chunks_chash_octet_check, idx_chunks_tenant_chash, HNSW / GIN / trigram",
     "new_form": "Declared on the parent so every leaf inherits them. On the retired table, indexes and index-backed constraints (PK, UNIQUE) are renamed with a _retired_225 suffix before the swap (those names are schema-wide). CHECK and FK names are per table and need no rename.",
     "fallback_form": "Declared on the parent so every tenant leaf inherits them; same retired-table renames."},
    {"id": "T10", "title": "Grants on nexus.chunks (grants-nexus-svc.xml, grants-nexus-diag.xml; runAlways)",
     "new_form": "Re-issued on the new parent (runAlways, so they re-run on every walk). New leaves receive the same grants as the parent, including MAINTAIN for the purge VACUUM.",
     "fallback_form": "Same."},
    {"id": "T11", "title": "Isolation checks: ChunksIsolationCheck.verifyAtStartup (Main.java:131), doctor RLS canary _RLS_TENANT_TABLES (health.py:3408)",
     "new_form": "Extended to assert FORCE RLS and the policy on every model partition and leaf.",
     "fallback_form": "Extended to assert FORCE RLS and the policy on every tenant leaf."},
    {"id": "T12", "title": "taxonomy_centroids (taxonomy-007) and its upsert (TaxonomyCentroidRepository.java:124)",
     "new_form": "Gains embedding_model; partitioned the same way (model, then tenant). The upsert's conflict target includes the model. A centroid whose dimension changes (nexus-2qryr) is deleted and re-inserted, not upserted (an upsert cannot move a row across partitions).",
     "fallback_form": "LIST-partitioned by tenant_id only; PK and upsert unchanged."},
    {"id": "P-DIM", "title": "Dimension CHECK: exactly_one_embedding on chunks and taxonomy_centroids (Technical Design, Model key, Dimension CHECK)",
     "new_form": "Each model partition carries a CHECK that its model's vector column is the only non-null one (for example embedding_1024 IS NOT NULL AND embedding_768 IS NULL AND embedding_384 IS NULL). The parent's exactly-one-of-three CHECK is replaced per partition. Disputed collections are re-registered under a placeholder model (migration step 2b) so the CHECK never wedges the walk.",
     "fallback_form": "No dimension CHECK per partition; the existing exactly-one-of-three CHECK stays."},
    {"id": "P-PK", "title": "Primary keys and UNIQUE constraints on chunks and its referencing tables (Technical Design, Keys)",
     "new_form": "chunks: PK becomes (tenant_id, collection, chash, embedding_model) because a partitioned table's PK must include its partition keys; (tenant_id, collection, chash) still identifies one chunk because a collection has one model. The referencing tables' PKs and UNIQUEs do not change.",
     "fallback_form": "chunks keeps its three-column PK, which already includes the partition key tenant_id."},
    {"id": "P-READ", "title": "Read path: search function families and the engine callers (Technical Design, Read path)",
     "new_form": "Per-dimension function bodies keep their shape and gain `embedding_model = $model AND tenant_id = $tenant` predicates so force_custom_plan prunes to one leaf at plan time; RLS kept. The engine passes the model it already resolves through CollectionRegistry. hybrid_search_<dim> has no caller and is left as is.",
     "fallback_form": "The read path adds only the tenant_id predicate."},
    {"id": "P-ROUTER", "title": "Router probe probeSelectedRows (nexus-tu8wp.6, Technical Design, Read path)",
     "new_form": "Adds the same embedding_model and tenant_id predicates as the search functions.",
     "fallback_form": "Adds only the tenant_id predicate."},
    {"id": "P-RENAME", "title": "Collection rename: canonical branch and cross-model COPY branch (Technical Design, Write path)",
     "new_form": "Canonical: insert the new registry row, then UPDATE the children's collection; model and tenant are unchanged so the UPDATE stays in the leaf and the manifest follows by ON UPDATE CASCADE. Cross-model COPY branch (CatalogRepository ~9096-9104): the explicit manifest UPDATE also sets embedding_model to the target collection's model.",
     "fallback_form": "Unchanged."},
    {"id": "P-QUARANTINE", "title": "Quarantine: reaper_quarantine_chunks and the quarantine / restore functions (vectors-024; Technical Design, Write path)",
     "new_form": "DELETE ... RETURNING into INSERT registers the quarantine sibling with the origin's model so the row stays in its model partition; the INSERT is among the rewritten write sites.",
     "fallback_form": "Unchanged."},
    {"id": "P-KEYS", "title": "Referencing tables: catalog_document_chunks, topic_assignments, chunk_orphaned_at (Technical Design, Keys)",
     "new_form": "Every table that references a chunk gains embedding_model text NOT NULL, written by the same code that writes its collection (nullable, backfill, then SET NOT NULL at step 4; NOT NULL because the FKs are MATCH SIMPLE). Their indexes, constraints, triggers, policies and grants other than the chunk FKs are unaffected by the key change unless a row below says so.",
     "fallback_form": "No embedding_model column is added."},
    {"id": "X1", "title": "NOT in the RDR table: reference that needs no rewrite for the key change (reads, DELETEs, UPDATEs by key, typed field references) in a live function, view or Java method",
     "new_form": "No rewrite is forced by the key change: a three-column join to the new parent stays valid. P1.3 decides per site whether to add the embedding_model / tenant_id predicates for pruning. Not named by the RDR table.",
     "fallback_form": "Unchanged."},
    {"id": "X2", "title": "NOT in the RDR table: historical changeset content (superseded, dropped, or one-time DML on the old layout)",
     "new_form": "None. Executes before the migration changeset against the old layout. If run again by a rollback round trip it runs against the layout of its time. A runAlways row is never historical: see run_always.",
     "fallback_form": "None."},
    {"id": "X5", "title": "NOT in the RDR table: DimTables, the typed accessor and name authority for every typed write site",
     "new_form": "ChunkTable / CentroidTable (and CHUNKS_TABLE_NAME / CENTROIDS_TABLE_NAME) are the single authority every typed jOOQ write goes through (DimTables javadoc). They must carry embedding_model for the typed channel, or the typed writers cannot supply it. The RDR table names only the raw-SQL sites (PgVectorRepository:1168, TaxonomyCentroidRepository:124). Generated jOOQ classes (Tables.CHUNKS and the record classes) change with the schema; the jOOQ record-count guard moves.",
     "fallback_form": "Unchanged: no new column; the accessors keep their shape."},
    {"id": "X6", "title": "NOT in the RDR table: Java registries that list the tables by name (COLLECTION_SCOPED_TABLES, TenantScope VACUUM_ALLOWED_TABLES / PURGE_VACUUM_TABLES, VERIFY_RELATIONS, pg_catalog lookups by name)",
     "new_form": "Decide per list whether the partitioned parent, its model partitions and tenant leaves, or all of them belong in it. COLLECTION_SCOPED_TABLES drives the collection rename and move UPDATEs (the Technical Design's Rename paragraph depends on it). The VACUUM lists name nexus.chunks, and VACUUM (ANALYZE) on a partitioned parent behaves differently from a plain table. Not named by the RDR table.",
     "fallback_form": "The same decision per list, over tenant leaves only."},
    {"id": "X4", "title": "NOT in the RDR table: table and column definitions of chunks (CREATE TABLE and ADD/ALTER COLUMN history)",
     "new_form": "The new partitioned chunks_new / taxonomy_centroids_new must reproduce the CURRENT column shape (retention, last_written_at, NOT NULL metadata, bytea chash and the rest), which this history builds up across changesets. P1.3 should derive it from the walked schema, not replay the history. Not named by the RDR table beyond the added embedding_model and the three typed vector columns.",
     "fallback_form": "Same, minus embedding_model; LIST-partitioned by tenant_id."},
    {"id": "X3", "title": "NOT in the RDR table: other reference needing a decision",
     "new_form": "Not mapped to a Technical Design row. Decide in P1.3.",
     "fallback_form": "Not mapped."},
]
TD_BY_ID = {r["id"]: r for r in TD_ROWS}

_WRITE_VERBS = {"INSERT", "UPDATE", "DELETE", "TRUNCATE"}
_DDL_VERBS = {"ALTER TABLE", "CREATE INDEX", "CREATE POLICY", "ALTER POLICY", "DROP POLICY", "CREATE TRIGGER",
              "CREATE TABLE", "GRANT", "REVOKE", "REFERENCES"}
_CHUNK_FKS = {"fk_catalog_chunks_chunk": "T01", "topic_assignments_chunk_fk": "T02",
              "chunk_orphaned_at_chunk_fk": "T03", "chunks_collection_fk": "T04"}
_VIEWS_T07 = {"live_chunks", "collection_vector_stats", "diag_chash_conformance"}
_REFERENCING = {"catalog_document_chunks", "topic_assignments", "chunk_orphaned_at"}


def _verbs(row: dict[str, Any]) -> set[str]:
    return {s["verb"] for s in row.get("sites", [])}


def _code_rdr_rows(row: dict[str, Any], fn_rdr: dict[str, str] | None = None) -> list[str]:
    """Applicable Technical Design rows for a Java method or a live function body,
    most specific first. The first is the primary."""
    name = row["name"]
    base = name.split("/")[-1].split("@")[0]
    sites = row.get("sites", [])
    verbs = {s["verb"] for s in sites}
    writes = {(s["verb"], s["table"]) for s in sites if s["verb"] in _WRITE_VERBS}
    out: list[str] = []
    low = base.lower()
    if row["kind"] == "function" and name.startswith("stamp_chunks_on_manifest"):
        out.append("T06")
    if "SET CONSTRAINTS" in verbs:
        out.append("T01")
    if writes and "quarantine" in low:
        out.append("P-QUARANTINE")
    if writes and "rename" in low:
        out.append("P-RENAME")
    if any(t == "taxonomy_centroids" for _, t in writes):
        out.append("T12")
    if any(v == "INSERT" and t != "taxonomy_centroids" for v, t in writes):
        out.append("T05")
    searchy = re.search(r"(search|hybrid|plain_|text_gated|graph_hop|topic_scoped|metadata_scoped|aspect_scoped|ann_query)", low)
    if "CALLS" in verbs:
        # a caller inherits its callees' rows: it changes only if the routine's signature or
        # semantics change (the search families gain a model parameter or predicate)
        for site in sites:
            if site["verb"] == "CALLS":
                out.append((fn_rdr or {}).get(site["object"], "P-READ"))
    elif row["kind"] == "function" and searchy:
        out.append("P-READ")
    if "NAME-LITERAL" in verbs and not (writes or "CALLS" in verbs):
        out.append("X6")
    if not out:
        out.append("X1")
    uniq = list(dict.fromkeys(out))
    return [x for x in uniq if x != "X1"] or uniq  # "no rewrite needed" never outranks a row that applies


def map_rdr(row: dict[str, Any], fn_rdr: dict[str, str] | None = None) -> list[str]:
    """Technical Design rows for one inventory row, primary first. Mapping is by
    category (kind, object name, verbs); the rows are in ``TD_ROWS``."""
    kind = row["kind"]
    name = row["name"]
    tables = set(row["tables"])
    verbs = _verbs(row)
    if row["source"] == "python":
        return [row.get("rdr_hint", "T11")]
    if row["source"] == "java":
        base = name.split("/")[-1].split("@")[0]
        if row["file"].endswith("/DimTables.java"):
            return ["X5"]
        if base == "probeSelectedRows":
            return ["P-ROUTER"]
        if name.split("/")[0] == "ChunksIsolationCheck":
            return ["T11"]
        return _code_rdr_rows(row, fn_rdr)
    # changelog rows
    if row.get("live") is False:
        return ["X2"]
    if kind in ("rollback",):
        return ["X2"]
    if kind == "set-constraints":
        return ["T01"]
    if row.get("op") in ("validate", "drop", "rename") and not row.get("run_always"):
        return ["X2"]  # one-time maintenance of an object some other row defines
    if kind in ("fk", "constraint"):
        if name in _CHUNK_FKS:
            return [_CHUNK_FKS[name]]
        host = row.get("host", "")
        if row.get("ctype") in ("PRIMARY KEY", "UNIQUE") and host in set(TABLES):
            return ["P-PK"]
        if "exactly_one_embedding" in name:
            return ["P-DIM"]
        refs_chunks = any(s.get("table") == "chunks" and s["verb"] == "REFERENCES" for s in row.get("sites", []))
        if host == "chunks" or refs_chunks or name.startswith("chunks_"):
            return ["T09"]
        if host in _REFERENCING:
            return ["P-KEYS"]
        if host == "taxonomy_centroids":
            return ["T12"]
        return ["X3"]
    if kind == "rls" and name in ("no force", "disable"):
        return ["X2"]
    if kind == "rls" or kind == "policy":
        host = row.get("host", "")
        if host == "chunks":
            return ["T08"]
        if host == "taxonomy_centroids":
            return ["T12"]
        return ["P-KEYS"]
    if kind == "index":
        host = row.get("host", "")
        if host == "chunks":
            return ["T09"]
        if host == "taxonomy_centroids":
            return ["T12"]
        return ["P-KEYS"]
    if kind == "grant":
        return ["T10"] if "chunks" in tables else (["T12"] if "taxonomy_centroids" in tables else ["P-KEYS"])
    if kind in ("grant-schema-wide",):
        return ["T10"]
    if kind == "view":
        return ["T07"] if name in _VIEWS_T07 else ["X1"]
    if kind == "trigger":
        fn = row.get("function") or ""
        if fn.startswith("stamp_chunks_on_manifest"):
            return ["T06"]
        return ["T06"] if row.get("host") == "catalog_document_chunks" else ["P-KEYS"]
    if kind == "table":
        return ["T12"] if name == "taxonomy_centroids" else (["P-KEYS"] if name in _REFERENCING else ["X4"])
    if kind == "comment":
        return ["X4"]
    if kind in ("column", "alter-other"):
        host = row.get("host", "")
        if host == "taxonomy_centroids":
            return ["T12"]
        if host in _REFERENCING:
            return ["P-KEYS"]
        return ["X4"] if host == "chunks" else ["X3"]
    if kind == "function":
        return _code_rdr_rows(row)
    if kind in ("do-block", "dml", "precondition", "drop"):
        for d in row.get("defines", []):
            kd, _, nm = d.partition(":")
            if kd == "view" and nm in _VIEWS_T07:
                return ["T07"]
            if kd == "policy" and "chunks" in tables:
                return ["T08"]
        if row.get("run_always"):
            if verbs & _WRITE_VERBS:
                return ["T05"]
            if verbs & {"GRANT", "REVOKE"}:
                return ["T10"]
            return ["X3"]
        # a DO block that creates live objects hides them from the head; classify by its DDL sites
        ddl = [s for s in row.get("sites", []) if s["verb"] in _DDL_VERBS]
        if ddl and kind == "do-block":
            for s in ddl:
                if s["verb"] in {"GRANT", "REVOKE"}:
                    return ["T10"]
                if s["verb"] in {"CREATE POLICY", "ALTER POLICY"}:
                    return ["T08"]
                if s["verb"] == "REFERENCES":
                    return ["T01"]
            return ["X2"]
        return ["X2"]
    return ["X3"]


def attach_forms(rows: list[dict[str, Any]]) -> None:
    fn_rdr = {r["name"]: map_rdr(r)[0] for r in rows if r["kind"] == "function" and r.get("live")}
    for r in rows:
        tds = map_rdr(r, fn_rdr)
        td = tds[0]
        r["rdr_row"] = td
        if len(tds) > 1:
            r["rdr_also"] = tds[1:]
        r["new_form"] = TD_BY_ID[td]["new_form"]
        r["fallback_form"] = TD_BY_ID[td]["fallback_form"]


# ---------------------------------------------------------------------------
# Generation, comparison, coverage
# ---------------------------------------------------------------------------


def git_sha(repo: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                              encoding="utf-8", check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def generate(roots: Roots, source_sha: str | None = None) -> dict[str, Any]:
    sql_rows, cons, stats = changelog_pass(roots)
    routine_names = sorted({r["name"] for r in sql_rows if r["kind"] == "function" and r.get("live") and r["tables"]})
    jrows = java_rows(roots, routine_names, cons)
    prows = python_rows(roots)
    rows = sql_rows + jrows + prows
    # stable order: changelog (include order, changeset line, slot), java (path, pos), python
    rows.sort(key=lambda r: (r["_o"], r["id"]))
    for r in rows:
        del r["_o"]
    attach_forms(rows)
    for r in rows:
        for s in r["sites"]:
            s.setdefault("line", 0)
    counts = Counter(r["kind"] for r in rows)
    by_td = Counter(r["rdr_row"] for r in rows)
    return {
        "schema": SCHEMA_VERSION,
        "tables": list(TABLES),
        "generated_against": source_sha if source_sha is not None else git_sha(roots.repo),
        "scanned": {"changelog": CHANGELOG_REL, "java": JAVA_REL, "changesets": stats["changesets"]},
        "summary": {"rows": len(rows), "by_kind": dict(sorted(counts.items())), "by_rdr_row": dict(sorted(by_td.items()))},
        "mapping_note": (
            "rdr_row / new_form / fallback_form come from the Technical Design of "
            "docs/rdr/rdr-225-vector-tables-per-embedding-model.md (accepted at c2415c3c8). T01-T12 are the "
            "table's rows; a row is mapped to its named object where the table names one (the four FKs, the "
            "stamp_* triggers, the three views, the policies, the named constraints and indexes, the grants "
            "changesets, the isolation checks, taxonomy_centroids) and BY CATEGORY otherwise (write sites, "
            "constraints and indexes, grants, centroid rows). P-* rows are prose sections of the Technical "
            "Design that name code the table does not. X1-X6 are NOT in the RDR: their forms say what the RDR "
            "implies and flag the decision for P1.3. X2 is historical changeset content."
        ),
        "td_rows": TD_ROWS,
        "constraints_tracked": dict(sorted(cons.items())),
        "unscannable": unscannable(roots, cons),
        "rows": rows,
    }


def _strip_lines(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_lines(v) for k, v in obj.items() if k != "line"}
    if isinstance(obj, list):
        return [_strip_lines(v) for v in obj]
    return obj


def compare(pinned: dict[str, Any], fresh: dict[str, Any]) -> list[str]:
    """Differences between a pinned inventory and a fresh one, one line each. Line
    numbers and the recorded sha are not differences."""
    a = {r["id"]: _strip_lines(r) for r in pinned["rows"]}
    b = {r["id"]: _strip_lines(r) for r in fresh["rows"]}
    out: list[str] = []
    for k in sorted(set(a) - set(b)):
        out.append(f"MISSING from this tree: {k}")
    for k in sorted(set(b) - set(a)):
        out.append(f"NEW in this tree, not in the pinned inventory: {k}")
    for k in sorted(set(a) & set(b)):
        if a[k] != b[k]:
            fields = [f for f in sorted(set(a[k]) | set(b[k])) if a[k].get(f) != b[k].get(f)]
            out.append(f"CHANGED {k}: fields {fields}")
    if pinned.get("td_rows") != fresh.get("td_rows"):
        out.append("CHANGED td_rows (the Technical Design mapping table)")
    if pinned.get("constraints_tracked") != fresh.get("constraints_tracked"):
        out.append("CHANGED constraints_tracked (the FK names SET CONSTRAINTS is matched against; "
                   "the coverage check reads this list from the pinned file)")
    # not a difference against the pin: a tree the scanner cannot read is a failure on its own
    out += [f"UNSCANNABLE {u}" for u in fresh.get("unscannable", [])]
    return out


# --- crude, independent coverage detector -----------------------------------

_CRUDE_SQL = re.compile(rf'(?<![\w.$"])(?:(?:"nexus"|nexus)\.)?"?(?:{_TBL_ALT})"?(?![\w$"])', re.I)
_CRUDE_JAVA_CONSTS = re.compile(
    rf"(?<![\w$])(?:CHUNKS_TABLE_NAME|CENTROIDS_TABLE_NAME|{_JT}|{'|'.join(JOOQ_RECORD_CLASSES)})(?![\w$])"
)
_SQL_SHAPE = re.compile(
    r'\b(?:FROM|JOIN|INTO|UPDATE|ONLY|TABLE|REFERENCES|ON|TRUNCATE|USING)\s+(?:(?:"nexus"|nexus)\.)?"?(?:' + _TBL_ALT + r')"?(?![\w$])|'
    r'(?<![\w.$"])(?:"nexus"|nexus)\."?(?:' + _TBL_ALT + r')"?(?![\w$])',
    re.I,
)


def java_file_names_a_table(src: str, cons_re: re.Pattern[str] | None) -> bool:
    """The independent, file-grained Java check the lint uses (see ``crude_locations``).
    It reads the same comment-free, import-free token stream as the generator but none of
    its classification, so a verb the generator does not know still trips it."""
    toks, _ = _strip_imports(java_tokens(src))
    if java_name_literals(toks):
        return True
    for i, t in enumerate(toks):
        if t.kind == "str" and (_SQL_SHAPE.search(t.text) or (cons_re and cons_re.search(t.text))):
            return True
        if t.kind == "id" and _CRUDE_JAVA_CONSTS.fullmatch(t.text):
            if t.text == "CENTROIDS" and not (i + 1 < len(toks) and toks[i + 1].text == "."):
                continue
            return True
    return False


_XML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_COMMENT_ELEMENT = re.compile(r"<comment\b[^>]*?(?:/>|>.*?</comment\s*>)", re.S)
_CHANGESET_OPEN = re.compile(r"<changeSet\b(?P<attrs>[^>]*)>", re.S)
_ATTR_ID = re.compile(r"""\bid\s*=\s*(?:"(?P<d>[^"]*)"|'(?P<s>[^']*)')""")


def raw_changesets(xml: str) -> list[tuple[str, str]]:
    """``(changeset id, body text)`` read from the raw XML with no parser: XML comments and
    ``<comment>`` elements removed (they are prose), the opening tag's own attributes dropped
    (a changeset id such as ``chunks-003`` names no table), SQL comments blanked. Everything
    else stays, so an attribute such as ``tableName="chunks"`` is visible."""
    xml = _COMMENT_ELEMENT.sub(" ", _XML_COMMENT.sub(" ", xml))
    opens = list(_CHANGESET_OPEN.finditer(xml))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(opens):
        end = opens[i + 1].start() if i + 1 < len(opens) else len(xml)
        mi = _ATTR_ID.search(m.group("attrs"))
        cs_id = html.unescape((mi.group("d") if mi.group("d") is not None else mi.group("s")) if mi else "")
        out.append((cs_id, sql_code(xml[m.end():end])))
    return out


def crude_locations(roots: Roots, constraints: dict[str, str] | None = None) -> set[str]:
    """Every location that names a table, found without the structured scanner:
    ``changelog:<file>#<changeset>`` for a changeset whose raw text (SQL, rollback,
    preconditions AND the attributes of native change elements) names one, and
    ``java:<file>`` for a Java file whose code names a table through a SQL-shaped literal or
    a typed constant. Deliberately coarser than the generator (file grain for Java), and it
    does not call ``parse_changelog``, so a blind spot in the parser cannot hide in it."""
    locs: set[str] = set()
    cons = list((constraints or STATIC_CONSTRAINTS))
    cons_re = re.compile(r"(?<![\w$])(?:" + "|".join(map(re.escape, cons)) + r")(?![\w$])") if cons else None
    for fname in master_order(roots.changelog_dir):
        path = roots.changelog_dir / fname
        if not path.exists():
            continue
        for cs_id, text in raw_changesets(path.read_text(encoding="utf-8")):
            if _CRUDE_SQL.search(text) or (cons_re and cons_re.search(text) and _SC.search(text)):
                locs.add(f"changelog:{path.name}#{cs_id}")
    for path in sorted(roots.java_dir.rglob("*.java")):
        if java_file_names_a_table(path.read_text(encoding="utf-8"), cons_re):
            locs.add(f"java:{path.relative_to(roots.java_dir).as_posix()}")
    return locs


def inventory_locations(inventory: dict[str, Any]) -> set[str]:
    locs: set[str] = set()
    for r in inventory["rows"]:
        if r["source"] == "changelog":
            locs.add(f"changelog:{r['file'].split('/')[-1]}#{r['changeset']}")
        elif r["source"] == "java":
            locs.add(f"java:{r['file'][len(JAVA_REL) + 1:] if r['file'].startswith(JAVA_REL) else r['file']}")
    return locs


def uncovered(roots: Roots, inventory: dict[str, Any]) -> list[str]:
    return sorted(crude_locations(roots, inventory.get("constraints_tracked")) - inventory_locations(inventory))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def problems(roots: Roots, pinned: dict[str, Any]) -> list[str]:
    """Everything wrong with ``pinned`` against the tree at ``roots``: rows that differ
    from a fresh generation, and locations the independent crude detector finds that
    the pinned inventory does not list. Empty means current."""
    out = compare(pinned, generate(roots, source_sha=""))
    out += [f"NOT COVERED by the pinned inventory: {loc}" for loc in uncovered(roots, pinned)]
    return out


def dumps(inv: dict[str, Any]) -> str:
    return json.dumps(inv, indent=1, sort_keys=False, ensure_ascii=False) + "\n"


def load_pinned(path: Path = PINNED_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", type=Path, default=REPO_ROOT)
    ap.add_argument("--pinned", type=Path, default=PINNED_PATH)
    ap.add_argument("--source-sha", default=None, help="sha to record (default: git rev-parse HEAD of --repo)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write the pinned inventory")
    mode.add_argument("--check", action="store_true", help="diff a fresh generation against the pinned file")
    args = ap.parse_args(argv)
    roots = default_roots(args.repo)
    fresh = generate(roots, args.source_sha)
    if args.write:
        if fresh["unscannable"]:
            for u in fresh["unscannable"]:
                sys.stdout.write(f"UNSCANNABLE {u}\n")
            sys.stdout.write("refusing to write: the scanner cannot read the changesets above\n")
            return 2
        args.pinned.parent.mkdir(parents=True, exist_ok=True)
        args.pinned.write_text(dumps(fresh), encoding="utf-8")
        sys.stdout.write(f"wrote {args.pinned} rows={fresh['summary']['rows']} against {fresh['generated_against']}\n")
        return 0
    pinned = load_pinned(args.pinned)
    diff = compare(pinned, fresh) + [f"NOT COVERED by the pinned inventory: {loc}" for loc in uncovered(roots, pinned)]
    for line in diff:
        sys.stdout.write(line + "\n")
    if diff:
        sys.stdout.write("inventory is stale: review the differences, then run `python scripts/rdr225_inventory.py --write`\n")
        return 1
    sys.stdout.write(f"inventory current ({fresh['summary']['rows']} rows)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
