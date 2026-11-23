#!/usr/bin/env python3
"""RDR-229 P3.5c (nexus-p2m1x.25): the census of Java (jOOQ and SQL-string) readers of a collection name column.

P3.1's rdr229_p31_writer_census.py counted WRITES (insertInto / update / mergeInto). This counts READS: one row per
(file, line, table, name_column, op) where service/src/main/java uses a name column as a value, and op is the clause it
sits in: where, join_on, join_using, select, group_by, order_by, having, on_conflict, returning, set_value (the right-hand
side of a SET), result (a Record read: get / getValue / fetch(field) / into), values, expr (a bare field reference: a
variable, a return, a Condition built for a later clause) or accessor_def (DimTables' typed accessors). The two upserts the
RDR names are the on_conflict rows of PgVectorRepository (ch.collection()) and TaxonomyCentroidRepository (ct.collection()).

Name columns: name_column of db/rdr229/p31-checks.tsv (collection, physical_collection, source_collection,
target_collection, superseded_by), plus catalog_collections.name (matched only on that table or an alias bound to it: bare
NAME is too common). A WRITE is not a read: an insertInto column list, a columns(...) list and the first argument of a
set(...) are skipped (P3.1's writer census holds them).

How an occurrence is found and given its table:
  jooq     TABLE.COLUMN over a generated jOOQ constant (CHUNKS.COLLECTION). A TABLE that is not in p31-checks.tsv (a view, or
           catalog_reconcile_rows) is still a row; the note column says "not in p31-checks".
  alias    x.COLUMN where x was bound by `x = TABLE.as("...")` (the nearest binding above it in the file).
  helper   ch.collection() / ct.collection() where the variable was declared DimTables.ChunkTable / CentroidTable (or bound by
           DimTables.CHUNKS.get(dim) / CENTROIDS.get(dim)). Other x.collection() calls are Java record accessors, not columns,
           and are counted in the totals line, never rows.
  literal  DSL.field("collection") / DSL.name("collection") over a name-column literal: the table is the one table of the
           enclosing statement that carries that column; else the row is UNRESOLVED.
  sql      a string literal (or the literals of one statement, joined) that holds SQL and a name column: the SQL is read by
           P3.5b's reads() rules, the clause by the keyword before it; the table from `nexus.<t>` or FROM/JOIN, else UNRESOLVED.

METADATA KEYS (nexus-p2m1x.25, the second reader class). A collection label can also be read through a key of a jsonb column, with no
name column in sight. The name_column of such a row is "metadata.<key>". The keys are LABEL_KEYS below, each with the writer that proves it
carries a label; ID_KEYS are the id twins (read and counted in the output, never a row). The forms found: DSL.jsonbGetAttributeAsText /
jsonbGetAttribute (key literal or a static final String constant), DSL.function("jsonb_extract_path_text" | "jsonb_extract_path" |
"jsonb_exists" | "jsonb_object_field_text" | "jsonb_object_field", ..., metadata, key), and -> / ->> / #>> / #> / jsonb_extract_path_text /
jsonb_exists / ? over a metadata column inside a string literal. A Field bound to one of these (`Field<String> origin = ...;`, a
local or a class constant) is an accessor_def row, and every later use of the variable in its scope is a row in the clause it sits in
(via "bound"): `ORIGIN_TAG.eq(origin)` is a where row though the key is named once. A read whose key is not a literal or a constant (a
user-supplied key: StoreGetProjection, a metadata filter) is counted in the output ("variable-key"), never a row, because no key is
read; a bound Field that a method returns or a non-private constant that another file uses is noted in the output, not followed.

An UNRESOLVED occurrence is a row with table "?" (so it needs a disposition, and the drift check sees it) and an UNRESOLVED line
in this script's output. Nothing is dropped. What the scan cannot see: a name column reached through a Field<String> parameter
handed to a helper (the helper's own use of the parameter is a bare expression with no table), and SQL a changeset holds (P3.5b's).

Usage: rdr229_p35c_java_reader_census.py <tree-root> [--write] [--check]
  default: print totals and the UNRESOLVED lines; --write rewrites service/src/test/resources/rdr229/p35c-java-reader-census.tsv;
  --check exits 1 when the committed file differs from a fresh run.
"""
from __future__ import annotations

import collections
import pathlib
import re
import sys

ROOT = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else pathlib.Path(".").resolve()
JAVA = ROOT / "service/src/main/java"
OUT = ROOT / "service/src/test/resources/rdr229/p35c-java-reader-census.tsv"
CHECKS = ROOT / "service/src/main/resources/db/rdr229/p31-checks.tsv"

NAME_CONST = {"COLLECTION", "PHYSICAL_COLLECTION", "SOURCE_COLLECTION", "TARGET_COLLECTION", "SUPERSEDED_BY"}
LISTED = collections.defaultdict(set)  # table -> name columns, from p31-checks.tsv
for _line in CHECKS.read_text().splitlines():
    if not _line or _line.startswith("#") or _line.startswith("table\t"):
        continue
    _t, _col = _line.split("\t")[:2]
    if _col != "-":
        LISTED[_t].add(_col)
LISTED["catalog_collections"].add("name")
NAME_COLS = sorted({c for cols in LISTED.values() for c in cols})
HELPER_DECL = {"ChunkTable": "chunks", "CentroidTable": "taxonomy_centroids"}
HELPER_GET = {"CHUNKS": "chunks", "CENTROIDS": "taxonomy_centroids"}

# ---------------------------------------------------------------- Java text
def blank(s):
    return re.sub(r"[^\n]", " ", s)


def strip_comments(s):
    """Blank // and /* */ comments (keeping newlines and offsets), skipping string, text-block and char literals."""
    out, i, n = [], 0, len(s)
    while i < n:
        if s.startswith('"""', i):
            j = s.find('"""', i + 3)
            j = n if j < 0 else j + 3
            out.append(s[i:j])
            i = j
        elif s[i] in "\"'":
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
            out.append(blank(s[i:j]))
            i = j
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def literals(code):
    """(start, end, text) of each string / text-block literal of comment-stripped code."""
    out, i, n = [], 0, len(code)
    while i < n:
        if code.startswith('"""', i):
            j = code.find('"""', i + 3)
            j = n if j < 0 else j
            out.append((i, j + 3, code[i + 3:j]))
            i = j + 3
        elif code[i] == '"':
            j = i + 1
            while j < n and code[j] != '"':
                j += 2 if code[j] == "\\" else 1
            out.append((i, j + 1, code[i + 1:j]))
            i = j + 1
        elif code[i] == "'":
            j = i + 1
            while j < n and code[j] != "'":
                j += 2 if code[j] == "\\" else 1
            i = j + 1
        else:
            i += 1
    return out


def mask(code, lits):
    """Code with every literal's body blanked to 'x' (same offsets), so parens inside SQL text do not read as calls."""
    chars = list(code)
    for a, b, _ in lits:
        for k in range(a, b):
            if chars[k] != "\n":
                chars[k] = "x"
    return "".join(chars)


# ---------------------------------------------------------------- the call tree of masked code
class Call:
    __slots__ = ("name", "open", "close", "parent", "idx")

    def __init__(self, name, open_, parent, idx):
        self.name, self.open, self.close, self.parent, self.idx = name, open_, None, parent, idx


def call_tree(m):
    calls, stack = [], []
    for i, c in enumerate(m):
        if c == "(":
            j = i - 1
            while j >= 0 and m[j] in " \t\r\n":
                j -= 1
            k = j
            while k >= 0 and (m[k].isalnum() or m[k] in "_$"):
                k -= 1
            name = m[k + 1:j + 1]
            if name in ("if", "for", "while", "switch", "catch", "synchronized", "return", "new") or not name or name[0].isdigit():
                name = ""
            call = Call(name, i, stack[-1] if stack else None, len(calls))
            calls.append(call)
            stack.append(call)
        elif c == ")" and stack:
            stack.pop().close = i
    for c in stack:
        c.close = len(m)
    return calls


CLAUSE = {
    "where": "where", "whereExists": "where", "whereNotExists": "where", "having": "having", "on": "join_on",
    "using": "join_using", "groupBy": "group_by", "orderBy": "order_by", "partitionBy": "partition_by",
    "select": "select", "selectDistinct": "select", "fields": "select", "distinctOn": "distinct_on",
    "onConflict": "on_conflict", "returning": "returning", "returningResult": "returning", "values": "values",
    "get": "result", "getValue": "result", "getValues": "result", "fetch": "result", "fetchOne": "result",
    "fetchMap": "result", "fetchGroups": "result", "fetchSet": "result", "fetchValue": "result", "fetchOptional": "result",
    "fetchAny": "result", "into": "result", "intoMap": "result", "intoGroups": "result", "intoSet": "result",
    "fetchSingle": "result",
}
CHAIN_LOGIC = {"and", "or", "not", "andNot", "orNot", "andExists", "orExists", "andNotExists", "orNotExists"}
CHAIN_HEADS = {"where": "where", "on": "join_on", "having": "having", "using": "join_using", "whereExists": "where",
               "whereNotExists": "where"}
WRITE_LISTS = {"insertInto", "columns", "keys", "key"}


def first_arg_end(m, call):
    depth = 0
    for i in range(call.open + 1, call.close):
        c = m[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            return i
    return call.close


def op_of(m, calls, pos):
    """The clause `pos` sits in, or None when the occurrence is a write (a column list or a SET target)."""
    enclosing = sorted((c for c in calls if c.open < pos < c.close), key=lambda c: -c.open)
    for c in enclosing:
        n = c.name
        if n in WRITE_LISTS:
            return None
        if n == "set":
            return None if pos < first_arg_end(m, c) else "set_value"
        if n in CLAUSE:
            return CLAUSE[n]
        if n in CHAIN_LOGIC:
            sibs = [s for s in calls if s.parent is c.parent and s.close is not None and s.close < c.open and s.name in CHAIN_HEADS]
            if sibs:
                return CHAIN_HEADS[max(sibs, key=lambda s: s.close).name]
    return "expr"


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1



# ---------------------------------------------------------------- metadata keys
# jsonb keys that carry a collection LABEL, each with the writer that proves it (a key counted here is one a writer fills with a label):
#   origin_collection  chunks.metadata: the quarantine move writes it into every chunk it takes (vectors-077 gc_quarantine_orphans,
#                      gc_quarantine_orphans_bounded and reaper_quarantine_chunks as jsonb_build_object('origin_collection', ...);
#                      QuarantineOrigin.retagRowsOf's tag merge; RestoreIdFill.relabelOriginTag's jsonb_set on an import).
# Not labels: quarantined_at / quarantine_expires_at / reaper_quarantined_at (timestamps), quarantined_by (the actor 'engine-reaper'),
# co_discovered_by (a catalog_links actor); gc_audit.details carries an origin_collection too, but it is only written (QuarantineOrigin
# details.put, the gc_* audit producers) and no statement reads it back by key.
LABEL_KEYS = ("origin_collection",)
ID_KEYS = ("origin_collection_id",)       # the id twin of origin_collection: noted in the output, not a label read
JSONB_READ_FNS = {"jsonb_extract_path_text", "jsonb_extract_path", "jsonb_exists", "jsonb_object_field_text", "jsonb_object_field"}
STRING_CONSTS = {}                        # static final String NAME = "literal"; over every scanned file (the key often is one)
variable_key_reads = []                   # (file, line): a jsonb read whose key is neither a literal nor a known constant
id_key_reads = []                         # (file, line): a read of an ID_KEYS key
unfollowed_bindings = []                  # (file, line, var): a Field bound to a label read that a method returns or another file may use
for _p in sorted(JAVA.rglob("*.java")):
    for _mm in re.finditer(r"\bstatic\s+final\s+String\s+(\w+)\s*=\s*\"([^\"]*)\"\s*;", _p.read_text()):
        STRING_CONSTS[_mm.group(1)] = _mm.group(2)


def arg_spans(m, call):
    """(start, end) of each top-level argument of a call, in the masked text."""
    spans, depth, start = [], 0, call.open + 1
    for i in range(call.open + 1, call.close):
        c = m[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            spans.append((start, i))
            start = i + 1
    if m[start:call.close].strip():
        spans.append((start, call.close))
    return spans


def key_of(text):
    """The key a jsonb accessor's key argument names: a literal, a bare or qualified static final String constant, or None."""
    t = re.sub(r"^(?:DSL\.)?(?:val|inline)\(\s*(.*?)\s*\)$", r"\1", text.strip(), flags=re.S)
    mm = re.fullmatch(r"\"([^\"]*)\"", t)
    if mm:
        return mm.group(1)
    mm = re.fullmatch(r"(?:[\w.]*\.)?([A-Z][A-Z0-9_]*)", t)
    return STRING_CONSTS.get(mm.group(1)) if mm else None


def block_pairs(m):
    """[(open, close)] of every {...} of masked code."""
    out, stack = [], []
    for i, c in enumerate(m):
        if c == "{":
            stack.append(i)
        elif c == "}" and stack:
            out.append((stack.pop(), i))
    out += [(o, len(m)) for o in stack]
    return out


def scan_metadata_keys(p, f, code, m, calls, binds, bound):
    """The jsonb key reads of a file: accessor calls, the Fields they are bound to, and their uses (see the module docstring)."""
    blocks = block_pairs(m)
    reads_found = []   # (call, key, jsonb_arg_text)
    for c in calls:
        spans = arg_spans(m, c)
        if c.name in ("jsonbGetAttributeAsText", "jsonbGetAttribute") and len(spans) == 2:
            reads_found.append((c, key_of(code[spans[1][0]:spans[1][1]]), code[spans[0][0]:spans[0][1]]))
        elif c.name == "function" and len(spans) >= 4:
            fn = re.fullmatch(r"\s*\"([a-z_]+)\"\s*", code[spans[0][0]:spans[0][1]])
            if fn and fn.group(1) in JSONB_READ_FNS:
                reads_found.append((c, key_of(code[spans[3][0]:spans[3][1]]), code[spans[2][0]:spans[2][1]]))
    for c, key, jarg in reads_found:
        line = line_of(code, c.open)
        if key is None:
            variable_key_reads.append((f, line))
            continue
        if key in ID_KEYS:
            id_key_reads.append((f, line))
            continue
        if key not in LABEL_KEYS:
            continue
        table, note = "?", "the jsonb column is not bound to a table in this file"
        mm = re.fullmatch(r"\s*(?:[\w.]*\.)?([A-Za-z_]\w*)\.METADATA\s*", jarg)
        if mm:
            var = mm.group(1)
            b = None if var.isupper() else bound(var, c.open)
            if var.isupper() and var.lower() in LISTED:
                table, note = var.lower(), ""
            elif var.isupper():
                table, note = var.lower(), "not in p31-checks"
            elif b and b[1] == "alias":
                table, note = b[0], ""
        name_start = c.open
        while name_start > 0 and m[name_start - 1] in " \t\r\n":
            name_start -= 1
        name_start -= len(c.name)
        st_start = max(code.rfind(";", 0, name_start), code.rfind("{", 0, name_start), code.rfind("}", 0, name_start)) + 1
        # `Type var = [DSL.]accessor(...)[.chained(...)];`: the accessor sits at the top level of the declaration's right-hand side,
        # so var holds the key's value (or a cast / test of it); an accessor nested in a call's arguments does not bind the result
        head = m[st_start:name_start]
        decl = re.match(r"\s*(?:(?:final|static|private|public|protected)\s+)*[\w<>\[\].?, ]+?\s(\w+)\s*=(?!=)\s*(?:[\w$.]*\.)?$", head)
        col = "metadata." + key
        if decl:
            var = decl.group(1)
            vpos = st_start + decl.start(1)
            add(f, line, table, col, "accessor_def", "metadata_key", note)
            # the variable's scope: the whole class body for a class-level field, else from the declaration to the end of its block
            enc = min((b for b in blocks if b[0] < vpos < b[1]), key=lambda b: b[1] - b[0], default=(0, len(m)))
            header = m[max(0, max(m.rfind(";", 0, enc[0]), m.rfind("{", 0, enc[0]), m.rfind("}", 0, enc[0])) + 1):enc[0]]
            field_level = bool(re.search(r"\b(class|interface|record|enum)\b", header))
            if field_level and not re.search(r"\bprivate\b", m[st_start:vpos]):
                unfollowed_bindings.append((f, line, var))
            lo, hi = (enc[0], enc[1]) if field_level else (vpos, enc[1])
            for um in re.finditer(rf"(?<![\w$.]){re.escape(var)}(?![\w$(])", m[lo:hi]):
                upos = lo + um.start()
                if upos == vpos:
                    continue
                op = op_of(m, calls, upos)
                if op is not None:
                    add(f, line_of(code, upos), table, col, op, "bound", note)
        else:
            op = op_of(m, calls, c.open)
            if op is not None:
                if re.match(r"\s*return\b", m[st_start:name_start]) or re.search(r"\breturn\s*(?:DSL\.)?\s*$", m[st_start:name_start]):
                    unfollowed_bindings.append((f, line, "<returned>"))
                add(f, line, table, col, op, "metadata_key", note)


META_SQL = r"(?:(\w+)\.)?\w*metadata\w*"
META_SQL_FORMS = (
    rf"{META_SQL}\s*(?:->>|->)\s*'(\w+)'",
    rf"{META_SQL}\s*#>>?\s*'\{{(\w+)[,}}]",
    rf"jsonb_(?:extract_path(?:_text)?|exists|object_field(?:_text)?)\(\s*{META_SQL}\s*,\s*'(\w+)'",
    rf"{META_SQL}\s*\?\??\s*'(\w+)'",
)


def scan_metadata_sql_literals(f, code, lits):
    """A metadata key read written as SQL text inside a Java string literal (the literals of one statement joined, as the name-column scan does)."""
    for x, y, t in lits:
        for form in META_SQL_FORMS:
            for mm in re.finditer(form, t):
                key = mm.group(2)
                if key in ID_KEYS:
                    id_key_reads.append((f, line_of(code, x + 1 + mm.start())))
                    continue
                if key not in LABEL_KEYS:
                    continue
                aliases = sql_aliases(t)
                named = {tt.lower() for tt in re.findall(r"\bnexus\.([a-z_][a-z0-9_]*)", t, re.I)} | set(aliases.values())
                qual = (mm.group(1) or "").lower()
                cand = [aliases[qual]] if qual in aliases else sorted(named)
                ks = list(KW.finditer(t[:mm.start()]))
                op = KW_OP[re.sub(r"\s+", " ", ks[-1].group(1).upper())] if ks else "expr"
                add(f, line_of(code, x + 1 + mm.start()), cand[0] if len(cand) == 1 else "?", "metadata." + key, op, "sql_metadata_key",
                    "" if len(cand) == 1 else f"{len(cand)} candidate tables in the SQL text")

# ---------------------------------------------------------------- Java occurrences
rows = {}          # (file, line, table, column, op) -> set of via
notes = {}         # same key -> note
unresolved = []
skipped_accessors = collections.Counter()
files = sorted(JAVA.rglob("*.java"))


def add(f, line, table, col, op, via, note=""):
    key = (f, str(line), table, col, op)
    rows.setdefault(key, set()).add(via)
    if note:
        notes[key] = note
    if table == "?":
        unresolved.append((f, line, col, op, via))


def bindings(code):
    """[(pos, var, table)] of every alias and helper binding in a file, in source order."""
    out = []
    for mm in re.finditer(r"\b(\w+)\s*=\s*([A-Z][A-Z0-9_]*)\.as\(", code):
        out.append((mm.start(), mm.group(1), mm.group(2).lower(), "alias"))
    for mm in re.finditer(r"\b(\w+)\s*=\s*(?:[a-z][\w]*\.)*([A-Z][A-Z0-9_]*)\s*;", code):   # var d = CATALOG_DOCUMENTS; / = dev.x.Tables.CHUNKS;
        out.append((mm.start(), mm.group(1), mm.group(2).lower(), "alias"))
    # a parameter or local typed by the generated record class: CatalogDocumentChunks row
    for mm in re.finditer(r"\b([A-Z][a-z]\w*)\s+(\w+)\s*[,)=;]", code):
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", mm.group(1)).lower()
        if snake in LISTED:
            out.append((mm.start(), mm.group(2), snake, "alias"))
    for mm in re.finditer(r"\b(ChunkTable|CentroidTable)\s+(\w+)\b", code):
        out.append((mm.start(), mm.group(2), HELPER_DECL[mm.group(1)], "helper"))
    for mm in re.finditer(r"\b(\w+)\s*=\s*(?:DimTables\.)?(CHUNKS|CENTROIDS)\.get\(", code):
        out.append((mm.start(), mm.group(1), HELPER_GET[mm.group(2)], "helper"))
    return sorted(out)


def scan_java(p):
    raw = p.read_text()
    code = strip_comments(raw)
    f = p.relative_to(ROOT).as_posix()
    lits = literals(code)
    m = mask(code, lits)
    calls = call_tree(m)
    binds = bindings(m)

    def bound(var, pos):
        hit = None
        for bp, bv, bt, bk in binds:
            if bp > pos:
                break
            if bv == var:
                hit = (bt, bk)
        return hit

    # TABLE.COLUMN and alias.COLUMN
    for mm in re.finditer(r"(?:(?<![\w$.])|(?<=\bTables\.))([A-Za-z_][A-Za-z0-9_]*)\.([A-Z][A-Z0-9_]*)\b(?!\s*\()", m):   # also the fully qualified dev.nexus.service.jooq.nexus.Tables.X.NAME
        var, col = mm.group(1), mm.group(2)
        pos = mm.start(1)
        if var.isupper():
            # a generated constant: CHUNKS.COLLECTION, CATALOG_COLLECTIONS.NAME
            if col in NAME_CONST or (var == "CATALOG_COLLECTIONS" and col == "NAME"):
                t = var.lower()
                c = col.lower()
                if col == "NAME" and t != "catalog_collections":
                    continue
                op = op_of(m, calls, pos)
                if op is None:
                    continue
                add(f, line_of(code, pos), t, "name" if t == "catalog_collections" and c == "name" else c, op, "jooq",
                    "" if c in LISTED.get(t, ()) else "not in p31-checks")
        else:
            b = bound(var, pos)
            if not (b and b[1] == "alias") and col in NAME_CONST:
                # x.COLLECTION over a variable no `x = TABLE.as(..)` binds (a parameter, a derived table): never dropped
                op = op_of(m, calls, pos)
                if op is not None:
                    add(f, line_of(code, pos), "?", col.lower(), op, "alias", f"{var} is not bound to a table in this file")
                continue
            if b and b[1] == "alias" and (col in NAME_CONST or (col == "NAME" and b[0] == "catalog_collections")):
                op = op_of(m, calls, pos)
                if op is None:
                    continue
                c = col.lower()
                add(f, line_of(code, pos), b[0], c, op, "alias", "" if c in LISTED.get(b[0], ()) else "not in p31-checks")
    # ch.collection() / ct.collection() through a typed helper
    for mm in re.finditer(r"(?<![\w$.])([a-z_][A-Za-z0-9_]*)\.collection\(\)", m):
        var, pos = mm.group(1), mm.start(1)
        b = bound(var, pos)
        if b and b[1] == "helper":
            op = op_of(m, calls, pos)
            if op is None:
                continue
            add(f, line_of(code, pos), b[0], "collection", op, "helper")
        else:
            skipped_accessors[var] += 1
    # r.getSourceCollection() / getTargetCollection() / getCollection() / getPhysicalCollection() / getSupersededBy() on a variable typed by a generated
    # record class (ReembedJobsRecord r): the typed accessor of a name column, which the r.get(CONSTANT) rule never sees. A variable not typed by a
    # record class in this file (a lambda parameter) is counted as a skipped accessor, as x.collection() is.
    rec_binds = sorted((mm.start(), mm.group(2), re.sub(r"(?<!^)(?=[A-Z])", "_", mm.group(1)).lower())
                       for mm in re.finditer(r"\b([A-Z]\w*)Record\s+(\w+)\b", m))
    for mm in re.finditer(r"(?<![\w$.])([a-z_][A-Za-z0-9_]*)\.get(Collection|PhysicalCollection|SourceCollection|TargetCollection|SupersededBy)\(\)", m):
        var, pos = mm.group(1), mm.start(1)
        col = re.sub(r"(?<!^)(?=[A-Z])", "_", mm.group(2)).lower()
        hit = None
        for bp, bv, bt in rec_binds:
            if bp > pos:
                break
            if bv == var:
                hit = bt
        if hit and col in LISTED.get(hit, ()):
            op = op_of(m, calls, pos)
            if op is not None:
                add(f, line_of(code, pos), hit, col, "result" if op == "expr" else op, "record_getter")
        else:
            skipped_accessors["get:" + var] += 1
    # DSL.field("collection") / DSL.name("collection"), t.field("collection", String.class)
    for mm in re.finditer(r"(?:field|name)\(\s*(?:[^()\"]*,\s*)?(?:\"[^\"]*\"\s*,\s*)*\"(collection|physical_collection|source_collection|target_collection|superseded_by)\"", code):
        col, pos = mm.group(1), mm.start()
        op = op_of(m, calls, pos)
        if op is None:
            continue
        enc = [c for c in calls if c.open < pos < c.close and c.name in ("of",)]
        head = code[max(0, pos - 900):pos]
        dm = list(re.finditer(r"\b(ChunkTable|CentroidTable)\s+of\(", head))
        if p.name == "DimTables.java" and dm:
            add(f, line_of(code, pos), HELPER_DECL[dm[-1].group(1)], col, "accessor_def", "literal")
            continue
        # the tables the enclosing statement names
        st_start = max(code.rfind(";", 0, pos), code.rfind("{", 0, pos), code.rfind("}", 0, pos)) + 1
        st_end = code.find(";", pos)
        stmt = code[st_start:st_end if st_end > 0 else len(code)]
        tabs = {t.lower() for t in re.findall(r"\b([A-Z][A-Z0-9_]*)\b", mask(stmt, [])) if t.lower() in LISTED and col in LISTED[t.lower()]}
        tabs |= {HELPER_DECL[h] for h in re.findall(r"\b(ChunkTable|CentroidTable)\b", stmt) if col in LISTED[HELPER_DECL[h]]}
        if not tabs:
            near = [t.lower() for t in re.findall(r"DSL\.name\(\s*\"nexus\"\s*,\s*\"([a-z_][a-z0-9_]*)\"", code[max(0, pos - 1500):pos])]
            near = [t for t in near if t in LISTED and col in LISTED[t]]
            if near:
                tabs = {near[-1]}
        add(f, line_of(code, pos), next(iter(tabs)) if len(tabs) == 1 else "?", col, op, "literal",
            "" if len(tabs) == 1 else f"{len(tabs)} candidate tables in the statement")
    scan_metadata_keys(p, f, code, m, calls, binds, bound)
    scan_metadata_sql_literals(f, code, lits)
    scan_sql_strings(f, code, lits)


# ---------------------------------------------------------------- SQL strings
# SQL-shaped text (the engine's SQL is upper-case; lower-case prose such as 'select them from the collection' is not), not prose that happens to hold the word SELECT: a SELECT with its FROM, an UPDATE with its SET, a DELETE FROM, an INSERT INTO.
SQL_WORD = re.compile(r"\bSELECT\b[^;]{0,4000}?\bFROM\b|\bUPDATE\s+[\w.%]+(?:\s+\w+)?\s+SET\b|\bDELETE\s+FROM\b|\bINSERT\s+INTO\b|\bCREATE\s+(?:UNIQUE\s+)?INDEX\b", re.S)
KW = re.compile(r"\b(SELECT|FROM|JOIN|ON\s+CONFLICT|ON|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|SET|RETURNING|USING)\b", re.I)
KW_OP = {"SELECT": "select", "FROM": "from", "JOIN": "join_on", "ON": "join_on", "WHERE": "where", "GROUP BY": "group_by",
         "ORDER BY": "order_by", "HAVING": "having", "SET": "set_value", "RETURNING": "returning", "USING": "join_using",
         "ON CONFLICT": "on_conflict"}
SQL_NAME_WORDS = [c for c in NAME_COLS if c != "name"]


def sql_reads(stmt):
    """(offset, column, qualifier) of each name-column read in a SQL text: P3.5b's rules, shorter (INSERT lists and SET targets are writes)."""
    spans = []
    for mm in re.finditer(r"\bINSERT\s+INTO\s+[\w.%]+(?:\s+(?:AS\s+)?\w+)?\s*\(", stmt, re.I):
        depth = 0
        for i in range(mm.end() - 1, len(stmt)):
            depth += {"(": 1, ")": -1}.get(stmt[i], 0)
            if depth == 0:
                spans.append((mm.end() - 1, i + 1))
                break
    for mm in re.finditer(r"\bSET\s+(\w+(?:\.\w+)?)\s*=", stmt, re.I):
        spans.append((mm.start(1), mm.end(1)))
    inside = lambda p: any(a <= p < b for a, b in spans)
    out = []
    for c in SQL_NAME_WORDS:
        for mm in re.finditer(rf"(?<![\w$%:])(?:(\w+)\.)?{c}(?![\w$%(])", stmt):
            q = mm.end() - len(c)
            if not inside(q) and not re.search(r"\bAS\s+$", stmt[max(0, q - 8):q], re.I):
                out.append((q, c, mm.group(1)))
    if re.search(r"\bcatalog_collections\b", stmt, re.I):
        for mm in re.finditer(r"(?<![\w$])([a-z_][a-z0-9_]*)\.name(?![\w$])", stmt, re.I):
            if not inside(mm.start()):
                out.append((mm.start(), "name", mm.group(1)))
    return sorted(set(out), key=lambda t: (t[0], t[1], t[2] or ""))


def sql_aliases(text):
    """alias -> table of every FROM / JOIN / UPDATE <table> [AS] <alias> in the SQL text."""
    out = {}
    for mm in re.finditer(r"\b(?:FROM|JOIN|UPDATE)\s+(?:nexus\.)?([a-z_][a-z0-9_]*)(?:\s+(?:AS\s+)?([a-z_][a-z0-9_]*))?", text, re.I):
        t, a = mm.group(1).lower(), (mm.group(2) or "").lower()
        out[t] = t
        if a and a not in SQL_STOP:
            out[a] = t
    return out


SQL_STOP = {"where", "join", "on", "left", "right", "inner", "outer", "cross", "using", "group", "order", "limit", "union", "set",
            "values", "returning", "as", "and", "or", "full", "natural", "lateral", "for", "having", "window", "offset", "except",
            "intersect", "select", "from", "into", "when", "then", "do"}


def scan_sql_strings(f, code, lits):
    # group literals by the statement they sit in (up to ';' or '{' / '}' outside literals)
    bounds, i, n = [], 0, len(code)
    lit_at = {a: b for a, b, _ in lits}
    start = 0
    while i < n:
        if i in lit_at:
            i = lit_at[i]
            continue
        if code[i] in ";{}":
            bounds.append((start, i))
            start = i + 1
        i += 1
    bounds.append((start, n))
    for a, b in bounds:
        seg = [(x, y, t) for x, y, t in lits if a <= x < b]
        if not seg:
            continue
        # the literals of one statement, joined with a space, each remembering its source offset; SQL -- comments blanked
        text, spans = "", []
        for x, y, t in seg:
            body = re.sub(r"--[^\n]*", lambda mm: " " * len(mm.group(0)), t) if code.startswith('"""', x) else t
            spans.append((len(text), len(text) + len(body), x + (3 if code.startswith('"""', x) else 1)))
            text += body + " "
        if not SQL_WORD.search(text):
            continue
        aliases = sql_aliases(text)
        named = {t.lower() for t in re.findall(r"\bnexus\.([a-z_][a-z0-9_]*)", text, re.I)} | set(aliases.values())
        # a table named by DimTables' constant, concatenated into the text (ChashRepository.PROBE_SQL)
        if "CHUNKS_TABLE_NAME" in code[a:b]:
            named.add("chunks")
        if "CENTROIDS_TABLE_NAME" in code[a:b]:
            named.add("taxonomy_centroids")
        for q, col, qual in sql_reads(text):
            s0, _e0, x0 = next(((s_, e_, x_) for s_, e_, x_ in spans if s_ <= q < e_), spans[0])
            src_pos = x0 + (q - s0)
            ks = list(KW.finditer(text[:q]))
            op = KW_OP[re.sub(r"\s+", " ", ks[-1].group(1).upper())] if ks else "expr"
            carries = lambda t: (t == "catalog_collections") if col == "name" else (col in LISTED.get(t, ()) or t in ("collection_vector_stats", "live_chunks"))
            if qual and qual.lower() in aliases:
                cand = [aliases[qual.lower()]] if carries(aliases[qual.lower()]) else []
            else:
                cand = sorted(t for t in named if carries(t))
            add(f, line_of(code, src_pos), cand[0] if len(cand) == 1 else "?", col, op, "sql",
                "" if len(cand) == 1 else f"{len(cand)} candidate tables in the SQL text")


for p in files:
    scan_java(p)

# ---------------------------------------------------------------- output
HEAD = ("file", "line", "table", "name_column", "op", "via", "note")
body = sorted(rows, key=lambda k: (k[0], int(k[1]), k[2], k[3], k[4]))
text = "# RDR-229 P3.5c (nexus-p2m1x.25): every Java read of a collection name column in service/src/main/java.\n"
text += "# Generated by scripts/rdr229_p35c_java_reader_census.py (a drift check: --check). One row per (file, line, table, name_column, op).\n"
text += "# op is the clause the column sits in (where, join_on, select, group_by, order_by, on_conflict, ...); via says how it was found\n"
text += "# (jooq constant, alias, helper ch/ct, literal, sql string). table ? is UNRESOLVED (named in the script's output). Writes are P3.1's.\n"
text += "# The two upserts the RDR names are the on_conflict rows of PgVectorRepository (chunks) and TaxonomyCentroidRepository (taxonomy_centroids).\n"
text += "\t".join(HEAD) + "\n"
for k in body:
    text += "\t".join(k[:5] + ("+".join(sorted(rows[k])), notes.get(k, "-"))) + "\n"

if __name__ == "__main__":
    by_table = collections.Counter(k[2] for k in body)
    by_op = collections.Counter(k[4] for k in body)
    by_col = collections.Counter(k[3] for k in body)
    print("rows", len(body), "files", len({k[0] for k in body}), "of", len(files), "scanned")
    print("table", dict(sorted(by_table.items(), key=lambda kv: -kv[1])))
    print("op", dict(sorted(by_op.items(), key=lambda kv: -kv[1])))
    print("name_column", dict(sorted(by_col.items(), key=lambda kv: -kv[1])))
    print("via", dict(collections.Counter(v for vs in rows.values() for v in vs)))
    print("on_conflict rows", [(k[0].split("/")[-1], k[1], k[2]) for k in body if k[4] == "on_conflict"])
    print("record accessors skipped (x.collection() with no ChunkTable/CentroidTable binding)", sum(skipped_accessors.values()),
          dict(skipped_accessors.most_common(8)))
    print("metadata keys", {k: sum(1 for r in body if r[3] == "metadata." + k) for k in LABEL_KEYS}, "id-key reads (not rows)", len(set(id_key_reads)),
          "variable-key reads (not rows)", len(set(variable_key_reads)), sorted({f.split("/")[-1] for f, _ in variable_key_reads}))
    for f, line, var in sorted(set(unfollowed_bindings)):
        print("NOT FOLLOWED", f, line, var)
    for f, line, col, op, via in sorted(set(unresolved)):
        print("UNRESOLVED", f, line, col, op, via)
    print("unresolved", len(set(unresolved)))
    if "--write" in sys.argv:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text)
    if "--check" in sys.argv:
        sys.exit(0 if OUT.exists() and OUT.read_text() == text else 1)
