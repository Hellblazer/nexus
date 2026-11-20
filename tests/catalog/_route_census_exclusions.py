# SPDX-License-Identifier: AGPL-3.0-or-later
"""The exclusion tables of tests/catalog/test_fake_catalog_handler_route_census.py.

Moved out of the census test so the test file stays under the test size cap
(scripts/file_size_ratchet.py). The census imports both tables; their meaning
and rules are unchanged: every entry carries its reason, and the census refuses
a stale entry as loudly as a missing one.
"""
from __future__ import annotations

#: Java routes with NO FakeCatalogHandler branch — each falls through to the
#: fake's generic catchall (`{"error": "unknown GET op"}` 404 for GET,
#: `{"ok": True}` 200 for POST). Auditable, never silent — the same
#: discipline as test_shape_parity_tripwire.py's EXCLUSIONS dict. Every entry
#: here is drift *found*, not drift *fixed* (out of this bead's file-touch
#: scope); if a future change makes one of these routes reachable through the
#: live fake server, delete the entry (the completeness gate below refuses a
#: stale one).
ROUTE_EXCLUSIONS: dict[str, str] = {
    "/sensitive": (
        "nexus-f3yqd: DELIBERATELY engine-only for now (engine 0.1.188) — the "
        "client half (an HttpCatalogClient method behind the doctor's bounded "
        "reads) is a separate change, so there is no Catalog or "
        "HttpCatalogClient method to fake yet; adding the Python half here "
        "would make this commit a both-halves wire-contract commit "
        "(docs/wire-contract-pending.md). Its wire shape — count, limit, "
        "capped, and documents of {tumbler, physical_collection}, the 400 on "
        "a limit outside 1..10000 and the 503 on a statement timeout — is "
        "validated Java-side by CatalogSensitiveListRouteTest. When the "
        "client method lands, DELETE this entry and fake the route instead; "
        "the census checks both directions, so a stale exclusion here fails "
        "just as loudly as a missing one"
    ),
    "/uri_segment_census": (
        "RDR-229 P1.2 (nexus-p2m1x.2): engine route for the residue doctor row; the "
        "client method (HttpCatalogClient.uri_segment_census, engine_reads.uri_segments) is "
        "tested against a stubbed client, as sensitive_locators is, so the fake serves no "
        "branch for it. Its wire shape (count, limit, capped, groups of {table, scheme, "
        "segment, collection, content_type, rows}, the 400 on a limit outside 1..10000 and "
        "the 503 on a statement timeout) is validated Java-side by UriSegmentCensusRouteTest"
    ),
    "/manifest/foreign_collection": (
        "GH #1611: engine route for the doctor's foreign-collection row; the "
        "client method (HttpCatalogClient.manifest_foreign_collection) is "
        "tested against a stubbed transport, as sensitive_documents is, so "
        "the fake serves no branch for it. Its wire shape (count, limit, "
        "capped, documents of {tumbler, physical_collection, collections, "
        "chunks}, the 400 on a limit outside 1..10000 and the 503 on a "
        "statement timeout) is validated Java-side by "
        "ManifestForeignCollectionRouteTest"
    ),
    "/manifest/fillable": (
        "nexus-f3yqd: DELIBERATELY engine-only for now (engine 0.1.188, route "
        "R1 of T2 nexus/rung-detect-route-spec-2026-11-10) — the client half "
        "(the rungs' _engine_fillable slot) is a separate change, so there is "
        "no HttpCatalogClient method to fake yet; adding the Python half "
        "here would make this commit a both-halves wire-contract commit "
        "(docs/wire-contract-pending.md). Its wire shape — kind, fillable, "
        "limit, capped, rule and stamp, the 400 on a bad kind or limit and "
        "the 503 on a statement timeout — is validated Java-side by "
        "ManifestFillableRouteTest. The older /manifest/section_label_fillable "
        "stays faked for old clients. When the client method lands, DELETE "
        "this entry and fake the route instead; the census checks both "
        "directions, so a stale exclusion here fails just as loudly as a "
        "missing one"
    ),
    "/manifest/fill": (
        "nexus-f3yqd: DELIBERATELY engine-only for now (engine 0.1.188, route "
        "R2 of T2 nexus/rung-detect-route-spec-2026-11-10) — the client half "
        "(the rungs' converge step starting the fill and polling it, and the "
        "policy flag for an unattended run against a managed service) is a "
        "separate change, so there is no HttpCatalogClient method to fake "
        "yet; adding the Python half here would make this commit a "
        "both-halves wire-contract commit (docs/wire-contract-pending.md). "
        "Its wire shape — the 202 {job_id, created}, the 400 on a bad kind, "
        "dry_run or batch_rows, the 409 for the other dry_run, and the poll "
        "GET /manifest/fill/{job_id} (state, stage, done_rows, total_rows, "
        "filled, cursor, counts, safe_to_stop, on_stop), which carries its id "
        "in the path and is dispatched from the handler's default branch, so "
        "the census does not see it as a case label — is validated Java-side "
        "by ManifestFillRoutesTest, ManifestFillJobTest and "
        "ManifestFillConcurrencyTest. When the client method lands, DELETE "
        "this entry and fake the route instead; the census checks both "
        "directions, so a stale exclusion here fails just as loudly as a "
        "missing one"
    ),
    "/collections/rehome/status": (
        "nexus-wsx4l: the pollable half of the engine-only re-home below, and "
        "excluded for the same reason. It exists as a separate route because a "
        "re-home measured 169 s against a ~30 s edge read deadline, so the "
        "submit's response never reaches the caller and the outcome has to be "
        "readable afterwards; CatalogHandlerRehomeTest validates its wire shape "
        "Java-side"
    ),
    "/collections/rehome": (
        "nexus-wsx4l: DELIBERATELY engine-only — there is no Catalog or "
        "HttpCatalogClient method for it, so it is outside the "
        "shared-surface parity gate by construction rather than by "
        "oversight. The bounded re-home is an operator primitive driven "
        "from the conexus side against the engine's HTTP surface directly; "
        "adding a Python half would make every commit carrying it a "
        "both-halves wire-contract commit (docs/wire-contract-pending.md) "
        "and put an operator repair verb on the paired-release choreography "
        "for no caller that wants it. Its wire shape — the required "
        "source/target/max_documents keys, the 409 refusal mapping, and the "
        "exact response field names a caller's loop reads, remaining_rows "
        "and done included — is validated Java-side by "
        "CatalogHandlerRehomeTest, and its transaction semantics by "
        "CollectionRehomeTest. If a Python client method is ever added, "
        "DELETE this entry and fake the route instead; the census checks "
        "both directions, so a stale exclusion here fails just as loudly as "
        "a missing one"
    ),
    "/collections/delete": (
        "backs HttpCatalogClient.delete_collection() (RDR-164 P2) — NOT "
        "Catalog.delete_collection_projection (different name/shape by "
        "design), so it is outside the Catalog/HttpCatalogClient "
        "shared-surface parity gate in test_shape_parity_tripwire.py "
        "entirely; delete_collection() is unit-tested against a mocked "
        "_post, not the live fake server"
    ),
    "/collections/set_aspects_enabled": (
        "nexus-l46pu: backs HttpCatalogClient.set_collection_aspects_enabled(), "
        "a _SERVICE_ONLY_WRITE_OPS entry (src/nexus/catalog/factory.py) with "
        "no canonical Catalog/CatalogWriter counterpart — the tenant-wide "
        "docs__ aspect-extraction opt-in on catalog_collections did not exist "
        "before this bead, same disposition as update_many/delete_many above. "
        "Round-tripped end to end against the REAL engine substrate in "
        "tests/test_collection_aspects_cmd.py (no catalog mocking, per that "
        "file's own docstring), not this harness's fake server; the wire "
        "shape (400/404 mapping, the docs-only content_type guard) is pinned "
        "Java-side by CatalogHandlerSetAspectsEnabledTest"
    ),
    "/links/upsert_many": (
        "RDR-193 Part A4 (nexus-u5m5g.4, client nexus-u5m5g.8): backs HttpCatalogClient.links_upsert_many(), a "
        "_SERVICE_ONLY_WRITE_OPS entry with no canonical Catalog counterpart, so it is outside the shared-surface "
        "parity gate by construction. FakeCatalogHandler does not serve it: the client method and the generators that "
        "ride it are driven against the purpose-built fake in tests/catalog/_link_engine_fake.py (wire-faithful to "
        "CatalogLinksHandler.java; tests/catalog/test_http_catalog_client_links.py, test_link_generator_scoped_reads.py) "
        "and the real engine (tests/integration/test_rdr193_linking_e2e.py). The wire shape is validated Java-side by "
        "CatalogHandlerLinksBatchRoutesTest and the SQL by CatalogLinksUpsertManyTest"
    ),
    "/links/same_content": (
        "RDR-193 Part A4 (nexus-u5m5g.4, client nexus-u5m5g.8): backs HttpCatalogClient.links_same_content(), outside "
        "the shared-surface parity gate for the same reason as /links/upsert_many above, and exercised by the same "
        "fake and the real engine; validated Java-side by CatalogHandlerLinksBatchRoutesTest and "
        "CatalogLinkSameContentTest"
    ),
    "/reconcile/stage": (
        "RDR-193 Part A1 (nexus-u5m5g.3): DELIBERATELY engine-only until the client switch "
        "(Step 6, which adds HttpCatalogClient.reconcile_stage and drops this entry): there is "
        "no Catalog or HttpCatalogClient method for it yet, so it is outside the shared-surface "
        "parity gate by construction. Its wire shape is validated Java-side by "
        "CatalogHandlerReconcileRoutesTest, and the end state it produces is compared with the "
        "old client path by the Part D parity fixture. The census checks both directions, so "
        "this entry must go when the fake gains the route"
    ),
    "/reconcile/commit": (
        "RDR-193 Part A1 (nexus-u5m5g.3): the submit half of the reconcile job pair, engine-only "
        "until the client switch (Step 6) for the same reason as /reconcile/stage above; "
        "validated Java-side by CatalogHandlerReconcileRoutesTest (202, the same run's running "
        "job, 409, 503). The poll, GET /reconcile/{job_id}, carries its id in the path and is "
        "dispatched from the handler's default branch, so the census does not see it as a case "
        "label"
    ),
    "/doc/resolve_or_register": (
        "RDR-193 Part A1 (nexus-u5m5g.3): DELIBERATELY engine-only until the client switch "
        "(Step 6 collapses _register_or_lookup_doc_id, _catalog_markdown_hook and store_hook onto "
        "it and adds HttpCatalogClient.resolve_or_register): no Catalog or HttpCatalogClient "
        "method exists yet. Validated Java-side by CatalogHandlerReconcileRoutesTest; delete this "
        "entry when the fake gains the route"
    ),
    "/update_many": (
        "HttpCatalogClient-only batch optimization (nexus-xedhp) with no "
        "Catalog equivalent method, so it is outside the shared-surface "
        "parity gate; update_many() is unit-tested against a mocked _post "
        "(test_update_many_pages_and_preserves_order et al.), not the live "
        "fake server"
    ),
    "/delete_many": (
        "HttpCatalogClient-only batch optimization (nexus-xedhp) with no "
        "Catalog equivalent method, so it is outside the shared-surface "
        "parity gate; delete_many() is unit-tested against a mocked _post, "
        "not the live fake server"
    ),
    "/doc/register_many": (
        "IS on the shared surface (register_many exists on both Catalog and "
        "HttpCatalogClient) and DOES have a live-fake-server REGISTRY entry "
        "in test_shape_parity_tripwire.py — but the fake's generic "
        "`else: send({'ok': True})` catchall lacks a 'tumblers' key, so "
        "HttpCatalogClient.register_many's page-failure fallback "
        "(nexus-9dvqy) silently treats the page as failed and retries "
        "per-doc via /doc/register (which IS faked), masking the gap. The "
        "batch route's real wire shape is validated by the Java-side tests, "
        "not this harness — a pre-existing masking gap found while building "
        "this guard (nexus-0948s), documented here rather than fixed, per "
        "this bead's file-touch constraints"
    ),
    "/manifest/write_many": (
        "backs HttpCatalogClient.write_manifest_many() via raw _post — "
        "unit-tested against a mocked _post, not the live fake server. "
        "REVISITED nexus-wxjr6 (2026-08-09, the CLIENT half of nexus-kl2z6's "
        "combined write, per the breadcrumb this entry used to carry): "
        "HttpCatalogClient.write_manifest_many() now DOES send `chunks`/"
        "`collection`/`sweep`/`force_re_embed` (the ChunkBatcher flush-grain "
        "path, indexer.py's _batch_flush) — the condition that used to gate "
        "this revisit has triggered. Decision: EXTEND this exclusion rather "
        "than add a FakeCatalogHandler branch. The route's exclusion reason "
        "was never actually about `chunks` being absent — it is, and "
        "remains, that this method (like its sibling `/manifest/resync` "
        "and `/manifest/write`/`/manifest/append` above) is exercised "
        "exclusively via a mocked `_post` in tests/catalog/"
        "test_manifest_write_many.py and tests/db/test_http_catalog_client.py "
        "(ack-echo RAISE, sweep accounting, chunks payload construction — "
        "all unit-tested against the mock, not the live fake server). "
        "Building a live-fake branch that fabricates chunks_written/swept/"
        "sweep_skipped/sweep_detail would be new test infrastructure "
        "disproportionate to this bead's scope (a combined-write response "
        "shape, not a simple echo); if a future bead needs write_many "
        "exercised through the live fake server, delete this entry and add "
        "the branch then"
    ),
    "/manifest/resync": (
        "backs HttpCatalogClient.resync_chunk_count_cache() via raw _post "
        "— unit-tested against a mocked _post, not the live fake server"
    ),
    "/list_by_file_paths": (
        "nexus-1vc0n: backs HttpCatalogClient.find_all_by_file_paths(), which "
        "IS on the shared Catalog/HttpCatalogClient surface (catalog_protocol.py) "
        "— but its owner-agnostic batch semantics (N paths -> live docs across "
        "every owner, feeding indexer._catalog_hook's announce_cross_owner_mint "
        "at O(1) round trips per page instead of the O(N) a per-document lookup "
        "would cost) are round-tripped against the REAL engine substrate in "
        "tests/catalog/test_1vc0n_bulk_file_path_lookup.py, not this harness's "
        "fake server. The one-shot 404-engine-floor warning "
        "(_find_all_by_file_paths_404_warned) and the per-page batching are "
        "exercised there; a FakeCatalogHandler branch would need to fabricate "
        "multi-owner resolution the fake's single-tenant model does not carry"
    ),
    "/by_title": (
        "nexus-gacz0: backs HttpCatalogClient.find_by_title_exact(), on the shared "
        "Catalog/HttpCatalogClient surface (catalog_protocol.py). Its exactness, "
        "owner/content_type scope, tombstone and tenant filtering and the absence of "
        "a page cap are pinned engine-side by CatalogHandlerByTitleTest; the client's "
        "use of it and its 404/405 fallback to the full-text search are pinned in "
        "tests/catalog/test_gacz0_find_by_title_exact_route.py against a stubbed "
        "_get, and the route is round-tripped against the real engine in "
        "tests/test_ghost_by_title_past_first_page.py. A FakeCatalogHandler branch "
        "would re-implement exact-title filtering the fake's single canned entry "
        "does not carry. Delete this entry if a fake branch is ever added"
    ),
    "/owners/sweep_next_seq_drift": (
        "admin one-shot converge verb (nexus-0ehwe item 5, engine commit "
        "55615902) with NO Python client method yet — the live-cloud "
        "invocation is an operator relay against the engine directly, and "
        "the route is exercised by the Java-side NextSeqSweepTest + "
        "CatalogHandlerSweepNextSeqDriftTest, not this harness; if a "
        "Python client method is ever added, delete this entry and fake "
        "the route"
    ),
    "/import/chunk": (
        "ETL bulk-import path, formerly called via raw client._post() from "
        "the since-deleted src/nexus/db/t2/catalog_etl.py (nexus-i711w "
        "Stage 2 sub-stage A) — outside the Catalog/HttpCatalogClient "
        "shared-surface parity gate by construction; the engine route "
        "retires with RDR-187 .11"
    ),
    "/import/collection": (
        "ETL bulk-import path, formerly called via raw client._post() from "
        "the since-deleted src/nexus/db/t2/catalog_etl.py (nexus-i711w "
        "Stage 2 sub-stage A) — outside the Catalog/HttpCatalogClient "
        "shared-surface parity gate by construction; the engine route "
        "retires with RDR-187 .11"
    ),
    "/docs/by-scheme": (
        "nexus-uwggq.1.5 (RDR-241 Phase 2). It HAS a client consumer "
        "(HttpCatalogClient.documents_by_scheme, called by `nx doctor "
        "--check-references`); it is excluded here only because no "
        "start_fake_server() test reaches it. The consumer is exercised against "
        "a local HTTP server (tests/test_doctor_references_by_scheme.py::"
        "test_client_method_round_trip) and against the real engine substrate "
        "(tests/test_doctor_references.py::TestRealCatalogRoundTrip). Delete this "
        "entry if a FakeCatalogHandler branch for it is ever added"
    ),
    "/docs/by-author": (
        "nexus-uwggq.2.3 (RDR-241 Phase 2): the client reader HttpCatalogClient.documents_by_author "
        "is tested against a stubbed transport (tests/catalog/test_engine_reads_phase2_clients.py) "
        "and the route by its engine tests; no start_fake_server test reaches it. "
        "REMOVE THIS ENTRY when a FakeCatalogHandler branch is added"
    ),
    "/docs/by-meta": (
        "nexus-uwggq.2.4 (RDR-241 Phase 2): the client reader HttpCatalogClient.documents_by_meta "
        "is tested against a stubbed transport (tests/catalog/test_engine_reads_phase2_clients.py) "
        "and the route by its engine tests; no start_fake_server test reaches it. "
        "REMOVE THIS ENTRY when a FakeCatalogHandler branch is added"
    ),
    "/sensitive/locators": (
        "nexus-uwggq.2.5 (RDR-241 Phase 2): the client reader HttpCatalogClient.sensitive_locators "
        "is tested against a stubbed transport (tests/catalog/test_engine_reads_phase2_clients.py) "
        "and the route by its engine tests; no start_fake_server test reaches it. "
        "REMOVE THIS ENTRY when a FakeCatalogHandler branch is added"
    ),
    "/gc_audit/record": (
        "nexus-jqvzk, an engine surface with no client caller. `nx t3 gc` "
        "emitted through it (nexus-fduai) until nexus-wbfpw.18 moved the verb "
        "onto the engine's bounded quarantine route, which writes its own "
        "gc_audit row per batch (actor engine); HttpCatalogClient."
        "record_gc_audit was deleted with its last caller. REMOVE THIS ENTRY "
        "when a client caller returns — an exclusion outliving its reason is "
        "the stale-allowlist class this repo has been bitten by (see the "
        "mode-lint orphans, nexus-th15h)"
    ),
    "/gc_audit/list": (
        "nexus-jqvzk, the read half of the audit surface. It HAS client "
        "consumers (HttpCatalogClient.gc_audit_list, called by `nx catalog "
        "gc-audit list` and by the nx doctor purge-trash audit check in "
        "health.py); it is excluded here only because the live fake server "
        "carries no gc_audit table to answer from, and those consumers are "
        "exercised against a mocked transport (tests/test_catalog_gc_audit_cmd.py, "
        "tests/test_health_service_checks.py) and against the real engine "
        "substrate (tests/test_wbfpw18_t3_gc_substrate.py). Delete this entry "
        "if a FakeCatalogHandler branch for it is ever added"
    ),
}

#: Fake routes with NO CatalogHandler.java counterpart — either they belong
#: to a different HTTP handler class entirely, or the reason must say so.
KNOWN_NON_CATALOG_HANDLER_FAKE_ROUTES: dict[str, str] = {
    "/v1/chash/lookup": (
        "belongs to ChashHandler.java (service/src/main/java/dev/nexus/"
        "service/http/ChashHandler.java), a different HTTP handler class "
        "registered on a different top-level path — not part of "
        "CatalogHandler.java's route switch, so it can never appear there"
    ),
}
