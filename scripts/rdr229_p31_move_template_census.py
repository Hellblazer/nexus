"""RDR-229 P3.1 fix round B (nexus-p2m1x.17): the census of registered data-move templates.

A NOT VALID CHECK is evaluated on every row version a statement writes, so a registered move whose template UPDATEs (or INSERTs
into) a table that carries a P3.1 CHECK, without setting that table's collection id columns, fails 23514 on any row the stamp has
not reached. The worker classes 23514 permanent, so the move fails for good. rdr229_p31_writer_census.py covers Java sites and SQL
functions; this module covers the third writer, the move template.

It reads every ``INSERT INTO nexus.data_moves (<columns>) VALUES (...), (...)`` of the changelog tree, at top level and inside an
``on_complete`` text (a move that registers a move), by the columns it names, and for each move records the label, the driving table
and the columns its template writes (the SET targets of an UPDATE, the column list of an INSERT). A template is a VIOLATION when its
driving table is guarded by a P3.1 CHECK (db/rdr229/p31-checks.tsv, the same list) and it does not write every id column of that
table, unless another registered move names it in ``supersedes``: a superseded move is never run, so its template writes nothing.

What it does not read: a move the engine registers from Java (none exists yet; P3.3b and later add some, and each must be added here), and a
template whose SET is built by anything but literal text.
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass

CHANGELOG_DIR = "service/src/main/resources/db/changelog"
MASTER = "db.changelog-master.xml"
KIND = "data_move"


@dataclass(frozen=True)
class Move:
    label: str
    table: str            # the driving table (schema-less)
    file: str             # changelog file name
    line: int             # the INSERT's line
    template_line: int    # the template literal's first line
    op: str               # update | insert | other
    written: tuple[str, ...]   # columns the template writes (SET targets, or the INSERT column list)
    supersedes: str | None


# ---------------------------------------------------------------- the guarded tables

def read_guarded(checks: pathlib.Path) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """table -> (name columns, id columns) for every table the CHECK list guards (a NAME_ID, SUPERSEDED or NOT_NULL row)."""
    names: dict[str, list[str]] = {}
    ids: dict[str, list[str]] = {}
    for line in checks.read_text().splitlines():
        if not line or line.startswith("#") or line.startswith("table\t"):
            continue
        table, name_col, id_col, _constraint, kind = line.split("\t")
        if kind not in ("NAME_ID", "SUPERSEDED", "NOT_NULL"):
            continue
        ids.setdefault(table, [])
        names.setdefault(table, [])
        if id_col not in ids[table]:
            ids[table].append(id_col)
        if name_col != "-" and name_col not in names[table]:
            names[table].append(name_col)
    return {t: (tuple(names[t]), tuple(ids[t])) for t in ids}


# ---------------------------------------------------------------- reading the text

def _blank(m: re.Match) -> str:
    return re.sub(r"[^\n]", " ", m.group(0))


def strip_noise(text: str) -> str:
    """Blank XML comments, <comment> elements and <rollback> blocks, keeping every offset and newline."""
    text = re.sub(r"<!--.*?-->", _blank, text, flags=re.S)
    text = re.sub(r"<comment>.*?</comment>", _blank, text, flags=re.S)
    return re.sub(r"<rollback>.*?</rollback>", _blank, text, flags=re.S)


_DOLLAR = re.compile(r"\$(\w*)\$")


def _skip_string(text: str, i: int) -> int | None:
    """If a quoted string starts at i, the index just past it; else None. Single quotes double to escape; dollar quotes close on the same tag."""
    c = text[i]
    if c == "'":
        j = i + 1
        while j < len(text):
            if text[j] == "'":
                if text.startswith("''", j):
                    j += 2
                    continue
                return j + 1
            j += 1
        return len(text)
    if c == "$":
        m = _DOLLAR.match(text, i)
        if m and (m.group(1) == "" or not m.group(1)[0].isdigit()):
            end = text.find(m.group(0), m.end())
            return len(text) if end < 0 else end + len(m.group(0))
    return None


def _split_list(text: str, i: int) -> tuple[list[tuple[int, int]], int]:
    """From the '(' at i: the (start, end) span of each top-level comma-separated item, and the index just past the closing ')'."""
    assert text[i] == "("
    items: list[tuple[int, int]] = []
    depth, j, start = 1, i + 1, i + 1
    while j < len(text):
        skipped = _skip_string(text, j)
        if skipped is not None:
            j = skipped
            continue
        c = text[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                items.append((start, j))
                return items, j + 1
        elif c == "," and depth == 1:
            items.append((start, j))
            start = j + 1
        j += 1
    return items, len(text)


def _literal(tok: str) -> str | None:
    """The text of a quoted literal, or None for NULL, or the bare token."""
    tok = tok.strip()
    if tok.upper() == "NULL":
        return None
    if tok.startswith("'") and tok.endswith("'"):
        return tok[1:-1].replace("''", "'")
    m = _DOLLAR.match(tok)
    if m and tok.endswith(m.group(0)) and len(tok) >= 2 * len(m.group(0)):
        return tok[len(m.group(0)):-len(m.group(0))]
    return tok


# ---------------------------------------------------------------- the template

def _top_level_split(s: str) -> list[str]:
    out, depth, start, i = [], 0, 0, 0
    while i < len(s):
        skipped = _skip_string(s, i)
        if skipped is not None:
            i = skipped
            continue
        c = s[i]
        depth += {"(": 1, ")": -1}.get(c, 0)
        if c == "," and depth == 0:
            out.append(s[start:i])
            start = i + 1
        i += 1
    out.append(s[start:])
    return out


def _keyword_at_depth0(s: str, words: tuple[str, ...]) -> int:
    """Index of the first of ``words`` (as a whole word) at paren depth 0 outside strings, or len(s)."""
    depth, i = 0, 0
    pat = re.compile(r"(" + "|".join(words) + r")\b", re.I)
    while i < len(s):
        skipped = _skip_string(s, i)
        if skipped is not None:
            i = skipped
            continue
        c = s[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and (i == 0 or not (s[i - 1].isalnum() or s[i - 1] == "_")) and pat.match(s, i):
            return i
        i += 1
    return len(s)


def parse_template(template: str) -> tuple[str, tuple[str, ...]]:
    """(op, written columns) of a move template: ``UPDATE <driving_table> SET a = .., b = .. FROM|WHERE ..``
    or ``INSERT INTO <driving_table> (a, b) ..``."""
    t = template.strip()
    m = re.match(r"UPDATE\s+\S+(?:\s+(?:AS\s+)?\w+)?\s+SET\b", t, re.I)
    if m:
        body = t[m.end():]
        body = body[: _keyword_at_depth0(body, ("FROM", "WHERE", "RETURNING"))]
        cols = []
        for assign in _top_level_split(body):
            lhs = assign.split("=", 1)[0].strip().strip('"')
            lhs = lhs.rsplit(".", 1)[-1]
            if lhs:
                cols.append(lhs)
        return "update", tuple(cols)
    m = re.match(r"INSERT\s+INTO\s+\S+\s*\(", t, re.I)
    if m:
        items, _ = _split_list(t, m.end() - 1)
        return "insert", tuple(t[a:b].strip().strip('"') for a, b in items)
    return "other", ()


_INSERT = re.compile(r"INSERT\s+INTO\s+nexus\.data_moves\s*\(", re.I)


def parse_moves(text: str, filename: str) -> list[Move]:
    """Every move the text registers, in textual order: a top-level INSERT and one nested in an ``on_complete``."""
    code = strip_noise(text)
    moves: list[Move] = []
    for m in _INSERT.finditer(code):
        cols_items, after = _split_list(code, m.end() - 1)
        columns = [code[a:b].strip().strip('"') for a, b in cols_items]
        v = re.compile(r"\s*VALUES\s*", re.I).match(code, after)
        if not v:
            continue
        pos = v.end()
        while pos < len(code) and code[pos] == "(":
            items, pos = _split_list(code, pos)
            if len(items) != len(columns):
                break
            row = {c: (a, b) for c, (a, b) in zip(columns, items)}
            lit = {c: _literal(code[a:b]) for c, (a, b) in row.items()}
            if lit.get("label") and lit.get("template") is not None:
                op, written = parse_template(lit["template"])
                a, b = row["template"]
                first = a + (len(code[a:b]) - len(code[a:b].lstrip()))
                moves.append(Move(
                    label=lit["label"], table=lit.get("driving_table") or "", file=filename,
                    line=code.count("\n", 0, m.start()) + 1, template_line=code.count("\n", 0, first) + 1,
                    op=op, written=written, supersedes=lit.get("supersedes")))
            sep = re.compile(r"\s*,\s*").match(code, pos)
            if not sep:
                break
            pos = sep.end()
    return moves


def parse_tree(root: pathlib.Path, skip: tuple[str, ...] = ()) -> list[Move]:
    """Every move the master changelog's includes register, in include order; ``skip`` names include files left out (a what-if)."""
    base = root / CHANGELOG_DIR
    master = re.sub(r"<!--.*?-->", "", (base / MASTER).read_text(), flags=re.S)
    moves: list[Move] = []
    for inc in re.findall(r'<include\s+file="([^"]+)"', master):
        name = inc.split("/")[-1]
        path = root / "service/src/main/resources" / inc
        if name in skip or not path.exists():
            continue
        moves.extend(parse_moves(path.read_text(), name))
    return moves


# ---------------------------------------------------------------- the verdict

# Moves the census cannot show are safe by their template alone, with the reason each is. An entry that no longer names a registered move is
# itself an error (tests/test_rdr229_p31_move_template_census.py), so the list cannot outlive what it excuses.
HANDLED: dict[str, str] = {
    "repair-catalog-collection-model-version":
        "D2's repair UPDATEs model_version of registry rows, tombstones included; vectors-068-19 adds the superseded_by_id CHECK only while no "
        "tombstone with a blank model_version and no id remains (its CONTINUE precondition), so the CHECK is absent while this move is open",
    "rdr229-reown-repo-stems-from-curator":
        "the template is a data-modifying WITH, not an UPDATE ... SET; it selects only live registry rows (c.superseded_by = ''), whose "
        "superseded_by_id is NULL, so the CHECK holds for every row version it writes (vectors-067)",
}


def violations(moves: list[Move], guarded: dict) -> list[tuple[Move, str]]:
    """The moves whose template writes a guarded table without every one of its id columns (or whose shape the scan cannot read), that no
    registered move supersedes and that HANDLED does not excuse."""
    superseded = {m.supersedes for m in moves if m.supersedes}
    out = []
    for m in moves:
        if m.table not in guarded or m.label in superseded or m.label in HANDLED:
            continue
        _names, ids = guarded[m.table]
        if m.op == "other":
            out.append((m, f"template over guarded table {m.table} is neither UPDATE .. SET nor INSERT: the scan cannot see what it writes; "
                           f"review it against the CHECK and add the label to HANDLED with the reason"))
            continue
        missing = [c for c in ids if c not in m.written]
        if missing:
            out.append((m, f"{m.op} of guarded table {m.table} does not set {', '.join(missing)} "
                           f"(a row with a name and no id fails the NOT VALID CHECK with 23514; supersede the move or set the id)"))
    return out


def census_rows(moves: list[Move], guarded: dict) -> list[tuple]:
    """TSV rows in rdr229_p31_writer_census.py's ten columns, one per move whose driving table is a census table."""
    superseded = {m.supersedes: m.label for m in moves if m.supersedes}
    bad = {m.label: why for m, why in violations(moves, guarded)}
    rows = []
    for m in moves:
        if m.table not in guarded and m.table not in HISTORY_TABLES:
            continue
        names, ids = guarded.get(m.table, ((), ()))
        written_names = [c for c in names if c in m.written]
        if m.label in bad:
            note = "VIOLATION: " + bad[m.label]
        elif m.label in superseded:
            note = f"superseded by {superseded[m.label]}"
        elif m.label in HANDLED:
            note = "handled: " + HANDLED[m.label]
        elif m.table in guarded:
            note = "sets " + ",".join(ids)
        else:
            note = ""
        rows.append((FAMILY.get(m.table, "ops"), m.table, f"{m.label} [{m.file}:{m.template_line}]", m.line, m.op, KIND,
                     ",".join(written_names) or "-", ",".join(ids) or "-", "yes" if m.table in guarded else "no", note))
    return rows


# the census tables with no CHECK (history); a move over one is listed for completeness
HISTORY_TABLES = {"relevance_log", "search_telemetry", "hook_failures", "gc_audit"}
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
