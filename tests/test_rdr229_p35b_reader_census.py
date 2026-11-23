# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.5b (nexus-p2m1x.24): the committed reader census is a fresh run of its script.

``service/src/test/resources/rdr229/p35b-reader-census.tsv`` lists every live SQL object (function, view, trigger,
policy) whose text reads a collection name column, so P3.5b's surface is known to be complete. A reader added or
redefined without a fresh run fails here; the engine test Rdr229P35bReaderCensusLiveCatalogTest compares the same
object set against pg_catalog of a migrated database, which catches an object this parse misses.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_SCRIPT = _REPO / "scripts" / "rdr229_p35b_reader_census.py"
_CENSUS = _REPO / "service" / "src" / "test" / "resources" / "rdr229" / "p35b-reader-census.tsv"


def _rows() -> list[list[str]]:
    lines = [ln for ln in _CENSUS.read_text().splitlines() if ln and not ln.startswith("#")]
    return [ln.split("\t") for ln in lines[1:]]


def test_the_committed_census_is_a_fresh_run():
    proc = subprocess.run([sys.executable, str(_SCRIPT), str(_REPO), "--check"], capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, (
        "the committed reader census differs from a fresh run; regenerate it with "
        f"`python3 scripts/rdr229_p35b_reader_census.py . --write`:\n{proc.stdout[-2000:]}{proc.stderr[-1000:]}"
    )


def test_the_census_names_the_readers_the_rdr_and_the_fork_run_name():
    rows = _rows()
    assert len(rows) > 200, f"only {len(rows)} census rows: the script stopped seeing the readers"
    objects = {(r[0], r[1]): r[5] for r in rows}
    # the five views P4.1's DROP COLUMN ... CASCADE dropped on the fork (T2 nexus_rdr/229-p32-fork-results-2026-11-20)
    for view in ("collection_health_meta", "collection_doc_counts", "collection_vector_stats", "topics_with_counts",
                 "live_chunks"):
        assert ("view", f"nexus.{view}") in objects, view
    # the two views the RDR names read a chunk-family table, so they read both shapes (form B, no CONTINUE)
    assert objects[("view", "nexus.collection_vector_stats")] == "B"
    assert objects[("view", "nexus.live_chunks")] == "B"
    # catalog-044's trigger fires on a name column, and its function matches catalog_collections.name
    assert ("trigger", "nexus.catalog_documents.catalog_documents_owner_to_collection") in objects
    owner_fn = [r for r in rows if r[1] == "nexus.catalog_documents_owner_to_collection"]
    assert {r[4] for r in owner_fn} >= {"catalog_collections.name", "physical_collection"}


def test_a_write_is_not_a_read():
    rows = _rows()
    # an INSERT's column list and an UPDATE's SET target are P3.1's writer census, never a reader row
    for r in rows:
        assert r[4] in {"collection", "physical_collection", "source_collection", "target_collection", "superseded_by",
                        "catalog_collections.name"} or r[4].startswith("metadata."), r


_DISPOSITIONS = _REPO / "service" / "src" / "test" / "resources" / "rdr229" / "p35b-reader-dispositions.tsv"
_VOCABULARY = {"id", "both", "label-kept", "parse-removed", "writer-kept", "name-kept", "id-twin", "no-caller", "unchanged-fallback"}
_TWIN = "_by_id"  # the id-path twin of a name-kept search function (linda 2026-11-22)
# P4.1 (nexus-p2m1x.35) redefines these views without their name arm, then drops the name columns with RESTRICT (linda 2026-11-22)
_P41_REDEFINE_VIEWS = {"nexus.collection_vector_stats", "nexus.live_chunks"}
# nexus-p2m1x.25 (linda 2026-11-24): a metadata-key row (name_column "metadata.<key>") may be n-a; it is refused on a column reader
_VOCABULARY = _VOCABULARY | {"n-a"}


def _dispositions() -> dict[tuple[str, str, str], tuple[str, str]]:
    lines = [ln for ln in _DISPOSITIONS.read_text().splitlines() if ln and not ln.startswith("#")]
    out: dict[tuple[str, str, str], tuple[str, str]] = {}
    for ln in lines[1:]:
        kind, obj, col, disposition, reason = (ln.split("\t") + [""] * 5)[:5]
        assert (kind, obj, col) not in out, f"two disposition rows for {kind} {obj} {col}"
        out[(kind, obj, col)] = (disposition, reason.strip())
    return out


def test_every_census_reader_has_a_disposition_and_every_disposition_a_reader():
    readers = {(r[0], r[1], r[4]) for r in _rows()}
    disp = _dispositions()
    missing = sorted(readers - disp.keys())
    stale = sorted(disp.keys() - readers)
    assert not missing, f"{len(missing)} census readers have no disposition, e.g. {missing[:10]}"
    assert not stale, f"{len(stale)} dispositions name no census reader: {stale[:10]}"


_WRITERS = _REPO / "service" / "src" / "test" / "resources" / "rdr229" / "p31-writer-census.tsv"


def _sql_writers() -> set[str]:
    """The SQL functions P3.1's writer census lists as writing a collection column (file_or_function is 'nexus.f [file:line]')."""
    out = set()
    for ln in _WRITERS.read_text().splitlines():
        f = ln.split("\t")
        if len(f) > 5 and f[5] == "sql_function":
            out.add(f[2].split(" ")[0])
    return out


def _rule_breaches(dispositions, forms, parses, writers=frozenset()) -> list[str]:
    out = []
    for (kind, obj, col), (disposition, reason) in sorted(dispositions.items()):
        where = f"{kind} {obj} {col}"
        if disposition not in _VOCABULARY:
            out.append(f"{where}: {disposition!r} is not one of {sorted(_VOCABULARY)}")
        elif disposition == "label-kept" and col != "catalog_collections.name" and not col.startswith("metadata."):
            out.append(f"{where}: label-kept is only for a read of the registry label (linda 2026-11-22)")
        elif disposition == "label-kept" and not reason:
            out.append(f"{where}: label-kept needs a one-line reason")
        elif disposition == "label-kept" and col.startswith("metadata.") and not (reason.startswith("VIOLATING") or "nexus-p2m1x.35" in reason):
            out.append(f"{where}: label-kept on a metadata key needs the R1 reaper reason (nexus-p2m1x.35) or a VIOLATING reason (linda 2026-11-24)")
        elif disposition == "n-a" and not col.startswith("metadata."):
            out.append(f"{where}: n-a is only for a metadata-key row (name_column metadata.<key>), never a column reader (linda 2026-11-24)")
        elif disposition == "n-a" and not reason:
            out.append(f"{where}: n-a needs a reason")
        elif disposition == "both" and forms.get((kind, obj)) != "B":
            out.append(f"{where}: both is the form-B shape; a form-A reader reads the id")
        elif disposition == "parse-removed" and parses.get((kind, obj), "-") == "-":
            out.append(f"{where}: parse-removed on a body the census finds no parse in")
        elif disposition == "writer-kept" and (forms.get((kind, obj)) != "A" or obj not in writers):
            out.append(f"{where}: writer-kept is for a form A object that P3.1's writer census lists as a SQL writer")
        elif disposition == "writer-kept" and not reason:
            out.append(f"{where}: writer-kept needs a one-line reason")
        elif disposition == "name-kept" and (forms.get((kind, obj)) != "B" or f"{obj}{_TWIN}" not in reason):
            out.append(f"{where}: name-kept is for a form-B function whose reason names its twin {obj}{_TWIN}")
        elif disposition == "no-caller" and (forms.get((kind, obj)) != "B" or kind != "function" or not reason):
            out.append(f"{where}: no-caller is for a form B function with no engine caller, with its reason")
        elif disposition == "id-twin" and (not obj.endswith(_TWIN) or kind != "function"
                                           or dispositions.get((kind, obj[:-len(_TWIN)], col), ("", ""))[0] != "name-kept"):
            out.append(f"{where}: id-twin is for a function <name>{_TWIN} whose original <name> is name-kept")
    return out


def test_each_disposition_keeps_its_rule():
    rows = _rows()
    forms = {(r[0], r[1]): r[5] for r in rows}
    parses = {(r[0], r[1]): r[6] for r in rows}
    assert _rule_breaches(_dispositions(), forms, parses, _sql_writers()) == []


def test_the_rules_refuse_what_they_name():
    forms = {("function", "f_a"): "A", ("view", "v_b"): "B"}
    parses = {("function", "f_a"): "-", ("view", "v_b"): "-"}
    bad = {
        ("function", "f_a", "collection"): ("label-kept", "a reason"),                  # label-kept off the registry label
        ("function", "f_a", "catalog_collections.name"): ("label-kept", ""),            # no reason
        ("view", "v_b", "collection"): ("rename", ""),                                  # not in the vocabulary
        ("function", "f_a", "physical_collection"): ("both", ""),                       # both on form A
        ("function", "f_a", "superseded_by"): ("parse-removed", ""),                    # no parse to remove
        ("view", "v_b", "physical_collection"): ("writer-kept", "a reason"),             # writer-kept on form B
        ("function", "f_a", "source_collection"): ("writer-kept", "a reason"),           # f_a is not a SQL writer
        ("function", "f_a", "target_collection"): ("name-kept", "f_a_by_id"),            # name-kept on form A
        ("view", "v_b", "superseded_by"): ("name-kept", "no twin named"),                 # name-kept without its twin
        ("function", "f_c_by_id", "collection"): ("id-twin", ""),                         # its original is not name-kept
        ("function", "f_a", "metadata.origin_collection"): ("n-a", ""),                  # n-a with no reason
        ("view", "v_b", "metadata.origin_collection"): ("label-kept", "no ruling cited"),  # label-kept on a key, no R1 or VIOLATING reason
        ("function", "f_d", "collection"): ("id-twin", ""),                               # not a <name>_by_id
        ("function", "f_d", "superseded_by"): ("n-a", "a reason"),                       # n-a on a column reader
    }
    assert len(_rule_breaches(bad, forms, parses, frozenset())) == len(bad)
    assert _rule_breaches({("function", "f_a", "collection"): ("writer-kept", "stamps an id")}, forms, parses,
                          frozenset({"f_a"})) == []
    good = {("function", "f_a", "catalog_collections.name"): ("label-kept", "looks up the user's label"),
            ("view", "v_b", "collection"): ("both", ""),
            ("function", "f_a", "metadata.origin_collection"): ("n-a", "a file's labels are mapped to fresh ids, no live read"),
            ("view", "v_b", "metadata.origin_collection"): ("label-kept", "R1 reaper ruling (nexus-p2m1x.35): holds until P4.1"),
            ("function", "f_g", "metadata.origin_collection"): ("label-kept", "VIOLATING: label match after the latch; P3.7 switches it to id")}
    assert _rule_breaches(good, forms, parses) == []
    twin_forms = {("function", "f_e"): "B", ("function", "f_e_by_id"): "B"}
    twins = {("function", "f_e", "collection"): ("name-kept", "the id path is nexus.f_e_by_id"),
             ("function", "f_e_by_id", "collection"): ("id-twin", "matches by id")}
    assert _rule_breaches(twins, twin_forms, {}) == []


def test_the_views_that_read_both_shapes_are_p41s_redefine_list():
    rows = _rows()
    form_b_views = {r[1] for r in rows if r[0] == "view" and r[5] == "B"}
    assert form_b_views == _P41_REDEFINE_VIEWS, (
        f"the form-B views are P4.1's redefine-first list (nexus-p2m1x.35); census has {sorted(form_b_views)}")
    both_views = {obj for (kind, obj, _c), (d, _r) in _dispositions().items() if kind == "view" and d == "both"}
    assert both_views == _P41_REDEFINE_VIEWS, f"views disposed 'both': {sorted(both_views)}"


def _live_rows() -> list[list[str]]:
    proc = subprocess.run([sys.executable, str(_SCRIPT), str(_REPO), "--live"], capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-1000:]
    lines = [ln for ln in proc.stdout.splitlines() if ln and not ln.startswith("#")]
    return [ln.split("\t") for ln in lines[1:]]


def test_after_p35b_a_form_a_object_reads_no_name_but_the_label_or_a_writers_stamp():
    """The whole-tree census, P3.5b's own changelogs included: a form A object's name reads are the registry label, or the
    writer-kept stamp resolve of an object P3.5b left unchanged. Anything else is a form A reader still keyed on the name."""
    live = _live_rows()
    assert len(live) > 200, f"only {len(live)} live rows: --live stopped seeing the readers"
    kept = {(k, o, c) for (k, o, c), (d, _r) in _dispositions().items() if d == "writer-kept"}
    bad = sorted({(r[0], r[1], r[4]) for r in live
                  if r[5] == "A" and r[4] != "catalog_collections.name" and (r[0], r[1], r[4]) not in kept})
    assert not bad, f"form A objects still read a name column after P3.5b: {bad}"
    assert not any(r[0] == "trigger" for r in live), "the catalog-044 trigger still fires on a name column after P3.5b"


_P35B_FILES = (_REPO / "service" / "src" / "main" / "resources" / "db" / "changelog" / "vectors-077-rdr229-p35b-readers-both.xml",
               _REPO / "service" / "src" / "main" / "resources" / "db" / "changelog" / "vectors-078-rdr229-p35b-readers-continue.xml")


def _redefined_by_p35b() -> set[tuple[str, str]]:
    """(kind, object) of every function, view and trigger vectors-077 or vectors-078 creates or replaces, comments stripped."""
    out: set[tuple[str, str]] = set()
    for f in _P35B_FILES:
        text = re.sub(r"<!--.*?-->", "", f.read_text(), flags=re.S)
        for m in re.finditer(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(nexus\.\w+)\s*\(", text, re.I):
            out.add(("function", m.group(1).lower()))
        for m in re.finditer(r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(nexus\.\w+)", text, re.I):
            out.add(("view", m.group(1).lower()))
        for m in re.finditer(r"CREATE\s+TRIGGER\s+(\w+)[^;]*?\bON\s+(nexus\.\w+)", text, re.I | re.S):
            out.add(("trigger", f"{m.group(2).lower()}.{m.group(1).lower()}"))
    return out


def test_a_both_or_id_disposition_names_an_object_p35b_redefines():
    """critique S4 (2026-11-23): 'both' and 'id' claim a redefinition, so the object must be created or replaced in
    vectors-077/078; an object P3.5b leaves on its last definer (no match to redefine) is 'unchanged-fallback' with a reason."""
    redefined = _redefined_by_p35b()
    assert len(redefined) > 50, f"non-vacuity: only {len(redefined)} objects found in vectors-077/078"
    claims = {(k, o) for (k, o, _c), (d, _r) in _dispositions().items() if d in ("both", "id")}
    assert not sorted(claims - redefined), f"'both'/'id' on objects vectors-077/078 do not redefine: {sorted(claims - redefined)}"
    kept = {(k, o) for (k, o, _c), (d, r) in _dispositions().items() if d == "unchanged-fallback"}
    assert not sorted(kept & redefined), f"'unchanged-fallback' on objects vectors-077/078 do redefine: {sorted(kept & redefined)}"
    assert all(r for (_k, _o, _c), (d, r) in _dispositions().items() if d == "unchanged-fallback"), "unchanged-fallback needs a reason"


# critique S5 (2026-11-23): the live SQL objects that still read a collection name column after P3.5b (form B's name arm, the
# name-kept search functions, writer stamp resolves, registry label reads). P4.1 removes the name columns, so it lowers this
# to 0; any other change to the number is a reader added or removed and must be read, then the pin moved in the same commit.
_LIVE_NAME_READERS_PIN = 129


def test_the_live_name_readers_are_pinned_for_p41_to_lower():
    live = _live_rows()
    objects = {(r[0], r[1]) for r in live}
    assert len(objects) == _LIVE_NAME_READERS_PIN, (
        f"{len(objects)} live SQL objects read a collection name column (pin {_LIVE_NAME_READERS_PIN}); P4.1 lowers it to 0")


# ---- the metadata-key reads (nexus-p2m1x.25): the scanner on a planted changelog -----------------------------------------------

_PLANTED = """<?xml version="1.0"?>
<databaseChangeLog><changeSet id="p-1" author="t"><sql splitStatements="false">
CREATE OR REPLACE FUNCTION nexus.planted(p_origin text) RETURNS bigint LANGUAGE plpgsql AS $f$
BEGIN
    PERFORM 1 FROM nexus.chunks c WHERE c.metadata->>'origin_collection' = p_origin;
    PERFORM 1 FROM nexus.chunks c WHERE COALESCE((c.metadata)->'origin_collection', 'x') = 'y';
    PERFORM 1 FROM nexus.chunks c WHERE c.metadata #>> '{origin_collection}' = p_origin;
    PERFORM 1 FROM nexus.chunks c WHERE jsonb_extract_path_text(c.metadata, 'origin_collection') = p_origin;
    PERFORM 1 FROM nexus.chunks c WHERE jsonb_exists(c.metadata, 'origin_collection');
    PERFORM 1 FROM nexus.chunks c WHERE c.metadata ? 'origin_collection';
    PERFORM 1 FROM nexus.chunks c WHERE c.metadata->>'quarantined_at' IS NOT NULL;
    PERFORM 1 FROM nexus.chunks c WHERE c.metadata->>'origin_collection_id' IS NOT NULL;
    UPDATE nexus.chunks c SET metadata = c.metadata || jsonb_build_object('origin_collection', p_origin) WHERE c.tenant_id = 't';
    RETURN 1;
END $f$;
</sql></changeSet></databaseChangeLog>
"""


def test_the_scanner_finds_each_form_of_a_metadata_key_read(tmp_path):
    res = tmp_path / "service/src/main/resources"
    (res / "db/changelog").mkdir(parents=True)
    (res / "db/rdr229").mkdir(parents=True)
    (res / "db/rdr229/p31-checks.tsv").write_text((_REPO / "service/src/main/resources/db/rdr229/p31-checks.tsv").read_text())
    (res / "db/changelog/db.changelog-master.xml").write_text(
        '<databaseChangeLog><include file="db/changelog/planted.xml"/></databaseChangeLog>')
    (res / "db/changelog/planted.xml").write_text(_PLANTED)
    proc = subprocess.run([sys.executable, str(_SCRIPT), str(tmp_path), "--write"], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-1500:]
    out = tmp_path / "service/src/test/resources/rdr229/p35b-reader-census.tsv"
    rows = [ln.split("\t") for ln in out.read_text().splitlines() if ln and not ln.startswith("#")][1:]
    lines = _PLANTED.splitlines()
    hit = {int(r[2].split(":")[1]) for r in rows if r[1] == "nexus.planted" and r[4] == "metadata.origin_collection"}
    # one row per (statement, column): each read statement of the function is a row, the id key, another key and the jsonb_build_object WRITE are not
    want = {n for n, ln in enumerate(lines, 1) if "PERFORM" in ln and "origin_collection" in ln and "origin_collection_id" not in ln}
    assert len(want) == 6
    # statement_at is the line the statement starts on: its PERFORM, except the first, which starts at the BEGIN above it
    first = min(want)
    assert hit == (want - {first}) | {first - 1}, (hit, want, rows)
    assert "id-key reads (never rows) 1 in 1 objects" in proc.stdout, proc.stdout
