# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single-source-of-truth engine-version floor (nexus-9qq85, nexus-b6qlf).

Replaces the two independently-drifting pinned constants
``guided_upgrade.REQUIRED_RELEASE_VERSION`` and
``managed_endpoint.MIN_MANAGED_RELEASE_VERSION`` — both modules now import
:data:`nexus.engine_version.REQUIRED_ENGINE_VERSION` and
:func:`nexus.engine_version.parse_engine_version`. This is the ONLY place the
pinned-floor value is asserted; ``test_guided_upgrade_version_pin.py`` and
``test_managed_endpoint.py`` exercise behavior, not the pin itself.
"""

from __future__ import annotations

from nexus.engine_version import REQUIRED_ENGINE_VERSION, parse_engine_version


class TestRequiredEngineVersion:
    def test_pinned_floor_is_current(self) -> None:
        # (0,1,5)->(0,1,8) for nexus-x2g1z; ->(0,1,34) for 6.5.0: the client
        # hard-requires catalog-012 (graph-hop `where` — pre-012 engines
        # silently ignore the key, the H2 version-skew failure class) and
        # catalog-013-1b (pre-1b engines fail boot VALIDATE on tenants with
        # legacy 64-char chash rows — the nexus-1wjmq incident). ->(0,1,39)
        # for nexus-rn3wo.1: T1 scratch now defaults to the PG-backed service
        # with no Chroma fallback, and every engine before v0.1.38 has a
        # native-image reflection gap that 500s on every T1 get/search/list
        # (nexus-opr9m). ->(0,1,41) for the 2026-07-13 release-gate arc:
        # service-mode remediation consent audit needs telemetry-002-consents
        # (v0.1.40+), retention markers + range where-operators need v0.1.41,
        # and tags <=0.1.40 are invalid rollback targets post-A6.
        # ->(0,1,42) 2026-07-14: fix-delivery rule (per Hal) — the engine
        # carries the catalog-015 FTS filename-token fix (nexus-8gue1) and
        # indexed_at repair provenance (nexus-p5qk8); local installs receive
        # engine fixes ONLY via this floor/pin, so an advertised engine fix
        # moves the floor even with zero client-side hard dependency.
        # ->(0,1,43) 2026-07-15: fix-delivery rule again — GH #1402
        # (nexus-0gis0): grants-nexus-svc-1's bulk GRANT crash-looped boot on
        # any install whose schema carries the superuser-owned diag view;
        # v0.1.42 and earlier are broken upgrade targets for that class.
        # ->(0,1,44) 2026-07-16: hard dependency — the 6.11.0 tier-writes
        # read-parity surfaces (nx tier-status, doctor tier-discipline, the
        # SessionEnd summary; nexus-59wjj/ov13k) call the new
        # GET /v1/telemetry/tier_writes/query route. Pre-44 engines 404 it
        # and every surface degrades to the honest fallback forever.
        # Deployed + cloud-gated 2026-07-16 (recall 12/12, hybrid p95
        # 1920ms < 2376 bound).
        # ->(0,1,47) 2026-07-18: hard dependency — RDR-186 retired every
        # client-local substrate behind the /v1 trio: /v1/remap (v0.1.45),
        # /v1/ladder (v0.1.46), /v1/pipeline (v0.1.47). pipeline.db and
        # ladder.db are DELETED; pre-47 engines 404 the routes and PDF
        # indexing / ladder completion recording have no fallback. Cut +
        # published + cold-validated 2026-07-18; cloud deploy = Hal's relay
        # (release Step 0's floor gate blocks the cut until confirmed).
        # ->(0,1,49) 2026-07-19: hard dependency — the RDR-180 cohort. The
        # client's chash cutover (64-hex producers, guided land-then-
        # transform, chash-rekey rung) requires the bytea schema (rdr180
        # changesets) + /v1/remap/rekey + staging endpoints; and v0.1.49
        # carries rdr180-16-analyze-rewritten-tables, the BUG-0148
        # stale-planner-stats fix — an un-ANALYZEd ALTER TYPE rewrite
        # degrades sparse-gate hybrid search to ZERO rows with all health
        # probes green (conexus-xpg7). Fix-delivery rule applied: the floor
        # is the only vehicle for local installs. Deployed + per-query-diff
        # cloud-gated 2026-07-19 (parity 104/113 == baseline, recall AC3
        # 12/12 real values, xpg7 probes 3/3 J=1.0).
        # ->(0,1,51) 2026-07-21: the RDR-187 conformance tail — v0.1.50
        # carries the chash_index DROP prerequisites; v0.1.51 delivers the
        # 339xv/kmd5b/b878d fixes (planner blind-spot on just-written rows)
        # with every octet CHECK validated at arc close. One-engine-per-
        # release: v0.1.51 is the exact engine this release was tested
        # with. Deployed + cloud-gated GREEN 2026-07-21.
        # ->(0,1,52) 2026-07-22: hard dependency — RDR-188 server-side
        # rerank. The client search path repointed to the engine's fused
        # rerank envelope and DELETED its own Voyage reranker; pre-52
        # engines cannot serve the rerank contract. Also carries
        # catalog-016 (TOCTOU dupe backstop; tombstoned the live 201-dupe
        # class at deploy, 403 rows/276 uris, unique index proves no
        # survivor). Deployed + per-query-diff cloud-gated GREEN
        # 2026-07-23Z: parity 106/113 == baseline, zero per-query
        # regressions, reranker=rerank-2.5 active, zero degrades (T2
        # [21062]).
        # ->(0,1,56) 2026-07-25: ONE-ENGINE-IDENTITY bump, NOT a feature
        # dependency — and that distinction is the point. v0.1.53-.56 were
        # cut, gated and deployed while this sat at (0,1,52), delivering
        # four tags of engine work to zero local-mode installs. The old
        # "bump only if the release hard-requires the features" carve-out
        # is what allowed that; it is retired (Hal directive 2026-07-15,
        # swept from the checklists 2026-07-25) and
        # scripts/check_engine_release_floor.py now FAILS the release when
        # a published tag is ahead of this constant. Deployed + cloud-gated
        # GREEN 2026-07-25, deploy verified independently before the bump.
        # ->(0,1,57) 2026-07-27: the T2/catalog read-correctness cohort —
        # four SILENT-WRONG-ANSWER defects, none of which raised: memory
        # FTS could not find a word inside a dotted title, so every
        # .md-suffixed T2 note was unfindable by title fragment
        # (nexus-22r1f); memory and catalog search did not fold Latin-1
        # diacritics, so 'resume' missed 'résumé' where the SQLite baseline
        # matched; tombstoned documents stayed visible to 21 catalog LIST
        # reads (nexus-23wlw); graph traversal ignored include_heuristic
        # (nexus-ybj1b). Fix-delivery rule applied: cloud users already
        # have these, local installs get ONLY what this constant names.
        # STEP-6 structurally cannot see any of it (its legs read
        # chunks_{384,768,1024}; these touch nexus.memory and
        # nexus.catalog_documents), so the evidence is conexus's separate
        # read-only probe: 11/11 live, Cyrillic UNCHANGED at the Latin-1
        # boundary. Deployed + cloud-gated GREEN 2026-07-27 (parity 105/113
        # byte-identical to the v0.1.56 baseline, recall AC-3 12/12),
        # record-deploy verified against the live /version before this bump
        # (T2 [21164]).
        # ->(0,1,58) 2026-07-28: the nexus-onjvy write-only-surface cohort.
        # Three READ routes for data the engine already wrote and no route
        # returned: /v1/telemetry/hook_failures/list (the log that surfaces
        # SILENT hook failures could be written and never inspected),
        # /v1/taxonomy/assignments/details (similarity / assigned_at /
        # source_collection written by assign, projected by nothing), and
        # /hubs' staleness aggregates so detect_hubs(warn_stale=) stops
        # being accepted-and-dropped. No DDL — the columns all existed.
        # Also carries analyze-002, which conexus confirmed working on
        # deploy (both rewritten tables FRESH with no hand-run ANALYZE,
        # where v0.1.57 needed one as nexus_admin).
        # Gates: engine suite 1475/0/0 twice; CANDIDATE SHAKEOUT PASSED
        # pre-tag; ACQUIRE GATE 12/12 on the PUBLISHED cosign-verified
        # bytes; deployed to api.conexus-nexus.com and record-deploy
        # verified against the live /version before this bump.
        # NOT established, so do not infer it: v0.1.58's STEP-6 parity
        # moved 105 -> 104 and that is CORPUS DRIFT (+1,514 chunks / +34
        # docs between captures), not this release. An ekn9n topic-boost
        # attribution was floated across the bus and WITHDRAWN by both
        # sides — apply_topic_boost is Python-client-only and absent from
        # the engine, so it cannot run on the /v1/vectors/* path STEP-6
        # measures (nexus-j46lz).
        # ->(0,1,59) for the 7.0.0 catalog defect set: mqd6t's tombstone
        # filters across every manifest-rooted read (chashesForCollection,
        # docsForChashes, getManifest, getManifestMany, resolveChash's doc_id
        # attribution, two PgVectorRepository siblings) + the non-resurrection
        # rule; e4gel's chunk_count re-derivation, GUARDED so an incidental
        # update cannot zero a positive count against an EMPTY manifest (that
        # disagreement is the GH #1371 damage signature reconcile classifies
        # on); s4e1n's co_discovered_by link-merge fold; tz1cx's real
        # metadata.doc_id route; ekaxn's alias hop; jqvzk's gc_audit surface.
        # 9ssih was deliberately HELD OUT — its 400 would have broken every
        # already-installed client, nothing client-side caught it, and its
        # client half had to ship first.
        # SCHEMA: two new changesets, catalog-018-1/-2 (207 -> 209), creating
        # nexus.gc_audit. A NEW table: no existing table rewritten, no executed
        # changeset mutated — conexus called it the safest deploy shape they
        # have taken; rollback is a plain image revert.
        # Gates: engine suite 1517/0; deployed (digest sha256:ba7a0a18...,
        # cosign KMS + spdxjson verified, on-host verify PASS at redeploy);
        # STEP-6 parity BYTE-IDENTICAL to the v0.1.58 baseline (113/104,
        # p50_jaccard 1.0, zero per-query regressions); recall 12/12 local ==
        # cloud, zero vacuous legs; corpus drift NONE (the check built after
        # the v0.1.58 mis-attribution, live for the first time). Relay [21293].
        # AND: nexus-bwulw's CLOUD CLIENT-PATH GATE, written as EXPECTED RED,
        # now PASSES all four legs from a cloud-mode box against the live edge.
        # conexus's STEP-6 probes the engine DIRECTLY and structurally cannot
        # see the client path — it was green throughout the period three client
        # features were dead on arrival for cloud boxes.
        # HONEST LIMIT, as reported: their run's exit code was swallowed by a
        # shell redirect, so the PASS is the script's sentinel line, not a
        # captured 0. Equivalent by the script's own definition; not claimed as
        # a verified numeric code.
        # ->(0,1,60) 2026-08-01: FIX-DELIVERY RULE. No hard client dependency —
        # every route this release needs already existed — but local installs
        # receive engine fixes ONLY through this floor/pin, so a cut + deployed
        # + gated tag moves it unconditionally (Hal directive 2026-07-15, after
        # the 14h GH #1402 incident; the carve-out version of this rule is what
        # caused the identical 2026-07-14 v0.1.42 episode).
        # What the fixes ARE matters here: TWO P0s, both found by REVIEW rather
        # than by CI, both in CatalogRepository within one week.
        #   nexus-v6za0  rename onto a POPULATED supersede tombstone silently
        #                merged two collections across two vector spaces —
        #                silent BY CONSTRUCTION, since cross-model means the
        #                rows land in different chunks_<dim> tables so nothing
        #                collides and nothing aborts. Third attempt at one
        #                guard: liveness and identity were both proxies, and
        #                both shipped or nearly shipped behind green suites.
        #   nexus-upg3s  that fix's own regression — converging by identity
        #                blanked repo_root/repo_hash/description when the
        #                payload omitted them, and repo_root anchors
        #                deriveSourceUri, so already-registered files re-tumble
        #                (the nexus-3e4s class, itself a ~6,500-row event).
        #   nexus-kjjab  a rotated NX_SERVICE_TOKEN 23505'd on the AUTH
        #                BOOTSTRAP path and hard-aborted boot, HTTP never
        #                binding, recurring on every restart.
        # Pre-60 engines carry all three. An install left below this floor is
        # exposed to two silent data-corruption paths, which is the strongest
        # fix-delivery case the rule has had.
        # v0.1.61 (2026-08-02, relay [21347]): the 7.0.0-wave residue tag —
        # count/cap guards on the manifest reverse-lookup chain (ocf52/
        # uu4b9/b9puj: the union guard for a sweep that DELETES since
        # 43b7932d), NUL sanitization + typed 422s (yvzhz/dmrkm), the 9ssih
        # dangling-link validation (client half released in 7.0.0 — the
        # check_client_release_precondition gate enforced the ordering),
        # tombstone semantics end-to-end (graphBFS relays, four views via
        # catalog-019, seven write guards), 4j80w link timestamps, pzdol/
        # h77a2 lookup legs, supersede/rename guards, list pagination.
        # Deployed ~2026-08-02T00:15Z, cosign+SBOM verified on host;
        # Liquibase 209->216 all CREATE OR REPLACE + grants (no rewrite).
        # Gated: parity 103/112 p50_jaccard 1.0 ZERO per-query regressions,
        # recall@20 12/12 cloud==local, hybrid p95 in the 0.1.60 baseline
        # band, nexus-bwulw client-path gate TRUE EXIT 0 all four legs.
        # HONEST LIMIT: the /v1/vectors/search p95 tail leg was red in the
        # step-6 aggregate (1539-1922ms vs 1302 bound) — evidenced as a
        # pre-existing DB-side stochastic tail (p50 flat across versions,
        # tail already widening in 0.1.59/60), tracked as conexus-5moe;
        # Hal called the gate green 2026-08-02 on that evidence.
        # ->(0,1,62) 2026-08-02 (nexus-koms3): engine-service-v0.1.62 deployed
        # + cloud-gated (STEP-6 full green, nexus-bwulw client-path gate TRUE
        # EXIT 0 both sides, sweep clean checked=65 healed=0 — nexus-0ehwe
        # closing evidence, T2 [21359]). check_engine_release_floor exits
        # clean. This bump also flips TestSmokeLegDiscriminatorDoesNotOutlive
        # ItsPower and TestMvvAllowlistDoesNotOutliveItsTrigger below from
        # their pre-fence green state to requiring the nexus-308ph build_ref
        # discriminator (local-service-gate.sh) and the removed
        # fresh-install-mvv.sh allowlist entry, respectively — both landed in
        # the same change as this bump.
        # ->(0,1,65) 2026-08-04 (7.2.0 paired release): v0.1.63 (tombstone
        # filter + purge-trash route + store-get/register engine halves),
        # v0.1.64 (purge-trash interval-parity — production purged 61/61,
        # census 0), v0.1.65 (dead-set liveness predicates — real-tenant
        # EXPLAIN 153ms -> 0.7ms per hybrid query). Each published +
        # acquire-gated + deployed + edge-gated 2026-08-03/04. The 7.2.0
        # client verbs (purge-trash, store-get) hard-require the v0.1.63+
        # routes; fix-delivery rule covers the rest. Deploy-first: cloud was
        # serving 0.1.65 before this bump landed.
        # ->(0,1,67) 2026-08-07 (7.3.0 release): v0.1.66 (begin-many fence
        # route [vw594 F1], chash-conformance report [du2dw], owner
        # deactivate/reactivate [cw262], nx_answer_runs query [eho3u]) and
        # v0.1.67 (12-way parallel CCE fan-out, bit-identical vectors +
        # jittered retries [9okyk], shutdown-lifecycle fixes). Both
        # published + acquire-gated + deployed + edge-gated (conexus
        # [21504]/[21576]: step-6 exit 0, client-path 4/4). Deploy-first:
        # cloud was serving 0.1.67 before this bump landed. The 7.3.0
        # client halves (owners --census/--execute, answer-runs,
        # manifest-verify --list, chash-conformance doctor check, CLI
        # store fencing) hard-require the v0.1.66+ routes. This bump also
        # trips Test8hpadAllowlistDoesNotOutliveItsTrigger: the
        # fresh-install-mvv ALLOWLIST_REGEX entries retire in the SAME
        # change (nexus-8hpad).
        # ->(0,1,68) 2026-08-07 (7.4.0 paired release): engine-service-v0.1.68
        # (nexus-lns3o engine half, dc87dd3c) — POST
        # /v1/taxonomy/assignments/assign_from_chashes, server-side
        # compute-and-persist topic assignment from just-upserted chunk
        # chashes. Published + acquire-gated + deployed + edge-gated ahead of
        # this bump (paired-release choreography). Client half nexus-yu9w5
        # switches taxonomy_assign_batch_hook to the route with no fallback.
        # ->(0,1,69) 2026-08-09 (7.5.0 paired release): engine-service-v0.1.69
        # — kl2z6 combined write (chunks+manifest atomic in write_many's
        # per-doc transaction) + the vc6dh-corrected staging guard and its
        # Liquibase index + the sweep_detail.reason vocabulary, plus the
        # 11gh6/3wtku manifest-insert-vs-sweep write-skew closure and the
        # 4okz4 chash/staging jOOQ conversion. Cut, shakeout-gated,
        # acquire-gated on the published bytes, DEPLOYED and cloud-gated
        # green (STEP-6 PASS, 0 failures/0 advisories, no parity
        # regressions) BEFORE this bump. Client halves ship here: nexus-wxjr6
        # (flush path sends the combined call; ack-echo RAISES against an
        # engine below this identity, so the floor bump is mandatory, not
        # optional).
        # -> (0,1,70) 2026-08-10, conexus 7.6.0 (PAIRED release). Carries the
        # RDR-191 Phase 1 engine half — catalog-023's three
        # `POST /v1/vectors/gc/*` anti-join routes (gc_quarantine_orphans /
        # gc_restore_rereferenced / gc_expire_quarantine) — plus
        # `GET /v1/catalog/descendants`, whose absence made every `subtree`
        # query silently return at most one 500-row page. BOTH client halves
        # ship in this same release, so the bump is mandatory, not optional:
        # floor-lag would ship a client whose pinned engine lacks the engine
        # halves of its own features (the 7.1.0/v0.1.62 inversion).
        # Cut on e1cb78a1, `service/` tree identical to green-service-ci
        # c84480ec. Gated: --shakeout PASSED pre-tag (it caught a Phase D
        # census that had never run in-container, and then caught a wrong
        # first fix for it), --acquire PASSED post-publish against the
        # published bytes (/version release_version=0.1.70). Cloud deploy
        # fires at client-tag push under the paired-release choreography;
        # the floor gate ran in --paired-deploy mode and its POST-TAG VERIFY
        # (re-run WITHOUT the flag) is owed once the deploy lands.
        # -> (0,1,71) 2026-08-11, conexus 7.6.0 (PAIRED release).
        # v0.1.70 IS A SKIPPED VERSION: cut, published and fully gated, but
        # never pinned by any release. This release's own sandbox shakedown
        # caught a defect in it at step 11/11 — gc_quarantine_orphans and
        # gc_restore_rereferenced registered their destination collection in
        # catalog_collections UNCONDITIONALLY and BEFORE the anti-join ran,
        # so a pass that moved zero rows left an unreferenced projection row
        # behind permanently (nx catalog doctor --collections-drift reports
        # it as projection_not_in_t3). Bead nexus-syfes. Fixed by catalog-024
        # via CREATE OR REPLACE over an untouched catalog-023 (never edit a
        # shipped changelog in place), cut as v0.1.71. Everything the
        # (0,1,70) note above describes is CARRIED FORWARD by v0.1.71 — it is
        # a superset, not a replacement, so both client halves named there
        # still ship against this floor.
        # Cut on 8c2e9fa6. Gated: engine suite 1974/0/0 (1 skipped),
        # --shakeout CANDIDATE SHAKEOUT PASSED pre-tag, --acquire ACQUIRE
        # GATE PASSED post-publish against the published bytes
        # (/version release_version=0.1.71). DEPLOYED + cloud-gate GREEN
        # 2026-08-11, a single hop 0.1.69 -> 0.1.71 since v0.1.70 was never
        # deployed either (STEP-6 PASS, 0 failures, 0 advisories, no parity
        # regressions). POST-DEPLOY VERIFY DONE, not owed:
        # check_engine_release_floor.py WITHOUT --paired-deploy exits 0,
        # cloud release_version=0.1.71 == floor.
        #
        # BUMPED to (0,1,73) 2026-08-12: v0.1.72 was cut+deployed same day
        # (cloud went 0.1.71 -> 0.1.72) but the RDR-191 manifest bundle
        # (caller-supplied collection, catalog-025 NOT NULL, commit
        # 498c9295) landed AFTER its cut, so v0.1.73 was cut from
        # bd716286a to carry it — v0.1.72 is now a floor-skipped version
        # for LOCAL installs (deployed to cloud, never pinned). Gated:
        # full engine suite BUILD SUCCESS on bd716286a, --shakeout
        # CANDIDATE SHAKEOUT PASSED pre-tag on the same tip. DEPLOY of
        # 0.1.73 is the pending relay at this bump; until it lands the
        # managed handshake legitimately reads cloud 0.1.72 < floor —
        # check_engine_release_floor.py documents that state, and the
        # --acquire post-publish gate covers the published bytes.
        #
        # BUMPED to (0,1,74) 2026-08-12, same day: v0.1.74 cut from
        # d784d8c6e carries the RDR-191 wave-2 delete-path family
        # (catalog-026 purge-trash grace-window chunk sweep, catalog-027
        # quarantine manifest guard, the tombstone-aware delete anti-join
        # + explicit reap retraction, and the manifest-read wire field
        # kzso5 — the client half of which is version-tolerant and only
        # fully activates against this engine). v0.1.73 was deployed and
        # recorded the same day, so this is a one-step floor move, not a
        # skip. Gated: full engine suite 2010/0/0 on d784d8c6e,
        # --shakeout CANDIDATE SHAKEOUT PASSED pre-tag on the same tip;
        # deploy relay is the pending step at this bump.
        #
        # BUMPED to (0,1,75) 2026-08-13: the RDR-191 Phase 4 GATE-4
        # co-release (nexus-o8dil.21) — v0.1.75 cut from 40ef3696e (the
        # repoint-batch merge) carries the unify DDL (vectors-004 /
        # taxonomy-007 / vectors-005) AND the rung retarget (.15), the
        # pairing F14a forbids splitting. Fully gated BEFORE this bump:
        # full engine suite 2075/0/0 on the tagged tree, --shakeout
        # CANDIDATE SHAKEOUT PASSED, post-publish --acquire PASSED
        # (263 migrations, 0 failed), DEPLOYED and cloud-gated 2026-08-14
        # (conexus [22485]: STEP-6 green, client-path gate green, row
        # invariant exact). This floor bump rides conexus 7.8.0 per the
        # paired-release choreography — engine-service-v0.1.79 (published,
        # acquire-gated, TWICE rehearsed against a forked production cluster:
        # walk CLEAN 58s, diag surface verified restored as nexus_diag; the
        # v0.1.77/v0.1.78 tags were burned/superseded en route — 77 by a
        # stale native-smoke probe, 78 by the diag-grants strip, both fixed
        # and gated at 79). The same release ships the catalog-030 client
        # half (3b2901141, nexus-o8dil.33) per the wire-contract ledger.
        #
        # BUMPED to (0,1,80) 2026-08-18: the RDR-156 P5 co-release
        # (conexus 7.10.0, paired choreography) — v0.1.80 cut from
        # 25de2c688 carries vectors-007 (nexus.hybrid_search_384/768/1024,
        # additive-only SQL functions; no client caller yet, no wire
        # coupling). Fully gated BEFORE this bump: full engine suite green
        # on the byte-identical service/ tree, --shakeout CANDIDATE
        # SHAKEOUT PASSED, --candidate-migration PASSED (delta=1,
        # invariants EXACT), published-client write gate PASSED (7.9.0 x
        # candidate), post-publish --acquire PASSED. Deploy relay arms at
        # the v7.10.0 client-tag push.
        #
        # BUMPED to (0,1,82) 2026-08-19: conexus 7.11.0. v0.1.82 cut from
        # 433246839 carries nexus-0uuit (topics.doc_count deadlock: the
        # taxonomy-013 ordered single-row FOR UPDATE loop plus a
        # DeadlockRetry belt on assignFromChashes, closing the live cloud
        # 500s from the 2026-08-15 incident) and nexus-sybbh (catalog-033
        # gc_audit producers on every reap/purge path, in the same
        # transaction as their deletes). Fully gated BEFORE this bump: full
        # engine suite 2148/0/0 twice on the tagged tree, --shakeout
        # CANDIDATE SHAKEOUT PASSED, --candidate-migration PASSED (delta=5,
        # row invariants EXACT over a populated v0.1.80 floor store),
        # published-client write gate PASSED (published 7.10.0 x candidate),
        # post-publish --acquire PASSED 12/12. DEPLOYED and cloud-gated
        # 2026-08-19 BEFORE this release (recall 12/12 exact, zero parity
        # regressions, gc_audit producers confirmed firing in production);
        # record-deploy written, cloud-client-path gate PASSED 3/3. So this
        # is NOT a paired bump: the engine was already live when 7.11.0 cut.
        assert REQUIRED_ENGINE_VERSION == (0, 1, 82)


class TestParseEngineVersion:
    def test_parses_plain_and_v_prefixed(self) -> None:
        assert parse_engine_version("0.1.5") == (0, 1, 5)
        assert parse_engine_version("v1.2.3") == (1, 2, 3)
        assert parse_engine_version("V1.2.3") == (1, 2, 3)

    def test_rejects_blank_none_and_whitespace(self) -> None:
        for bad in (None, "", "   "):
            assert parse_engine_version(bad) is None

    def test_rejects_dev_and_snapshot_qualifiers(self) -> None:
        for bad in ("1.0-SNAPSHOT", "0.1.6-dev", "0.1.9-SNAPSHOT", "0.1.8-dev"):
            assert parse_engine_version(bad) is None

    def test_rejects_malformed_segment_counts(self) -> None:
        for bad in ("0.1", "1.2.3.4", "x.y.z", "unknown"):
            assert parse_engine_version(bad) is None

    def test_rejects_trailing_qualifiers(self) -> None:
        for bad in ("0.1.8-rc1", "0.1.8+meta"):
            assert parse_engine_version(bad) is None

    def test_rejects_negative_components(self) -> None:
        assert parse_engine_version("-1.0.0") is None

    def test_rejects_non_string_types_via_caller_guard(self) -> None:
        # parse_engine_version itself only declares str | None; callers that
        # receive JSON-confused non-string values (bool/int/list/dict) must
        # coerce/guard before calling. Confirm the str-typed contract holds
        # for the values callers DO pass through.
        assert parse_engine_version("1.2.3") == (1, 2, 3)


class TestFloorComparison:
    def test_below_floor_compares_less(self) -> None:
        assert parse_engine_version("0.1.5") < REQUIRED_ENGINE_VERSION

    def test_at_floor_compares_equal(self) -> None:
        floor_str = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
        assert parse_engine_version(floor_str) == REQUIRED_ENGINE_VERSION

    def test_above_floor_compares_greater(self) -> None:
        assert parse_engine_version("0.2.0") > REQUIRED_ENGINE_VERSION


def test_module_is_stdlib_only_leaf() -> None:
    """engine_version.py must import cleanly with zero ``nexus.*`` deps — it is
    a leaf module both ``nexus.db`` and ``nexus.migration`` import from, so any
    ``nexus`` import here risks a circular-import class of bug."""
    import ast
    import pathlib

    import nexus.engine_version as mod

    src = pathlib.Path(mod.__file__).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "nexus":
            raise AssertionError(f"engine_version.py imports from nexus: {node.module}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "nexus":
                    raise AssertionError(f"engine_version.py imports nexus: {alias.name}")


class TestDownstreamConsumersTrackTheFloor:
    def test_cold_rehearsal_tag_is_at_least_the_floor(self) -> None:
        """The migration-rehearsal COLD_TAG default must satisfy the
        guided-upgrade version pin, or `run.sh --cold` fail-closes at the
        version gate before migrating anything (demonstrated live
        2026-07-12: the (0,1,39) floor bump left COLD_TAG at v0.1.37 and
        the cold MVV died on 'engine-service v0.1.37 < required v0.1.39').
        Same drift class as the CI stamp-step regex (fixed 975dcd9a) and
        the original two hand-typed pins nexus-b6qlf unified — every
        hand-written downstream consumer of the floor gets a tripwire."""
        import re
        from pathlib import Path

        run_sh = Path(__file__).parent.parent / (
            "tests/e2e/migration-rehearsal/run.sh"
        )
        m = re.search(
            r'COLD_TAG="\$\{NEXUS_SERVICE_TAG:-engine-service-v(\d+\.\d+\.\d+)\}"',
            run_sh.read_text(),
        )
        assert m, "COLD_TAG default not found/parseable in run.sh"
        assert parse_engine_version(m.group(1)) >= REQUIRED_ENGINE_VERSION, (
            f"COLD_TAG default v{m.group(1)} is below the "
            f"guided-upgrade floor {REQUIRED_ENGINE_VERSION} — bump it "
            "with the floor (AGENTS.md § Engine-service release)"
        )


class TestSmokeLegDiscriminatorDoesNotOutliveItsPower:
    """nexus-308ph tripwire (mechanizing the x81ks critique's Significant-1).

    The local-service gate's smoke leg currently discriminates WHICH artifact
    is serving (freshly-stamped dev jar vs the pinned release binary that
    nx init --service auto-starts, nexus-4e96a) only ACCIDENTALLY: the
    RUNFENCE index-run routes 404 on pre-fence releases and 200 on the fresh
    jar. The moment the pinned floor reaches an engine that SHIPS those
    routes (v0.1.62+), both artifacts answer identically and the smoke leg
    silently loses its discriminating power — the third
    assertion-passes-because-it-cannot-discriminate instance of 2026-08-02
    (tmsnz, 4e96a, this). nexus-308ph is the durable fix: a per-run build
    nonce stamped into release.properties and asserted by the smoke leg.

    This test rather than an eyeballed bead comment IS the revisit trigger
    (nexus-i5c2u: eyeball steps get skipped): it stays green while the floor
    is pre-fence, and goes RED at the floor bump unless the gate script
    carries the nonce assertion by then.
    """

    def test_floor_below_fence_or_nonce_discriminator_present(self) -> None:
        from pathlib import Path

        from nexus.engine_version import REQUIRED_ENGINE_VERSION

        if REQUIRED_ENGINE_VERSION < (0, 1, 62):
            return  # pre-fence floor: the 404-vs-200 discriminator still works

        gate = Path(__file__).resolve().parent / "e2e" / "local-service-gate.sh"
        text = gate.read_text(encoding="utf-8")
        assert "build_ref" in text, (
            f"REQUIRED_ENGINE_VERSION={REQUIRED_ENGINE_VERSION} ships the "
            "RUNFENCE routes, so the smoke leg's fence-round-trip no longer "
            "discriminates the serving artifact (pinned release vs stamped "
            "jar answer identically) — land nexus-308ph (per-run build_ref "
            "nonce in release.properties, asserted by the smoke leg) before "
            "bumping the floor past 0.1.61, or the gate reverts to the "
            "4e96a vacuity class with nothing to catch it."
        )


class TestMvvAllowlistDoesNotOutliveItsTrigger:
    """nexus-5xn3k.6 substantive-critic SIGNIFICANT (T2
    nexus/5xn3k6-critique-2026-08-02 [21355]).

    ``tests/e2e/fresh-install-mvv.sh``'s ``ALLOWLIST_REGEX`` for the
    "engine predates the index-run fence" doctor warning (a pre-fence
    engine 404s ``manifest_verify_all()`` on a virgin box, so
    ``_check_dangling_manifests`` renders a soft-warn instead of a clean
    pass) carries a prose "REMOVAL TRIGGER" comment — delete once
    ``REQUIRED_ENGINE_VERSION`` names a tag containing 3cf64d48 (v0.1.62 or
    later) — but nothing MECHANICALLY enforced it. When the floor crosses
    that line, the 404 branch simply stops firing: the allowlist regex
    then matches nothing, the MVV gate passes green whether or not the now
    -dead entry is removed, and the entry silently outlives its own
    trigger — the exact "stale interim exception nobody notices" shape
    nexus-ac4id itself was.

    Mirrors ``TestSmokeLegDiscriminatorDoesNotOutliveItsPower`` directly
    above (same tripwire idiom, different fence-gated artifact): THIS
    test, not an eyeballed bead comment, IS the revisit trigger
    (nexus-i5c2u: eyeball steps get skipped). It stays green while the
    floor predates the fence and goes RED the moment
    ``REQUIRED_ENGINE_VERSION`` crosses v0.1.62 unless the allowlist entry
    has already been deleted from ``fresh-install-mvv.sh`` in the SAME
    change that bumps the floor.

    GREP-LEVEL PARITY (comment both sides): the matching comment lived at
    ``tests/e2e/fresh-install-mvv.sh``'s ``ALLOWLIST_REGEX`` definition and
    at ``health.py::_check_dangling_manifests``'s ``status == 404`` branch.
    RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15: ``_check_dangling_manifests``
    itself is DELETED (the manifest-chunk FK makes the dangling state it
    detected unreachable) — this test's assertion (the allowlist entry stays
    absent) is now permanently true independent of that removal, since the
    entry was already gone at the v0.1.62 floor bump this class pins. Kept
    as a historical regression guard, not because the parity it names still
    has three live sides.
    """

    def test_floor_below_fence_or_allowlist_entry_removed(self) -> None:
        from pathlib import Path

        from nexus.engine_version import REQUIRED_ENGINE_VERSION

        if REQUIRED_ENGINE_VERSION < (0, 1, 62):
            return  # pre-fence floor: a virgin box still 404s manifest_verify_all()

        gate = Path(__file__).resolve().parent / "e2e" / "fresh-install-mvv.sh"
        text = gate.read_text(encoding="utf-8")
        assert "engine predates the index-run fence" not in text, (
            f"REQUIRED_ENGINE_VERSION={REQUIRED_ENGINE_VERSION} ships "
            "3cf64d48's manifest/verify routes, so a virgin box's doctor no "
            "longer takes the pre-fence 404 branch in "
            "_check_dangling_manifests — delete the ALLOWLIST_REGEX entry "
            "for 'engine predates the index-run fence' in "
            "tests/e2e/fresh-install-mvv.sh (nexus-5xn3k.6 "
            "code-review-expert CRITICAL / substantive-critic SIGNIFICANT, "
            "2026-08-02) in the SAME change that bumps the floor past "
            "v0.1.61, or the entry silently outlives its own removal "
            "trigger — the ac4id vacuity class recurring one arc later."
        )


class Test8hpadAllowlistDoesNotOutliveItsTrigger:
    """nexus-8hpad (filed while validating nexus-796zn, 2026-08-05).

    ``tests/e2e/fresh-install-mvv.sh``'s ``ALLOWLIST_REGEX`` carries two
    scoped entries — "chash.conformance" (bead nexus-du2dw, RDR-180) and
    the vw594-anchored "stale index-run fences: ... report index_state but
    it is NULL" (bead nexus-vw594 F1) — for two doctor warnings that fire
    unconditionally on the CURRENTLY-PINNED v0.1.65 engine floor because
    both features are client-shipped but ENGINE-HALF-INERT (both beads'
    own close notes say so). Mirrors ``TestMvvAllowlistDoesNotOutliveItsTrigger``
    directly above (same tripwire idiom), with one difference: unlike the
    3cf64d48/v0.1.62 case, the future engine tag that carries both routes
    does not exist yet, so there is no commit sha to key the trigger on.
    Keyed instead on ``REQUIRED_ENGINE_VERSION`` moving past the KNOWN-inert
    v0.1.65 floor — the moment the floor bumps at all, both routes are
    presumed carried (per the paired-release choreography: a floor bump
    always rides the engine tag that motivated it) and the entries must be
    deleted in that SAME change, or they silently outlive their trigger —
    the exact ac4id/5xn3k.6 vacuity class recurring a second time.

    THIS test, not human memory or a bead comment, IS the revisit trigger
    (nexus-i5c2u: eyeball steps get skipped).
    """

    def test_floor_at_0_1_65_or_allowlist_entries_removed(self) -> None:
        from pathlib import Path

        from nexus.engine_version import REQUIRED_ENGINE_VERSION

        if REQUIRED_ENGINE_VERSION <= (0, 1, 65):
            return  # floor hasn't moved past the known-engine-half-inert version yet

        gate = Path(__file__).resolve().parent / "e2e" / "fresh-install-mvv.sh"
        text = gate.read_text(encoding="utf-8")
        assert "chash.conformance" not in text and "nexus-du2dw" not in text, (
            f"REQUIRED_ENGINE_VERSION={REQUIRED_ENGINE_VERSION} has moved "
            "past the known-inert v0.1.65 floor — presumably carrying "
            "nexus-du2dw's /chash/conformance route now, so delete the "
            "'chash.conformance' ALLOWLIST_REGEX entry (and its comment "
            "block) from tests/e2e/fresh-install-mvv.sh (nexus-8hpad) in "
            "the SAME change, or the entry silently outlives its own "
            "removal trigger — the ac4id/5xn3k.6 vacuity class recurring."
        )
        assert "report index_state but it is NULL" not in text and "nexus-vw594" not in text, (
            f"REQUIRED_ENGINE_VERSION={REQUIRED_ENGINE_VERSION} has moved "
            "past the known-inert v0.1.65 floor — presumably carrying "
            "vw594 F1's begin-many route now, so delete the vw594-anchored "
            "'stale index-run fences' ALLOWLIST_REGEX entry (and its "
            "comment block) from tests/e2e/fresh-install-mvv.sh "
            "(nexus-8hpad) in the SAME change, or the entry silently "
            "outlives its own removal trigger."
        )


class TestDescendantsFallbackDoesNotOutliveItsRoute:
    """Substantive critique 2026-08-10 finding 1 (T2
    nexus/chroma-residue-C1-T0.1-critique-2026-08-10), on top of ab7907fb
    (T2 nexus/chroma-residue-plan-2026-08-10 §C1).

    ``HttpCatalogClient.descendants()`` prefers the dedicated ``GET
    /v1/catalog/descendants`` engine route (one unbounded query, complete
    by construction) and falls back to ``_descendants_via_paginated_list``
    — an EXHAUSTIVE paginated ``/list`` walk — on a 404, because
    ``REQUIRED_ENGINE_VERSION`` was pinned at ``(0, 1, 69)`` when the route
    was added and the route ships in a LATER engine tag. That fallback was
    the exact shape of a real, measured bug (0% coverage on 11 of 12 large
    subtrees) before this fix, so it is a deliberate, temporary safety net
    — not a design commitment. Named risk (critique finding 1): a
    "temporary" Chroma-era page loop with no retirement mechanism becomes
    permanent dead code nobody notices, because once every client-served
    engine carries the route (``REQUIRED_ENGINE_VERSION`` >= the route's
    ship version), the 404 branch can never fire again and the fallback
    method is unreachable.

    The route's exact ship version is NOT established with certainty here
    (the ``descendants()`` docstring in ``http_catalog_client.py`` guesses
    "v0.1.70+", but that file was out of this fix's edit surface and the
    guess is unverified) — so this tripwire keys on the one fact that IS
    certain: the route did not exist at ``REQUIRED_ENGINE_VERSION ==
    (0, 1, 69)``, so it ships in some tag strictly after that. The instant
    the floor advances past ``(0, 1, 69)``, every engine a client is
    permitted to run against is required to carry the route, and the
    paginated fallback becomes dead code that should be deleted in the
    same change that bumps the floor.

    THIS test, not human memory or a bead comment, IS the revisit trigger
    (nexus-i5c2u: eyeball steps get skipped). It stays green while the
    floor is pinned at or below ``(0, 1, 69)`` and goes RED the moment
    ``REQUIRED_ENGINE_VERSION`` advances past it, unless
    ``_descendants_via_paginated_list`` and its 404-triggered call site
    have already been deleted from ``http_catalog_client.py`` in that SAME
    change.
    """

    def test_floor_at_0_1_69_or_fallback_removed(self) -> None:
        from pathlib import Path

        from nexus.engine_version import REQUIRED_ENGINE_VERSION

        if REQUIRED_ENGINE_VERSION <= (0, 1, 69):
            return  # route not guaranteed present on every servable engine yet

        client = (
            Path(__file__).resolve().parent.parent
            / "src" / "nexus" / "catalog" / "http_catalog_client.py"
        )
        text = client.read_text(encoding="utf-8")
        assert "_descendants_via_paginated_list" not in text, (
            f"REQUIRED_ENGINE_VERSION={REQUIRED_ENGINE_VERSION} has moved "
            "past (0, 1, 69) — the version pinned when GET "
            "/v1/catalog/descendants was added (ab7907fb, T2 "
            "nexus/chroma-residue-plan-2026-08-10 §C1). The exact tag that "
            "ships the route was not established with certainty (this "
            "tripwire keys on the one certain fact: it postdates 0.1.69), "
            "but a floor bump past that point means every engine a client "
            "is permitted to run against now carries the route, so the "
            "404-triggered pagination fallback can never fire again. "
            "DELETE: the `_descendants_via_paginated_list` method in "
            "src/nexus/catalog/http_catalog_client.py, the `except "
            "httpx.HTTPStatusError` / `status_code == 404` branch in "
            "`descendants()` that calls it, and the matching fallback "
            "tests in tests/catalog/test_descendants_pagination_"
            "completeness.py (TestDescendantsFallbackOn404) — in the SAME "
            "change that bumps the floor, or this becomes exactly the "
            "'permanent Chroma-era page loop under a temporary label' "
            "named in critique finding 1 "
            "(T2 nexus/chroma-residue-C1-T0.1-critique-2026-08-10)."
        )


class TestGcServersideFallbackDoesNotOutliveItsRoute:
    """RDR-191 Phase 1: ``nexus.gc_quarantine_orphans`` / ``gc_restore_rereferenced``
    / ``gc_expire_quarantine`` (catalog-023) ship at ``REQUIRED_ENGINE_VERSION
    == (0, 1, 69)`` — the route did not exist yet at that pin, so it ships in
    SOME tag strictly after it. Same shape as
    ``TestDescendantsFallbackDoesNotOutliveItsRoute`` (ab7907fb): this test is
    the revisit trigger, not human memory or a bead comment.

    IMPORTANT ASYMMETRY vs. the ``descendants`` tripwire, spelled out so a
    future editor does not over-delete: ``descendants()``'s fallback was
    PURELY an engine-version workaround (a 404-triggered branch with no other
    reason to exist), so the whole fallback method retires. The GC
    ``*_serverside`` wrappers in ``nexus/catalog/chunk_quarantine.py``
    (``quarantine_orphans_serverside`` / ``restore_rereferenced_serverside``
    / ``expire_quarantine_serverside``) have TWO independent reasons to
    return ``None`` — a 404 (engine predates the route: retires once the
    floor passes) AND ``getattr(db, "gc_quarantine_orphans", None) is None``
    (``db`` has no HTTP capability at all — local/in-memory mode, which
    NEVER retires, engine version is irrelevant to it). Only the
    ``VectorServiceError`` / ``code == 404`` branches in those three
    functions are what this floor bump makes unreachable — the
    ``fn is None`` attribute-absence branches, the functions themselves, and
    the client-side ``quarantine_orphans``/``restore_rereferenced``/
    ``expire_quarantine`` implementations they fall back to (RDR-191's own
    apparatus-retirement is Phase 5, a separate and much larger change) MUST
    NOT be deleted by this trigger.
    """

    def test_floor_at_0_1_69_or_404_branches_revisited(self) -> None:
        from pathlib import Path

        from nexus.engine_version import REQUIRED_ENGINE_VERSION

        if REQUIRED_ENGINE_VERSION <= (0, 1, 69):
            return  # route not guaranteed present on every servable HTTP-capable engine yet

        cq = (
            Path(__file__).resolve().parent.parent
            / "src" / "nexus" / "catalog" / "chunk_quarantine.py"
        )
        text = cq.read_text(encoding="utf-8")
        assert "code == 404" not in text and "getattr(exc, \"code\", None) == 404" not in text, (
            f"REQUIRED_ENGINE_VERSION={REQUIRED_ENGINE_VERSION} has moved "
            "past (0, 1, 69) — the version pinned when the RDR-191 "
            "gc_quarantine_orphans/gc_restore_rereferenced/gc_expire_quarantine "
            "routes were added (catalog-023-quarantine-functions.xml). Every "
            "HTTP-capable engine a client is now permitted to run against "
            "carries the route, so the 404-triggered fallback branches in "
            "quarantine_orphans_serverside/restore_rereferenced_serverside/"
            "expire_quarantine_serverside (nexus/catalog/chunk_quarantine.py) "
            "can never fire again FOR AN HTTP db. REVISIT (do not blind-"
            "delete): decide whether to simplify those three functions' "
            "exception handling now that a 404 can only mean a genuine "
            "server error, not a version-skew fallback trigger — but leave "
            "the `fn is None` branch, the three *_serverside functions "
            "themselves, and the client-side quarantine_orphans/"
            "restore_rereferenced/expire_quarantine apparatus they fall "
            "back to firmly in place (local/in-memory mode has no HTTP "
            "route ever, independent of engine version; and RDR-191's own "
            "apparatus retirement is Phase 5, not triggered by this floor)."
        )
