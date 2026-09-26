# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx t3 census-manifest-less`` (RDR-192 Step 2, client half, bead nexus-wbfpw.5).

Wraps ``POST /v1/vectors/manifest-less-census`` (engine half: bead
nexus-wbfpw.4, ``HttpVectorClient.manifest_less_census``). Per
``tests/AGENTS.md``: integration over mocks, real substrate via
``t2_service_env``.

Two exit paths cannot be reached through the real engine built from this
worktree's OWN ``service/`` sources, and are exercised against a stubbed
client instead, with the reason recorded at each site:

  - exit 1 (``unclassified`` bucket > 0): the route's SQL ``CASE``
    (``scripts/sql/manifest_less_census.sql``) is exhaustive over every
    reachable (owner-tumbler, deleted_at, total_count, own_count)
    combination — ``unclassified`` is a defensive "reported, never
    dropped" catch-all the current engine can never actually emit.
  - the no-route exit code: the dev jar built by ``scripts/build-gate-jar
    .sh`` is built from THIS checkout's ``service/`` sources, which
    already carry the route (bead nexus-wbfpw.4 is done) — a 404 can only
    come from an OLDER engine, which this suite has no way to boot.

Both are pure CLI branch logic (read the totals dict, pick an exit code),
not engine SQL correctness, so a stubbed client response/exception is the
right tool: it isolates the CLIENT-side contract this bead owns from the
ENGINE-side contract nexus-wbfpw.4 already proved with
``ManifestLessCensusIntegrationTest``.
"""
from __future__ import annotations

import hashlib
import json as _json

import pytest
from click.testing import CliRunner

from nexus.commands.t3 import (
    _CENSUS_BUCKETS,
    _EXIT_NO_ROUTE,
    t3,
)
from nexus.db.http_vector_client import VectorServiceError


def _chash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# ── real-engine substrate: one row of each reachable bucket + both owner
# paths a chunk can carry (forward, reverse, none) ──────────────────────────


def _seed_all_reachable_buckets(tenant: str, coll: str) -> dict[str, str]:
    """Seed one manifest-less chunk per reachable bucket, plus one properly
    manifested chunk (excluded from every bucket, counted in
    scope_chunk_total). Returns {bucket_or_label: chash}."""
    import nexus.db.http_vector_client as hvc
    from tests._catalog_fixture_ops import ActiveCatalog

    cat = ActiveCatalog()
    db = hvc.HttpVectorClient(tenant=tenant)
    owner = cat.register_owner("wbfpw5census", "curator")

    chash_no_owner = _chash(f"{coll}:no-owner")
    chash_legacy_reverse = _chash(f"{coll}:legacy-reverse")
    chash_dead_owner = _chash(f"{coll}:dead-owner")
    chash_superseded = _chash(f"{coll}:superseded")
    chash_manifested = _chash(f"{coll}:manifested")

    # no-owner: no catalog_doc_id/doc_id metadata at all, no reverse match.
    # legacy-unmanifested via REVERSE: no forward pointer either, but a
    # live NOTE-shaped document's own metadata.doc_id names this chash.
    # dead-owner: forward pointer names a TOMBSTONED document.
    # superseded: forward pointer names a LIVE document that has manifest
    # rows in THIS collection, none naming this chash.
    tumbler_dead = str(cat.register(
        owner, "wbfpw5-dead-doc", content_type="knowledge",
        physical_collection=coll, file_path=f"/tmp/{coll}/dead.txt",
    ))
    cat.delete_document(tumbler_dead)

    tumbler_live = str(cat.register(
        owner, "wbfpw5-live-doc", content_type="knowledge",
        physical_collection=coll, file_path=f"/tmp/{coll}/live.txt",
    ))

    # Note-shaped (no file_path) live document whose OWN doc_id names the
    # legacy-reverse chash — the reverse notes-guard rescue.
    cat.register(
        owner, "wbfpw5-note-doc", content_type="knowledge",
        physical_collection=coll, meta={"doc_id": chash_legacy_reverse},
    )

    # T3 chunks must exist BEFORE the manifest write below -- catalog-029's
    # fk_catalog_chunks_chunk 409s a manifest row against a chash with no
    # nexus.chunks row yet.
    db.upsert_chunks_with_embeddings(
        coll,
        ids=[chash_no_owner, chash_legacy_reverse, chash_dead_owner,
             chash_superseded, chash_manifested],
        documents=[
            "no-owner probe text", "legacy-reverse probe text",
            "dead-owner probe text", "superseded probe text",
            "manifested probe text",
        ],
        embeddings=[],
        metadatas=[
            {"chunk_text_hash": chash_no_owner, "title": "no-owner.txt:1-1"},
            {"chunk_text_hash": chash_legacy_reverse, "title": "legacy-reverse.txt:1-1"},
            {
                "chunk_text_hash": chash_dead_owner, "title": "dead-owner.txt:1-1",
                "catalog_doc_id": tumbler_dead,
            },
            {
                "chunk_text_hash": chash_superseded, "title": "superseded.txt:1-1",
                "catalog_doc_id": tumbler_live,
            },
            {
                "chunk_text_hash": chash_manifested, "title": "manifested.txt:1-1",
                "catalog_doc_id": tumbler_live,
            },
        ],
    )

    cat.write_manifest(
        tumbler_live, [{"chash": chash_manifested, "position": 0}], collection=coll,
    )

    return {
        "no-owner": chash_no_owner,
        "legacy-unmanifested": chash_legacy_reverse,
        "dead-owner": chash_dead_owner,
        "superseded": chash_superseded,
        "manifested": chash_manifested,
        "tumbler_dead": tumbler_dead,
        "tumbler_live": tumbler_live,
    }


@pytest.mark.integration
def test_census_reports_every_reachable_bucket_with_owner_and_exits_clean(
    runner: CliRunner, t2_service_env,
) -> None:
    tenant = t2_service_env
    coll = "knowledge__wbfpw5census-clean__bge-base-en-v15-768__v1"
    ids = _seed_all_reachable_buckets(tenant, coll)

    result = runner.invoke(t3, ["census-manifest-less", "--collection", coll])
    assert result.exit_code == 0, result.output

    assert "superseded: 1" in result.output
    assert "legacy-unmanifested: 1" in result.output
    assert "dead-owner: 1" in result.output
    assert "no-owner: 1" in result.output
    assert "unclassified: 0" in result.output
    assert "total: 5" in result.output  # scope_chunk_total: 4 manifest-less + 1 manifested

    # Text names each item's owner tumbler and path.
    assert f"{ids['no-owner']}" in result.output
    assert "owner=- (none)" in result.output
    assert f"{ids['dead-owner']}  owner={ids['tumbler_dead']} (forward)" in result.output
    assert f"{ids['superseded']}  owner={ids['tumbler_live']} (forward)" in result.output
    assert f"{ids['legacy-unmanifested']}  owner=" in result.output
    assert "(reverse)" in result.output, (
        "the reverse tie-break rescue must be visibly distinguishable "
        "from a forward owner in text output"
    )
    # the properly-manifested chash never appears as a census item
    assert ids["manifested"] not in result.output


@pytest.mark.integration
def test_census_json_parses_and_matches_text_counts_plus_owners(
    runner: CliRunner, t2_service_env,
) -> None:
    tenant = t2_service_env
    coll = "knowledge__wbfpw5census-json__bge-base-en-v15-768__v1"
    ids = _seed_all_reachable_buckets(tenant, coll)

    text_result = runner.invoke(t3, ["census-manifest-less", "--collection", coll])
    json_result = runner.invoke(
        t3, ["census-manifest-less", "--collection", coll, "--json"]
    )
    assert text_result.exit_code == 0, text_result.output
    assert json_result.exit_code == 0, json_result.output

    payload = _json.loads(json_result.output)
    row = payload["collections"][0]
    assert row["collection"] == coll
    assert row["totals"]["superseded"] == 1
    assert row["totals"]["legacy-unmanifested"] == 1
    assert row["totals"]["dead-owner"] == 1
    assert row["totals"]["no-owner"] == 1
    assert row["totals"]["unclassified"] == 0
    assert row["scope_chunk_total"] == 5

    # owners map: owner_tumbler / owner_path per chash, same identity the
    # text output names.
    assert row["owners"][ids["dead-owner"]] == {
        "owner_tumbler": ids["tumbler_dead"], "owner_path": "forward",
    }
    assert row["owners"][ids["superseded"]] == {
        "owner_tumbler": ids["tumbler_live"], "owner_path": "forward",
    }
    assert row["owners"][ids["no-owner"]] == {
        "owner_tumbler": None, "owner_path": None,
    }
    assert row["owners"][ids["legacy-unmanifested"]]["owner_path"] == "reverse"

    for bucket in _CENSUS_BUCKETS:
        assert f"{bucket}: {row['totals'][bucket]}" in text_result.output


@pytest.mark.integration
def test_census_require_zero_violation_exits_2(
    runner: CliRunner, t2_service_env,
) -> None:
    tenant = t2_service_env
    coll = "knowledge__wbfpw5census-reqzero__bge-base-en-v15-768__v1"
    _seed_all_reachable_buckets(tenant, coll)

    clean_result = runner.invoke(t3, ["census-manifest-less", "--collection", coll])
    assert clean_result.exit_code == 0, clean_result.output

    result = runner.invoke(
        t3, ["census-manifest-less", "--collection", coll, "--require-zero", "no-owner"],
    )
    assert result.exit_code == 2, result.output
    assert "no-owner" in result.output


@pytest.mark.integration
def test_census_all_on_a_tenant_with_no_collections_exits_3(
    runner: CliRunner, t2_service_env,
) -> None:
    # Fresh tenant per t2_service_env, nothing seeded — genuinely zero
    # collections (excluding quarantine-*, which cannot exist either).
    result = runner.invoke(t3, ["census-manifest-less", "--all"])
    assert result.exit_code == 3, result.output
    assert "no" in result.output.lower() and "collection" in result.output.lower()


def test_census_mutually_exclusive_collection_and_all(runner: CliRunner) -> None:
    result = runner.invoke(
        t3, ["census-manifest-less", "--collection", "x", "--all"],
    )
    assert result.exit_code != 0
    result_neither = runner.invoke(t3, ["census-manifest-less"])
    assert result_neither.exit_code != 0


def test_census_rejects_unknown_require_zero_bucket(runner: CliRunner) -> None:
    result = runner.invoke(
        t3,
        ["census-manifest-less", "--collection", "x", "--require-zero", "bogus-bucket"],
    )
    assert result.exit_code != 0
    assert "bogus-bucket" in result.output


# ── stubbed-client paths: unreachable through the real engine (see module
# docstring) ─────────────────────────────────────────────────────────────


class _StubT3Client:
    """Stands in for what ``nexus.db.make_t3()`` actually returns in
    production with no injected ``_client``: the ``HttpVectorClient``
    itself, not a ``T3Database`` facade (see that factory's docstring) --
    ``manifest_less_census``/``list_collections`` live directly on it."""

    def __init__(self, page_response=None, raise_error: VectorServiceError | None = None):
        self._page_response = page_response
        self._raise_error = raise_error
        self.calls: list[tuple[str, int, int]] = []

    def manifest_less_census(self, collection: str, limit: int = 100, offset: int = 0) -> dict:
        self.calls.append((collection, limit, offset))
        if self._raise_error is not None:
            raise self._raise_error
        return self._page_response

    def list_collections(self):
        return [{"name": "knowledge__stub__bge-base-en-v15-768__v1"}]


def test_census_unclassified_bucket_exits_1(monkeypatch, runner: CliRunner) -> None:
    """The route's CASE is exhaustive on the real engine (see module
    docstring) — this pins the CLIENT's own reaction to a totals dict
    reporting unclassified > 0, which the engine's response contract
    permits even if the current engine build never emits it."""
    stub_client = _StubT3Client(page_response={
        "collection": "c", "returned": 1,
        "chashes": {"superseded": [], "legacy-unmanifested": [], "dead-owner": [],
                    "no-owner": [], "unclassified": ["a" * 64]},
        "owners": {"a" * 64: {"owner_tumbler": None, "owner_path": None}},
        "totals": {"superseded": 0, "legacy-unmanifested": 0, "dead-owner": 0,
                   "no-owner": 0, "unclassified": 1},
        "scope_chunk_total": 1,
    })
    monkeypatch.setattr("nexus.db.make_t3", lambda: stub_client)
    result = runner.invoke(t3, ["census-manifest-less", "--collection", "c"])
    assert result.exit_code == 1, result.output
    assert "unclassified: 1" in result.output


def test_census_no_route_engine_exits_distinct_code_never_a_traceback(
    monkeypatch, runner: CliRunner,
) -> None:
    stub_client = _StubT3Client(
        raise_error=VectorServiceError("POST ... -> HTTP 404: not found", code=404),
    )
    monkeypatch.setattr("nexus.db.make_t3", lambda: stub_client)
    result = runner.invoke(t3, ["census-manifest-less", "--collection", "c"])
    assert result.exit_code == _EXIT_NO_ROUTE, result.output
    assert result.exit_code != 1
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"a pre-route engine must never surface as a traceback: {result.exception!r}"
    )
    assert "nexus-wbfpw.4" in result.output or "census route" in result.output.lower()
    assert "pending" in result.output.lower()


def test_census_paging_merges_multiple_pages(monkeypatch, runner: CliRunner) -> None:
    """The client method pages internally (limit clamped, loop while
    returned == limit) — this is pure CLI-side loop logic, tested here
    without needing 300+ real seeded rows."""
    chash1, chash2 = "a" * 64, "b" * 64

    def _totals(no_owner: int) -> dict:
        return {"superseded": 0, "legacy-unmanifested": 0, "dead-owner": 0,
                 "no-owner": no_owner, "unclassified": 0}

    def _paged_response(collection, limit=100, offset=0):
        # limit is patched to 1 (one chash per page), so two real items
        # need a full page each, THEN an empty page to signal the end
        # (returned < limit) -- exactly the real route's own convention.
        # A FOURTH call (offset=3) would mean the loop never terminated;
        # there is deliberately no branch for it, so a regression here
        # fails loud (KeyError) rather than looping forever.
        pages = {
            0: {
                "collection": collection, "returned": 1,
                "chashes": {"superseded": [], "legacy-unmanifested": [],
                            "dead-owner": [], "no-owner": [chash1], "unclassified": []},
                "owners": {chash1: {"owner_tumbler": None, "owner_path": None}},
                "totals": _totals(2), "scope_chunk_total": 2,
            },
            1: {
                "collection": collection, "returned": 1,
                "chashes": {"superseded": [], "legacy-unmanifested": [],
                            "dead-owner": [], "no-owner": [chash2], "unclassified": []},
                "owners": {chash2: {"owner_tumbler": None, "owner_path": None}},
                "totals": _totals(2), "scope_chunk_total": 2,
            },
            2: {
                "collection": collection, "returned": 0,
                "chashes": {"superseded": [], "legacy-unmanifested": [],
                            "dead-owner": [], "no-owner": [], "unclassified": []},
                "owners": {}, "totals": _totals(2), "scope_chunk_total": 2,
            },
        }
        return pages[offset]

    stub_client = _StubT3Client()
    stub_client.manifest_less_census = _paged_response  # type: ignore[method-assign]
    monkeypatch.setattr("nexus.db.make_t3", lambda: stub_client)
    # Force a tiny page size so two pages are needed even for two rows —
    # by patching the module-level page size the CLI passes internally.
    monkeypatch.setattr("nexus.commands.t3._CENSUS_PAGE_LIMIT", 1)
    result = runner.invoke(t3, ["census-manifest-less", "--collection", "c", "--json"])
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    row = payload["collections"][0]
    assert sorted(row["chashes"]["no-owner"]) == sorted([chash1, chash2])
    assert len(row["owners"]) == 2
