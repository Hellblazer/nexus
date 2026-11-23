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
_DISPOSITIONS = _RES / "p35c-java-reader-dispositions.tsv"
_JAVA = "service/src/main/java/dev/nexus/service/"

_NAME_COLUMNS = {"collection", "physical_collection", "source_collection", "target_collection", "superseded_by", "name"}
_OPS = {"where", "join_on", "join_using", "select", "group_by", "order_by", "having", "on_conflict", "returning", "set_value",
        "result", "values", "expr", "accessor_def", "partition_by", "distinct_on", "from"}
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
        assert r[3] in _NAME_COLUMNS, r
        assert r[4] in _OPS, r
        assert r[1].isdigit(), r
        assert (_REPO / r[0]).is_file(), r


_VOCABULARY = {"id", "both", "label-kept", "parse-removed"}


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
        elif disposition == "label-kept" and col != "name":
            out.append(f"{where}: label-kept is only for a read of the registry label, catalog_collections.name (linda 2026-11-22)")
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
    }
    assert len(_rule_breaches(bad)) == len(bad)
    good = {("java", "db/X.java catalog_collections where", "name"): ("label-kept", "the user's label, not a key"),
            ("java", "db/X.java chunks where", "collection"): ("id", ""),
            ("java", "db/X.java topics where", "collection"): ("both", "the stamp may not have reached topics")}
    assert _rule_breaches(good) == []
