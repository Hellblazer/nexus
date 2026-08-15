# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-heizf / nexus-h1zu0: real-engine coverage for `nx catalog
manifest-verify --list`.

Every test in this suite is routed to a real per-test-tenant engine
catalog by the autouse ``_pin_t2_substrate`` fixture (tests/conftest.py)
— no explicit substrate fixture request needed, same convention as
tests/test_y8qtj_source_uri_client_wave.py and
tests/test_catalog_doctor_new_checks.py (``ActiveCatalog`` seeding +
``CliRunner`` invocation in the SAME process/tenant).

These tests seed a GENUINELY dangling manifest row — register a document,
then write a manifest row referencing a chash that is NEVER upserted into
T3 — the same shape the 2026-08-04 nexus-55l58 shakedown found 188 of in
production. Unlike the unit-level FakeCat tests in
tests/test_catalog_verify_cmd.py (argument parsing, output formatting),
these prove the wiring against the real
``nexus.manifest_verify_all()``/``nexus.manifest_orphans(dim)`` stored
functions end to end.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from nexus.cli import main
from tests._catalog_fixture_ops import ActiveCatalog, active_reader, fk_dropped_for_dangling_seed

_SEQ = [0]


def _next_seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _chunk(chash: str, position: int) -> dict[str, Any]:
    return {
        "chash": chash, "position": position, "chunk_index": None,
        "line_start": None, "line_end": None, "char_start": None, "char_end": None,
    }


def _fake_chash(seed: int) -> str:
    """A syntactically-valid 64-hex chash that was never upserted to T3."""
    return f"{seed:064x}"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _seed_dangling_doc(
    *, positions: int = 1, model_token: str = "bge-base-en-v15-768",
) -> tuple[str, str, list[str]]:
    """Register a document and write a manifest with *positions* chashes,
    none of which are ever upserted to T3 — a genuinely dangling manifest.

    *model_token* defaults to a routable token (nexus-h1zu0's
    ``dim_for_model_token`` resolves it to 768); pass an unrecognized one
    to seed the actual h1zu0 divergence shape against the real engine.

    nexus-dbzxb (RDR-191 Phase 5 Python collateral, idiom 3): this whole
    file's SUBJECT is a genuinely-dangling manifest row (per its own
    module docstring) — the chash must NEVER be upserted to T3, which is
    exactly the state ``fk_catalog_chunks_chunk`` now makes unreachable
    via the normal write path (write_manifest 409s immediately without a
    matching real ``nexus.chunks`` row). The census/enumeration functions
    under test read ``nexus.chunks``/``chunks_<dim>`` directly, so seeding
    a REAL stub chunk (idiom 1/2's approach) would flip "missing" to
    "present" and invert this suite's own premise —
    ``fk_dropped_for_dangling_seed`` (idiom 3) drops the constraint for
    this one write, producing a row that stays genuinely dangling after
    commit, indistinguishable from pre-catalog-029 production data.

    Returns ``(collection, doc_id, [chash, ...])``.
    """
    cat = ActiveCatalog()
    seq = _next_seq()
    collection = f"knowledge__heizf-{seq}__{model_token}__v1"
    owner = cat.register_owner(f"heizf-{seq}", "curator")
    tumbler = cat.register(
        owner, f"heizf dangling doc {seq}",
        content_type="knowledge",
        physical_collection=collection,
    )
    chashes = [_fake_chash(seq * 100 + i) for i in range(positions)]
    with fk_dropped_for_dangling_seed():
        cat.write_manifest(
            str(tumbler), [_chunk(c, i) for i, c in enumerate(chashes)], collection=collection,
        )
    return collection, str(tumbler), chashes


class TestSeededDanglingRowParity:
    """nexus-h1zu0's core falsification: the census (manifest_verify_all)
    and the enumeration (manifest_orphans, wired via --list) must agree on
    which collections are damaged."""

    def test_census_and_list_agree_on_seeded_collection(self, runner) -> None:
        collection, doc_id, chashes = _seed_dangling_doc()

        census = active_reader().manifest_verify_all() or {}
        census_rows = [
            r for r in (census.get("collections") or [])
            if r.get("collection") == collection
        ]
        assert census_rows, (
            f"{collection} absent from manifest_verify_all census — "
            "seeding did not produce a comparable row"
        )
        assert int(census_rows[0]["missing"]) >= 1

        result = runner.invoke(main, ["catalog", "manifest-verify", "--list", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)

        assert collection not in payload["unroutable_collections"], (
            f"{collection} is census-counted but NOT enumerable — exactly "
            "the nexus-h1zu0 divergence this test exists to catch"
        )
        matches = [c for c in payload["collections"] if c["collection"] == collection]
        assert matches, f"{collection} missing from --list output despite the census seeing it"
        enumerated_doc_ids = {d["doc_id"] for d in matches[0]["documents"]}
        assert doc_id in enumerated_doc_ids

    def test_every_damaged_census_collection_is_enumerable(self, runner) -> None:
        """The general form of the parity check, not pinned to one seeded
        collection: for EVERY collection the census reports as damaged,
        --list must either enumerate it or name it in
        unroutable_collections — never silently omit it."""
        _seed_dangling_doc()
        _seed_dangling_doc(positions=2)

        census = active_reader().manifest_verify_all() or {}
        damaged = {
            r["collection"] for r in (census.get("collections") or [])
            if int(r.get("missing", 0) or 0) > 0
        }
        assert damaged, "seeding produced no damaged collections to compare"

        result = runner.invoke(main, ["catalog", "manifest-verify", "--list", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)

        enumerated = {c["collection"] for c in payload["collections"]}
        unroutable = set(payload["unroutable_collections"])
        accounted_for = enumerated | unroutable
        missing_entirely = damaged - accounted_for
        assert not missing_entirely, (
            f"damaged collection(s) silently absent from BOTH the "
            f"enumeration and unroutable_collections: {missing_entirely}"
        )

    def test_unrecognized_model_token_surfaces_in_unroutable_against_real_engine(
        self, runner,
    ) -> None:
        """substantive-critic Significant-2 (2026-08-05): the prior version
        of this class only ever seeded ROUTABLE (bge-768) collections, so
        the actual nexus-h1zu0 divergence — a collection whose token is
        outside every ``manifest_orphans`` IN-list — was never proven
        against the real engine, only unit-tested with a FakeCat. Seed one
        here: the census (``manifest_verify_all``, which does NOT route by
        token) must still count it as damaged, while the enumeration
        (``--list``) must name it in ``unroutable_collections`` — never
        silently omit it from BOTH."""
        collection, doc_id, chashes = _seed_dangling_doc(
            model_token="some-unrecognized-legacy-token-9000",
        )

        census = active_reader().manifest_verify_all() or {}
        census_rows = [
            r for r in (census.get("collections") or [])
            if r.get("collection") == collection
        ]
        assert census_rows, (
            f"{collection} absent from the census — manifest_verify_all "
            "unexpectedly routes by token after all (would invalidate the "
            "premise this test checks)"
        )
        assert int(census_rows[0]["missing"]) >= 1

        result = runner.invoke(main, ["catalog", "manifest-verify", "--list", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)

        assert collection in payload["unroutable_collections"], (
            f"{collection} (unrecognized model token) must be named in "
            "unroutable_collections — the census counted it as damaged "
            "but the enumeration cannot route it to a chunks_<dim> table"
        )
        enumerated = {c["collection"] for c in payload["collections"]}
        assert collection not in enumerated, (
            "an unroutable collection must not ALSO appear as if "
            "successfully enumerated — that would be a dishonest accounting"
        )
        # The general accounting invariant, restated for this one seed:
        # damaged (census) is fully covered by enumerated | unroutable.
        damaged = {
            r["collection"] for r in (census.get("collections") or [])
            if int(r.get("missing", 0) or 0) > 0
        }
        unroutable = set(payload["unroutable_collections"])
        assert not (damaged - (enumerated | unroutable)), (
            "census-vs-enumeration accounting is dishonest: some damaged "
            "collection is absent from both enumerated and unroutable"
        )


class TestManifestVerifyListHappyPath:
    def test_positions_compact_into_a_range(self, runner) -> None:
        collection, doc_id, chashes = _seed_dangling_doc(positions=3)

        result = runner.invoke(main, ["catalog", "manifest-verify", "--list"])
        assert result.exit_code == 1, result.output
        assert collection in result.output
        assert doc_id in result.output
        assert "positions=[0-2]" in result.output
        assert "distinct_chashes=3" in result.output

    def test_text_and_json_agree_on_row_count(self, runner) -> None:
        collection, doc_id, chashes = _seed_dangling_doc()

        json_result = runner.invoke(main, ["catalog", "manifest-verify", "--list", "--json"])
        assert json_result.exit_code == 1, json_result.output
        payload = json.loads(json_result.stdout)
        assert payload["total_rows"] == 1

        text_result = runner.invoke(main, ["catalog", "manifest-verify", "--list"])
        assert text_result.exit_code == 1, text_result.output
        assert "1 dangling manifest row(s)" in text_result.output
