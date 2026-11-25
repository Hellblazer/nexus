# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.5c (nexus-p2m1x.25): the committed Java reader census is a fresh run of its script.

``service/src/test/resources/rdr229/p35c-java-reader-census.tsv`` lists every read of a collection name column in
``service/src/main/java`` (a jOOQ constant, an alias, a ``ch.collection()`` helper, a literal field name, a SQL string), with
the clause it sits in. P3.5c switches each to the id from gate 3 on, so its surface must be known to be complete. A reader
added or moved without a fresh run fails here. An occurrence the script cannot resolve is a row with table ``?`` and an
UNRESOLVED line in its output; the test pins that the two agree, so none is dropped.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_SCRIPT = _REPO / "scripts" / "rdr229_p35c_java_reader_census.py"
_RES = _REPO / "service" / "src" / "test" / "resources" / "rdr229"
_CENSUS = _RES / "p35c-java-reader-census.tsv"
_EXCLUDED = _RES / "p35c-java-reader-census-excluded.tsv"
_DISPOSITIONS = _RES / "p35c-java-reader-dispositions.tsv"
_JAVA = "service/src/main/java/dev/nexus/service/"

_NAME_COLUMNS = {"collection", "physical_collection", "source_collection", "target_collection", "superseded_by", "name"}
_OPS = {"where", "join_on", "join_using", "select", "group_by", "order_by", "having", "on_conflict", "returning", "set_value",
        "result", "values", "expr", "accessor_def", "partition_by", "distinct_on", "from"}
_LABEL_KEYS = ("origin_collection",)   # the jsonb keys that carry a collection label (the script's LABEL_KEYS, with its writer proof)
_HEADER = ["file", "line", "table", "name_column", "op", "via", "note"]
# Floors a working scan clears by a wide margin (the run that wrote the file found 617 rows in 49 of 388 files, 22 tables).
_MIN_ROWS, _MIN_FILES, _MIN_TABLES = 400, 30, 15


def _rows() -> list[list[str]]:
    lines = [ln for ln in _CENSUS.read_text().splitlines() if ln and not ln.startswith("#")]
    assert lines[0].split("\t") == _HEADER
    return [ln.split("\t") for ln in lines[1:]]


def _run(*flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(_SCRIPT), str(_REPO), *flags], capture_output=True, text=True, timeout=300)


def test_the_committed_census_is_a_fresh_run():
    proc = _run("--check")
    assert proc.returncode == 0, (
        "the committed Java reader census differs from a fresh run; regenerate it with "
        f"`python3 scripts/rdr229_p35c_java_reader_census.py . --write`:\n{proc.stdout[-2000:]}{proc.stderr[-1000:]}"
    )


def test_the_census_is_not_vacuous():
    rows = _rows()
    assert len(rows) >= _MIN_ROWS, f"only {len(rows)} census rows: the script stopped seeing the readers"
    assert len({r[0] for r in rows}) >= _MIN_FILES
    tables = {r[2] for r in rows if r[2] != "?"}
    assert len(tables) >= _MIN_TABLES, sorted(tables)
    # the clauses the RDR's item 6 names must each be seen at least once
    assert {"where", "join_on", "select", "group_by", "order_by", "on_conflict"} <= {r[4] for r in rows}
    # the files the stamp's readers live in
    files = {r[0] for r in rows}
    for f in ("db/CatalogRepository.java", "db/TaxonomyRepository.java", "vectors/PgVectorRepository.java",
              "vectors/TaxonomyCentroidRepository.java"):
        assert _JAVA + f in files, f


def test_the_two_upserts_are_on_conflict_rows():
    on_conflict = {(r[0], r[2], r[3]) for r in _rows() if r[4] == "on_conflict"}
    # RDR item 6, P3.5c: PgVectorRepository's chunk upsert and TaxonomyCentroidRepository's centroid upsert take the id key as
    # their conflict target from gate 3; today each names the name key (ch.collection() / ct.collection()).
    assert (_JAVA + "vectors/PgVectorRepository.java", "chunks", "collection") in on_conflict
    assert (_JAVA + "vectors/TaxonomyCentroidRepository.java", "taxonomy_centroids", "collection") in on_conflict


def test_every_unresolved_occurrence_is_a_row_and_a_printed_line():
    printed = {tuple(ln.split()[1:5]) for ln in _run().stdout.splitlines() if ln.startswith("UNRESOLVED ")}
    in_file = {(r[0], r[1], r[3], r[4]) for r in _rows() if r[2] == "?"}
    assert printed == in_file, f"printed only: {sorted(printed - in_file)[:5]}; in the file only: {sorted(in_file - printed)[:5]}"
    assert printed, "a run with no unresolved occurrence at all means the resolver stopped asking (the census holds dynamic tables)"


def test_a_write_is_not_a_read_and_every_row_is_well_formed():
    for r in _rows():
        assert len(r) == len(_HEADER), r
        assert r[3] in _NAME_COLUMNS or r[3] in {"metadata." + k for k in _LABEL_KEYS}, r
        assert r[4] in _OPS, r
        assert r[1].isdigit(), r
        assert (_REPO / r[0]).is_file(), r


_VOCABULARY = {"id", "both", "label-kept", "parse-removed", "n-a"}


def _row_object(r: list[str]) -> str:
    return f"{r[0].removeprefix(_JAVA)} {r[2]} {r[4]}"


def _dispositions() -> dict[tuple[str, str, str], tuple[str, str]]:
    lines = [ln for ln in _DISPOSITIONS.read_text().splitlines() if ln and not ln.startswith("#")]
    assert lines[0].split("\t") == ["kind", "object", "name_column", "disposition", "reason"]
    out: dict[tuple[str, str, str], tuple[str, str]] = {}
    for ln in lines[1:]:
        kind, obj, col, disposition, reason = (ln.split("\t") + [""] * 5)[:5]
        assert (kind, obj, col) not in out, f"two disposition rows for {kind} {obj} {col}"
        out[(kind, obj, col)] = (disposition, reason.strip())
    return out


def test_every_census_reader_has_a_disposition_and_every_disposition_a_reader():
    readers = {("java", _row_object(r), r[3]) for r in _rows()}
    disp = _dispositions()
    missing = sorted(readers - disp.keys())
    stale = sorted(disp.keys() - readers)
    assert not missing, f"{len(missing)} census readers have no disposition, e.g. {missing[:10]}"
    assert not stale, f"{len(stale)} dispositions name no census reader: {stale[:10]}"


def _rule_breaches(dispositions) -> list[str]:
    out = []
    for (kind, obj, col), (disposition, reason) in sorted(dispositions.items()):
        where = f"{kind} {obj} {col}"
        if kind != "java":
            out.append(f"{where}: kind is always 'java'")
        elif disposition not in _VOCABULARY:
            out.append(f"{where}: {disposition!r} is not one of {sorted(_VOCABULARY)}")
        elif disposition == "label-kept" and col != "name" and not col.startswith("metadata."):
            out.append(f"{where}: label-kept is only for a read of the registry label, catalog_collections.name (linda 2026-11-22)")
        elif disposition == "label-kept" and col.startswith("metadata.") and not (reason.startswith("VIOLATING") or "nexus-p2m1x.35" in reason):
            out.append(f"{where}: label-kept on a metadata key needs the R1 reaper reason (nexus-p2m1x.35) or a VIOLATING reason (linda 2026-11-24)")
        elif disposition == "n-a" and not col.startswith("metadata."):
            out.append(f"{where}: n-a is only for a metadata-key row (name_column metadata.<key>), never a column reader (linda 2026-11-24)")
        elif disposition == "n-a" and not reason:
            out.append(f"{where}: n-a needs a reason")
        elif disposition == "label-kept" and not reason:
            out.append(f"{where}: label-kept needs a one-line reason")
        elif disposition == "both" and not reason:
            out.append(f"{where}: both needs a one-line reason (which tables the stamp may not have reached)")
        elif disposition == "parse-removed":
            out.append(f"{where}: parse-removed does not apply to a Java reader (the census has no parse column; P3.5a owns name parsing)")
    return out


def test_each_disposition_keeps_its_rule():
    assert _rule_breaches(_dispositions()) == []


def test_the_rules_refuse_what_they_name():
    bad = {
        ("java", "db/X.java chunks where", "collection"): ("label-kept", "a reason"),    # label-kept off the registry label
        ("java", "db/X.java catalog_collections where", "name"): ("label-kept", ""),     # no reason
        ("java", "db/X.java chunks select", "collection"): ("rename", ""),               # not in the vocabulary
        ("java", "db/X.java chunks join_on", "collection"): ("both", ""),                # both with no reason
        ("java", "db/X.java chunks group_by", "collection"): ("parse-removed", ""),      # no parse in a Java census
        ("sql", "db/X.java chunks order_by", "collection"): ("id", ""),                  # kind is java
        ("java", "db/X.java chunks having", "collection"): ("n-a", "a reason"),          # n-a on a column reader
        ("java", "db/X.java chunks values", "metadata.origin_collection"): ("n-a", ""),  # n-a with no reason
        ("java", "db/X.java chunks returning", "metadata.origin_collection"): ("label-kept", "no ruling cited"),  # label-kept on a key, no R1 or VIOLATING reason
    }
    assert len(_rule_breaches(bad)) == len(bad)
    good = {("java", "db/X.java catalog_collections where", "name"): ("label-kept", "the user's label, not a key"),
            ("java", "db/X.java chunks where", "collection"): ("id", ""),
            ("java", "db/X.java topics where", "collection"): ("both", "the stamp may not have reached topics"),
            ("java", "db/X.java chunks expr", "metadata.origin_collection"): ("n-a", "import maps a file's labels to fresh ids, no live read"),
            ("java", "db/X.java chunks select", "metadata.origin_collection"): ("label-kept", "R1 reaper ruling (nexus-p2m1x.35): holds until P4.1"),
            ("java", "db/X.java chunks result", "metadata.origin_collection"): ("label-kept", "VIOLATING: a label match; P3.7 (taggedWith by id) switches it to id")}
    assert _rule_breaches(good) == []


# ---- the metadata-key reads (nexus-p2m1x.25): the scanner on a planted tree ------------------------------------------------

_PLANTED = '''package dev.nexus.service;
import org.jooq.*;
import org.jooq.impl.DSL;
import static dev.nexus.service.jooq.nexus.Tables.*;
class Planted {
    static final String TAG_KEY = "origin_collection";
    static final String ID_KEY = "origin_collection_id";
    private static final Field<String> TAG = DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, TAG_KEY);
    Condition viaConstant(String o) { return TAG.eq(o); }
    Condition viaLocal(DSLContext ctx, String o) {
        Field<String> local = DSL.jsonbGetAttribute(CHUNKS.METADATA, "origin_collection").cast(String.class);
        return local.eq(o);
    }
    void direct(DSLContext ctx, String o) {
        ctx.selectOne().from(CHUNKS).where(DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "origin_collection").eq(o)).fetch();
        ctx.selectOne().from(CHUNKS).where(DSL.function("jsonb_extract_path_text", String.class, CHUNKS.METADATA, DSL.val("origin_collection")).eq(o)).fetch();
        ctx.fetch("SELECT 1 FROM nexus.chunks c WHERE c.metadata ->> 'origin_collection' = ?", o);
        ctx.fetch("SELECT 1 FROM nexus.chunks c WHERE jsonb_exists(c.metadata, 'origin_collection')");
    }
    void notLabels(DSLContext ctx, String key) {
        DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "title");                       // another key
        DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, ID_KEY);                        // the id twin: counted, never a row
        DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, key);                           // a variable key: counted, never a row
        DSL.val(new String[] {"origin_collection"});                                 // a jsonb_set path, a write
        ctx.fetch("SELECT 1 FROM nexus.chunks c WHERE c.metadata ->> 'origin_collection_id' IS NULL");
    }
}
'''


def _planted_run(tmp_path):
    java = tmp_path / "service/src/main/java/dev/nexus/service"
    java.mkdir(parents=True)
    (java / "Planted.java").write_text(_PLANTED)
    checks = tmp_path / "service/src/main/resources/db/rdr229"
    checks.mkdir(parents=True)
    (checks / "p31-checks.tsv").write_text((_REPO / "service/src/main/resources/db/rdr229/p31-checks.tsv").read_text())
    proc = subprocess.run([sys.executable, str(_SCRIPT), str(tmp_path), "--write"], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-1500:]
    out = tmp_path / "service/src/test/resources/rdr229/p35c-java-reader-census.tsv"
    rows = [ln.split("\t") for ln in out.read_text().splitlines() if ln and not ln.startswith("#")][1:]
    return proc.stdout, [r for r in rows if r[3].startswith("metadata.")]


def test_the_scanner_finds_each_form_of_a_metadata_key_read(tmp_path):
    stdout, rows = _planted_run(tmp_path)
    got = {(int(r[1]), r[4], r[5]) for r in rows}
    lines = {n: ln for n, ln in enumerate(_PLANTED.splitlines(), 1)}
    at = lambda needle: [n for n, ln in lines.items() if needle in ln]
    assert all(r[3] == "metadata.origin_collection" and r[2] == "chunks" for r in rows), rows
    # a Field bound to the read (a class constant resolved through TAG_KEY, and a local): the declaration and every use
    (decl,) = at("private static final Field<String> TAG")
    (use,) = at("return TAG.eq")
    (loc,) = at("Field<String> local")
    (loc_use,) = at("return local.eq")
    assert (decl, "accessor_def", "metadata_key") in got and (use, "expr", "bound") in got
    assert (loc, "accessor_def", "metadata_key") in got and (loc_use, "expr", "bound") in got   # a cast of the read still binds
    # a direct accessor, the extract_path function, and the two SQL-string forms
    (acc,) = at(".where(DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, \"origin_collection\")")
    (fn,) = at('DSL.function("jsonb_extract_path_text"')
    (arrow,) = at("c.metadata ->> 'origin_collection' = ?")
    (exists,) = at("jsonb_exists(c.metadata, 'origin_collection')")
    assert (acc, "where", "metadata_key") in got and (fn, "where", "metadata_key") in got
    assert (arrow, "where", "sql_metadata_key") in got and (exists, "where", "sql_metadata_key") in got
    # not a row: another key, the id twin, a variable key, a jsonb_set path, the id key inside a SQL string
    flagged = {r[1] for r in rows}
    for needle in ('"title"', "ID_KEY)", "key);", "new String[]", "origin_collection_id' IS NULL"):
        assert not (set(map(str, at(needle))) & flagged), needle
    assert "id-key reads (not rows) 2" in stdout and "variable-key reads (not rows; the sidecar lists them) 1" in stdout and "UNREVIEWED variable-key read" in stdout, stdout


# ---- the variable-key reads: none silently dropped (nexus-p2m1x.25) ---------------------------------------------------------

_EX_HEADER = ["file", "line", "method", "accessor", "status", "reason"]
_EX_STATUS = {"excluded", "n-a (R4)"}


def _excluded() -> list[list[str]]:
    lines = [ln for ln in _EXCLUDED.read_text().splitlines() if ln and not ln.startswith("#")]
    assert lines[0].split("\t") == _EX_HEADER
    return [ln.split("\t") for ln in lines[1:]]


def test_every_variable_key_read_is_listed_with_a_status_and_a_reason():
    rows = _excluded()
    assert len(rows) >= 15, f"only {len(rows)} variable-key reads listed: the script stopped seeing them"
    for r in rows:
        assert len(r) == len(_EX_HEADER), r
        assert r[4] in _EX_STATUS, f"{r[0]}:{r[1]} is {r[4]!r}: read the site and give it a status and a reason in EXCLUSIONS"
        assert r[5].strip() not in ("", "-"), r
        assert (_REPO / r[0]).is_file(), r
    assert "UNREVIEWED" not in _run().stdout
    # the label keys, in writing: the census header names the jsonb key it scans for, the sidecar names tuples.body 'collection' (R4)
    assert "# metadata label keys: origin_collection" in _CENSUS.read_text().splitlines()
    assert "tuples.body 'collection'" in _EXCLUDED.read_text()
    (aspect,) = [r for r in rows if r[0].endswith("db/AspectTaskRefs.java")]
    assert aspect[4] == "n-a (R4)" and "R4" in aspect[5]


def test_the_sidecar_agrees_with_a_source_scan_of_the_accessor_calls():
    """Independent of the script's call tree: every line that calls jsonbGetAttribute(AsText) with a key that is neither a string literal
    nor an UPPER_CASE constant is a sidecar row, and every such sidecar row is such a line."""
    import re
    accessor = re.compile(r"jsonbGetAttribute(?:AsText)?\(")
    literal_key = re.compile(r"jsonbGetAttribute(?:AsText)?\([^()]*(?:\([^()]*\))*[^()]*,\s*(?:\"[^\"]*\"|[A-Z][A-Z0-9_.]*)\s*\)")
    found = set()
    for p in sorted((_REPO / "service/src/main/java").rglob("*.java")):
        for i, ln in enumerate(p.read_text().splitlines(), 1):
            if ln.strip().startswith(("//", "*", "/*")):
                continue
            if accessor.search(ln) and not literal_key.search(ln):
                found.add((p.relative_to(_REPO).as_posix(), i))
    listed = {(r[0], int(r[1])) for r in _excluded() if r[3].startswith("jsonbGetAttribute")}
    assert found == listed, f"scan only: {sorted(found - listed)}; sidecar only: {sorted(listed - found)}"
    assert len(found) >= 10
