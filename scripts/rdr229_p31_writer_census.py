#!/usr/bin/env python3
"""RDR-229 P3.1 stage 2a (nexus-p2m1x.17): the writer census for the dual write.

One row per write site that stage 2b must make set collection_id: a jOOQ insertInto / update / mergeInto in
service/src/main/java (literal TABLE constants, plus the helper-routed shapes: ch.table(), ct.table(), t.table() over
COLLECTION_SCOPED_TABLES, TenantImportLoad's by-name insert), and every live nexus.* SQL function whose body
INSERTs or UPDATEs a table of the Expand list (last definer in changelog include order, rollback blocks and comments
stripped, DROP FUNCTION honoured).

An UPDATE counts even when it does not name the collection column: a NOT VALID CHECK is evaluated on every updated row
version, so a row the stamp has not reached (collection set, collection_id NULL) fails any UPDATE that does not set the id.

Usage: rdr229_p31_writer_census.py <tree-root> [--write] [--check]
  default: print totals only; --write rewrites service/src/test/resources/rdr229/p31-writer-census.tsv;
  --check exits 1 when the committed file differs from a fresh run (a drift check for stage 2b).
"""
import collections
import pathlib
import re

import changelog_text  # scripts/ is on sys.path when a script here is run or imported by the tests
import sys

ROOT = pathlib.Path(sys.argv[1]).resolve()
JAVA = ROOT / "service/src/main/java"
RES = ROOT / "service/src/main/resources"
OUT = ROOT / "service/src/test/resources/rdr229/p31-writer-census.tsv"
CHECKS = RES / "db/rdr229/p31-checks.tsv"

FAMILY = {
    "chunks": "chunks",
    "catalog_documents": "catalog", "catalog_document_chunks": "catalog", "chunk_orphaned_at": "catalog",
    "catalog_collections": "catalog", "catalog_reconcile_rows": "catalog",
    "topics": "taxonomy", "topic_assignments": "taxonomy", "taxonomy_meta": "taxonomy", "taxonomy_centroids": "taxonomy",
    "document_aspects": "aspects", "document_highlights": "aspects", "canary_query_ring": "aspects",
    "aspect_extraction_queue": "aspects",
    "chash_remap": "ops", "pdf_pipeline": "ops", "reembed_jobs": "ops",
    "relevance_log": "ops", "search_telemetry": "ops", "hook_failures": "ops", "gc_audit": "ops",
}
# name columns per table (from the one list) plus the two tables the list does not carry
NAME_COLS = collections.defaultdict(list)
ID_COLS = collections.defaultdict(list)
HAS_CHECK = set()
for line in CHECKS.read_text().splitlines():
    if not line or line.startswith("#") or line.startswith("table\t"):
        continue
    t, name_col, id_col, constraint, kind = line.split("\t")
    if kind == "NOT_NULL":
        HAS_CHECK.add(t)
        continue
    if name_col not in NAME_COLS[t]:
        NAME_COLS[t].append(name_col)
        ID_COLS[t].append(id_col)
    if constraint != "-":
        HAS_CHECK.add(t)
NAME_COLS["catalog_reconcile_rows"] = ["physical_collection"]  # not in the expand list; no CHECK
ID_COLS["catalog_reconcile_rows"] = ["-"]
TABLES = sorted(FAMILY)

# collection-name column constants -> table
NAME_CONST = {
    "COLLECTION", "PHYSICAL_COLLECTION", "SOURCE_COLLECTION", "TARGET_COLLECTION", "SUPERSEDED_BY",
}


def scan_statement(txt, start):
    """The text from start to the end of the enclosing statement: a ';' at depth 0 or the close of an enclosing call."""
    depth, i, n = 0, start, len(txt)
    while i < n:
        c = txt[i]
        if txt.startswith('"""', i):
            j = txt.find('"""', i + 3)
            i = (j if j >= 0 else n) + 3
            continue
        if c == '"':
            i += 1
            while i < n and txt[i] != '"':
                i += 2 if txt[i] == "\\" else 1
            i += 1
            continue
        if c == "'":
            i += 1
            while i < n and txt[i] != "'":
                i += 2 if txt[i] == "\\" else 1
            i += 1
            continue
        if txt.startswith("//", i):
            j = txt.find("\n", i)
            i = n if j < 0 else j
            continue
        if txt.startswith("/*", i):
            j = txt.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth < 0:
                return txt[start:i]
        elif c == ";" and depth <= 0:
            return txt[start:i]
        i += 1
    return txt[start:]


def strip_comments(s):
    """Blank // and /* */ comments (keeping newlines and offsets), skipping string and char literals."""
    out, i, n = [], 0, len(s)
    while i < n:
        if s.startswith('"""', i):
            j = s.find('"""', i + 3)
            j = n if j < 0 else j + 3
            out.append(s[i:j])
            i = j
        elif s[i] == '"' or s[i] == "'":
            q, j = s[i], i + 1
            while j < n and s[j] != q:
                j += 2 if s[j] == "\\" else 1
            out.append(s[i:j + 1])
            i = j + 1
        elif s.startswith("//", i):
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


def set_regions(span):
    """The argument text of each .set( call in the span (an UPDATE's written columns)."""
    out = []
    for m in re.finditer(r"\.set\(", span):
        depth, i = 1, m.end()
        while i < len(span) and depth:
            depth += {"(": 1, ")": -1}.get(span[i], 0)
            i += 1
        out.append(span[m.end():i - 1])
    return out


def name_columns_written(table, op, span):
    cols = []
    region = span if op == "insert" else "\n".join(set_regions(span))
    up = table.upper()
    for c in NAME_COLS.get(table, []):
        if c == "-":
            continue
        pat = rf"\b{up}\.{c.upper()}\b|\bt\.collection\(\)|\bchunk\.collection\(\)|DSL\.name\(\"{c}\"\)"
        # helper-routed tables name the column through the helper, so any .collection() / .collection call counts
        if re.search(pat, region) or (table in ("chunks", "taxonomy_centroids") and re.search(r"\.collection\(\)|\bCOLLECTION\b", region)):
            cols.append(c)
    return cols


rows = []  # family, table, file, line, op, kind, name_cols_written, id_cols, check, note


# RDR-229 P3.5a (nexus-p2m1x.23): the registry column. The writer's order against the registry read, by what the scan can prove from the
# method that holds the statement: `require` (a registry read, key share, precedes or sits in the method), `history` (a history table:
# stamped unlocked, exempt by linda's ruling), `verb` (a retiring or renaming verb, which locks the registry row FOR UPDATE itself),
# `sql` (a SQL function: called by one of the above), `move` (a data move's template), and `unproven` (none of those markers in the method:
# its caller may read the registry first, which this scan cannot see). The count of `unproven` rows is a ratchet: --write refuses to
# raise the ceiling in the committed file, so a new write site that proves nothing cannot appear unnoticed.
HISTORY_TABLES = {"relevance_log", "search_telemetry", "hook_failures", "gc_audit"}
REGISTRY_READ = re.compile(r"requireForWrite\w*|requireWritable|requireAllForWrite|requireIdForWrite|CollectionIds\.require|registryFirst"
                           r"|lockAndRequireWritable|DocumentCollectionRef\.(?:normalize\w*|idOf)|RequestedCollectionRead\.require"
                           r"|consumeRequestedId|ensureCollectionRegistered\w*|labelNow|requireRegistered|CollectionRegistry\.require")
VERB = re.compile(r"runRename|lockRows|casSupersede|deleteRegistryRow|retireAndRegister|CollectionTombstones\.|RenameGuard")
MEMBER = re.compile(r"\n    (?!return |if |for |while |switch |try|else|new |throw |\}|//|\*|/\*)[A-Za-z@][^;{}=\n]*(?:\n[^;{}=\n]*)*?\)\s*"
                    r"(?:throws[^{;]*)?\{")


def registry_class(table, code, pos):
    """(class, method): the order is judged against the statement at `pos`, not the method as a whole: a `require` is a registry read that
    comes BEFORE the first write statement of this site (a read after the write proves nothing about its lock order)."""
    if table in HISTORY_TABLES:
        return "history", ""
    heads = [m for m in MEMBER.finditer(code) if m.end() <= pos]
    if not heads:
        return "unproven", ""
    head = heads[-1]
    start = head.end()
    decl = re.sub(r"@\w+(\([^)]*\))?", "", head.group(0))
    named = re.search(r"(\w+)\s*\(", decl)
    method = named.group(1) if named else ""
    nxt = MEMBER.search(code, pos)
    body = code[start:nxt.start() if nxt else len(code)]
    if VERB.search(body):
        return "verb", method
    return ("require" if REGISTRY_READ.search(code[start:pos]) else "unproven"), method


def add(table, f, line, op, kind, cols, note="", registry="sql"):
    method = ""
    if isinstance(registry, tuple):
        registry, method = registry
    rows.append((FAMILY[table], table, f, line, op, kind, ",".join(cols) or "-",
                 ",".join(ID_COLS.get(table, [])) or "-", "yes" if table in HAS_CHECK else "no", note, registry, method))


# ---------------------------------------------------------------- Java sites
HELPER_TABLE = {"ch": "chunks", "ct": "taxonomy_centroids"}
SCOPED = [  # CatalogRepository.COLLECTION_SCOPED_TABLES, read from source below
]
cr = (JAVA / "dev/nexus/service/db/CatalogRepository.java").read_text()
m = re.search(r"COLLECTION_SCOPED_TABLES = List\.of\((.*?)\)\);", cr, re.S)
SCOPED = re.findall(r'new CollectionScopedTable\("([a-z_]+)"', m.group(1))
EXPORT = re.findall(r'(?:data|rls)\("([a-z_]+)"\)', (JAVA / "dev/nexus/service/db/TenantExport.java").read_text())

CALL = re.compile(r"\b(insertInto|update|mergeInto)\(\s*([A-Za-z_][\w.]*(?:\([^()]*\))?)")
unresolved = []
for p in sorted(JAVA.rglob("*.java")):
    txt = p.read_text()
    code = strip_comments(txt)
    rel = p.relative_to(ROOT).as_posix()
    for mm in CALL.finditer(code):
        op = "insert" if mm.group(1) in ("insertInto", "mergeInto") else "update"
        arg = mm.group(2)
        line = code.count("\n", 0, mm.start()) + 1
        span = scan_statement(code, mm.start())
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", arg):
            t = arg.lower()
            if t in FAMILY:
                add(t, rel, line, op, "jooq", name_columns_written(t, op, span), registry=registry_class(t, code, mm.start()))
            continue
        hm = re.fullmatch(r"(\w+)\.table\(\)", arg)
        if hm and hm.group(1) in HELPER_TABLE:
            t = HELPER_TABLE[hm.group(1)]
            add(t, rel, line, op, "helper", name_columns_written(t, op, span), f"{hm.group(1)}.table() helper",
                registry=registry_class(t, code, mm.start()))
        elif hm and hm.group(1) == "t" and p.name == "CatalogRepository.java":
            for t in SCOPED:
                if t in FAMILY:
                    add(t, rel, line, op, "helper", name_columns_written(t, op, span) or [NAME_COLS[t][0]],
                        "COLLECTION_SCOPED_TABLES loop (rename/re-home)", registry=registry_class(t, code, mm.start()))
        elif "DSL.table(DSL.name(e.schema()" in code[mm.start():mm.start() + 200]:
            for t in EXPORT:
                if t in FAMILY:
                    add(t, rel, line, op, "by-name", [c for c in NAME_COLS[t] if c != "-"],
                        "TenantImportLoad by-name INSERT ... SELECT over TenantExport.TABLES", registry="unproven")
        elif arg in ("meta", "bytes", "tenant", "collection") or arg.startswith(("new ", "ByteBuffer", "id,", "k.", "model.")):
            continue  # a MessageDigest / Mac update, not a jOOQ write
        else:
            unresolved.append((rel, line, op, arg))

# ---------------------------------------------------------------- SQL functions
master = re.sub(r"<!--.*?-->", "", (RES / "db/changelog/db.changelog-master.xml").read_text(), flags=re.S)
includes = re.findall(r'<include\s+file="([^"]+)"', master)
FN_RE = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(nexus\.[a-z0-9_]+)\s*\((.*?)\)\s*RETURNS", re.I | re.S)
DROP_RE = re.compile(r"DROP\s+FUNCTION\s+(?:IF\s+EXISTS\s+)?(nexus\.[a-z0-9_]+)\s*(\([^)]*\))?", re.I)
live = {}
for inc in includes:
    p = RES / inc
    if not p.exists():
        continue
    raw = p.read_text()
    blank = lambda mm: re.sub(r"[^\n]", " ", mm.group(0))
    text = changelog_text.strip_noise(raw)  # comments before rollbacks; the order lives in scripts/changelog_text.py
    events = [(mm.start(), "def", mm.group(1).lower(), mm) for mm in FN_RE.finditer(text)]
    events += [(mm.start(), "drop", mm.group(1).lower(), mm) for mm in DROP_RE.finditer(text)]
    for pos, kind, name, mm in sorted(events, key=lambda e: e[0]):
        if kind == "drop":
            live.pop(name, None)
            continue
        dq = re.search(r"AS\s+(\$[a-zA-Z_]*\$)", text[mm.end():])
        body, bstart = "", mm.end()
        if dq:
            tag = dq.group(1)
            bstart = mm.end() + dq.end()
            e = text.find(tag, bstart)
            body = text[bstart:e if e > 0 else len(text)]
        live[name] = (inc.split("/")[-1], text.count("\n", 0, mm.start()) + 1, text.count("\n", 0, bstart) + 1, body)

fn_sites = 0
for name in sorted(live):
    f, line, bline, body = live[name]
    body = re.sub(r"--[^\n]*", lambda mm: " " * len(mm.group(0)), body)
    for t in TABLES:
        for mm in re.finditer(r"\b(INSERT\s+INTO|UPDATE|MERGE\s+INTO)\s+nexus\." + t + r"\b", body, re.I):
            op = "update" if mm.group(1).upper().startswith("UPDATE") else "insert"
            semi = body.find(";", mm.start())
            win = body[mm.start():semi if semi > 0 else len(body)]
            cols = [c for c in NAME_COLS.get(t, []) if c != "-" and re.search(rf"\b{c}\b", win)]
            add(t, f"{name} [{f}:{bline + body.count(chr(10), 0, mm.start())}]", line, op, "sql_function", cols)
            fn_sites += 1
    # dynamic SQL over %I.%I (format) names the table in an argument; list for review
    if re.search(r"(INSERT\s+INTO|UPDATE)\s+%I\.%I", body, re.I):
        unresolved.append((f"{name} [{f}]", line, "dynamic", "format(%I.%I)"))

# ---------------------------------------------------------------- registered move templates
# (the third writer: a data move's template UPDATEs a guarded table on a schedule of its own; see rdr229_p31_move_template_census.py)
import rdr229_p31_move_template_census as MT  # noqa: E402  (scripts/ is on sys.path when this file is run)

GUARDED = MT.read_guarded(CHECKS)
MOVES = MT.parse_tree(ROOT)
rows.extend(tuple(r) + ("move", "") for r in MT.census_rows(MOVES, GUARDED))
template_violations = MT.violations(MOVES, GUARDED)

HEAD = ("family", "table", "file_or_function", "line", "op", "kind", "name_columns_written", "id_columns", "has_check", "note", "registry", "method")
rows = sorted(set(rows), key=lambda r: (r[0], r[1], r[2], int(r[3]), r[4]))
text = "# RDR-229 P3.1 stage 2a (nexus-p2m1x.17): every write site stage 2b must make set collection_id.\n"
text += "# Generated by scripts/rdr229_p31_writer_census.py (a drift check: --check). One row per write site: the jOOQ call or SQL statement.\n"
text += "# line: the call's line in file; for kind=sql_function the file column is 'function [changelog:line of the statement]' and line is the function's CREATE line.\n"
text += "# name_columns_written '-': the site writes no collection name column, but an UPDATE of a CHECK-guarded row must still set the id (the row may be\n"
text += "# unstamped) and an INSERT must set it when the table has a CHECK; has_check says whether the table carries a P3.1 CHECK (history tables and catalog_reconcile_rows do not).\n"
text += "# kinds: jooq (literal TABLE constant), helper (ch.table(), ct.table(), COLLECTION_SCOPED_TABLES loop), by-name (TenantImportLoad), sql_function,\n"
text += "# data_move (a registered nexus.data_moves template; file column is 'label [changelog:template line]', line the INSERT's; note says whether it sets the id, is superseded or handled, or is a VIOLATION).\n"
unproven = sum(1 for r in rows if r[10] == "unproven")
old_ceiling = None
if OUT.exists():
    mo = re.search(r"^# unproven-ceiling: (\d+)$", OUT.read_text(), re.M)
    old_ceiling = int(mo.group(1)) if mo else None
ceiling = unproven if old_ceiling is None or "--allow-raise" in sys.argv else min(old_ceiling, unproven)
text += "# registry: require (a registry read, key share, in the method that holds the statement), history (a history table: stamped unlocked, exempt), verb (a verb that locks the\n"
text += "# registry row itself), sql (a SQL function), move (a data move's template), unproven (the scan sees no registry read in the method: its caller may read it first).\n"
text += f"# unproven-ceiling: {ceiling}\n"
text += "\t".join(HEAD) + "\n" + "".join("\t".join(map(str, r)) + "\n" for r in rows)

if "--write" in sys.argv:
    if old_ceiling is not None and unproven > old_ceiling and "--allow-raise" not in sys.argv:
        print(f"REFUSED: {unproven} write sites prove no registry read, over the committed ceiling {old_ceiling}; read the registry first in the new site,"
              " or classify it, or pass --allow-raise with the reason in the commit message")
        sys.exit(2)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
if "--check" in sys.argv:
    for m, why in template_violations:
        print(f"VIOLATION {m.label} [{m.file}:{m.template_line}]: {why}")
    if unproven > ceiling:
        print(f"UNPROVEN {unproven} write sites prove no registry read, over the ceiling {ceiling}")
    sys.exit(0 if OUT.exists() and OUT.read_text() == text and not template_violations and unproven <= ceiling else 1)

by_family = collections.Counter(r[0] for r in rows)
by_table = collections.Counter(r[1] for r in rows)
by_kind = collections.Counter(r[5] for r in rows)
print("rows", len(rows), dict(by_kind))
print("family", dict(by_family))
print("table", dict(sorted(by_table.items())))
print("java jooq/helper/by-name rows", sum(1 for r in rows if r[5] != "sql_function"), "sql_function rows", fn_sites,
      "live sql functions writing", len({r[2].split(" ")[0] for r in rows if r[5] == "sql_function"}))
print("data_move rows", sum(1 for r in rows if r[5] == "data_move"), "registered templates", len(MOVES), "violations", len(template_violations))
for m, why in template_violations:
    print(f"VIOLATION {m.label} [{m.file}:{m.template_line}]: {why}")
for u in unresolved:
    print("UNRESOLVED", u)
