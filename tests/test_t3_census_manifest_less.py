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
    _EXIT_ENGINE_ERROR,
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
    from tests._catalog_fixture_ops import ActiveCatalog
    from tests._chunk_seed import seed_chunks_direct

    cat = ActiveCatalog()
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
    # nexus.chunks row yet. Substrate SQL, not upsert-chunks: the engine
    # refuses that route's ownerless writes from RDR-223 Phase 3 on, and four
    # of these five chunks are ownerless by design (that is the census's subject).
    seed_chunks_direct(
        coll, tenant=tenant,
        ids=[chash_no_owner, chash_legacy_reverse, chash_dead_owner,
             chash_superseded, chash_manifested],
        documents=[
            "no-owner probe text", "legacy-reverse probe text",
            "dead-owner probe text", "superseded probe text",
            "manifested probe text",
        ],
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


def test_census_json_with_require_zero_violation_stdout_still_parses(
    runner: CliRunner, t2_service_env,
) -> None:
    """Review round 1 CRITICAL finding: with --json, a violated
    --require-zero used to append a plain-text ``--require-zero
    violated: ...`` line to the SAME stdout stream as the JSON payload,
    so a consumer doing ``json.loads(stdout)`` -- the exact machine-gate
    use case the exit-code contract exists for -- got a JSONDecodeError
    instead of the payload plus a clean exit code. The violation line now
    goes to stderr unconditionally; ``result.stdout`` (click 8.2+ keeps
    stdout/stderr separate, unlike the combined ``result.output``) must
    stay pure JSON."""
    tenant = t2_service_env
    coll = "knowledge__wbfpw5census-reqzero-json__bge-base-en-v15-768__v1"
    _seed_all_reachable_buckets(tenant, coll)

    result = runner.invoke(
        t3,
        ["census-manifest-less", "--collection", coll, "--json", "--require-zero", "no-owner"],
    )
    assert result.exit_code == 2, result.output

    payload = _json.loads(result.stdout)
    row = payload["collections"][0]
    assert row["totals"]["no-owner"] == 1

    # The violation notice is a human diagnostic, not part of the JSON
    # document -- it belongs on stderr, never mixed into stdout (the
    # bucket name "no-owner" legitimately appears IN the JSON payload
    # itself, so the discriminating check is the "violated" sentence,
    # not the bucket name).
    assert "violated" in result.stderr.lower()
    assert "violated" not in result.stdout.lower()


def test_census_quarantine_collection_exits_engine_error_never_traceback(
    runner: CliRunner, t2_service_env,
) -> None:
    """Review round 1 Important-1 finding: an explicitly-named
    ``quarantine-*`` --collection is not filtered client-side (only
    --all's catalog-driven listing excludes quarantine collections), so
    the engine's 400 refusal (VectorHandler.requireNotQuarantineCollection)
    used to fall through the bare ``raise`` in the 404-only except clause
    and surface as a raw traceback. It must now print one clear line
    naming the collection and error, and exit non-zero with a documented
    code -- never a traceback."""
    result = runner.invoke(
        t3, ["census-manifest-less", "--collection", "quarantine-wbfpw5-census-probe"],
    )
    assert result.exit_code == _EXIT_ENGINE_ERROR, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"a non-404 engine error must never surface as a traceback: {result.exception!r}"
    )
    assert "quarantine-wbfpw5-census-probe" in result.output


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

    def __init__(
        self,
        page_response=None,
        raise_error: VectorServiceError | None = None,
        list_response: list[dict] | None = None,
        list_raise_error: VectorServiceError | None = None,
    ):
        self._page_response = page_response
        self._raise_error = raise_error
        self._list_response = (
            list_response if list_response is not None
            else [{"name": "knowledge__stub__bge-base-en-v15-768__v1"}]
        )
        self._list_raise_error = list_raise_error
        self.calls: list[tuple[str, int, int]] = []

    def manifest_less_census(self, collection: str, limit: int = 100, offset: int = 0) -> dict:
        self.calls.append((collection, limit, offset))
        if self._raise_error is not None:
            raise self._raise_error
        return self._page_response

    def list_collections(self, lifecycle_state=None, *, strict: bool = False) -> list[dict]:
        # Mirrors HttpVectorClient.list_collections's real contract
        # (nexus-wbfpw.5 review round 1, Significant-2): strict=False
        # swallows a listing failure to [], strict=True re-raises it --
        # the census verb's --all path always passes strict=True so exit
        # 3 can mean "genuinely zero collections", never "listing failed".
        if self._list_raise_error is not None:
            if strict:
                raise self._list_raise_error
            return []
        return self._list_response


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
    # Wording describes the MECHANISM (upgrade the engine past its version
    # floor), never a dated snapshot of which tag carries the route --
    # review round 1 Significant-3 finding: engine-service-v0.1.133 makes
    # a frozen "no tag carries it yet" claim false the day it is cut.
    assert "upgrade" in result.output.lower() or "required_engine_version" in result.output.lower()
    assert "2026-09-26" not in result.output


def test_census_paging_merges_multiple_pages(monkeypatch, runner: CliRunner) -> None:
    """The client method pages internally (limit clamped, loop while
    returned == limit) — this is pure CLI-side loop logic, tested here
    without needing 300+ real seeded rows.

    Page size is patched to 2, with THREE items across a full page 0
    (returned == limit == 2) and a PARTIAL page 1 (returned == 1 < limit
    == 2) so the loop boundary is `returned < limit`, not merely
    `returned > 0` — a `> 0` implementation would treat page 1's nonzero
    `returned` as "keep going" and request a fourth, undefined page,
    failing loud with a ``KeyError`` rather than silently passing (review
    round 1 Suggestion-1). Page 0 and page 1 also carry DELIBERATELY
    DIFFERENT totals/scope_chunk_total sentinels (2 vs 99) — the real
    route never does this (they are collection-wide and identical on
    every page), but a stub that kept them equal could not tell "read
    totals from page 0 only" apart from "last page's totals win"; the
    final result must equal page 0's values, never page 1's.
    """
    chash1, chash2, chash3 = "a" * 64, "b" * 64, "c" * 64

    def _totals(no_owner: int) -> dict:
        return {"superseded": 0, "legacy-unmanifested": 0, "dead-owner": 0,
                 "no-owner": no_owner, "unclassified": 0}

    def _paged_response(collection, limit=100, offset=0):
        # A THIRD call (offset=4) would mean the loop never terminated on
        # page 1's partial return; there is deliberately no branch for
        # it, so a regression here fails loud (KeyError) rather than
        # looping forever.
        pages = {
            0: {
                "collection": collection, "returned": 2,
                "chashes": {"superseded": [], "legacy-unmanifested": [],
                            "dead-owner": [], "no-owner": [chash1, chash2], "unclassified": []},
                "owners": {
                    chash1: {"owner_tumbler": None, "owner_path": None},
                    chash2: {"owner_tumbler": None, "owner_path": None},
                },
                "totals": _totals(2), "scope_chunk_total": 2,
            },
            2: {
                "collection": collection, "returned": 1,
                "chashes": {"superseded": [], "legacy-unmanifested": [],
                            "dead-owner": [], "no-owner": [chash3], "unclassified": []},
                "owners": {chash3: {"owner_tumbler": None, "owner_path": None}},
                "totals": _totals(99), "scope_chunk_total": 99,
            },
        }
        return pages[offset]

    stub_client = _StubT3Client()
    stub_client.manifest_less_census = _paged_response  # type: ignore[method-assign]
    monkeypatch.setattr("nexus.db.make_t3", lambda: stub_client)
    # Force a tiny page size so two pages are needed even for three rows —
    # by patching the module-level page size the CLI passes internally.
    monkeypatch.setattr("nexus.commands.t3._CENSUS_PAGE_LIMIT", 2)
    result = runner.invoke(t3, ["census-manifest-less", "--collection", "c", "--json"])
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    row = payload["collections"][0]
    assert sorted(row["chashes"]["no-owner"]) == sorted([chash1, chash2, chash3])
    assert len(row["owners"]) == 3
    assert row["totals"]["no-owner"] == 2, "totals must be read from page 0 only"
    assert row["scope_chunk_total"] == 2, "scope_chunk_total must be read from page 0 only"


def test_census_all_stub_success_uses_the_default_list_collections(
    monkeypatch, runner: CliRunner,
) -> None:
    """The dead `_StubT3Client.list_collections()` default (review round 1
    Suggestion-2) is exercised here: a clean --all pass through a single
    stub-listed collection, otherwise untested through the stub path."""
    stub_client = _StubT3Client(page_response={
        "collection": "knowledge__stub__bge-base-en-v15-768__v1", "returned": 0,
        "chashes": {b: [] for b in _CENSUS_BUCKETS},
        "owners": {}, "totals": {b: 0 for b in _CENSUS_BUCKETS}, "scope_chunk_total": 0,
    })
    monkeypatch.setattr("nexus.db.make_t3", lambda: stub_client)
    result = runner.invoke(t3, ["census-manifest-less", "--all"])
    assert result.exit_code == 0, result.output
    assert "knowledge__stub__bge-base-en-v15-768__v1" in result.output
    assert stub_client.calls == [
        ("knowledge__stub__bge-base-en-v15-768__v1", 300, 0),
    ]


def test_census_all_mid_loop_engine_error_reports_completed_and_fails(
    monkeypatch, runner: CliRunner,
) -> None:
    """Review round 1 Important-1/Important-2 finding, --all half: a
    non-404 VectorServiceError mid-loop (the SECOND of three collections)
    must not discard the first collection's already-computed census --
    --json still emits a parseable document naming which collection
    failed, censusing stops there (the third collection is never
    attempted), and the exit code reports the failure distinctly from a
    clean run."""
    empty_totals = {b: 0 for b in _CENSUS_BUCKETS}

    def _census(collection, limit=100, offset=0):
        if collection == "second":
            raise VectorServiceError("HTTP 500: boom", code=500)
        return {
            "collection": collection, "returned": 0,
            "chashes": {b: [] for b in _CENSUS_BUCKETS},
            "owners": {}, "totals": dict(empty_totals), "scope_chunk_total": 0,
        }

    stub_client = _StubT3Client(
        list_response=[{"name": "first"}, {"name": "second"}, {"name": "third"}],
    )
    stub_client.manifest_less_census = _census  # type: ignore[method-assign]
    monkeypatch.setattr("nexus.db.make_t3", lambda: stub_client)
    result = runner.invoke(t3, ["census-manifest-less", "--all", "--json"])
    assert result.exit_code == _EXIT_ENGINE_ERROR, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)

    payload = _json.loads(result.stdout)
    assert [row["collection"] for row in payload["collections"]] == ["first"]
    assert payload["census_error"]["collection"] == "second"
    assert "500" in payload["census_error"]["error"] or "boom" in payload["census_error"]["error"]
    # censusing stopped at the failure -- "third" was never attempted.
    assert "third" not in result.stdout


def test_census_all_listing_failure_is_not_reported_as_exit_3(
    monkeypatch, runner: CliRunner,
) -> None:
    """Review round 1 Significant-2 finding: HttpVectorClient.list_collections
    swallows a non-404 failure and returns [] by default (a contract many
    other callers rely on and this bead must not change) -- exit 3 must
    mean "the listing succeeded and found no collections", never "the
    listing itself failed". The stub's `strict=True` path re-raises,
    matching the real client's new `strict` keyword."""
    stub_client = _StubT3Client(
        list_raise_error=VectorServiceError("HTTP 503: service unavailable", code=503),
    )
    monkeypatch.setattr("nexus.db.make_t3", lambda: stub_client)
    result = runner.invoke(t3, ["census-manifest-less", "--all"])
    assert result.exit_code != 3, result.output
    assert result.exit_code == _EXIT_ENGINE_ERROR, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "503" in result.output or "service unavailable" in result.output.lower()


# ── review round 2: --json must carry exactly one parseable document on
# EVERY exit path, and exit-code precedence must never hide a finding ──────


def test_census_all_json_no_collections_stdout_still_parses(
    monkeypatch, runner: CliRunner,
) -> None:
    """Review round 2 CRITICAL finding: exit 3 (--all, listing succeeded
    but genuinely empty) used to print a plain, non-err=True sentence to
    stdout and never reach the --json render block at all, so
    ``json.loads(result.stdout)`` raised ``JSONDecodeError`` -- the same
    defect class round 1's CRITICAL fixed, on an untested path. The
    notice is a diagnostic now (stderr, unconditionally); stdout carries
    the document (empty collections, exit_code 3) under --json exactly
    as it does for every other exit."""
    stub_client = _StubT3Client(list_response=[])
    monkeypatch.setattr("nexus.db.make_t3", lambda: stub_client)
    result = runner.invoke(t3, ["census-manifest-less", "--all", "--json"])
    assert result.exit_code == 3, result.output

    payload = _json.loads(result.stdout)
    assert payload["collections"] == []
    assert payload["collections_discovered"] == 0
    assert payload["collections_censused"] == 0
    assert payload["census_error"] is None
    assert payload["exit_code"] == 3

    assert "no" in result.stderr.lower() and "collection" in result.stderr.lower()
    assert "no" not in result.stdout.lower()


def test_census_no_route_json_stdout_still_parses(
    monkeypatch, runner: CliRunner,
) -> None:
    """Review round 2 CRITICAL finding: exit 4 (no-route) used to
    ``sys.exit`` before the JSON render block was ever reached, so
    ``result.stdout`` was completely empty under --json --
    ``json.loads('')`` raised ``JSONDecodeError``. Stdout must now carry a
    parseable document even though there is nothing to census; the
    human-readable upgrade message stays on stderr only."""
    stub_client = _StubT3Client(
        raise_error=VectorServiceError("POST ... -> HTTP 404: not found", code=404),
    )
    monkeypatch.setattr("nexus.db.make_t3", lambda: stub_client)
    result = runner.invoke(t3, ["census-manifest-less", "--collection", "c", "--json"])
    assert result.exit_code == _EXIT_NO_ROUTE, result.output

    payload = _json.loads(result.stdout)
    assert payload["collections"] == []
    assert payload["census_error"]["kind"] == "no_route"
    assert payload["census_error"]["collection"] == "c"
    assert payload["exit_code"] == _EXIT_NO_ROUTE

    assert "upgrade" in result.stderr.lower()
    assert "upgrade" not in result.stdout.lower()


def test_census_all_listing_failure_json_stdout_still_parses(
    monkeypatch, runner: CliRunner,
) -> None:
    """Review round 2 CRITICAL finding: the listing-failure flavor of
    exit 5 (--all's ``list_collections(strict=True)`` raising) used to
    ``sys.exit`` before the JSON render block, leaving ``result.stdout``
    empty under --json -- the identical defect the mid-loop flavor of
    exit 5 was already fixed for in round 1, just on the OTHER trigger
    path for the same exit code."""
    stub_client = _StubT3Client(
        list_raise_error=VectorServiceError("HTTP 503: service unavailable", code=503),
    )
    monkeypatch.setattr("nexus.db.make_t3", lambda: stub_client)
    result = runner.invoke(t3, ["census-manifest-less", "--all", "--json"])
    assert result.exit_code == _EXIT_ENGINE_ERROR, result.output

    payload = _json.loads(result.stdout)
    assert payload["collections"] == []
    assert payload["collections_discovered"] is None
    assert payload["census_error"]["kind"] == "listing_failed"
    assert "503" in payload["census_error"]["error"]
    assert payload["exit_code"] == _EXIT_ENGINE_ERROR


def test_census_all_engine_error_wins_over_unclassified_but_it_is_still_reported(
    monkeypatch, runner: CliRunner,
) -> None:
    """Round-3 critique: the precedence rule (an engine error, exit 5, wins
    over unclassified > 0, exit 1, but the finding is never hidden) is
    documented for both data conditions; this pins the unclassified half."""
    unclassified_totals = {b: 0 for b in _CENSUS_BUCKETS}
    unclassified_totals["unclassified"] = 1

    def _census(collection, limit=100, offset=0):
        if collection == "second":
            raise VectorServiceError("HTTP 500: boom", code=500)
        return {
            "collection": collection, "returned": 0,
            "chashes": {b: [] for b in _CENSUS_BUCKETS},
            "owners": {}, "totals": unclassified_totals, "scope_chunk_total": 1,
        }

    stub_client = _StubT3Client(
        list_response=[{"name": "first"}, {"name": "second"}],
    )
    stub_client.manifest_less_census = _census  # type: ignore[method-assign]
    monkeypatch.setattr("nexus.db.make_t3", lambda: stub_client)

    result = runner.invoke(t3, ["census-manifest-less", "--all", "--json"])
    assert result.exit_code == _EXIT_ENGINE_ERROR, result.output
    payload = _json.loads(result.stdout)
    assert payload["census_error"]["collection"] == "second"
    assert payload["unclassified"] is True
    assert payload["collections"][0]["totals"]["unclassified"] == 1

    text_result = runner.invoke(t3, ["census-manifest-less", "--all"])
    assert text_result.exit_code == _EXIT_ENGINE_ERROR, text_result.output
    assert "unclassified: 1" in text_result.output


def test_census_all_engine_error_wins_over_require_zero_but_violation_still_reported(
    monkeypatch, runner: CliRunner,
) -> None:
    """Review round 2 Significant finding: exit-code precedence between a
    real engine error (5) and a --require-zero violation already observed
    on an earlier collection (2) was undocumented and untested -- an
    engine error mid-`--all` used to short-circuit the require-zero check
    entirely (the ``if census_error is not None: sys.exit(5)`` guard fired
    before ``zero_violations`` was even computed), so a violation already
    seen on "first" was silently absent from the exit code AND, before
    this fix, from the require_zero_violations bookkeeping too. The exit
    code must be 5 (an incomplete census cannot pass a gate), but the
    violation itself must still be visible in the document (and in text
    mode) -- never hidden behind the 5."""
    empty_totals = {b: 0 for b in _CENSUS_BUCKETS}
    violating_totals = dict(empty_totals)
    violating_totals["no-owner"] = 1

    def _census(collection, limit=100, offset=0):
        if collection == "second":
            raise VectorServiceError("HTTP 500: boom", code=500)
        return {
            "collection": collection, "returned": 0,
            "chashes": {b: [] for b in _CENSUS_BUCKETS},
            "owners": {}, "totals": violating_totals, "scope_chunk_total": 1,
        }

    stub_client = _StubT3Client(
        list_response=[{"name": "first"}, {"name": "second"}],
    )
    stub_client.manifest_less_census = _census  # type: ignore[method-assign]
    monkeypatch.setattr("nexus.db.make_t3", lambda: stub_client)

    result = runner.invoke(
        t3, ["census-manifest-less", "--all", "--json", "--require-zero", "no-owner"],
    )
    assert result.exit_code == _EXIT_ENGINE_ERROR, result.output

    payload = _json.loads(result.stdout)
    assert payload["census_error"]["collection"] == "second"
    assert payload["require_zero_violations"] == ["no-owner"]
    assert payload["collections"][0]["totals"]["no-owner"] == 1

    text_result = runner.invoke(
        t3, ["census-manifest-less", "--all", "--require-zero", "no-owner"],
    )
    assert text_result.exit_code == _EXIT_ENGINE_ERROR, text_result.output
    assert "no-owner: 1" in text_result.output
