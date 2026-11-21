# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.1 fix round B (nexus-p2m1x.17, review gate B2): the writer census covers registered data-move templates.

A NOT VALID CHECK is enforced on every row version a statement writes, so a registered move whose template UPDATEs (or INSERTs
into) a guarded table without setting that table's collection id fails 23514 on any row the stamp has not reached, and the worker
classes it permanent. The census had Java sites and SQL functions only, so P2.10's three residue moves (catalog-049-2, templates
that UPDATE source_uri) passed it. ``rdr229_p31_move_template_census`` reads every ``INSERT INTO nexus.data_moves`` of the
changelog tree (top level and inside an ``on_complete`` text) and flags a template with no id in its SET (or column list) on a guarded
table, unless another registered move supersedes it (a superseded move is never run).

NON-VACUITY: the real-tree tests pin a floor on the moves found, and one reads the tree with the supersede file left out, which must
flag exactly P2.10's three residue moves.
"""
from __future__ import annotations

import pathlib

import pytest

import rdr229_p31_move_template_census as census

pytestmark = pytest.mark.lint

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKS = ROOT / "service/src/main/resources/db/rdr229/p31-checks.tsv"
CENSUS = ROOT / "service/src/test/resources/rdr229/p31-writer-census.tsv"
SUPERSEDE_FILE = "catalog-052-residue-moves-supersede.xml"
RESIDUE_LABELS = {
    "rdr229-residue-catalog-documents",
    "rdr229-residue-document-aspects",
    "rdr229-residue-document-highlights",
}


@pytest.fixture(scope="module")
def guarded():
    return census.read_guarded(CHECKS)


def _insert(label: str, template: str, table: str, *, supersedes: str | None = None, on_complete: str = "NULL",
            quoting: str = "dollar") -> str:
    """One INSERT in the shapes the changelog uses: a dollar-quoted template (catalog-049-2) or a single-quoted one (vectors-070)."""
    tpl = f"$t$" + template + "$t$" if quoting == "dollar" else "'" + template.replace("'", "''") + "'"
    cols = "label, template, driving_schema, driving_table, chunk_size, sleep_ms, statement_timeout_ms, verification, on_complete"
    vals = f"'{label}', {tpl}, 'nexus', '{table}', 1000, 50, 30000, $v$SELECT 1$v$, {on_complete}"
    if supersedes:
        cols += ", supersedes"
        vals += f", '{supersedes}'"
    cols += ", changeset_id"
    vals += ", 'plant-1'"
    return f"INSERT INTO nexus.data_moves ({cols})\nVALUES ({vals});\n"


NO_ID = "UPDATE <driving_table> SET source_uri = 'x' WHERE <chunking_clause> AND t.collection = 'a'"
WITH_ID = ("UPDATE <driving_table> SET source_uri = 'x', collection_id = COALESCE(t.collection_id, c.collection_id) "
           "FROM nexus.catalog_collections c WHERE <chunking_clause> AND c.name = t.collection")


def _flagged(text: str, guarded) -> list[str]:
    moves = census.parse_moves(text, "planted.xml")
    return [m.label for m, _why in census.violations(moves, guarded)]


# ---------------------------------------------------------------- the planted cases

@pytest.mark.parametrize("quoting", ["dollar", "single"])
def test_a_planted_template_that_writes_source_uri_on_a_guarded_table_without_the_id_is_caught(guarded, quoting):
    text = _insert("planted-no-id", NO_ID, "document_aspects", quoting=quoting)
    assert _flagged(text, guarded) == ["planted-no-id"]
    reason = census.violations(census.parse_moves(text, "planted.xml"), guarded)[0][1]
    assert "collection_id" in reason and "document_aspects" in reason


def test_the_same_template_with_the_id_in_its_set_passes(guarded):
    assert _flagged(_insert("planted-with-id", WITH_ID, "document_aspects"), guarded) == []


def test_an_update_of_the_id_alone_by_a_stamp_shape_passes(guarded):
    stamp = ("UPDATE <driving_table> SET collection_id = COALESCE(t.collection_id, (SELECT cc.collection_id FROM nexus.catalog_collections cc "
             "WHERE cc.tenant_id = t.tenant_id AND cc.name = t.collection)) WHERE t.collection_id IS NULL AND <chunking_clause>")
    assert _flagged(_insert("planted-stamp", stamp, "document_aspects", quoting="single"), guarded) == []


def test_a_template_over_a_table_with_no_check_is_not_flagged(guarded):
    assert "dm_test_table" not in guarded
    assert _flagged(_insert("planted-other", NO_ID, "dm_test_table"), guarded) == []


def test_each_table_names_its_own_id_column(guarded):
    # catalog_documents' id twin is physical_collection_id, not collection_id: setting the wrong twin is still a violation
    wrong = "UPDATE <driving_table> SET source_uri = 'x', collection_id = 1 WHERE <chunking_clause>"
    assert _flagged(_insert("planted-wrong-twin", wrong, "catalog_documents"), guarded) == ["planted-wrong-twin"]
    right = "UPDATE <driving_table> SET source_uri = 'x', physical_collection_id = 1 WHERE <chunking_clause>"
    assert _flagged(_insert("planted-right-twin", right, "catalog_documents"), guarded) == []


def test_a_move_that_another_registered_move_supersedes_is_not_flagged(guarded):
    text = _insert("planted-old", NO_ID, "document_aspects") + _insert("planted-new", WITH_ID, "document_aspects", supersedes="planted-old")
    assert _flagged(text, guarded) == []


def test_a_template_registered_inside_an_on_complete_is_found(guarded):
    child = _insert("planted-child", NO_ID, "document_aspects")
    parent = _insert("planted-parent", "UPDATE <driving_table> SET physical_collection_id = 1 WHERE <chunking_clause>", "catalog_documents",
                     on_complete="$oc$\n" + child + "$oc$")
    moves = census.parse_moves(parent, "planted.xml")
    assert sorted(m.label for m in moves) == ["planted-child", "planted-parent"]
    assert _flagged(parent, guarded) == ["planted-child"]


def test_comments_and_rollbacks_are_not_read_as_registrations(guarded):
    text = ("<!-- " + _insert("in-xml-comment", NO_ID, "document_aspects") + " -->\n"
            "<comment>INSERT INTO nexus.data_moves (label) VALUES ('in-prose')</comment>\n"
            "<rollback><sql>DELETE FROM nexus.data_moves WHERE label = 'x'</sql></rollback>\n")
    assert census.parse_moves(text, "planted.xml") == []


def test_a_template_the_scan_cannot_read_over_a_guarded_table_is_flagged_until_someone_reasons_about_it(guarded):
    cte = "WITH x AS (SELECT 1) UPDATE nexus.document_aspects SET source_uri = 'y' FROM x WHERE true"
    text = _insert("planted-cte", cte, "document_aspects")
    assert _flagged(text, guarded) == ["planted-cte"]
    reason = census.violations(census.parse_moves(text, "planted.xml"), guarded)[0][1]
    assert "HANDLED" in reason


def test_every_handled_label_names_a_move_the_tree_registers():
    labels = {m.label for m in census.parse_tree(ROOT)}
    assert census.HANDLED, "non-vacuity: the allowlist is not empty"
    assert set(census.HANDLED) <= labels, f"stale HANDLED entries: {sorted(set(census.HANDLED) - labels)}"


# ---------------------------------------------------------------- the real tree

def test_the_real_tree_has_no_template_that_writes_a_guarded_table_without_the_id(guarded):
    moves = census.parse_tree(ROOT)
    labels = {m.label for m in moves}
    assert len(moves) >= 25, f"non-vacuity: the scan found only {len(moves)} registered templates"
    assert RESIDUE_LABELS <= labels and {"rdr229-p31-residue-catalog-documents", "rdr229-stamp-chunks"} <= labels
    assert census.violations(moves, guarded) == []


def test_without_the_supersede_file_the_census_flags_p210s_three_residue_moves(guarded):
    moves = census.parse_tree(ROOT, skip=(SUPERSEDE_FILE,))
    assert {m.label for m, _why in census.violations(moves, guarded)} == RESIDUE_LABELS


def test_the_committed_census_lists_every_registered_template_the_scan_finds(guarded):
    moves = census.parse_tree(ROOT)
    want = {tuple(r[:2]) + (r[2].split(" [")[0],) + tuple(r[4:]) for r in census.census_rows(moves, guarded)}
    got = set()
    for line in CENSUS.read_text().splitlines():
        f = line.split("\t")
        if len(f) == 10 and f[5] == "data_move":
            got.add(tuple(f[:2]) + (f[2].split(" [")[0],) + tuple(f[4:]))
    assert want, "non-vacuity: the scan produced census rows"
    assert got == want, "regenerate: scripts/rdr229_p31_writer_census.py <root> --write"
