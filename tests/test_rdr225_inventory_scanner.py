# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-225 P1.1 (nexus-3wh8d.6): unit tests and the planted-difference
self-test for ``scripts/rdr225_inventory.py``.

The planted-difference test is the non-vacuity proof for the generator. It
builds a small fixture tree that names all five tables (``nexus.chunks``,
``taxonomy_centroids``, ``catalog_document_chunks``, ``topic_assignments``,
``chunk_orphaned_at``) in changelogs and in Java, generates a baseline
inventory, then applies one planted difference at a time to a fresh copy and
requires ``compare`` to report it. Plants cover each of the five tables and
each of the three referencing tables' writers (the prototype's H5 patterns
missed ``catalog_document_chunks``, ``topic_assignments`` and
``chunk_orphaned_at`` and their writers: the fix-check item this closes).
"""
from __future__ import annotations

import copy
import dataclasses
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import rdr225_inventory as inv

# ---------------------------------------------------------------------------
# Fixture tree
# ---------------------------------------------------------------------------

MASTER = """<?xml version="1.0" encoding="UTF-8"?>
<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog">
    <include file="db/changelog/a-001-baseline.xml"/>
    <include file="db/changelog/b-001-functions.xml"/>
</databaseChangeLog>
"""

A001 = """<?xml version="1.0" encoding="UTF-8"?>
<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog">
    <changeSet id="a-1" author="t">
        <comment>chunks mentioned only in a comment must not count</comment>
        <sql>
        -- nexus.chunks in a SQL comment must not count either
        ALTER TABLE nexus.catalog_document_chunks
            ADD CONSTRAINT fk_catalog_chunks_chunk
            FOREIGN KEY (tenant_id, collection, chash)
            REFERENCES nexus.chunks (tenant_id, collection, chash)
            ON UPDATE CASCADE ON DELETE NO ACTION DEFERRABLE INITIALLY IMMEDIATE;
        ALTER TABLE nexus.topic_assignments
            ADD CONSTRAINT topic_assignments_chunk_fk
            FOREIGN KEY (tenant_id, collection, doc_id)
            REFERENCES nexus.chunks (tenant_id, collection, chash)
            ON UPDATE CASCADE ON DELETE CASCADE;
        ALTER TABLE nexus.chunk_orphaned_at
            ADD CONSTRAINT chunk_orphaned_at_chunk_fk
            FOREIGN KEY (tenant_id, collection, chash)
            REFERENCES nexus.chunks (tenant_id, collection, chash)
            ON UPDATE CASCADE ON DELETE CASCADE;
        CREATE INDEX idx_chunks_tenant_chash ON nexus.chunks (tenant_id, chash);
        CREATE POLICY tenant_isolation ON nexus.chunks USING (tenant_id = current_setting('app.tenant'));
        GRANT SELECT ON nexus.chunks TO nexus_diag;
        </sql>
    </changeSet>
    <changeSet id="a-2" author="t">
        <sql splitStatements="false">
        CREATE VIEW nexus.live_chunks AS SELECT c.* FROM nexus.chunks c;
        CREATE TABLE nexus.taxonomy_centroids (tenant_id text, topic_id bigint, embedding_384 vector(384));
        </sql>
    </changeSet>
</databaseChangeLog>
"""

B001 = """<?xml version="1.0" encoding="UTF-8"?>
<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog">
    <changeSet id="b-1" author="t" runOnChange="true">
        <sql splitStatements="false">
        CREATE OR REPLACE FUNCTION nexus.upsert_manifest(p_tenant text, p_chash text)
        RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO nexus.catalog_document_chunks (tenant_id, collection, chash)
            VALUES (p_tenant, 'c', p_chash)
            ON CONFLICT (tenant_id, collection, chash) DO NOTHING;
            SET CONSTRAINTS nexus.fk_catalog_chunks_chunk DEFERRED;
            DELETE FROM nexus.chunk_orphaned_at WHERE tenant_id = p_tenant;
            INSERT INTO nexus.topic_assignments (tenant_id, collection, doc_id, topic_id)
            SELECT p_tenant, 'c', p_chash, 1 FROM nexus.chunks WHERE chash = p_chash;
        END $$;
        </sql>
    </changeSet>
    <changeSet id="b-2" author="t">
        <sql splitStatements="false">
        CREATE OR REPLACE FUNCTION nexus.write_chunk(p_chash text)
        RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text)
            VALUES ('t', 'c', p_chash, 'x')
            ON CONFLICT (tenant_id, collection, chash) DO UPDATE SET chunk_text = EXCLUDED.chunk_text;
            INSERT INTO nexus.taxonomy_centroids (tenant_id, topic_id) VALUES ('t', 1)
            ON CONFLICT (tenant_id, topic_id) DO UPDATE SET topic_id = EXCLUDED.topic_id;
        END $$;
        </sql>
    </changeSet>
</databaseChangeLog>
"""

REPO_JAVA = '''\
package dev.nexus.service.db;

import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;

public class Repo {
    /** Javadoc mentioning nexus.chunks must not count. */
    private static final String JSON = "{\\"chunks\\": [";

    void upsertManifest(Object ctx) {
        String sql = "INSERT INTO nexus.catalog_document_chunks (tenant_id, collection, chash) "
            + "VALUES (?, ?, ?) ON CONFLICT (tenant_id, collection, chash) DO NOTHING";
        ctx.execute("SET CONSTRAINTS nexus.fk_catalog_chunks_chunk DEFERRED");
    }

    void stampOrphans(Object ctx) {
        ctx.insertInto(CHUNK_ORPHANED_AT)
           .columns(CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.CHASH)
           .values(1, 2, 3)
           .onConflict(CHUNK_ORPHANED_AT.TENANT_ID, CHUNK_ORPHANED_AT.COLLECTION, CHUNK_ORPHANED_AT.CHASH)
           .doNothing();
    }

    void assign(Object ctx) {
        String sql = """
            INSERT INTO nexus.topic_assignments (tenant_id, collection, doc_id, topic_id)
            VALUES (?, ?, ?, ?) ON CONFLICT (tenant_id, collection, doc_id) DO NOTHING
            """;
    }

    void centroids(Object ctx) {
        String sql = "INSERT INTO " + DimTables.CENTROIDS_TABLE_NAME
            + " (tenant_id, topic_id) VALUES (?, ?) ON CONFLICT (tenant_id, topic_id) DO NOTHING";
    }

    String unrelated() {
        return "chunks written: pdf_chunks staging.chunks";
    }
}
'''


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def build_fixture(root: Path) -> inv.Roots:
    _write(root, "cl/db.changelog-master.xml", MASTER)
    _write(root, "cl/a-001-baseline.xml", A001)
    _write(root, "cl/b-001-functions.xml", B001)
    _write(root, "java/dev/nexus/service/db/Repo.java", REPO_JAVA)
    return inv.Roots(repo=root, changelog_dir=root / "cl", java_dir=root / "java", anchors=())


@pytest.fixture()
def fixture_roots(tmp_path: Path) -> inv.Roots:
    return build_fixture(tmp_path / "base")


def _gen(roots: inv.Roots) -> dict:
    return inv.generate(roots, source_sha="test-sha")


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


#: Identifiers that merely CONTAIN one of the five names, or name it behind another schema.
#: Each is run ALONE, because a decoy that sits beside a real reference hides inside it: the
#: set of tables found would still be right when the lookbehind or lookahead is gone.
DECOYS = [
    "nexus.pdf_chunks p", "staging.chunks s", "nexus.chunks_384 o", "nexus.live_chunks l", "nexus.chunks_new n",
    "nexus.topic_assignments_v2 t", "nexus.catalog_document_chunks_backup b", "nexus.chunk_orphaned_at2 o",
    "staging.topic_assignments t", 'staging."chunks" s', '"staging"."chunks" s', '"pdf_chunks" p',
    "nexus.xtaxonomy_centroids x",
]


@pytest.mark.parametrize("decoy", DECOYS)
def test_table_token_precision_decoy_is_not_a_table(decoy: str) -> None:
    """The five names match as whole tokens only: not as a substring of another identifier, not
    behind a foreign schema, and not the retired per-dimension tables. Run alone, so removing the
    NAMED lookbehind or lookahead turns a decoy into a site and this test fails."""
    assert inv.extract_sites(f"SELECT 1 FROM {decoy} WHERE true") == []
    assert inv.NAMED.search(f"SELECT 1 FROM {decoy}") is None
    assert inv._CRUDE_SQL.search(f"SELECT 1 FROM {decoy}") is None


def test_table_token_precision_word_boundaries_in_expressions() -> None:
    sql = "SELECT 1 WHERE p_floor_min_chunks > 0 AND x = total_chunks AND y = chunks_per_page AND z = n_chunks"
    assert inv.extract_sites(sql) == []


def test_table_token_precision_finds_exactly_the_real_references() -> None:
    sql = textwrap.dedent(
        """
        SELECT 1 FROM nexus.pdf_chunks p JOIN staging.chunks s ON true
          JOIN nexus.chunks_new o ON true JOIN nexus.live_chunks l ON true
          WHERE p_floor_min_chunks > 0 AND x = total_chunks;
        SELECT 2 FROM nexus.catalog_document_chunks m JOIN nexus.chunks c ON c.chash = m.chash;
        """
    )
    sites = inv.extract_sites(sql)
    assert [(s["verb"], s["table"]) for s in sites] == [("READ", "catalog_document_chunks"), ("READ", "chunks")]


def test_comments_are_not_scanned() -> None:
    sql = "-- nexus.chunks\n/* nexus.topic_assignments */ SELECT 1;"
    assert inv.extract_sites(inv.blank_comments(sql)) == []


def test_insert_columns_and_conflict_target() -> None:
    sql = (
        "INSERT INTO nexus.chunks (tenant_id, collection, chash) SELECT 1,2,3 "
        "ON CONFLICT (tenant_id, collection, chash) DO UPDATE SET chunk_text = EXCLUDED.chunk_text"
    )
    (site,) = inv.extract_sites(sql)
    assert site["verb"] == "INSERT"
    assert site["table"] == "chunks"
    assert site["columns"] == "tenant_id, collection, chash"
    assert site["conflict"] == "(tenant_id, collection, chash) DO UPDATE"


def test_insert_without_column_list_is_flagged() -> None:
    (site,) = inv.extract_sites("INSERT INTO nexus.chunks SELECT * FROM somewhere")
    assert site["columns"] is None
    assert site["conflict"] is None


def test_conflict_binds_to_its_own_insert_not_a_later_one() -> None:
    sql = (
        "INSERT INTO nexus.chunks (a) VALUES (1); "
        "INSERT INTO other (a) VALUES (1) ON CONFLICT (a) DO NOTHING"
    )
    (site,) = inv.extract_sites(sql)
    assert site["conflict"] is None


def test_set_constraints_site_is_found_by_constraint_name() -> None:
    sites = inv.extract_sites("SET CONSTRAINTS nexus.fk_catalog_chunks_chunk DEFERRED")
    assert [(s["verb"], s["table"]) for s in sites] == [("SET CONSTRAINTS", "chunks")]


def test_update_records_assigned_columns() -> None:
    (site,) = inv.extract_sites(
        "UPDATE nexus.catalog_document_chunks SET collection = 'x', embedding_model = y WHERE true"
    )
    assert site["verb"] == "UPDATE"
    assert site["columns"] == "collection, embedding_model"


def test_function_liveness_follows_the_latest_definition(tmp_path: Path) -> None:
    """A later CREATE OR REPLACE that no longer names the tables supersedes the
    earlier body, so the earlier row is not live. The live body is named."""
    root = tmp_path / "t"
    _write(root, "cl/db.changelog-master.xml", MASTER.replace("a-001-baseline", "f-1").replace("b-001-functions", "f-2"))
    body = "<changeSet id=\"{id}\" author=\"t\"><sql splitStatements=\"false\">{sql}</sql></changeSet>"
    wrap = "<databaseChangeLog xmlns=\"http://www.liquibase.org/xml/ns/dbchangelog\">{cs}</databaseChangeLog>"
    _write(root, "cl/f-1.xml", wrap.format(cs=body.format(id="f1", sql=(
        "CREATE OR REPLACE FUNCTION nexus.f(a int) RETURNS int LANGUAGE sql AS $$ "
        "SELECT count(*)::int FROM nexus.chunks $$;"))))
    _write(root, "cl/f-2.xml", wrap.format(cs=body.format(id="f2", sql=(
        "CREATE OR REPLACE FUNCTION nexus.f(a int) RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;"))))
    _write(root, "java/X.java", "class X {}\n")
    roots = inv.Roots(repo=root, changelog_dir=root / "cl", java_dir=root / "java", anchors=())
    rows = [r for r in _gen(roots)["rows"] if r["kind"] == "function"]
    assert len(rows) == 1
    assert rows[0]["live"] is False
    assert rows[0]["live_in"] == "f-2.xml#f2"


def test_baseline_fixture_rows(fixture_roots: inv.Roots) -> None:
    out = _gen(fixture_roots)
    rows = out["rows"]
    kinds = {r["kind"] for r in rows}
    assert {"fk", "index", "policy", "grant", "view", "table", "function", "java-method"} <= kinds
    fk_names = sorted(r["name"] for r in rows if r["kind"] == "fk")
    assert fk_names == [
        "chunk_orphaned_at_chunk_fk", "fk_catalog_chunks_chunk", "topic_assignments_chunk_fk",
    ]
    # every one of the five tables is named by some row
    named = {t for r in rows for t in r["tables"]}
    assert named == set(inv.TABLES)
    # jOOQ typed writer, text block writer and constant-named writer are all attributed
    java = {r["name"]: r for r in rows if r["kind"] == "java-method"}
    assert set(java) == {"Repo/upsertManifest", "Repo/stampOrphans", "Repo/assign", "Repo/centroids"}
    orphan_site = java["Repo/stampOrphans"]["sites"][0]
    assert orphan_site["verb"] == "INSERT" and orphan_site["table"] == "chunk_orphaned_at"
    # a typed jOOQ site is normalised to the raw-SQL spelling, so the two compare
    assert orphan_site["conflict"] == "(tenant_id, collection, chash) DO NOTHING"
    assert orphan_site["columns"] == "tenant_id, collection, chash"
    assert java["Repo/centroids"]["sites"][0]["table"] == "taxonomy_centroids"
    assert java["Repo/assign"]["sites"][0]["conflict"] == "(tenant_id, collection, doc_id) DO NOTHING"
    sc = java["Repo/upsertManifest"]["sites"]
    assert ("SET CONSTRAINTS", "chunks") in {(s["verb"], s["table"]) for s in sc}
    # The JSON key "chunks", the Javadoc mention and the unrelated string are not references
    assert "Repo/unrelated" not in java
    assert "Repo" not in java


SCRIPTS_DIR = Path(inv.__file__).resolve().parent

_GEN_SNIPPET = (
    "import sys\n"
    "from pathlib import Path\n"
    "import rdr225_inventory as inv\n"
    "r = Path(sys.argv[1])\n"
    "roots = inv.Roots(repo=r, changelog_dir=r / 'cl', java_dir=r / 'java', anchors=())\n"
    "sys.stdout.write(inv.dumps(inv.generate(roots, source_sha='fixed')))\n"
)


def _generate_in_subprocess(scripts_dir: Path, root: Path, hash_seed: str) -> bytes:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(scripts_dir)}
    done = subprocess.run([sys.executable, "-c", _GEN_SNIPPET, str(root)], env=env, capture_output=True,
                          check=True, timeout=120)
    return done.stdout


def test_generation_is_deterministic(fixture_roots: inv.Roots) -> None:
    """Two interpreters with different string-hash seeds must write byte-identical files:
    a set iterated where a sorted list belongs shows up as a reordering only across seeds."""
    seeds = ("1", "2", "3")
    outs = [_generate_in_subprocess(SCRIPTS_DIR, fixture_roots.repo, seed) for seed in seeds]
    assert outs[0] and outs[0] == outs[1] == outs[2]


def test_the_determinism_check_detects_an_unsorted_set(fixture_roots: inv.Roots, tmp_path: Path) -> None:
    """Non-vacuity of the check above: the same run over a copy of the script that iterates a
    set where it sorts one is NOT byte-identical across hash seeds."""
    mutant = tmp_path / "mutant"
    mutant.mkdir()
    text = (SCRIPTS_DIR / "rdr225_inventory.py").read_text(encoding="utf-8")
    needle = "return sorted({_t(m) for m in NAMED.finditer(text)})"
    assert needle in text
    (mutant / "rdr225_inventory.py").write_text(
        text.replace(needle, "return list({_t(m) for m in NAMED.finditer(text)})"), encoding="utf-8")
    outs = {_generate_in_subprocess(mutant, fixture_roots.repo, seed) for seed in ("1", "2", "3", "4", "5")}
    assert len(outs) > 1


def test_every_row_carries_its_rdr_forms(fixture_roots: inv.Roots) -> None:
    for r in _gen(fixture_roots)["rows"]:
        assert r["rdr_row"], r
        assert r["new_form"] and r["fallback_form"], r


def test_line_numbers_do_not_count_as_a_difference(fixture_roots: inv.Roots, tmp_path: Path) -> None:
    base = _gen(fixture_roots)
    shifted = build_fixture(tmp_path / "shifted")
    java = shifted.java_dir / "dev/nexus/service/db/Repo.java"
    java.write_text("// a new leading comment line\n\n" + java.read_text(encoding="utf-8"), encoding="utf-8")
    assert inv.compare(base, _gen(shifted)) == []


def _changelog_roots(tmp_path: Path, *sqls: str) -> inv.Roots:
    """A one-changeset-per-statement-group changelog tree and an empty Java tree."""
    root = tmp_path / "t"
    wrap = '<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog">{cs}</databaseChangeLog>'
    master = "".join(f'<include file="db/changelog/c{i}.xml"/>' for i in range(len(sqls)))
    _write(root, "cl/db.changelog-master.xml", wrap.format(cs=master))
    for i, sql in enumerate(sqls):
        _write(root, f"cl/c{i}.xml", wrap.format(
            cs=f'<changeSet id="c{i}" author="t"><sql splitStatements="false">{sql}</sql></changeSet>'))
    _write(root, "java/X.java", "class X {}\n")
    return inv.Roots(repo=root, changelog_dir=root / "cl", java_dir=root / "java", anchors=())


def test_create_table_inline_constraints_become_rows(tmp_path: Path) -> None:
    roots = _changelog_roots(tmp_path, (
        "CREATE TABLE nexus.chunk_orphaned_at (tenant_id text, collection text, chash bytea, "
        "CONSTRAINT chunk_orphaned_at_pk PRIMARY KEY (tenant_id, collection, chash), "
        "CONSTRAINT chunk_orphaned_at_chunk_fk FOREIGN KEY (tenant_id, collection, chash) "
        "REFERENCES nexus.chunks (tenant_id, collection, chash) ON DELETE CASCADE ON UPDATE CASCADE);"))
    rows = {r["name"]: r for r in _gen(roots)["rows"]}
    assert rows["chunk_orphaned_at_pk"]["rdr_row"] == "P-PK"
    fk = rows["chunk_orphaned_at_chunk_fk"]
    assert fk["kind"] == "fk" and fk["rdr_row"] == "T03" and fk["tables"] == ["chunk_orphaned_at", "chunks"]
    assert fk["sites"][0]["detail"] == "ON DELETE CASCADE ON UPDATE CASCADE"


def test_rls_liveness_only_the_last_force_is_live(tmp_path: Path) -> None:
    roots = _changelog_roots(
        tmp_path,
        "ALTER TABLE nexus.chunks ENABLE ROW LEVEL SECURITY; ALTER TABLE nexus.chunks FORCE ROW LEVEL SECURITY;",
        "ALTER TABLE nexus.chunks NO FORCE ROW LEVEL SECURITY; DELETE FROM nexus.chunks;"
        "ALTER TABLE nexus.chunks FORCE ROW LEVEL SECURITY;",
    )
    rls = [(r["changeset"], r["name"], r["live"]) for r in _gen(roots)["rows"] if r["kind"] == "rls"]
    assert ("c0", "force", False) in rls and ("c1", "force", True) in rls and ("c0", "enable", True) in rls
    assert ("c1", "no force", None) in rls


def test_drop_function_with_an_argument_list_kills_only_that_overload(tmp_path: Path) -> None:
    roots = _changelog_roots(
        tmp_path,
        "CREATE FUNCTION nexus.f(a int) RETURNS int LANGUAGE sql AS $$ SELECT count(*)::int FROM nexus.chunks $$;"
        "CREATE FUNCTION nexus.f(a int, b int) RETURNS int LANGUAGE sql AS $$ SELECT count(*)::int FROM nexus.chunks $$;",
        "DROP FUNCTION IF EXISTS nexus.f(int);",
    )
    live = {r["argc"]: r["live"] for r in _gen(roots)["rows"] if r["kind"] == "function"}
    assert live == {1: False, 2: True}


def test_bulk_grant_loop_is_a_schema_wide_row_even_though_it_names_no_table(tmp_path: Path) -> None:
    roots = _changelog_roots(tmp_path, (
        "DO $$ DECLARE rel RECORD; BEGIN FOR rel IN SELECT n.nspname, c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname IN ('nexus', 't1') LOOP "
        "EXECUTE format('GRANT SELECT ON %I.%I TO nexus_svc', rel.nspname, rel.relname); END LOOP; END $$;"))
    (row,) = [r for r in _gen(roots)["rows"] if r["kind"] == "grant-schema-wide"]
    assert row["rdr_row"] == "T10" and row["tables"] == []


def test_prose_in_raise_and_comment_statements_is_not_a_reference(tmp_path: Path) -> None:
    roots = _changelog_roots(tmp_path, (
        "DO $$ BEGIN RAISE NOTICE 'deleted % rows from nexus.chunks'; END $$;"
        "COMMENT ON TABLE nexus.frecency IS 'rows that point at nexus.chunks';"))
    assert _gen(roots)["rows"] == []


def test_java_name_literal_rule() -> None:
    toks, _ = inv._strip_imports(inv.java_tokens(textwrap.dedent(
        """
        class A {
            void m() {
                body.get("chunks");
                Map.of("chunks", rows);
                count().as("topic_assignments");
                q.eq("chunks");
                Set.of("nexus.catalog_document_chunks", "other");
                new CollectionScopedTable("taxonomy_centroids", T);
                M.put("x_check", "catalog_document_chunks");
                log("'chunks' must be a non-empty array");
            }
        }
        """)))
    found = sorted((t, d) for t, d, _ in inv.java_name_literals(toks))
    assert found == [
        ("catalog_document_chunks", "M.put("),
        ("catalog_document_chunks", "Set.of("),
        ("chunks", "q.eq("),
        ("taxonomy_centroids", "new CollectionScopedTable("),
    ]


def test_java_imports_are_not_usage() -> None:
    assert not inv.java_file_names_a_table(
        "import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;\nclass A {}\n", None)
    assert inv.java_file_names_a_table(
        "import static x.Tables.CHUNKS;\nclass A { void m(Object c) { c.deleteFrom(CHUNKS); } }\n", None)


def test_a_runalways_changeset_is_never_historical(tmp_path: Path) -> None:
    root = tmp_path / "t"
    _write(root, "cl/db.changelog-master.xml",
           '<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog"><include file="db/changelog/g.xml"/></databaseChangeLog>')
    _write(root, "cl/g.xml",
           '<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog">'
           '<changeSet id="g1" author="t" runAlways="true"><sql splitStatements="false">'
           "DO $$ BEGIN EXECUTE 'GRANT MAINTAIN ON nexus.chunks TO nexus_svc'; END $$;</sql></changeSet>"
           '<changeSet id="g2" author="t"><sql>DELETE FROM nexus.chunks;</sql></changeSet>'
           "</databaseChangeLog>")
    _write(root, "java/X.java", "class X {}\n")
    roots = inv.Roots(repo=root, changelog_dir=root / "cl", java_dir=root / "java", anchors=())
    by_cs = {r["changeset"]: r for r in _gen(roots)["rows"]}
    assert by_cs["g1"]["run_always"] and by_cs["g1"]["rdr_row"] == "T10"
    assert by_cs["g2"]["rdr_row"] == "X2"


# ---------------------------------------------------------------------------
# The planted-difference self-test
# ---------------------------------------------------------------------------


def _append_before_end(path: Path, addition: str, end: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert end in text
    path.write_text(text.replace(end, addition + end, 1), encoding="utf-8")


def _new_changeset(roots: inv.Roots, sql: str, cs_id: str) -> None:
    _append_before_end(
        roots.changelog_dir / "b-001-functions.xml",
        f'<changeSet id="{cs_id}" author="plant"><sql splitStatements="false">{sql}</sql></changeSet>\n',
        "</databaseChangeLog>",
    )


def _java_method(roots: inv.Roots, body: str) -> None:
    path = roots.java_dir / "dev/nexus/service/db/Repo.java"
    text = path.read_text(encoding="utf-8")
    idx = text.rindex("}")
    path.write_text(text[:idx] + body + "\n}\n", encoding="utf-8")


def _plant_fk_chunks(r: inv.Roots) -> None:
    _new_changeset(r, "ALTER TABLE nexus.some_other ADD CONSTRAINT plant_fk_into_chunks "
                      "FOREIGN KEY (a, b, c) REFERENCES nexus.chunks (tenant_id, collection, chash);", "p1")


def _plant_chunks_writer(r: inv.Roots) -> None:
    _new_changeset(r, "CREATE OR REPLACE FUNCTION nexus.plant_chunk_writer() RETURNS void LANGUAGE sql AS $$ "
                      "INSERT INTO nexus.chunks (tenant_id, collection, chash) VALUES ('t','c','h') "
                      "ON CONFLICT (tenant_id, collection, chash) DO NOTHING $$;", "p2")


def _plant_manifest_conflict_target(r: inv.Roots) -> None:
    p = r.changelog_dir / "b-001-functions.xml"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "ON CONFLICT (tenant_id, collection, chash) DO NOTHING;\n            SET CONSTRAINTS",
        "ON CONFLICT (tenant_id, collection, chash, embedding_model) DO NOTHING;\n            SET CONSTRAINTS", 1),
        encoding="utf-8")


def _plant_manifest_trigger(r: inv.Roots) -> None:
    _new_changeset(r, "CREATE TRIGGER plant_manifest_trg AFTER INSERT ON nexus.catalog_document_chunks "
                      "FOR EACH ROW EXECUTE FUNCTION nexus.upsert_manifest();", "p4")


def _plant_topic_assignments_java(r: inv.Roots) -> None:
    _java_method(r, '    void plantTopic(Object c) { c.execute("UPDATE nexus.topic_assignments SET collection = ? WHERE true"); }')


def _plant_topic_assignments_sql(r: inv.Roots) -> None:
    _new_changeset(r, "DELETE FROM nexus.topic_assignments WHERE topic_id = -1;", "p6")


def _plant_orphaned_at_sql(r: inv.Roots) -> None:
    _new_changeset(r, "INSERT INTO nexus.chunk_orphaned_at (tenant_id, collection, chash) "
                      "SELECT tenant_id, collection, chash FROM nexus.catalog_document_chunks;", "p7")


def _plant_orphaned_at_java(r: inv.Roots) -> None:
    _java_method(r, "    void plantOrphan(Object c) { c.deleteFrom(CHUNK_ORPHANED_AT).where(1).execute(); }")


def _plant_centroids_view(r: inv.Roots) -> None:
    _new_changeset(r, "CREATE VIEW nexus.plant_centroid_view AS SELECT * FROM nexus.taxonomy_centroids;", "p9")


def _plant_centroids_java(r: inv.Roots) -> None:
    _java_method(r, '    void plantCentroid(Object c) { c.execute("DELETE FROM nexus.taxonomy_centroids WHERE true"); }')


def _plant_manifest_java_column_list(r: inv.Roots) -> None:
    p = r.java_dir / "dev/nexus/service/db/Repo.java"
    p.write_text(p.read_text(encoding="utf-8").replace("(tenant_id, collection, chash) \"\n            + \"VALUES",
                                       "(tenant_id, collection, chash, embedding_model) \"\n            + \"VALUES", 1),
                      encoding="utf-8")


def _plant_supersession(r: inv.Roots) -> None:
    _new_changeset(r, "CREATE OR REPLACE FUNCTION nexus.write_chunk(p_chash text) RETURNS void "
                      "LANGUAGE sql AS $$ SELECT 1 $$;", "p12")


def _plant_chunks_java_raw(r: inv.Roots) -> None:
    _java_method(r, '    void plantChunkRaw(Object c) { c.execute("INSERT INTO nexus.chunks (tenant_id, collection, chash) '
                    'VALUES (?, ?, ?) ON CONFLICT (tenant_id, collection, chash) DO NOTHING"); }')


def _plant_chunks_java_typed(r: inv.Roots) -> None:
    _java_method(r, "    void plantChunkTyped(Object c) { c.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH)"
                    ".values(1, 2, 3).onConflict(CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH).doNothing().execute(); }")


def _plant_manifest_java_writer(r: inv.Roots) -> None:
    _java_method(r, '    void plantManifest(Object c) { c.execute("DELETE FROM nexus.catalog_document_chunks WHERE true"); }')


def _plant_quoted_identifier_reader(r: inv.Roots) -> None:
    _new_changeset(r, 'SELECT count(*) FROM nexus."topic_assignments";', "pq")


def _plant_jooq_record_class(r: inv.Roots) -> None:
    _java_method(r, "    void plantRecord() { ChunksRecord rec = new ChunksRecord(); rec.store(); }")


def _plant_removed_site(r: inv.Roots) -> None:
    p = r.changelog_dir / "a-001-baseline.xml"
    p.write_text(p.read_text(encoding="utf-8").replace("GRANT SELECT ON nexus.chunks TO nexus_diag;", "", 1), encoding="utf-8")


def _plant_regclass_literal(r: inv.Roots) -> None:
    _new_changeset(r, "SELECT to_regclass('nexus.topic_assignments');", "p14")


PLANTS = {
    # name: (apply, substring the diff must mention)
    "fk-into-chunks": (_plant_fk_chunks, "plant_fk_into_chunks"),
    "chunks-writer-function": (_plant_chunks_writer, "plant_chunk_writer"),
    "manifest-conflict-target-changed": (_plant_manifest_conflict_target, "upsert_manifest"),
    "manifest-trigger": (_plant_manifest_trigger, "plant_manifest_trg"),
    "topic-assignments-java-writer": (_plant_topic_assignments_java, "Repo/plantTopic"),
    "topic-assignments-sql-delete": (_plant_topic_assignments_sql, "b-001-functions.xml#p6"),
    "chunk-orphaned-at-sql-writer": (_plant_orphaned_at_sql, "b-001-functions.xml#p7"),
    "chunk-orphaned-at-java-jooq-writer": (_plant_orphaned_at_java, "Repo/plantOrphan"),
    "centroids-view": (_plant_centroids_view, "plant_centroid_view"),
    "centroids-java-writer": (_plant_centroids_java, "Repo/plantCentroid"),
    "manifest-java-column-list-changed": (_plant_manifest_java_column_list, "Repo/upsertManifest"),
    "function-superseded": (_plant_supersession, "write_chunk"),
    "site-removed": (_plant_removed_site, "a-001-baseline.xml#a-1"),
    "name-literal-regclass": (_plant_regclass_literal, "b-001-functions.xml#p14"),
    "chunks-java-raw-writer": (_plant_chunks_java_raw, "Repo/plantChunkRaw"),
    "chunks-java-typed-writer": (_plant_chunks_java_typed, "Repo/plantChunkTyped"),
    "manifest-java-raw-writer": (_plant_manifest_java_writer, "Repo/plantManifest"),
    "quoted-identifier-reader": (_plant_quoted_identifier_reader, "b-001-functions.xml#pq"),
    "jooq-record-class": (_plant_jooq_record_class, "Repo/plantRecord"),
}


def test_unchanged_fixture_reports_no_difference(fixture_roots: inv.Roots, tmp_path: Path) -> None:
    """Non-vacuity of the empty result: an untouched copy compares clean, so a
    clean diff on the real tree means something."""
    twin = build_fixture(tmp_path / "twin")
    assert inv.compare(_gen(fixture_roots), _gen(twin)) == []


@pytest.mark.parametrize("plant", sorted(PLANTS))
def test_planted_difference_is_found(plant: str, fixture_roots: inv.Roots, tmp_path: Path) -> None:
    apply, marker = PLANTS[plant]
    baseline = _gen(fixture_roots)
    mutated = build_fixture(tmp_path / "mutated")
    apply(mutated)
    diff = inv.compare(baseline, _gen(mutated))
    assert diff, f"plant {plant!r} produced no difference"
    assert any(marker in line for line in diff), (plant, marker, diff)


def test_plants_cover_all_five_tables_and_both_languages(fixture_roots: inv.Roots, tmp_path: Path) -> None:
    """Guard on the self-test itself. The plants between them must make the generator report a
    difference naming every one of the five tables from SQL, and a WRITE (INSERT, UPDATE or
    DELETE) to every one of the five from Java: a Java row that merely mentions a table (a
    SET CONSTRAINTS site, a read) is not a writer plant."""
    baseline = _gen(fixture_roots)
    before = {r["id"]: r for r in baseline["rows"]}
    seen_sql: set[str] = set()
    java_writes: set[str] = set()
    for name, (apply, _) in sorted(PLANTS.items()):
        mutated = build_fixture(tmp_path / f"cov-{name}")
        apply(mutated)
        for row in _gen(mutated)["rows"]:
            if before.get(row["id"]) == row:
                continue
            if row["source"] == "java":
                java_writes |= {s["table"] for s in row["sites"] if s["verb"] in {"INSERT", "UPDATE", "DELETE"}}
            else:
                seen_sql |= set(row["tables"])
    every = set(inv.TABLES)
    assert seen_sql >= every, every - seen_sql
    assert java_writes >= every, every - java_writes


def test_java_writer_plants_record_the_writes_they_plant(fixture_roots: inv.Roots, tmp_path: Path) -> None:
    """The typed and the raw writer to chunks both come out as an INSERT site with the
    same column list and conflict arbiter, so a typed site compares with a raw one."""
    for plant, method in ((_plant_chunks_java_raw, "Repo/plantChunkRaw"), (_plant_chunks_java_typed, "Repo/plantChunkTyped")):
        mutated = build_fixture(tmp_path / method.split("/")[1])
        plant(mutated)
        row = next(r for r in _gen(mutated)["rows"] if r["name"] == method)
        (site,) = [s for s in row["sites"] if s["verb"] == "INSERT"]
        assert site["table"] == "chunks"
        assert site["columns"] == "tenant_id, collection, chash"
        assert site["conflict"] == "(tenant_id, collection, chash) DO NOTHING"


def test_compare_reports_a_change_to_the_tracked_constraints_and_to_td_rows(fixture_roots: inv.Roots) -> None:
    """``uncovered`` reads ``constraints_tracked`` from the PINNED file, so a drift there
    must be a reported difference or the crude detector keeps reading a stale list."""
    base = _gen(fixture_roots)
    changed = copy.deepcopy(base)
    changed["constraints_tracked"]["a_new_fk"] = "chunks"
    assert any("constraints_tracked" in line for line in inv.compare(base, changed))
    assert inv.compare(base, copy.deepcopy(base)) == []


# ---------------------------------------------------------------------------
# Guard blind spots: quoted identifiers, native change elements, jOOQ records
# ---------------------------------------------------------------------------

QUOTED_SPELLINGS = ['nexus."chunks"', '"nexus"."chunks"', '"chunks"', 'nexus."CHUNKS"']


@pytest.mark.parametrize("spelling", QUOTED_SPELLINGS)
def test_quoted_identifiers_are_matched_by_the_generator_and_the_crude_detector(spelling: str) -> None:
    sql = f"SELECT 1 FROM {spelling} WHERE true"
    assert [s["table"] for s in inv.extract_sites(sql)] == ["chunks"]
    assert inv.NAMED.search(sql) is not None
    assert inv._CRUDE_SQL.search(sql) is not None
    assert [s["verb"] for s in inv.extract_sites(f"INSERT INTO {spelling} (a) VALUES (1)")] == ["INSERT"]
    assert [s["verb"] for s in inv.extract_sites(f"ALTER TABLE {spelling} ENABLE ROW LEVEL SECURITY")] == ["ALTER TABLE"]


@pytest.mark.parametrize("spelling", QUOTED_SPELLINGS)
def test_a_changeset_naming_a_table_in_quotes_is_a_row_and_a_crude_location(tmp_path: Path, spelling: str) -> None:
    roots = _changelog_roots(tmp_path, f"CREATE INDEX idx_q ON {spelling} (tenant_id);")
    rows = _gen(roots)["rows"]
    assert [(r["kind"], r["tables"]) for r in rows] == [("index", ["chunks"])]
    assert inv.crude_locations(roots) == {"changelog:c0.xml#c0"}


def test_a_quoted_name_in_java_sql_is_a_site(tmp_path: Path) -> None:
    root = tmp_path / "q"
    _write(root, "cl/db.changelog-master.xml",
           '<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog"></databaseChangeLog>')
    _write(root, "java/Q.java",
           'class Q { void m(Object c) { c.execute("SELECT 1 FROM nexus.\\"chunks\\" WHERE true"); } }\n')
    roots = inv.Roots(repo=root, changelog_dir=root / "cl", java_dir=root / "java", anchors=())
    (row,) = _gen(roots)["rows"]
    assert row["name"] == "Q/m" and row["tables"] == ["chunks"]
    assert inv.crude_locations(roots) == {"java:Q.java"}


def _raw_changelog_roots(tmp_path: Path, *changesets: str, extra_files: dict[str, str] | None = None) -> inv.Roots:
    """A repo-shaped tree (default roots) with one changelog file per raw changeset XML."""
    root = tmp_path / "raw"
    cl = "service/src/main/resources/db/changelog"
    wrap = '<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog">{cs}</databaseChangeLog>'
    master = "".join(f'<include file="db/changelog/n{i}.xml"/>' for i in range(len(changesets)))
    _write(root, f"{cl}/db.changelog-master.xml", wrap.format(cs=master))
    for i, cs in enumerate(changesets):
        _write(root, f"{cl}/n{i}.xml", wrap.format(cs=cs))
    _write(root, "service/src/main/java/X.java", "class X {}\n")
    for rel, text in (extra_files or {}).items():
        _write(root, rel, text)
    return dataclasses.replace(inv.default_roots(root), anchors=())


def _cs(inner: str, cs_id: str = "n0") -> str:
    return f'<changeSet id="{cs_id}" author="t">{inner}</changeSet>'


NATIVE_ELEMENTS = {
    "addColumn": '<addColumn schemaName="nexus" tableName="chunks"><column name="embedding_model" type="text"/></addColumn>',
    "createIndex": '<createIndex tableName="topic_assignments" indexName="i"><column name="a"/></createIndex>',
    "addForeignKeyConstraint": '<addForeignKeyConstraint baseTableName="catalog_document_chunks" baseColumnNames="a" '
                               'constraintName="f" referencedTableName="chunks" referencedColumnNames="a"/>',
    "insert": '<insert tableName="chunk_orphaned_at"><column name="a" value="1"/></insert>',
    "update": '<update tableName="taxonomy_centroids"><column name="a" value="1"/></update>',
    "dropTable": '<dropTable tableName="nexus.chunks"/>',
    "renameTable": '<renameTable oldTableName="chunks" newTableName="chunks_retired_225"/>',
}


@pytest.mark.parametrize("element", sorted(NATIVE_ELEMENTS))
def test_a_native_change_element_naming_a_table_is_unscannable_and_fails_the_check(tmp_path: Path, element: str) -> None:
    roots = _raw_changelog_roots(tmp_path, _cs(NATIVE_ELEMENTS[element]))
    found = inv.unscannable(roots)
    assert len(found) == 1 and "n0.xml#n0" in found[0] and element in found[0], found
    fresh = _gen(roots)
    assert fresh["unscannable"] == found
    # whatever the pinned file says, a tree with an unscannable element is a problem
    assert any("UNSCANNABLE" in p for p in inv.problems(roots, fresh))


def test_a_native_change_element_naming_no_table_is_not_a_problem(tmp_path: Path) -> None:
    roots = _raw_changelog_roots(
        tmp_path, _cs('<addColumn tableName="frecency"><column name="a" type="text"/></addColumn>'
                      "<sql>SELECT 1</sql>"))
    assert inv.unscannable(roots) == []
    assert inv.problems(roots, _gen(roots)) == []


@pytest.mark.parametrize("element", ['<sqlFile path="x.sql" relativeToChangelogFile="true"/>',
                                     '<customChange class="dev.nexus.Custom"/>'])
def test_sqlfile_and_customchange_are_unscannable_even_when_they_name_no_table(tmp_path: Path, element: str) -> None:
    roots = _raw_changelog_roots(tmp_path, _cs(element))
    found = inv.unscannable(roots)
    assert len(found) == 1 and "n0.xml#n0" in found[0], found
    assert any("UNSCANNABLE" in p for p in inv.problems(roots, _gen(roots)))


def test_a_native_element_inside_a_rollback_or_precondition_is_unscannable_too(tmp_path: Path) -> None:
    roots = _raw_changelog_roots(
        tmp_path,
        _cs('<preConditions onFail="MARK_RAN"><tableExists tableName="chunks"/></preConditions><sql>SELECT 1</sql>', "n0"),
        _cs('<sql>SELECT 1</sql><rollback><dropTable tableName="topic_assignments"/></rollback>', "n1"),
    )
    found = inv.unscannable(roots)
    assert len(found) == 2 and any("n0.xml#n0" in f for f in found) and any("n1.xml#n1" in f for f in found), found


def test_writing_the_pinned_file_refuses_a_tree_with_an_unscannable_element(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots = _raw_changelog_roots(tmp_path, _cs(NATIVE_ELEMENTS["addColumn"]))
    monkeypatch.setattr(inv, "default_roots", lambda repo=roots.repo: roots)  # no Python anchors in a tmp tree
    out = tmp_path / "out" / "pinned.json"
    assert inv.main(["--repo", str(roots.repo), "--pinned", str(out), "--write", "--source-sha", "x"]) != 0
    assert not out.exists()


def test_the_crude_detector_does_not_depend_on_the_changelog_parser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It reads the raw changeset text: a parser that returned nothing, or raised, must not
    blind it. Attribute-only references (a native tag) and prose in <comment> are the two
    sides: the first is found, the second is not."""
    roots = _raw_changelog_roots(
        tmp_path,
        _cs("<sql>DELETE FROM nexus.chunks;</sql>", "plain"),
        _cs('<addColumn tableName="topic_assignments"><column name="a" type="text"/></addColumn>', "native"),
        _cs("<comment>nexus.chunks is only prose here</comment><sql>SELECT 1</sql>", "prose"),
        _cs("<sql>-- nexus.chunks in a SQL comment\nSELECT 1</sql>", "sqlcomment"),
    )

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("crude_locations must not call parse_changelog")

    monkeypatch.setattr(inv, "parse_changelog", boom)
    assert inv.crude_locations(roots) == {"changelog:n0.xml#plain", "changelog:n1.xml#native"}


RECORD_NAMES = ["ChunksRecord", "CatalogDocumentChunksRecord", "TopicAssignmentsRecord", "TaxonomyCentroidsRecord",
                "ChunkOrphanedAtRecord"]


def test_jooq_record_class_names_are_exactly_the_generated_ones() -> None:
    assert sorted(inv.JOOQ_RECORD_CLASSES) == sorted(RECORD_NAMES)
    assert sorted(inv.JOOQ_RECORD_CLASSES.values()) == sorted(inv.TABLES)


@pytest.mark.parametrize("record", RECORD_NAMES)
def test_a_jooq_record_class_names_its_table_in_java(tmp_path: Path, record: str) -> None:
    root = tmp_path / "r"
    _write(root, "cl/db.changelog-master.xml",
           '<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog"></databaseChangeLog>')
    _write(root, "java/R.java",
           f"import dev.nexus.service.jooq.nexus.tables.records.{record};\n"
           f"class R {{ void m() {{ {record} r = new {record}(); r.store(); }} }}\n"
           f"class OnlyImported {{ }}\n")
    roots = inv.Roots(repo=root, changelog_dir=root / "cl", java_dir=root / "java", anchors=())
    (row,) = _gen(roots)["rows"]
    assert row["name"] == "R/m"
    assert row["tables"] == [inv.JOOQ_RECORD_CLASSES[record]]
    assert [s["verb"] for s in row["sites"]] == ["TYPED-REF"]
    assert inv.crude_locations(roots) == {"java:R.java"}


def test_an_imported_but_unused_jooq_record_class_is_not_a_reference() -> None:
    assert not inv.java_file_names_a_table(
        "import dev.nexus.service.jooq.nexus.tables.records.ChunksRecord;\nclass A { }\n", None)


# ---------------------------------------------------------------------------
# Splitting that respects quotes and brackets
# ---------------------------------------------------------------------------


def test_split_top_respects_brackets_and_quotes() -> None:
    assert inv.split_top("a text[] DEFAULT ARRAY['a','b'], b int DEFAULT 3") == [
        "a text[] DEFAULT ARRAY['a','b']", "b int DEFAULT 3"]
    assert inv.split_top("a text DEFAULT 'x,y', b text DEFAULT 'it''s, ok'") == [
        "a text DEFAULT 'x,y'", "b text DEFAULT 'it''s, ok'"]
    assert inv.split_top('"a,b" int, c int') == ['"a,b" int', "c int"]
    assert inv.split_top("f(1, 2), g[1, 2], h") == ["f(1, 2)", "g[1, 2]", "h"]


def test_balanced_skips_parentheses_inside_quotes() -> None:
    text = "(a text DEFAULT ')', b text DEFAULT '(') tail"
    assert text[:inv.balanced(text, 0)] == "(a text DEFAULT ')', b text DEFAULT '(')"


def test_function_argument_count_ignores_commas_in_array_defaults(tmp_path: Path) -> None:
    roots = _changelog_roots(tmp_path, (
        "CREATE FUNCTION nexus.f(a text[] DEFAULT ARRAY['a','b'], b int DEFAULT 3, OUT c text) RETURNS text "
        "LANGUAGE sql AS $$ SELECT count(*)::text FROM nexus.chunks $$;"
        "CREATE FUNCTION nexus.g(a text DEFAULT 'x,y') RETURNS int LANGUAGE sql AS $$ SELECT count(*)::int FROM nexus.chunks $$;"
        "DROP FUNCTION nexus.g(text[]);"))
    argc = {r["name"]: r["argc"] for r in _gen(roots)["rows"] if r["kind"] == "function"}
    assert argc == {"f": 2, "g": 1}


# ---------------------------------------------------------------------------
# Liveness under rename-based swaps (RDR steps 7.2, 7.4, 7.5)
# ---------------------------------------------------------------------------


def _by(rows: list[dict], kind: str, name: str, changeset: str | None = None) -> dict:
    (hit,) = [r for r in rows if r["kind"] == kind and r["name"] == name and changeset in (None, r["changeset"])]
    return hit


def test_renaming_a_table_away_makes_its_dependents_not_live(tmp_path: Path) -> None:
    roots = _changelog_roots(
        tmp_path,
        "CREATE TABLE nexus.chunks (tenant_id text, chash bytea);"
        "CREATE INDEX idx_chunks_t ON nexus.chunks (tenant_id);"
        "ALTER TABLE nexus.chunks ADD CONSTRAINT chunks_extra_chk CHECK (tenant_id IS NOT NULL);"
        "CREATE TRIGGER trg_chunks AFTER INSERT ON nexus.chunks FOR EACH ROW EXECUTE FUNCTION nexus.f();"
        "CREATE POLICY tenant_isolation ON nexus.chunks USING (true);",
        "ALTER TABLE nexus.chunks RENAME TO chunks_retired_225;",
    )
    rows = _gen(roots)["rows"]
    assert _by(rows, "table", "chunks")["live"] is False
    assert _by(rows, "index", "idx_chunks_t")["live"] is False
    assert _by(rows, "constraint", "chunks_extra_chk")["live"] is False
    assert _by(rows, "trigger", "trg_chunks")["live"] is False
    assert _by(rows, "policy", "tenant_isolation")["live"] is False
    (rename,) = [r for r in rows if r["kind"] == "rename-table"]
    assert rename["tables"] == ["chunks"] and rename["rdr_row"] == "X2"


def test_the_rename_target_is_a_definition_until_it_is_dropped(tmp_path: Path) -> None:
    roots = _changelog_roots(
        tmp_path,
        "CREATE TABLE nexus.chunks_new (tenant_id text, chash bytea);",
        "ALTER TABLE nexus.chunks_new RENAME TO chunks;",
        "DROP TABLE nexus.chunks;",
    )
    live = [r for r in _gen(roots)["rows"] if r["kind"] == "rename-table"]
    assert [(r["changeset"], r["live"]) for r in live] == [("c1", False)]
    only = _changelog_roots(tmp_path / "again", "CREATE TABLE nexus.chunks_new (tenant_id text);",
                            "ALTER TABLE nexus.chunks_new RENAME TO chunks;")
    (rename,) = [r for r in _gen(only)["rows"] if r["kind"] == "rename-table"]
    assert rename["live"] is True and rename["tables"] == ["chunks"]


def test_a_table_recreated_after_a_rename_does_not_revive_the_old_dependents(tmp_path: Path) -> None:
    roots = _changelog_roots(
        tmp_path,
        "CREATE TABLE nexus.chunks (tenant_id text); CREATE INDEX idx_old ON nexus.chunks (tenant_id);",
        "ALTER TABLE nexus.chunks RENAME TO chunks_retired_225;",
        "CREATE TABLE nexus.chunks (tenant_id text, embedding_model text); CREATE INDEX idx_new ON nexus.chunks (tenant_id);",
    )
    rows = _gen(roots)["rows"]
    assert _by(rows, "index", "idx_old")["live"] is False
    assert _by(rows, "index", "idx_new")["live"] is True
    assert _by(rows, "table", "chunks", "c0")["live"] is False and _by(rows, "table", "chunks", "c2")["live"] is True


def test_renaming_an_index_marks_the_old_name_not_live(tmp_path: Path) -> None:
    roots = _changelog_roots(
        tmp_path,
        "CREATE INDEX idx_chunks_old ON nexus.chunks (tenant_id); CREATE INDEX idx_chunks_keep ON nexus.chunks (tenant_id);"
        "CREATE INDEX idx_chunks_if ON nexus.chunks (tenant_id);",
        "ALTER INDEX nexus.idx_chunks_old RENAME TO idx_chunks_old_retired_225;"
        "ALTER INDEX IF EXISTS idx_chunks_if RENAME TO idx_chunks_if_retired_225;",
    )
    rows = _gen(roots)["rows"]
    old = _by(rows, "index", "idx_chunks_old")
    assert old["live"] is False and old["live_in"].endswith("#c1")
    assert _by(rows, "index", "idx_chunks_if")["live"] is False
    assert _by(rows, "index", "idx_chunks_keep")["live"] is True


def test_renaming_a_constraint_drops_the_old_name_and_defines_the_new_one(tmp_path: Path) -> None:
    roots = _changelog_roots(
        tmp_path,
        "ALTER TABLE nexus.chunks ADD CONSTRAINT chunks_old_chk CHECK (tenant_id IS NOT NULL);",
        "ALTER TABLE nexus.chunks RENAME CONSTRAINT chunks_old_chk TO chunks_new_chk;",
    )
    rows = _gen(roots)["rows"]
    old = _by(rows, "constraint", "chunks_old_chk", "c0")
    assert old["live"] is False and old["live_in"].endswith("#c1")
    new = _by(rows, "constraint", "chunks_new_chk")
    assert new["live"] is True and new["op"] == "rename-to" and new["renamed_from"] == "chunks_old_chk"
    assert new["host"] == "chunks"


def test_renaming_a_constraint_to_a_name_that_is_later_dropped(tmp_path: Path) -> None:
    roots = _changelog_roots(
        tmp_path,
        "ALTER TABLE nexus.chunks ADD CONSTRAINT chunks_old_chk CHECK (tenant_id IS NOT NULL);",
        "ALTER TABLE nexus.chunks RENAME CONSTRAINT chunks_old_chk TO chunks_new_chk;",
        "ALTER TABLE nexus.chunks DROP CONSTRAINT chunks_new_chk;",
    )
    assert _by(inv_rows := _gen(roots)["rows"], "constraint", "chunks_new_chk", "c1")["live"] is False
    assert inv_rows


# ---------------------------------------------------------------------------
# Python registries, recorded entry by entry
# ---------------------------------------------------------------------------

REGISTRY_PY = '''\
REG: tuple[str, ...] = (
    "nexus.aspect_queue",
    # "nexus.topic_assignments" was removed here; a comment must not count
    "nexus.chunks",
    "nexus.catalog_document_chunks",
)

OTHER: tuple[str, ...] = (
    "nexus.chunks",
)
'''


def _registry_roots(tmp_path: Path, text: str = REGISTRY_PY) -> inv.Roots:
    root = tmp_path / "reg"
    _write(root, "cl/db.changelog-master.xml",
           '<databaseChangeLog xmlns="http://www.liquibase.org/xml/ns/dbchangelog"></databaseChangeLog>')
    _write(root, "java/X.java", "class X {}\n")
    _write(root, "reg.py", text)
    return inv.Roots(repo=root, changelog_dir=root / "cl", java_dir=root / "java",
                     anchors=(inv.Registry("reg.py", r"^REG: tuple", "REG"), inv.Registry("reg.py", r"^OTHER: tuple", "OTHER")))


def test_a_registry_records_every_entry_and_every_matching_entry(tmp_path: Path) -> None:
    rows = {r["id"]: r for r in _gen(_registry_roots(tmp_path))["rows"] if r["source"] == "python"}
    assert sorted(rows) == [
        "python:reg.py#OTHER", "python:reg.py#OTHER:nexus.chunks",
        "python:reg.py#REG", "python:reg.py#REG:nexus.catalog_document_chunks", "python:reg.py#REG:nexus.chunks",
    ]
    container = rows["python:reg.py#REG"]
    assert container["entries"] == ['"nexus.aspect_queue"', '"nexus.chunks"', '"nexus.catalog_document_chunks"']
    assert container["tables"] == ["catalog_document_chunks", "chunks"]
    assert rows["python:reg.py#OTHER:nexus.chunks"]["tables"] == ["chunks"]


@pytest.mark.parametrize("edit,expect", [
    (lambda t: t.replace('    "nexus.chunks",\n    "nexus.catalog', '    "nexus.chunks",\n    "nexus.topic_assignments",\n    "nexus.catalog', 1),
     "NEW in this tree"),
    (lambda t: t.replace('    "nexus.catalog_document_chunks",\n', "", 1), "MISSING from this tree"),
    (lambda t: t.replace('    "nexus.aspect_queue",\n', '    "nexus.aspect_queue",\n    "nexus.unrelated",\n', 1), "CHANGED python:reg.py#REG"),
    (lambda t: t.replace('    "nexus.aspect_queue",\n', "", 1), "CHANGED python:reg.py#REG"),
], ids=["table-entry-added", "table-entry-removed", "other-entry-added", "other-entry-removed"])
def test_adding_or_removing_a_registry_entry_changes_the_inventory(tmp_path: Path, edit, expect: str) -> None:
    base = _gen(_registry_roots(tmp_path / "a"))
    mutated = _gen(_registry_roots(tmp_path / "b", edit(REGISTRY_PY)))
    diff = inv.compare(base, mutated)
    assert any(expect in line for line in diff), diff


def test_a_registry_that_moved_fails_loudly(tmp_path: Path) -> None:
    roots = _registry_roots(tmp_path, REGISTRY_PY.replace("REG: tuple", "RENAMED: tuple"))
    with pytest.raises(SystemExit, match="REG"):
        _gen(roots)
