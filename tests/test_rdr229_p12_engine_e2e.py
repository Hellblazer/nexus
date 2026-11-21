# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P1.2 (nexus-p2m1x.2), end to end on the LOCAL engine substrate (a hermetic PG and the gate jar of this
tree; never a managed tenant): the ``uri_residue`` doctor row and ``nx catalog collections-census``, over rows seeded
with one of every shape the bead names, through the real HTTP client and the real routes.

The seeding that no client call can do (a ``chroma://`` value under a prefix-free segment, a document-aspect row, a
tombstone whose successor row is gone) goes through the substrate's own psql, as a superuser, into this test's minted
tenant only.
"""
from __future__ import annotations

import json
import sys

import pytest
from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from nexus.commands import doctor as doctor_mod
from tests._catalog_fixture_ops import _run_psql
from tests._chunk_seed import collection_id_sql

pytestmark = pytest.mark.t2_tenant

LABEL = "e2elabel"                                        # a prefix-free label, content type docs, grandfathered
LABEL2 = "e2elabel2"                                      # the same tuple as LABEL, not grandfathered
DOCS = "docs__99-1__bge-base-en-v15-768__v1"
DOCS_V2 = "docs__99-1__bge-base-en-v15-768__v2"
KNOW = "knowledge__e2esub__bge-base-en-v15-768__v1"
CODE_BAD = "code__99-2__bge-base-en-v15-768__v1"          # its row says content_type docs: a mismatch
MINTED = "c123"
MINTED_SEED = "e2e-minted-seed"                           # registered under this name, then renamed to MINTED in SQL
TIE_LEGACY = "e2e-tie-label"                              # registered FIRST (older), name sorts after TIE_CONF
TIE_CONF = "docs__99-6__bge-base-en-v15-768__v1"          # same tuple and version, registered after: NAME ASC picks it
NOVER = "e2e-nover"                                       # a live row with an empty model_version
OLD = "docs__99-4__bge-base-en-v15-768__v1"               # tombstone whose successor row is deleted below
NEW = "docs__99-4__bge-base-en-v15-768__v2"


def _show(text: str) -> None:
    """The run's evidence, on stdout under ``-s``."""
    sys.stdout.write(text + "\n")


def _sql_str(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def _collection_id(tenant: str, collection: str) -> str:
    """RDR-229 P3.1: a row naming a collection carries the registry's id beside the name (the CHECKs on
    document_aspects and document_highlights); the registry row is made by ``register_collection`` above."""
    return collection_id_sql(tenant, collection)


def _seed(tenant: str) -> None:
    with HttpCatalogClient() as cat:
        model = "bge-base-en-v15-768"
        cat.register_collection(DOCS, content_type="docs", owner_id="99-1", embedding_model=model, model_version="v1")
        cat.register_collection(DOCS_V2, content_type="docs", owner_id="99-1", embedding_model=model, model_version="v2")
        cat.register_collection(KNOW, content_type="knowledge", owner_id="e2esub", embedding_model=model, model_version="v1")
        cat.register_collection(LABEL, content_type="docs", owner_id="99-5", embedding_model=model, model_version="v1")
        # RDR-229 P3.8: the engine refuses a second live collection on a tuple, so the two D4 pairs (LABEL/LABEL2, TIE_LEGACY/TIE_CONF)
        # register their second member at v2 and the SQL below sets it to v1, the shape an install that predates the guard carries
        cat.register_collection(LABEL2, content_type="docs", owner_id="99-5", embedding_model=model, model_version="v2")
        cat.register_collection(CODE_BAD, content_type="docs", owner_id="99-2", embedding_model=model, model_version="v1")
        # RDR-229 P3.6: the route refuses a NEW label of the minted form (c followed by digits), which only the engine
        # may mint, so MINTED registers under a seed name and the SQL below renames it to the form, the shape an install
        # that predates the rule carries.
        cat.register_collection(MINTED_SEED, content_type="code", owner_id="99-3", embedding_model=model, model_version="v1")
        cat.register_collection(TIE_LEGACY, content_type="docs", owner_id="99-6", embedding_model=model, model_version="v1")
        cat.register_collection(TIE_CONF, content_type="docs", owner_id="99-6", embedding_model=model, model_version="v2")
        # RDR-229 P2.5 (D1): the route refuses a NEW row with a blank model_version, so NOVER registers at v1
        # and the SQL below blanks it, the shape a legacy row still has on an install until D2 repairs it.
        cat.register_collection(NOVER, content_type="docs", owner_id="99-7", embedding_model=model, model_version="v1")
        cat.register_collection(OLD, content_type="docs", owner_id="99-4", embedding_model=model, model_version="v1")
        cat.register_collection(NEW, content_type="docs", owner_id="99-4", embedding_model=model, model_version="v2")
        assert cat.supersede_collection(OLD, NEW) == 1
        owner = cat.register_owner("p12-e2e-owner", owner_type="curator", tumbler_prefix="98.1")
        n = 0

        def doc(collection: str) -> str:
            nonlocal n
            n += 1
            return str(cat.register(owner, f"d{n}", content_type="prose", physical_collection=collection,
                                    source_uri=f"file:///p12e2e/d{n}.md", file_path=f"d{n}.md"))

        updates = []
        for collection, uri, count in (
            (LABEL, f"chroma://{LABEL}/a", 5),              # prefix-free, file-typed, segment is its collection
            (LABEL, "chroma://oldlabel/b", 2),              # ... segment is not its collection
            (KNOW, f"chroma://{KNOW}/k", 7),                # a legacy-spelled note
            (DOCS, f"chroma://{DOCS}/p", 11),               # file-routed segment: backfill-source-uri's residue
            (LABEL, f"nx-note://{LABEL}/n", 4),             # nx-note:// with a prefix-free segment, file-typed collection
            (KNOW, f"nx-note://{KNOW}/n", 13),              # an ordinary note
        ):
            for i in range(count):
                tumbler = doc(collection)
                updates.append((tumbler, f"{uri}{i}"))
        asp_hl_tumbler = doc(LABEL)
        aspect_docs = [doc(LABEL) for _ in range(3)]
    sql: list[str] = []  # _run_psql wraps the script in one transaction (-1)
    for tumbler, uri in updates:
        sql.append(f"UPDATE nexus.catalog_documents SET source_uri = {_sql_str(uri)} "
                   f"WHERE tenant_id = {_sql_str(tenant)} AND tumbler = {_sql_str(tumbler)};")
    for i in range(3):
        sql.append(
            "INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, "
            "extractor_name, source_uri, doc_id, collection_id) "
            f"VALUES ({_sql_str(tenant)}, {_sql_str(LABEL)}, 'p{i}.md', now(), 'm', "
            f"'e', {_sql_str(f'chroma://{LABEL}/asp{i}')}, {_sql_str(aspect_docs[i])}, {_collection_id(tenant, LABEL)});")
    sql.append(
        "INSERT INTO nexus.document_highlights (tenant_id, doc_id, source_uri, collection, ingested_at, collection_id) VALUES "
        f"({_sql_str(tenant)}, {_sql_str(asp_hl_tumbler)}, {_sql_str(f'chroma://{LABEL}/hl')}, {_sql_str(LABEL)}, now(), "
        f"{_collection_id(tenant, LABEL)});")
    # RDR-229 P3.8 (item 11): the client no longer writes legacy_grandfathered, so the grandfathered rows of the
    # census are flagged here, the way a pre-RDR-103 install carries them.
    for second in (LABEL2, TIE_CONF):
        sql.append(f"UPDATE nexus.catalog_collections SET model_version = 'v1' "
                   f"WHERE tenant_id = {_sql_str(tenant)} AND name = {_sql_str(second)};")
    sql.append(f"UPDATE nexus.catalog_collections SET name = {_sql_str(MINTED)} "
               f"WHERE tenant_id = {_sql_str(tenant)} AND name = {_sql_str(MINTED_SEED)};")
    for flagged in (LABEL, MINTED, NOVER):
        sql.append(f"UPDATE nexus.catalog_collections SET legacy_grandfathered = true "
                   f"WHERE tenant_id = {_sql_str(tenant)} AND name = {_sql_str(flagged)};")
    sql.append(f"UPDATE nexus.catalog_collections SET model_version = '' "
               f"WHERE tenant_id = {_sql_str(tenant)} AND name = {_sql_str(NOVER)};")
    # the dangling tombstone: OLD is superseded by NEW, and NEW's row goes away
    sql.append(f"DELETE FROM nexus.catalog_collections WHERE tenant_id = {_sql_str(tenant)} AND name = {_sql_str(NEW)};")
    _run_psql("\n".join(sql))


def _json_of(output: str) -> dict:
    lines = output.splitlines()
    return json.loads("\n".join(lines[next(i for i, line in enumerate(lines) if line.startswith("{")):]))


def test_the_residue_row_and_the_census_on_a_local_engine(t2_service_env) -> None:
    _seed(t2_service_env)
    runner = CliRunner()

    # ── B: the doctor row, on both surfaces, from the one engine route ─────────────────────────────────────────
    text = runner.invoke(main, ["catalog", "doctor", "--uri-residue"])
    _show("\n=== nx catalog doctor --uri-residue ===\n" + text.output)
    assert text.exit_code == 0, text.output
    assert "uri-residue: WARN" in text.output

    js = runner.invoke(main, ["catalog", "doctor", "--uri-residue", "--json"])
    report = _json_of(js.output)["uri_residue"]
    _show("=== nx catalog doctor --uri-residue --json (counts) ===\n" + json.dumps(report["counts"], indent=1, sort_keys=True))
    docs = report["counts"]["catalog_documents"]
    assert docs["chroma_prefix_free"]["file_typed"] == {"rows": 7, "segment_not_collection": 2}
    assert docs["chroma_prefix_free"]["other"] == {"rows": 7, "segment_not_collection": 0}
    assert docs["note_prefix_free_in_file_typed"] == {"rows": 4, "segment_not_collection": 0}
    assert docs["chroma_file_routed_segment"] == 11
    assert report["counts"]["document_aspects"]["chroma_prefix_free"]["file_typed"] == {"rows": 3, "segment_not_collection": 0}
    assert report["counts"]["document_highlights"]["chroma_prefix_free"]["file_typed"] == {"rows": 1, "segment_not_collection": 0}
    assert report["status"] == "warn"

    # nx doctor's supplementary row: the same text, and it does not gate
    assert "uri-residue" in doctor_mod._SUPPLEMENTARY_CHECK_NAMES
    assert doctor_mod._run_check_uri_residue() is None

    # ── A: the census of the registry rows ─────────────────────────────────────────────────────────────────────
    res = runner.invoke(main, ["catalog", "collections-census", "--tenant-label", "local-e2e"])
    _show("=== nx catalog collections-census ===\n" + res.output)
    assert res.exit_code == 0, res.output
    cj = json.loads(runner.invoke(main, ["catalog", "collections-census", "--tenant-label", "local-e2e", "--json"]).stdout)
    _show("=== nx catalog collections-census --json ===\n" + json.dumps(cj, indent=1, sort_keys=True))
    rv = cj["row_vs_name"]
    assert rv["content_type"]["mismatch"] == 1
    assert [x["name"] for x in rv["rows_that_disagree"]] == [CODE_BAD]
    assert rv["rows_that_disagree"][0]["differences"] == {"content_type": {"row": "docs", "name": "code"}}
    assert {LABEL, LABEL2, MINTED, TIE_LEGACY, NOVER} <= set(rv["not_conformant_names"])
    assert cj["label_clash"]["clashing_names"] == 0, "the registry's primary key makes a live clash impossible today"
    mt = cj["multi_candidate_tuples"]
    names = lambda groups: sorted([c["name"] for c in t["candidates"]] for t in groups["tuples"])  # noqa: E731
    assert names(mt["with_grandfathered_filter"]) == sorted([[DOCS, DOCS_V2], [TIE_CONF, TIE_LEGACY]])
    assert names(mt["without_grandfathered_filter"]) == sorted([[DOCS, DOCS_V2], [LABEL, LABEL2], [TIE_CONF, TIE_LEGACY]])
    assert all(c["model_version"] for t in mt["with_grandfathered_filter"]["tuples"] for c in t["candidates"])
    assert mt["ambiguous_only_without_the_filter"] == 1, "the 99-5 tuple is ambiguous only once the grandfathered filter goes"
    tied = mt["tied_at_top_version"]["with_grandfathered_filter"]
    assert [t["tied"] for t in tied["tuples"]] == [[TIE_CONF, TIE_LEGACY]], "only the 99-6 tuple ties at its top version"
    wc = mt["winner_changes_when_filter_dropped"]
    assert [(t["winner_with_filter"], t["winner_without_filter"]) for t in wc["tuples"]] == [(LABEL2, LABEL)]
    assert wc["tuples_that_only_gain_a_winner"]["count"] == 2, "the c123 and e2e-nover tuples hold only grandfathered rows"
    p14 = mt["p14_created_at_tie_break"]
    _show("=== P1.4 tie-break ===\n" + json.dumps(p14, indent=1, sort_keys=True))
    assert p14["tied_tuples_with_filter"] == 1
    assert p14["tied_candidates_without_created_at"] == 0, "list_collections carries created_at"
    [diff] = p14["created_at_winner_differs"]["tuples"]
    assert diff["winner_name_asc"] == TIE_CONF and diff["winner_created_at_then_name"] == TIE_LEGACY
    assert diff["created_at_winner_is_legacy_or_nonconformant"] is True and diff["conformant_siblings"] == [TIE_CONF]
    assert p14["created_at_winner_is_legacy_or_nonconformant_count"] == 1
    uv = cj["unusable_model_versions"]
    assert uv["count"] == 1 and uv["empty"] == 1 and [x["name"] for x in uv["rows"]] == [NOVER]
    nc = {x["name"]: x for x in rv["not_conformant_rows"]}
    assert nc[NOVER]["empty_fields"] == ["model_version"] and nc[NOVER]["legacy_grandfathered"] is True
    assert nc[LABEL]["empty_fields"] == [] and nc[LABEL]["content_type"] == "docs"
    assert cj["minted_form_names"]["names"] == [MINTED]
    assert cj["dangling_tombstones"]["rows"] == [{"name": OLD, "superseded_by": NEW}]
    assert cj["dangling_tombstones"]["dangling"] == 1
