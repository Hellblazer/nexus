#!/usr/bin/env python3
"""RDR-229 P3.5b (nexus-p2m1x.24): the census of SQL readers of a collection name column.

One row per (live object, statement, name column) where a statement of a live SQL object reads a collection name
column: a plpgsql or SQL function body, a view's query, a trigger (its column list and WHEN clause; the function it
EXECUTEs is a function row of its own) and a row-level security policy. "Live" is the last definer in
db.changelog-master.xml include order, with <rollback> blocks and XML comments stripped and DROP honoured, the rule
rdr229_p31_writer_census.py uses for functions.

A name column is a column of db/rdr229/p31-checks.tsv's name_column (collection, physical_collection, source_collection,
target_collection, superseded_by), plus nexus.catalog_collections.name, which is the key the others name: an occurrence
counts as catalog_collections.name only when it is qualified by that table or by an alias the statement binds to it
(bare `name` is too common to read). A WRITE of a name column is not a read: an INSERT's column list and an UPDATE's
SET target are skipped (P3.1's writer census holds them). Every other occurrence (WHERE, JOIN, SELECT list, GROUP BY,
ON CONFLICT target, RETURNING, a trigger's UPDATE OF list) is a read.

form: B when the object's text names a chunk-family table (nexus.chunks or a leaf, catalog_document_chunks,
topic_assignments, chunk_orphaned_at), so its redefinition reads both shapes in a plain changeset (RDR-246 refuses a
CONTINUE changeset that names one); A otherwise, a CONTINUE-gated redefinition on the stamp moves' completion.
parse: 'split_part' or 'quarantine' when the body parses a collection name (the RDR's SQL census pin).

What it does not read: SQL the engine builds in Java (P3.5a and P3.5c), and objects created from Java at runtime. The
engine test Rdr229P35bReaderCensusLiveCatalogTest compares this file's object set with pg_catalog of a migrated
database, so an object this parse misses fails there.

Usage: rdr229_p35b_reader_census.py <tree-root> [--write] [--check]
  default: print totals; --write rewrites service/src/test/resources/rdr229/p35b-reader-census.tsv;
  --check exits 1 when the committed file differs from a fresh run.
"""
from __future__ import annotations

import collections
import pathlib
import re

import changelog_text  # scripts/ is on sys.path when a script here is run or imported by the tests
import sys

ROOT = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else pathlib.Path(".").resolve()
RES = ROOT / "service/src/main/resources"
OUT = ROOT / "service/src/test/resources/rdr229/p35b-reader-census.tsv"
CHECKS = RES / "db/rdr229/p31-checks.tsv"

NAME_COLS = set()
for _line in CHECKS.read_text().splitlines():
    if not _line or _line.startswith("#") or _line.startswith("table\t"):
        continue
    _col = _line.split("\t")[1]
    if _col != "-":
        NAME_COLS.add(_col)
NAME_COLS = sorted(NAME_COLS)
CHUNK_FAMILY = re.compile(
    r"\bnexus\.(chunks(?:_[a-z0-9_]+)?|catalog_document_chunks|topic_assignments|chunk_orphaned_at)\b", re.I)
SQL_KEYWORDS = {"where", "join", "on", "left", "right", "inner", "outer", "cross", "using", "group", "order", "limit",
                "union", "set", "values", "returning", "as", "and", "or", "full", "natural", "lateral", "for", "having",
                "window", "offset", "except", "intersect", "select", "from", "into", "when", "then", "do", "where"}


def blank(mm):
    return re.sub(r"[^\n]", " ", mm.group(0))


def strip_sql(s):
    """Blank -- comments and '...' literals (keeping offsets); dollar-quoted inner bodies are left alone."""
    out, i, n = [], 0, len(s)
    while i < n:
        if s.startswith("--", i):
            j = s.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif s[i] == "'":
            j = i + 1
            while j < n:
                if s[j] == "'" and j + 1 < n and s[j + 1] == "'":
                    j += 2
                    continue
                if s[j] == "'":
                    break
                j += 1
            out.append("'" + re.sub(r"[^\n]", " ", s[i + 1:j]) + "'")
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def strip_comments_keep_literals(s):
    """Blank -- and /* */ comments outside '...' literals, keeping the literals (a parse is often a literal: 'quarantine-%')."""
    out, i, n = [], 0, len(s)
    while i < n:
        if s[i] == "'":
            j = i + 1
            while j < n and not (s[j] == "'" and (j + 1 >= n or s[j + 1] != "'")):
                j += 2 if s[j] == "'" else 1
            out.append(s[i:j + 1])
            i = j + 1
        elif s.startswith("--", i):
            j = s.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif s.startswith("/*", i):
            j = s.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", s[i:j]))
            i = j
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def statements(body):
    """(offset, text) of each ';'-separated statement of a stripped body, at paren depth 0."""
    out, depth, start = [], 0, 0
    for i, c in enumerate(body):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == ";" and depth <= 0:
            out.append((start, body[start:i]))
            start = i + 1
    if body[start:].strip():
        out.append((start, body[start:]))
    return out


def paren_span(s, open_at):
    depth = 0
    for i in range(open_at, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return open_at, i + 1
    return open_at, len(s)


def write_spans(stmt):
    """Spans of a statement where a name column is written, not read: INSERT column lists and UPDATE SET targets."""
    spans = []
    for mm in re.finditer(r"\bINSERT\s+INTO\s+[\w.%]+(?:\s+(?:AS\s+)?\w+)?\s*\(", stmt, re.I):
        spans.append(paren_span(stmt, mm.end() - 1))
    for mm in re.finditer(r"\bSET\s+", stmt, re.I):
        # each "<col> =" target up to WHERE / FROM / RETURNING at depth 0
        j, depth = mm.end(), 0
        end = len(stmt)
        while j < len(stmt):
            c = stmt[j]
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    end = j
                    break
                depth -= 1
            elif depth == 0 and re.match(r"\b(WHERE|FROM|RETURNING)\b", stmt[j:j + 9], re.I) and (j == 0 or not stmt[j - 1].isalnum() and stmt[j - 1] != "_"):
                end = j
                break
            j += 1
        region = stmt[mm.end():end]
        # targets: start of region and after each depth-0 comma
        depth = 0
        targets = [0]
        for k, c in enumerate(region):
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif c == "," and depth == 0:
                targets.append(k + 1)
        for t in targets:
            tm = re.match(r"\s*(\(?[\w\s,.]+?\)?)\s*=", region[t:])
            if tm:
                spans.append((mm.end() + t, mm.end() + t + tm.end(1) + (tm.start(1))))
    return spans


def cc_aliases(stmt):
    """Names a statement binds to nexus.catalog_collections: the table itself and any alias."""
    names = {"catalog_collections"}
    for mm in re.finditer(r"\b(?:nexus\.)?catalog_collections\b(?:\s+(?:AS\s+)?([a-z_][a-z0-9_]*))?", stmt, re.I):
        a = (mm.group(1) or "").lower()
        if a and a not in SQL_KEYWORDS:
            names.add(a)
    return names


def reads(stmt):
    """(offset, column) of each name-column read in one stripped statement."""
    ws = write_spans(stmt)
    inside = lambda p: any(a <= p < b for a, b in ws)
    out = []
    for c in NAME_COLS:
        for mm in re.finditer(rf"(?<![\w$]){c}(?![\w$])", stmt):
            # a parameter or variable reference like p_collection never matches; a column of a jsonb ->> 'x' was blanked
            # `AS collection` names an output column (a view's or a RETURNS TABLE's), not a read
            if not inside(mm.start()) and not re.search(r"\bAS\s+$", stmt[max(0, mm.start() - 8):mm.start()], re.I):
                out.append((mm.start(), c))
    if re.search(r"\bcatalog_collections\b", stmt, re.I):
        al = cc_aliases(stmt)
        for mm in re.finditer(r"(?<![\w$])([a-z_][a-z0-9_]*)\.name(?![\w$])", stmt, re.I):
            if mm.group(1).lower() in al and not inside(mm.start()):
                out.append((mm.start(), "catalog_collections.name"))
        # bare `name` in a statement whose only FROM is catalog_collections: WHERE name = ...
        if re.search(r"\bFROM\s+(?:nexus\.)?catalog_collections\b(?!\s*(?:AS\s+)?\w+\s*(?:,|\bJOIN\b))", stmt, re.I):
            for mm in re.finditer(r"\bWHERE\b[^;]*?(?<![\w$.])name(?![\w$])", stmt, re.I):
                p = mm.end() - 4
                if not inside(p):
                    out.append((p, "catalog_collections.name"))
    return sorted(set(out))


# ---------------------------------------------------------------- live objects
master = re.sub(r"<!--.*?-->", "", (RES / "db/changelog/db.changelog-master.xml").read_text(), flags=re.S)
includes = re.findall(r'<include\s+file="([^"]+)"', master)
FN_RE = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(nexus\.[a-z0-9_]+)\s*\(", re.I)
FN_DROP = re.compile(r"DROP\s+FUNCTION\s+(?:IF\s+EXISTS\s+)?(nexus\.[a-z0-9_]+)", re.I)
VIEW_RE = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?(MATERIALIZED\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?(nexus\.[a-z0-9_]+)", re.I)
VIEW_DROP = re.compile(r"DROP\s+(?:MATERIALIZED\s+)?VIEW\s+(?:IF\s+EXISTS\s+)?(nexus\.[a-z0-9_]+)", re.I)
TRG_RE = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?TRIGGER\s+([a-z0-9_]+)\s.*?\bON\s+(nexus\.[a-z0-9_]+)", re.I | re.S)
TRG_DROP = re.compile(r"DROP\s+TRIGGER\s+(?:IF\s+EXISTS\s+)?([a-z0-9_]+)\s+ON\s+(nexus\.[a-z0-9_]+)", re.I)
POL_RE = re.compile(r"CREATE\s+POLICY\s+([a-z0-9_\"]+)\s+ON\s+(nexus\.[a-z0-9_]+)", re.I)
POL_DROP = re.compile(r"DROP\s+POLICY\s+(?:IF\s+EXISTS\s+)?([a-z0-9_\"]+)\s+ON\s+(nexus\.[a-z0-9_]+)", re.I)


def stmt_end(text, start):
    """End of the SQL statement starting at start: ';' at paren depth 0 outside literals, or the </sql> close."""
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c == "'":
            j = i + 1
            while j < n and not (text[j] == "'" and (j + 1 >= n or text[j + 1] != "'")):
                j += 2 if text[j] == "'" else 1
            i = j + 1
            continue
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "$":
            mm = re.match(r"\$[a-zA-Z_]*\$", text[i:])
            if mm:
                e = text.find(mm.group(0), i + mm.end())
                i = n if e < 0 else e + mm.end()
                continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == ";" and depth <= 0:
            return i
        if text.startswith("</sql>", i) or text.startswith("]]>", i):
            return i
        i += 1
    return n


live = {}  # key -> (kind, object, file, create_line, body_line, body)
for inc in includes:
    p = RES / inc
    if not p.exists():
        continue
    raw = p.read_text()
    text = changelog_text.strip_noise(raw)  # comments before rollbacks; the order lives in scripts/changelog_text.py
    fname = inc.split("/")[-1]
    ev = []
    for mm in FN_RE.finditer(text):
        ev.append((mm.start(), "def", ("function", mm.group(1).lower()), mm))
    for mm in FN_DROP.finditer(text):
        ev.append((mm.start(), "drop", ("function", mm.group(1).lower()), mm))
    for mm in VIEW_RE.finditer(text):
        ev.append((mm.start(), "def", ("matview" if mm.group(1) else "view", mm.group(2).lower()), mm))
    for mm in VIEW_DROP.finditer(text):
        ev.append((mm.start(), "drop", ("view", mm.group(1).lower()), mm))
        ev.append((mm.start(), "drop", ("matview", mm.group(1).lower()), mm))
    for mm in TRG_RE.finditer(text):
        if stmt_end(text, mm.start()) < mm.end():
            continue  # the ON matched past the statement
        ev.append((mm.start(), "def", ("trigger", f"{mm.group(2).lower()}.{mm.group(1).lower()}"), mm))
    for mm in TRG_DROP.finditer(text):
        ev.append((mm.start(), "drop", ("trigger", f"{mm.group(2).lower()}.{mm.group(1).lower()}"), mm))
    for mm in POL_RE.finditer(text):
        ev.append((mm.start(), "def", ("policy", f"{mm.group(2).lower()}.{mm.group(1).strip(chr(34)).lower()}"), mm))
    for mm in POL_DROP.finditer(text):
        ev.append((mm.start(), "drop", ("policy", f"{mm.group(2).lower()}.{mm.group(1).strip(chr(34)).lower()}"), mm))
    for pos, what, key, mm in sorted(ev, key=lambda e: e[0]):
        if what == "drop":
            live.pop(key, None)
            continue
        cline = text.count("\n", 0, pos) + 1
        if key[0] == "function":
            dq = re.search(r"AS\s+(\$[a-zA-Z_]*\$)", text[mm.end():])
            if not dq:
                continue
            tag = dq.group(1)
            bstart = mm.end() + dq.end()
            e = text.find(tag, bstart)
            body = text[bstart:e if e > 0 else len(text)]
        else:
            bstart = pos  # the whole statement: a trigger's UPDATE OF list and WHEN clause precede its ON
            body = text[bstart:stmt_end(text, bstart)]
        live[key] = (key[0], key[1], fname, cline, text.count("\n", 0, bstart) + 1, body)

# ---------------------------------------------------------------- rows
rows = []
parse_count = 0
for key in sorted(live):
    kind, obj, f, cline, bline, body = live[key]
    raw_body = body
    code = strip_comments_keep_literals(body)  # a parse named in a -- comment is not a parse
    form = "B" if CHUNK_FAMILY.search(code) else "A"
    parse = []
    if re.search(r"split_part\s*\([^;]*?'__'", code, re.I | re.S):
        parse.append("split_part")
    if re.search(r"'quarantine-", code):
        parse.append("quarantine")
    parse_count += len(parse)
    sbody = strip_sql(body)
    for off, st in statements(sbody):
        cols = sorted({c for _, c in reads(st)})
        if not cols:
            continue
        line = bline + sbody.count("\n", 0, off + len(st) - len(st.lstrip()))
        tables = sorted({m.group(1).lower() for m in re.finditer(r"\bnexus\.([a-z_][a-z0-9_]*)", st)}
                        - {obj.split(".")[-1]} if kind != "trigger" else {obj.split(".")[1]})
        for c in cols:
            rows.append((kind, obj, f"{f}:{line}", str(cline), c, form, ",".join(parse) or "-",
                         ",".join(t for t in tables if not t.endswith("_fn")) or "-"))

HEAD = ("kind", "object", "statement_at", "create_line", "name_column", "form", "parse", "tables_named")
rows = sorted(set(rows))
text = "# RDR-229 P3.5b (nexus-p2m1x.24): every live SQL object statement that reads a collection name column.\n"
text += "# Generated by scripts/rdr229_p35b_reader_census.py (a drift check: --check). Live = last definer in master include order.\n"
text += "# One row per (object, statement, name column). statement_at is changelog:line of the statement; create_line the CREATE's line.\n"
text += "# form B: the object names a chunk-family table (read both shapes, plain changeset); A: CONTINUE-gated on the stamp moves.\n"
text += "# parse: the body parses a collection name (split_part on '__', a 'quarantine-' prefix). Writes (INSERT column lists, SET targets) are P3.1's.\n"
text += "\t".join(HEAD) + "\n" + "".join("\t".join(r) + "\n" for r in rows)

if __name__ == "__main__":
    objs = collections.Counter(r[0] for r in {(r[0], r[1]) for r in rows})
    print("rows", len(rows), "objects", sum(objs.values()), dict(objs))
    print("form", dict(collections.Counter(f for _, f in {(r[1], r[5]) for r in rows})))
    print("columns", dict(collections.Counter(r[4] for r in rows)))
    print("live objects scanned", dict(collections.Counter(k[0] for k in live)), "name-parsing bodies", parse_count)
    if "--write" in sys.argv:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text)
    if "--check" in sys.argv:
        sys.exit(0 if OUT.exists() and OUT.read_text() == text else 1)
