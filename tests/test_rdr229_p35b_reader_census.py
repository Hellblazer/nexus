# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.5b (nexus-p2m1x.24): the committed reader census is a fresh run of its script.

``service/src/test/resources/rdr229/p35b-reader-census.tsv`` lists every live SQL object (function, view, trigger,
policy) whose text reads a collection name column, so P3.5b's surface is known to be complete. A reader added or
redefined without a fresh run fails here; the engine test Rdr229P35bReaderCensusLiveCatalogTest compares the same
object set against pg_catalog of a migrated database, which catches an object this parse misses.
"""
from __future__ import annotations

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
                        "catalog_collections.name"}, r


_DISPOSITIONS = _REPO / "service" / "src" / "test" / "resources" / "rdr229" / "p35b-reader-dispositions.tsv"
_VOCABULARY = {"id", "both", "label-kept", "parse-removed", "writer-kept"}
# P4.1 (nexus-p2m1x.35) redefines these views without their name arm, then drops the name columns with RESTRICT (linda 2026-11-22)
_P41_REDEFINE_VIEWS = {"nexus.collection_vector_stats", "nexus.live_chunks"}


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
        elif disposition == "label-kept" and col != "catalog_collections.name":
            out.append(f"{where}: label-kept is only for a read of the registry label (linda 2026-11-22)")
        elif disposition == "label-kept" and not reason:
            out.append(f"{where}: label-kept needs a one-line reason")
        elif disposition == "both" and forms.get((kind, obj)) != "B":
            out.append(f"{where}: both is the form-B shape; a form-A reader reads the id")
        elif disposition == "parse-removed" and parses.get((kind, obj), "-") == "-":
            out.append(f"{where}: parse-removed on a body the census finds no parse in")
        elif disposition == "writer-kept" and (forms.get((kind, obj)) != "A" or obj not in writers):
            out.append(f"{where}: writer-kept is for a form A object that P3.1's writer census lists as a SQL writer")
        elif disposition == "writer-kept" and not reason:
            out.append(f"{where}: writer-kept needs a one-line reason")
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
    }
    assert len(_rule_breaches(bad, forms, parses, frozenset())) == len(bad)
    assert _rule_breaches({("function", "f_a", "collection"): ("writer-kept", "stamps an id")}, forms, parses,
                          frozenset({"f_a"})) == []
    good = {("function", "f_a", "catalog_collections.name"): ("label-kept", "looks up the user's label"),
            ("view", "v_b", "collection"): ("both", "")}
    assert _rule_breaches(good, forms, parses) == []


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
