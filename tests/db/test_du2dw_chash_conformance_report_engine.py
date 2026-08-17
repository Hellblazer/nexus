# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-du2dw: real-engine coverage for ``nexus.chash_conformance_report(dim)``.

RDR-180. The width-non-conformant chash probe (``octet_length(chash) <> 32``
— the GH #1414 / nexus-pnwu0 class) previously existed ONLY as a local
``psql`` invocation through the ``nexus_diag`` role
(``nexus.db.diag_connection`` — LOCAL-ONLY BY DESIGN, nexus-y3wuu), so
managed/cloud installs with no direct substrate access were permanently
blind to it. This engine route (``GET /v1/catalog/chash/conformance``,
``HttpCatalogClient.chash_conformance_report``) is the managed-mode
counterpart, following the ``manifest_orphans`` idiom (same file-shape
precedent as ``tests/db/test_heizf_manifest_verify_list_engine.py``).

Every test is routed to a real per-test-tenant engine catalog by the
autouse ``_pin_t2_substrate`` fixture (tests/conftest.py) — no explicit
substrate fixture request needed.

KILL-CONTROL NOTE (CORRECTED — code-review fix round, substantive-critic
CRITICAL nexus-5nrzk, T2 [21458]): the original claim here ("seeding is
impossible by construction") was FALSE AS WRITTEN. Seeding a genuine
width-non-conformant row through the real engine IS possible — but only
via a superuser CHECK-constraint-drop maneuver
(``service/src/test/java/dev/nexus/service/RekeyOpsIntegrationTest.java``'s
``withChecksDropped`` helper: drop the ``*_chash_octet_check`` constraints,
insert the poisoned row, re-add the constraints NOT VALID), done at the
JAVA test layer only. It is NOT reachable through this Python suite or any
client-facing HTTP write path — the width CHECK, even NOT VALID, still
enforces on every NEW write through the normal client/HTTP surface (see
``rdr180-001-bytea-chash.xml``); only a superuser DDL session can drop the
constraint to seed the reconstruction, and no Python-level fixture has
that access. So THIS file still cannot seed poison itself and continues to
prove only the WIRING — the route exists, returns the expected per-table
shape, is tenant-scoped, and reports clean (0) on a fresh store — end to
end against the real stored function. The actual non_conformant>0 branch
IS now proven end-to-end against a real poisoned row, at the Java layer:
``service/src/test/java/dev/nexus/service/ChashConformanceReportIntegrationTest.java``
(mirrors ``RekeyOpsIntegrationTest``'s seeding pattern). The fake-reader
layer proof
(``tests/test_health_service_checks.py::TestCheckChashConformanceReport``)
remains as the doctor-check-level coverage of the same branch, now
corroborated by real poison one layer down rather than merely asserted
absent.
"""
from __future__ import annotations

from tests._catalog_fixture_ops import (
    ActiveCatalog,
    active_reader,
    bypass_fk_seed_chunk,
    seed_manifest_chunks,
)

_SEQ = [0]


def _next_seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _chunk(chash: str, position: int) -> dict:
    return {
        "chash": chash, "position": position, "chunk_index": None,
        "line_start": None, "line_end": None, "char_start": None, "char_end": None,
    }


def _conformant_chash(seed: int) -> str:
    """A syntactically-valid 64-hex chash (32 conformant bytes)."""
    return f"{seed:064x}"


class TestChashConformanceReportWiring:
    def test_returns_expected_table_shape_for_each_dim(self) -> None:
        """The route + stored function round-trip: one row per covered
        table, with the exact keys the client contract promises."""
        for dim, expected_chunks_table in (
            (384, "nexus.chunks_384"),
            (768, "nexus.chunks_768"),
            (1024, "nexus.chunks_1024"),
        ):
            result = active_reader().chash_conformance_report(dim)
            assert result["dim"] == dim
            tables = result["tables"]
            names = {row["table_name"] for row in tables}
            assert names == {expected_chunks_table, "nexus.catalog_document_chunks"}, (
                f"dim={dim}: {names}"
            )
            for row in tables:
                assert {"table_name", "total", "non_conformant", "sample_chashes"} <= row.keys()
                assert isinstance(row["total"], int)
                assert isinstance(row["non_conformant"], int)
                assert isinstance(row["sample_chashes"], list)

    def test_fresh_store_reports_clean(self) -> None:
        """No non-conformant rows exist anywhere in a fresh test tenant —
        the schema-level width CHECKs (rdr180-001) enforce on every write,
        so this must be zero regardless of what else the suite has seeded."""
        for dim in (384, 768, 1024):
            result = active_reader().chash_conformance_report(dim)
            for row in result["tables"]:
                assert row["non_conformant"] == 0, (
                    f"dim={dim} table={row['table_name']}: "
                    f"{row['non_conformant']} non-conformant rows in a "
                    "fresh tenant — the width CHECK constraint failed to "
                    "enforce on write, or a prior test in this process "
                    "left poisoned state"
                )
                assert row["sample_chashes"] == []

    def test_catalog_document_chunks_total_reflects_seeded_manifest(self) -> None:
        """Writing a real (conformant) manifest row for THIS dim's model
        token must be visible in the catalog_document_chunks total — proves
        the function's collection IN-list routing actually selects real
        rows, not just an always-empty query."""
        seq = _next_seq()
        collection = f"knowledge__du2dw-{seq}__bge-base-en-v15-768__v1"
        cat = ActiveCatalog()
        owner = cat.register_owner(f"du2dw-{seq}", "curator")
        tumbler = cat.register(
            owner, f"du2dw conformance doc {seq}",
            content_type="knowledge",
            physical_collection=collection,
        )
        before = active_reader().chash_conformance_report(768)
        before_total = next(
            r["total"] for r in before["tables"]
            if r["table_name"] == "nexus.catalog_document_chunks"
        )

        chash = _conformant_chash(seq)
        # nexus-dbzxb (RDR-191 Phase 5 Python collateral): fk_catalog_
        # chunks_chunk requires a matching real nexus.chunks row; the
        # collection's model token (bge-base-en-v15-768) is locally
        # embeddable, so a real T3 write is idiom 1 here (this test isn't
        # about T3 presence, just the manifest row COUNT).
        seed_manifest_chunks(collection, [chash])
        cat.write_manifest(str(tumbler), [_chunk(chash, 0)], collection=collection)

        after = active_reader().chash_conformance_report(768)
        after_row = next(
            r for r in after["tables"]
            if r["table_name"] == "nexus.catalog_document_chunks"
        )
        assert after_row["total"] == before_total + 1, (
            f"expected catalog_document_chunks total to grow by exactly 1 "
            f"after seeding one manifest row for a routable (bge-768) "
            f"collection: before={before_total} after={after_row['total']}"
        )
        assert after_row["non_conformant"] == 0

    def test_unrecognized_model_token_is_invisible_by_the_same_in_list_caveat(
        self, t2_service_env,
    ) -> None:
        """Same known routing caveat as manifest_orphans (nexus-h1zu0): a
        collection whose model token is outside every dim's IN-list is
        invisible to this function. Documented here as a pinned behavior,
        not a silent surprise — mirrors
        test_unrecognized_model_token_surfaces_in_unroutable_against_real_engine
        in the manifest_orphans sibling file."""
        seq = _next_seq()
        collection = f"knowledge__du2dw-tok-{seq}__some-unrecognized-legacy-token-9000__v1"
        cat = ActiveCatalog()
        owner = cat.register_owner(f"du2dw-tok-{seq}", "curator")
        tumbler = cat.register(
            owner, f"du2dw unrouted doc {seq}",
            content_type="knowledge",
            physical_collection=collection,
        )
        before = active_reader().chash_conformance_report(768)
        before_total = next(
            r["total"] for r in before["tables"]
            if r["table_name"] == "nexus.catalog_document_chunks"
        )

        chash = _conformant_chash(seq + 10_000)
        # nexus-dbzxb: "some-unrecognized-legacy-token-9000" is not a
        # registered model token — every real /v1/vectors/* write endpoint
        # refuses it outright (HTTP 400: unknown embedding-model segment),
        # regardless of the same-model passthrough (proven empirically
        # against the real test engine). Idiom 1/2 cannot reach this
        # state; insert the stub chunk directly (idiom 3-adjacent —
        # unlike fk_dropped_for_dangling_seed, this row IS meant to be a
        # real, present chunk, just one the real HTTP naming gate would
        # reject on the way in).
        bypass_fk_seed_chunk(t2_service_env, collection, chash)
        cat.write_manifest(str(tumbler), [_chunk(chash, 0)], collection=collection)

        after = active_reader().chash_conformance_report(768)
        after_total = next(
            r["total"] for r in after["tables"]
            if r["table_name"] == "nexus.catalog_document_chunks"
        )
        assert after_total == before_total, (
            "an unrecognized model token's manifest row must NOT be "
            "counted by any dim's IN-list-scoped catalog_document_chunks "
            "total — this is the same nexus-h1zu0 routing caveat "
            "manifest_orphans has, carried forward deliberately"
        )

    def test_unsupported_dim_raises_value_error_client_side(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="dim must be one of"):
            active_reader().chash_conformance_report(512)
