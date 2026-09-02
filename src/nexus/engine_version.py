# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Single source of truth for the engine-service dependency version (nexus-b6qlf).

ONE-ENGINE MODEL (nexus-cfgo9, 2026-07-15, after the 14h GH #1402 delivery
failure): a conexus release has ONE engine dependency — the exact version it
was built and tested against. This is NOT a "minimum" or "floor" that older
engines might still limp along under; it is a required dependency, installed
like any other, on EVERY box, via EVERY install path (fresh ``nx init`` AND
convergence on an existing box's upgrade — see
:mod:`nexus.upgrade_finish`'s ``converge_engine``). A version mismatch on a
local install is a convergence step (install the dependency, restart the
service), never a user-facing refusal. The ONE place a ``>=`` "floor"
comparison legitimately survives is the cloud/managed handshake
(:mod:`nexus.db.managed_endpoint`): the client cannot install the managed
service's engine, and the managed deployment legitimately runs ahead of any
given client release between PyPI cuts, so "deployed >= tested-with" is the
right check there — nowhere else.

Prior to nexus-b6qlf this module's value was hand-maintained as TWO
independent constants that could silently drift apart:
``guided_upgrade.REQUIRED_RELEASE_VERSION`` (the native/local floor,
actively bumped alongside RDR work) and ``managed_endpoint.MIN_MANAGED_
RELEASE_VERSION`` (the managed-cloud floor, introduced 2026-06-24 at
``(0, 1, 8)`` and never bumped again). Both currently gate the identical
``release_version`` field on the same ``GET /version`` handshake — there was
never a topology reason for two numbers, only an accident of two modules
independently owning "their" constant. This module unifies both into one
pinned dependency version plus one parser; bumping it once moves the
requirement everywhere — and, since nexus-cfgo9, converges every existing
local install to match rather than merely raising a refusal threshold.

**Leaf module contract**: this file MUST NOT import anything from the
``nexus`` package (stdlib only). Both ``nexus.db.managed_endpoint`` and
``nexus.upgrade_ladder.provisioning`` (the version-pin's home since the
RDR-155 P4b P0e rehome out of ``nexus.migration.guided_upgrade``) import
from here, and those two packages have no dependency relationship with each
other — a leaf module is what lets both import the shared floor without
introducing a ``db`` <-> ``upgrade_ladder`` circular-import risk. Enforced by ``tests/test_engine_version.py::
test_module_is_stdlib_only_leaf`` (AST-walks this file's imports) and by the
release-checklist grep gate over this file for any intra-package import
statement, which must report nothing.
"""
from __future__ import annotations

#: THE engine-service release this conexus build was made and tested
#: against — the one engine dependency, pinned on the dedicated
#: ``release_version`` field of the unauthenticated ``GET /version``
#: handshake (RDR-002 contract; conexus PR #78). NOT ``app_version`` — that
#: field is the JAR's frozen Maven coordinate ``1.0-SNAPSHOT`` and is a
#: structural no-op to gate on (any build clears it).
#:
#: This is a DEPENDENCY VERSION, not a compatibility minimum: every local
#: (native/service-mode) install is expected to converge to exactly this
#: version, via fresh ``nx init`` (which installs it directly) or via
#: convergence on an existing box (:func:`nexus.upgrade_finish.converge_engine`,
#: run automatically post-upgrade and as the ``nx doctor`` backstop). Only
#: the managed-cloud handshake (:mod:`nexus.db.managed_endpoint`) still reads
#: this as a floor (``deployed >= this``), because a client cannot install
#: the cloud's engine and the cloud legitimately runs ahead of any one
#: client release.
#:
#: History: (0,1,5) -> (0,1,8) for nexus-x2g1z (2026-06-24, the managed-cloud
#: probe's introduction). -> (0,1,34) for 6.5.0: the client hard-requires
#: catalog-012 (graph-hop `where` — pre-012 engines silently ignore the key,
#: the H2 version-skew failure class) and catalog-013-1b (pre-1b engines fail
#: boot VALIDATE on tenants with legacy 64-char chash rows — the nexus-1wjmq
#: incident). -> (0,1,39) for nexus-rn3wo.1 (2026-07-12): T1 scratch now
#: defaults to the PG-backed service with no Chroma fallback, and every
#: engine before v0.1.38 has a native-image reflection-registration gap that
#: 500s on every T1 get/search/list (nexus-opr9m) — an engine below this no
#: longer degrades, it silently breaks a hard default. Bump this ONE
#: constant to move the dependency for every client path (native
#: guided-upgrade handoff, the managed-cloud probe, the automated
#: convergence pass, AND — since 2026-07-12 —
#: ``nexus.daemon.binary_install.PINNED_SERVICE_TAG``, the exact tag a fresh
#: local install downloads, which is now DERIVED from this constant rather
#: than independently hand-typed) — there is no second knob to remember.
#:
#: WHEN TO BUMP (refined 2026-07-14, per Hal): not only when the client
#: hard-requires new engine features — ALSO when the engine release carries
#: user-facing FIXES the client release will advertise. For local
#: service-mode installs this dependency/pin is the ONLY fix-delivery
#: vehicle: the engine on a local box moves via PINNED_SERVICE_TAG (fresh
#: installs) or convergence (existing installs — see nexus-cfgo9). A release
#: whose changelog claims an engine-side fix without moving this dependency
#: ships a broken promise to every local install and pins fresh installs to
#: the still-broken engine.
#:
#: -> (0,1,41) for the 2026-07-13 release-gate
#: arc: service-mode remediation consent audit hard-requires the consents
#: table (telemetry-002, v0.1.40+); retention markers + range where-operators
#: hard-require v0.1.41; and conexus declared tags <=0.1.40 invalid rollback
#: targets after the A6 view-era grants changeset (engine-rollback-floor-0141).
#: -> (0,1,42) 2026-07-14: catalog-015 FTS filename-token fix (nexus-8gue1,
#: the GH #1397 search blindness) + indexed_at repair provenance
#: (nexus-p5qk8) live in the engine — the fix-delivery rule above, applied.
#: -> (0,1,44) 2026-07-16: GET /v1/telemetry/tier_writes/query (nexus-59wjj)
#: — the 6.11.0 tier-status / doctor / session-end read parity hard-depends
#: on the route; deployed + cloud-gated 2026-07-16 (recall 12/12, hybrid
#: p95 1920ms < 2376 bound). One-engine-per-release: ship what was tested.
#: -> (0,1,47) 2026-07-18: RDR-186 SQLite retirement hard-depends on the
#: full /v1 trio — /v1/remap (v0.1.45, chash_remap facts + live-membership
#: convergence, nexus-146xx.4/.5), /v1/ladder (v0.1.46, completion ledger,
#: .12), /v1/pipeline (v0.1.47, the streaming-PDF buffer, .16). The client
#: has NO local fallback for any of them (pipeline.db/ladder.db deleted),
#: so a client at this release against an older engine cannot index PDFs
#: or record ladder completions — the floor IS the fix-delivery vehicle.
#: -> (0,1,49) 2026-07-19: the RDR-180 cohort. The client's chash cutover
#: (64-hex producers, bytea serving, guided land-then-transform, rekey rung)
#: hard-depends on the cohort schema (27+1 rdr180 changesets) and the
#: /v1/remap/rekey + staging endpoints; and v0.1.49 carries
#: rdr180-16-analyze-rewritten-tables, the BUG-0148 stale-planner-stats
#: product fix — a boot that applies the ALTER TYPE rewrite un-ANALYZEd
#: degrades sparse-gate hybrid search to zero rows with every health probe
#: green (conexus-xpg7). Deployed + per-query-diff cloud-gated 2026-07-19
#: (parity 104/113 == baseline, recall AC3 12/12, xpg7 probes 3/3 J=1.0).
#: -> (0,1,51) 2026-07-21: the RDR-187 conformance tail. v0.1.50 carries the
#: chash_index DROP migration prerequisites; v0.1.51 delivers the 339xv/
#: kmd5b/b878d fixes (planner blind-spot on just-written rows) and validated
#: every octet CHECK on the conformance arc close ([21020]). Deployed to
#: api.conexus-nexus.com + cloud-gated GREEN 2026-07-21. One-engine-per-
#: release: v0.1.51 is the exact engine this release was tested with.
#: -> (0,1,52) 2026-07-22: RDR-188 server-side rerank. The client's search
#: repoint (P2, rerank fused into the /v1/vectors/* handlers, client
#: Voyage reranker deleted) hard-depends on the engine-side rerank
#: envelope {results, rerank_degraded, rerank_model|rerank_error}; the
#: floor is the fix-delivery vehicle for local service-mode installs.
#: v0.1.52 also carries catalog-016 (TOCTOU dupe backstop; 016-0
#: tombstoned the live 201-dupe class at deploy — 403 rows/276 uris,
#: proportional to the known population — and 016-1's unique index
#: proves no live dupe survived). Deployed to api.conexus-nexus.com +
#: per-query-diff cloud-gated GREEN 2026-07-23Z (parity 106/113 ==
#: baseline with zero per-query regressions, recall AC-3 identical,
#: reranker=rerank-2.5 active with zero degrade events, T2 [21062]).
#: -> (0,1,56) 2026-07-25: ONE-ENGINE-IDENTITY bump, not a feature dependency.
#: v0.1.53-.56 were cut, gated and deployed while this constant sat at
#: (0,1,52), so four tags of engine work reached NO local-mode install —
#: cloud users get whatever conexus deployed, local installs get ONLY what
#: this constant names. Carries: v0.1.53 the P2-J chroma-trio + ingest-cloud
#: deletion (RDR-155 P4b) and the chash-410 recut; v0.1.55 the manifest
#: count guard (nexus-ir6eh, partial-truncation defense), /livez and
#: /version off the pool (nexus-hubc0), /links/orphaned; v0.1.56 the
#: nexus-asaod fix — an RLS row refusal on /import/* is a caller-resolvable
#: 409, not an opaque 500, without leaking that the id exists under another
#: tenant. Gates: --shakeout PASSED, published-artifact --acquire PASSED
#: 11/11 on a bare box (/version == the tag, store/index/search, doctor
#: clean), deployed to api.conexus-nexus.com and cloud-gated GREEN
#: 2026-07-25 (recall@20 12/12 local==cloud, hybrid parity 113/105 zero
#: per-query regressions, p95 1873ms < v0.1.55's 1886ms). Deploy verified
#: independently via `nx service probe` before this bump; record-deploy
#: written [21141]. Bumped AFTER the deploy on purpose — bumping first makes
#: every cloud client refuse the managed service as below-identity (GH #1402
#: inverted). Detection for the gap that let this drift: nexus-6igii.
#: -> (0,1,57) 2026-07-27: the T2/catalog read-correctness cohort. Four engine
#: defects, all of them silent-wrong-answer rather than error, all found while
#: porting the test suite onto the engine substrate (nexus-aqbrk):
#: memory FTS could not find a word INSIDE a dotted title, so every
#: .md-suffixed T2 note was unfindable by title fragment (nexus-22r1f,
#: 73fdd124); memory and catalog search did not fold Latin-1 diacritics, so
#: 'resume' missed 'résumé' where the SQLite baseline matched (c52068bb,
#: 0bd62f93); tombstoned documents stayed visible to 21 catalog LIST reads
#: (nexus-23wlw, 12614bdb); and graph traversal ignored include_heuristic, so
#: the implements-heuristic flood was back on by default (nexus-ybj1b,
#: 12614bdb). The floor IS the fix-delivery vehicle: cloud users already have
#: these, local-mode installs get ONLY what this constant names.
#: Gates: deployed to api.conexus-nexus.com (digest sha256:eeab1cad..., cosign
#: KMS verified at redeploy), STEP-6 GREEN — parity 105/113 BYTE-IDENTICAL to
#: the v0.1.56 baseline with zero per-query regressions, recall AC-3 12/12
#: local==cloud with zero vacuous legs. STEP-6 structurally CANNOT see this
#: release's behaviour change (its legs read chunks_{384,768,1024}; these
#: changesets touch nexus.memory and nexus.catalog_documents), so conexus wrote
#: a separate read-only probe: 11/11 PASS live, including the Latin-1 boundary
#: (Cyrillic 'Тодор' UNCHANGED — the fold is a 1:1 translate() matching FTS5's
#: remove_diacritics=1 exactly, NOT unaccent, which is absent from every
#: shipped PG bundle and over-folds). record-deploy written and verified
#: against the live /version before this bump; relay T2 [21164].
#: NOT in this tag, so do not assume them from the floor: analyze-002
#: (the BUG-0148 ANALYZE invariant, 308522b3) and the staging-4 rollback-split
#: fix (c13d0c84) both landed AFTER the cut.
#: -> (0,1,58) 2026-07-28: the nexus-onjvy write-only-surface cohort, plus the
#: two changesets the v0.1.57 note above flagged as deliberately absent.
#: THREE READ ROUTES for data the engine already wrote and no route returned:
#: GET /v1/telemetry/hook_failures/list (the failure log existed only as
#: /record + /trim, so the thing that surfaces SILENT hook failures could be
#: written and never inspected); POST /v1/taxonomy/assignments/details
#: (similarity / assigned_at / source_collection were written by assign and
#: projected by nothing, so an operator could not ask how confident an
#: assignment was); and /hubs now returns max_last_discover_at,
#: never_discovered_count and is_stale, so detect_hubs(warn_stale=) stops being
#: accepted-and-silently-dropped. No DDL — every column already existed.
#: Also carries analyze-002 (307 changesets now, up from 206), which conexus
#: confirmed working on deploy: both rewritten tables came back FRESH with no
#: hand-run ANALYZE, where v0.1.57 needed one as nexus_admin.
#: A TIMESTAMP WIRE FIX rides along and is worth knowing about: these reads
#: emit UTC ISO-8601 with explicit seconds via each repository's UTC_SECOND
#: formatter. OffsetDateTime.toString() renders in the JVM's LOCAL offset and
#: elides zero seconds, so the same instant crossed as "2026-04-08T17:00-07:00"
#: on one host and "...T00:00:00Z" on another — either property breaks a client
#: that compares these as strings, which the retired SQLite oracle did.
#: Gates: full engine suite 1475/0/0 twice consecutively; CANDIDATE SHAKEOUT
#: PASSED pre-tag (native build + verb matrix + zero 5xx under load); ACQUIRE
#: GATE PASSED 12/12 on the PUBLISHED, cosign-verified bytes; deployed to
#: api.conexus-nexus.com (digest sha256:dea44148..., cosign KMS + spdxjson
#: attestation verified at redeploy). record-deploy written and verified
#: against the live /version before this bump [21219 relay chain].
#: ONE THING NOT ESTABLISHED, so nobody infers it from the floor: v0.1.58's
#: STEP-6 parity moved 105 -> 104 and that is CORPUS DRIFT (+1,514 chunks,
#: +34 documents between captures), NOT this release. An ekn9n topic-boost
#: attribution was floated across the bus and WITHDRAWN by both sides:
#: apply_topic_boost is Python-client-only and absent from the engine, so it
#: cannot execute on the /v1/vectors/* path STEP-6 measures. See nexus-j46lz.
#: ── v0.1.59 (2026-07-31) — the 7.0.0 catalog defect set ──────────────────────
#: Carries nexus-mqd6t (tombstone filters across every manifest-rooted read:
#: chashesForCollection, docsForChashes, getManifest, getManifestMany,
#: resolveChash's doc_id attribution, two PgVectorRepository siblings — plus the
#: non-resurrection rule), nexus-e4gel (chunk_count re-derivation, guarded so an
#: incidental update cannot zero a positive count against an EMPTY manifest —
#: that disagreement is the GH #1371 damage signature reconcile classifies on),
#: nexus-s4e1n (the co_discovered_by link-merge fold), nexus-tz1cx (a real
#: metadata.doc_id lookup route), nexus-ekaxn (alias following on /show and
#: updateDocument) and nexus-jqvzk (the gc_audit surface).
#: nexus-9ssih was deliberately HELD OUT: its 400 would have broken every
#: already-installed client, since nothing client-side caught it. Its client
#: half shipped first (bc175f29-adjacent, a62649ef) and the engine half rides
#: the NEXT tag.
#: SCHEMA: exactly TWO new changesets, catalog-018-1/-2 (orders 517/518),
#: creating nexus.gc_audit + indexes + RLS. 207 -> 209. A NEW table, so no
#: existing table rewritten (no stale-planner-stats exposure) and no executed
#: changeset mutated (no Liquibase checksum risk) — conexus called it the safest
#: deploy shape they have taken; rollback to 0.1.58 is a plain image revert.
#: Gates: engine suite 1517/0; deployed to api.conexus-nexus.com (digest
#: sha256:ba7a0a18..., cosign KMS + spdxjson attestation verified, on-host
#: cosign verify PASS at redeploy); STEP-6 parity BYTE-IDENTICAL to the v0.1.58
#: baseline (113 evaluated / 104 passed / p50_jaccard 1.0, zero per-query
#: regressions), recall AC-3 12/12 local == cloud with zero vacuous legs,
#: latency margins the widest recorded, corpus drift NONE (41 collections,
#: drifted=false — the check built after the v0.1.58 mis-attribution, running
#: live for the first time). Relay [21293].
#: AND THE ONE THAT MATTERS MOST: nexus-bwulw's CLOUD CLIENT-PATH GATE, written
#: as EXPECTED RED, now PASSES all four legs from a real cloud-mode box against
#: the live public edge — /version fields come through (voyage threshold gating
#: ON, dimension-orphan tooling live), /health honors the ez5.1 contract for
#: bearers, the embedding_mode probe resolves, the read path round-trips.
#: conexus's own STEP-6 probes the engine DIRECTLY and structurally cannot see
#: the client path; it was green throughout the period those three features were
#: dead on arrival for cloud boxes.
#: HONEST LIMIT on that evidence, as reported: their run's exit code was
#: swallowed by a shell redirect, so the PASS is the script's literal sentinel
#: line rather than a captured 0. The script defines them as equivalent; the
#: numeric code was not verified and is not being claimed.
#:
#: ── v0.1.60 (2026-08-01) ──────────────────────────────────────────────────
#: THAT HONEST LIMIT IS NOW RESOLVED: conexus reports the client-path gate at
#: TRUE EXIT 0, all four legs, on this deploy. The caveat above stands as the
#: record of what v0.1.59's evidence actually was; it no longer applies.
#: CONTENT — ten service/ commits, and TWO of them were P0s found by REVIEW,
#: not by CI, both in the same file within one week:
#:   nexus-v6za0  rename onto a POPULATED supersede tombstone silently merged
#:                two collections across two vector spaces. Third attempt at
#:                one guard; liveness and identity were both proxies for the
#:                property that mattered, and both shipped or nearly shipped
#:                behind green suites. It now MEASURES emptiness.
#:   nexus-upg3s  that fix's OWN regression — converging by identity blanked
#:                repo_root/repo_hash/description when the payload omitted
#:                them. repo_root anchors deriveSourceUri, so it re-tumbles
#:                already-registered files (the nexus-3e4s class). 24 green
#:                tests missed it: the fixture created its owner WITHOUT the
#:                fields the bug destroyed.
#:   nexus-kjjab  a rotated NX_SERVICE_TOKEN raised an unhandled 23505 on the
#:                AUTH BOOTSTRAP path and hard-aborted boot, HTTP never
#:                binding. Plus the nexus-0ehwe arbiter class (pbawi/jq53b/
#:                z3ssg) and the rename/supersede chain.
#: SCHEMA: boot Liquibase applied 5 changesets, ALL GRANT-CLASS — no table
#: rewritten, so the RDR-180 stale-planner-stats trap did not apply.
#: Gates: native build trip-wire GREEN pre-tag (this set adds four new classes,
#: and GraalVM reachability fails only in the release build, i.e. AFTER the
#: tag); local full mvn suite on a tree verified BYTE-IDENTICAL to the tag
#: target; STEP-6 exit 0 with parity 104/113 IDENTICAL to the v0.1.59 baseline
#: and ZERO per-query jaccard movement; recall 12/12 local == cloud, no vacuous
#: legs; hybrid p95 1950.8ms against a 2376ms bound.
#: Deployed digest sha256:2b97cdd7...; live /version independently confirmed
#: UNAUTHENTICATED from this box (release_version 0.1.60, embedding_mode
#: voyage) — i.e. client-visibly, not merely engine-direct. Relay [21320],
#: reply [21321].
#: NOT EXERCISED: no token rotation was performed on this deploy, so kjjab's
#: ordering hazard was not tested in production. Rotation is safe from here
#: because the host now runs 0.1.60; it was NOT safe before it.
#:
#: -> (0,1,62) 2026-08-02 (nexus-koms3): engine-service-v0.1.62 deployed +
#: cloud-gated (STEP-6 full green, nexus-bwulw client-path gate TRUE EXIT 0
#: both sides, sweep clean checked=65 healed=0 — nexus-0ehwe closing
#: evidence, T2 [21359]). Deploy-before-floor-bump satisfied;
#: check_engine_release_floor exits clean, no paired-mode ack needed this
#: once. This bump also flips the nexus-308ph smoke-leg discriminator test
#: (tests/test_engine_version.py TestSmokeLegDiscriminatorDoesNotOutliveIts
#: Power) from its pre-fence green to requiring build_ref in
#: tests/e2e/local-service-gate.sh — landed in the same change as this bump.
#:
#: -> (0,1,65) 2026-08-04 (7.2.0 paired release): engine-service-v0.1.63
#: (tagged @7e3e8f55; carries dcf77fb7 tombstone filter + purge-trash route,
#: 897d5c29 store-get engine half, c24eac96 register created-vs-matched),
#: v0.1.64 (@9b3c89ad, purge-trash interval-parity fix —
#: production re-execute purged 61/61, census 0), and v0.1.65 (@17721b49,
#: dead-set liveness predicates — live EXPLAIN on the real tenant: 153ms ->
#: 0.7ms per hybrid query, ~200x; get/list 27ms fixed tax removed) were each
#: published + acquire-gated + DEPLOYED + edge-gated with record-deploy
#: written during the 2026-08-03/04 session (session-4 handoff). Cloud is
#: serving 0.1.65, so this bump is deploy-first (strictly safer than
#: deploy-at-tag-push): check_engine_release_floor needed no --paired-deploy
#: flag, the pin-currency red was the only gate failure. The client halves
#: (purge-trash + store-get verbs) ship in 7.2.0 alongside this pin.
#: -> (0,1,67) 2026-08-07 (7.3.0 release): engine-service-v0.1.66
#: (tagged @1afef1dc; carries f55435eb begin-many index-run fence route
#: [vw594 F1], dbd2cb46 chash-conformance report route [du2dw],
#: 355372e8 owner deactivate/reactivate [cw262], a155de19 nx_answer_runs
#: query [eho3u]) and v0.1.67 (@f96f75f0; 12-way parallel CCE embed
#: fan-out with bit-identical vectors + Equal-Jitter retries [9okyk],
#: EmbedderRouter/Main shutdown-lifecycle fixes) were each published +
#: acquire-gated + DEPLOYED + edge-gated with record-deploy written
#: (conexus relays T2 [21504]/[21576]: step-6 exit 0, client-path 4/4
#: legs). Cloud is serving 0.1.67, so this bump is deploy-first; the
#: client halves (owners --census/--execute, answer-runs, manifest-verify
#: --list, chash-conformance doctor check, CLI store-path fencing) ship
#: in 7.3.0 alongside this pin. This bump also retires the nexus-8hpad
#: fresh-install-mvv ALLOWLIST_REGEX entries (both engine halves now live
#: at the pinned floor) — enforced by
#: Test8hpadAllowlistDoesNotOutliveItsTrigger.
#: -> (0,1,68) 2026-08-07 (7.4.0 paired release): engine-service-v0.1.68
#: (nexus-lns3o engine half, dc87dd3c: POST
#: /v1/taxonomy/assignments/assign_from_chashes — server-side
#: compute-and-persist topic assignment from just-upserted chunk chashes,
#: eliminating the client's per-flush ~3MB embedding re-download). Published
#: + acquire-gated + deployed + edge-gated ahead of this bump (paired-release
#: choreography: engine tag cut first, client floor bump + route switch ride
#: the SAME client release, deploy fires at client-tag push). The client
#: half (nexus-yu9w5) switches taxonomy_assign_batch_hook to the route and
#: deletes the client-side compute_assignments/persist_assignments dance the
#: route replaces for THIS call site — no fallback, an engine below this
#: floor 404s and the hook fails loud via the RDR-172 tripwire.
#: BUMPED TO (0, 1, 71) — v0.1.70 is a SKIPPED version. It was cut,
#: published and fully gated, but a defect found after the cut
#: (nexus-syfes: gc_quarantine_orphans registered the quarantine sibling
#: even on a zero-orphan pass, leaving a permanently-orphaned
#: catalog_collections projection row that failed the release shakedown's
#: collections-drift gate) meant no release ever pinned it. catalog-024
#: fixes it via CREATE OR REPLACE over the untouched catalog-023.
#: v0.1.71 gates on this tree: engine suite 1974/0/0 (1 skipped),
#: run.sh --shakeout CANDIDATE SHAKEOUT PASSED, and
#: run.sh --acquire ACQUIRE GATE PASSED against the published bytes.
#: BUMPED TO (0, 1, 83) — conexus 7.12.0, paired release (2026-08-19). The
#: engine half of RDR-195 (nexus-kmtlp: token-aware Voyage sub-batch
#: planner, typed TOO_MANY_TOKENS_IN_BATCH -> adaptive halving, 422 with
#: structured detail instead of a bare 500, per-sub-request instrumentation)
#: plus grants-nexus-diag.xml. Gates on the tagged tree 119f6441b: engine
#: suite 2151/0/0, run.sh --shakeout CANDIDATE SHAKEOUT PASSED, run.sh
#: --candidate-migration REHEARSAL PASSED (floor v0.1.82 populated store,
#: delta=1, invariants exact), published-client-write-gate PASSED (7.11.0
#: client), RDR-195 MVV PASSED (laravel/framework, 0 TOO_MANY_TOKENS, 0 429).
#: The client half (byte-aware upsert paging, 422 detail surfaced) rides THIS
#: release; an engine below this floor answers the oversize case with a
#: bare 500, which is the defect this release fixes.
#: BUMPED TO (0, 1, 84) — conexus 7.13.0, paired release (2026-08-20). The
#: engine half of RDR-194 (nexus-tk070: the full FK-census arc, 11
#: changesets — taxonomy-014 tenant-scoped FK repoints + topics UNIQUE,
#: migration-002 DROP of dead migration_jobs, memory-003/plans-003
#: ttl -> ttl_days + CHECK + boundary-400, telemetry-006 frecency
#: 0=permanent retired, fk-005 deliberately-loose-edge comments). Gates on
#: the tagged tree d385a373c: engine suite 2193/0/0 (tree-identity with the
#: mvn-green p6b commit), --shakeout CANDIDATE SHAKEOUT PASSED,
#: --candidate-migration PASSED (floor v0.1.83 populated store, delta=11,
#: invariants EXACT), published-client-write-gate PASSED (7.12.0 client),
#: post-publish --acquire PASSED. DEPLOYED and cloud-gated 2026-08-20
#: BEFORE this release cut (STEP-6 PASS first-run zero advisories; every
#: migration NOTICE exact-matched the cc5/cc6 measured populations;
#: cloud-count-3 deploy-window verify ZERO). The client halves (memory_put
#: ttl contract, frecency $gt:0 predicate + write-path rejection) ride THIS
#: release per the wire-contract ledger entries f2d979113/6a7ff9915; an
#: engine below this floor lacks the ttl_days schema those clients bind.
#: Schema note: v0.1.84's renames are forward-only — a v0.1.83 image cannot
#: write against the walked schema.
#: ->(0,1,91) 2026-08-30 for 7.24.0 (nexus-zu9ln, paired): hygiene-001-not-null
#: (13 changesets: identity columns NOT NULL, orphan aspect rows and dead plans
#: deleted at the walk, plan_ttl_sweep) + nexus-4tosp stall events. NOT
#: additive: the engine 400s a blank doc_id at aspects upsert/enqueue, 409s a
#: NULL plan verb, and the aspects census route is gone; this client raises
#: before the wire and no longer has the census verb. v0.1.90 was burned on a
#: stale native-smoke.sh probe and ships to nobody. PITR fork-walk CLEAN,
#: every NOTICE count matched; --acquire PASSED on the published bytes.
#: ->(0,1,92) 2026-08-31 for 7.25.0 (nexus-ogccs, paired, all-[additive]):
#: OnnxModelPaths resolves the ONNX model root NX_ONNX_MODEL_DIR -> HOME ->
#: passwd user.home and the supervisor pins the root in the spawn env, so a
#: HOME-override box no longer green-inits then crashes the engine; plus
#: truthful schema_migration_complete counts (nexus-x0s52:
#: new_changesets/reexecuted_changesets/pending_at_start; applied_changesets
#: deliberately gone). Zero Liquibase changesets in the delta — no fork walk;
#: the [additive] ledger entry authorizes deploying BEFORE the client tag.
#: ->(0,1,94) 2026-09-02 for 7.27.0 (paired, all-[additive]); succeeds the
#: (0,1,93) pin that shipped with 7.26.0. Payload: VoyageRetryLoop
#: consolidation carrying the nexus-4ktfm ef_search floor and nexus-99r7y
#: request-scoped 429 budget semantics (nexus-1vpal), telemetry `since`
#: strict parsing — a malformed filter now 400s instead of degrading to a
#: silent `since now()` empty set (nexus-spbay), and the engine half of the
#: opt-in any-lexeme plan-search fallback (nexus-vi8fp). Zero Liquibase
#: changesets in the delta, so no PITR fork walk; both wire-ledger entries
#: lead with [additive], so the engine was tagged, deployed and cloud-gated
#: GREEN before this client tag (nexus-1emxn choreography (a)) — no refusal
#: window can open.
REQUIRED_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 94)

#: nexus-5uoxu: the first engine version whose telemetry trim honors the
#: ``dry_run`` field (the 3-arg ``trimSearchTelemetry`` overload, re-landed
#: with the revert-of-cba38ea41). Below this, an engine silently DROPS the
#: unknown field and the "preview" DELETES — so the client refuses
#: ``--trim-telemetry --dry-run`` against any serving engine older than
#: this, independent of REQUIRED_ENGINE_VERSION (which tracks the release
#: pairing, not this feature's floor — and at re-land time REQUIRED was
#: (0,1,80), the exact engine that deletes on preview, so it CANNOT serve
#: as this floor; substantive-critic 2026-08-19 prescription checked and
#: declined on that ground). Set to the next tag after v0.1.80, the newest
#: tag cut WITHOUT the overload; engine-service-v0.1.81 is cut from the
#: SAME tree this constant lands in (same session, Sam-authorized), so the
#: value is a real tag, not a standing guess — any later tag is >= it.
TRIM_DRY_RUN_MIN_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 81)


def parse_engine_version(raw: str | None) -> tuple[int, int, int] | None:
    """Parse ``X.Y.Z`` (optional leading ``v``/``V``) to a tuple, else ``None``.

    Fail-closed by construction: a blank, ``SNAPSHOT``/``dev``-qualified, or
    otherwise unparseable value returns ``None`` so the caller refuses. Trailing
    pre-release/build qualifiers (``-rc1``, ``+meta``) and a non-3-segment
    version (``0.1``, ``1.2.3.4``) are rejected rather than silently accepted
    — a dev/malformed identity is by definition not a comparable release.

    This is the union of the two previously-duplicated parsers
    (``guided_upgrade._parse_semver`` and ``managed_endpoint.
    _parse_release_version``) — read side by side, their bodies were
    byte-for-byte identical, so no behavior merge was needed beyond picking
    one canonical home.
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    if s[:1] in ("v", "V"):
        s = s[1:]
    lower = s.lower()
    if "snapshot" in lower or "dev" in lower:
        return None
    parts = s.split(".")
    if len(parts) != 3:
        return None
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError:
        return None
    if major < 0 or minor < 0 or patch < 0:
        return None
    return (major, minor, patch)
