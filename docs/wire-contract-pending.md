# Pending wire-contract pairings: engine halves deployed ahead of their client half

This is the ledger for the both-halves wire-contract tripwire (nexus-1vogq),
mirroring `conexus/PENDING_RELEASE.md`'s shape. It exists because of a real
incident: `498c92953` (RDR-191 GATE-2, manifest `collection` became NOT NULL
at the store) changed the engine's request validation and the client's wire
callers in the same commit. The engine tag deployed to the managed service
before any client release carried the fix. Every released client up to and
including v7.6.1 400'd on manifest writes for 34+ hours before the class was
diagnosed. Full trace: T2 `nexus/rdr-191-manifest-400-caller-trace-2026-08-14`.

`scripts/check_wire_contract_pairing.py` mechanizes detection: any commit in
`<newest published v* tag>..HEAD` that touches both the engine tree
(`service/`) and the client wire surface (client modules owning wire-contract
signatures, OR a hand-built `_post("/manifest...` / `_post("/import...` test
envelope -- the shape the 2026-08-14 bead comment added, since a raw test body
carries no method signature for a contract change to reconcile against) is a
**both-halves commit**.

**Rules, enforced by `tests/test_wire_contract_pairing_lint.py`:**

- Every both-halves commit MUST be listed under `## Unshipped` below, naming
  the owning bead and the engine tag that carries its engine half. A flagged
  commit absent from this section is UNDECLARED and fails the lint.
- When the client release carrying a commit's fix ships, move its line from
  `## Unshipped` to `## Shipped` (or delete it). An `## Unshipped` entry whose


  commit is already an ancestor of the newest published `v*` tag is STALE and
  fails the lint -- the ledger cannot quietly claim something is still pending
  once it has shipped.
- `scripts/check_engine_release_floor.py --paired-deploy` reads this ledger:
  a non-empty `## Unshipped` section blocks the paired-deploy path unless
  every entry's bead is named via `--ack-client-lag <bead-id>` (explicit
  paired-client acknowledgment, not a silent pass).
- `scripts/check_client_release_precondition.py` (protocol-audit [22511]
  Gap 1, 2026-08-14) reads this ledger too, unconditionally, on the
  UNPAIRED deploy path -- the ordinary "refresh the cloud engine" run that
  the paired-deploy branch above does not cover. Same `--ack-client-lag
  <bead-id>` escape shape.

---

## Unshipped

- `f34a4b82f20d28b0aee14c4f3dc1062d6d7c6351` -- bead nexus-fgxmk -- engine tag `engine-service-v0.1.89 (uncut; the next engine-service tag — the client half rides the client release whose REQUIRED_ENGINE_VERSION bumps to it)` -- `GET /v1/catalog/search` gains `ORDER BY tumbler` (engine half: CatalogRepository.searchDocuments) and the client documents that order contract + gains `find_by_title_exact` (client-only). Direction safety: NEW client + OLD engine = results still unordered (the docstring's order promise is unmet, nothing breaks; `find_by_title_exact` filters client-side and needs no engine change); OLD client + NEW engine = deterministic order where there was none. No request shape changed.
- `98911306253b3cb4dfe22fdaf8a5e730d5cfc92b` -- bead nexus-fgxmk -- engine tag `engine-service-v0.1.89 (uncut; same pairing as the line above)` -- javadoc + docstring wording only (string order, not registration order); no wire behaviour beyond the line above.

## Shipped

- `bc1847e08` -- bead nexus-lqqb2 -- shipped in `v7.22.0` -- engine half engine-service-v0.1.88 (tagged 2026-08-28 on 2ca52773f; pre-tag battery + PITR fork-walk CLEAN, deploy fires at the v7.22.0 client-tag push per the paired choreography) -- dead consent-audit wire DELETED both halves in one commit (client `record_consent`/`list_consents` + engine `/v1/telemetry/consents/{record,list}`). Deletion, so no released client calls it; an old client against a new engine gets 404 on a route nothing invokes.
- `72d25594e` -- bead nexus-v0x32 -- shipped in `v7.22.0` -- engine half engine-service-v0.1.88 (tagged 2026-08-28 on 2ca52773f; pre-tag battery + PITR fork-walk CLEAN, deploy fires at the v7.22.0 client-tag push per the paired choreography) -- `nx telemetry baseline`'s `GET /v1/telemetry/relevance/stats`; the client renders UNAVAILABLE-with-reason on an engine that lacks the route (fail-loud by design, no wedge).
- `36fad4487` -- bead nexus-8tnz2 -- shipped in `v7.22.0` -- engine half engine-service-v0.1.88 (tagged 2026-08-28 on 2ca52773f; pre-tag battery + PITR fork-walk CLEAN, deploy fires at the v7.22.0 client-tag push per the paired choreography) -- `GET /v1/catalog/docs/collection-counts-all` (all-rows counts for the drop-orphan-collections arm); an old engine 404s and the arm reports INCOMPLETE, refusing --execute (designed for exactly this window; extension pass a228e079).
- `c2d9c4302` -- bead nexus-ubnwk -- shipped in `v7.22.0` -- engine half engine-service-v0.1.88 (tagged 2026-08-28 on 2ca52773f; pre-tag battery + PITR fork-walk CLEAN, deploy fires at the v7.22.0 client-tag push per the paired choreography) -- `POST /v1/vectors/search-aspect-scoped` + `nexus.search_aspect_scoped_<dim>` (vectors-008) + aspects-004 doc_id backfill; the MCP tool fails loud through `_mcp_tool_error` on 404 until the tag deploys.

- `cc61d4c31` -- bead nexus-nyry9.10 -- shipped in `v7.14.0` -- engine half engine-service-v0.1.85 (deployed + cloud-gated 2026-08-21, paired client release) -- RDR-196 Phase 1: `steps[]` on POST /v1/telemetry/nx_answer_runs/record (engine half e87d2d9c7, nexus-nyry9.9: telemetry-007-1/-2 nx_answer_steps table + RLS, /version `nx_answer_steps_supported`), `include_steps` on the query route + nullable `nx_answer_runs.cost_usd` (engine half 93c3d7bf3, nexus-lme1s: telemetry-007-3), client half cc61d4c31 (steps write-through behind a /version capability probe; run cost_usd = sum of steps or null). Direction safety: new engine + old client = unchanged wire (steps optional, include_steps default off, null cost tolerated); old engine + new client = probe reports unsupported, run row only, never a 400. Shipped: REQUIRED_ENGINE_VERSION bumped to (0,1,85) for conexus 7.14.0; engine tagged on 73b6d4a0c, --acquire gate PASSED 2026-08-21.
- `6a7ff9915` -- bead nexus-tk070.p6b -- shipped in `v7.13.0` -- engine half engine-service-v0.1.84 (deployed + cloud-gated 2026-08-20 BEFORE the cut); RDR-194 D5, TWO INDEPENDENT wire surfaces, analyzed separately: (a) frecency SQL CHECK + boundary-400 -- genuinely new engine behavior, needs pairing. Engine half: nexus.frecency/staging.frecency ttl_days 0->NULL rewrite + CHECK (frecency_ttl_days_positive_chk, telemetry-006-frecency-ttl-null.xml) + TelemetryHandler.requirePositiveOrNullTtlDays boundary-400 on POST /v1/telemetry/frecency/upsert. Client half: T3Database.put/HttpVectorClient.put/nx store put/nx memory promote reject an explicit ttl_days<=0 with a typed ValueError BEFORE any wire call (nexus-24rof). Pairing, both directions: NEW client + OLD engine -- client-side rejection fires pre-HTTP, an un-paired old engine never sees a 0 from this client; safe. OLD client + NEW engine -- a pre-this-bead client can still send ttl_days=0; the new engine's CHECK/boundary-400/409 rejects it loudly, the intended D5 fail-loud, not a regression. Ack condition: the client release whose REQUIRED_ENGINE_VERSION bumps to the RDR-194 P7 paired engine tag carrying this CHECK. (b) The T3-chunk-metadata predicate flip (HttpVectorClient.expire $ne:0 -> $gt:0) needs NO pairing: safe specifically because $gt numeric-operand support was already live server-side before this bead (nexus-4l80g) -- a property of this operator's deployment history, not a general client-predicate truth.
- `f2d979113` -- bead nexus-tk070.p6a -- shipped in `v7.13.0` -- engine half engine-service-v0.1.84 (deployed + cloud-gated 2026-08-20 BEFORE the cut); RDR-194 D5: nexus.memory/nexus.plans ttl_days CHECK constraints (memory-003-ttl-days.xml, plans-003-ttl-days.xml) + MemoryHandler.requirePositiveOrNullTtl boundary-400 (engine half); MCP core.py ttl coercion deletion (client half). Ack condition: the client release whose REQUIRED_ENGINE_VERSION bumps to the RDR-194 P7 paired engine tag carrying these CHECKs. Bounded blast radius in the interim: an engine predating this commit still carries the retired coercePermanentTtl, so a client already carrying core.py's change talking to an un-paired old engine gets the pre-existing silent-coerce-to-NULL behavior, unchanged from before this bead.

- `f1c669b4792b43dc15d48dce344c48fa695bf287` -- bead nexus-zu4ma -- shipped in `v7.11.0` -- Bge768 padded-token-area sub-batching (engine half: engine-service-v0.1.81, deployed + cloud-gated 2026-08-19); client half (http_telemetry_store.py) rides v7.11.0, whose REQUIRED_ENGINE_VERSION=(0,1,82) floor carries the (0,1,81) bump (nexus-5uoxu conditions 2+4)

- `3b2901141627c98a4ad1c182bf021b44703d6d33` -- bead nexus-o8dil.33 -- shipped in `v7.8.0` -- catalog-030 (engine half: engine-service-v0.1.79); client half shipped 2026-08-17 in the paired release; the forced check was performed pre-deploy: v7.7.0's only callers were doctor diagnostics (degradation acknowledged via --ack-client-lag during the paired window, resolved the moment v7.8.0 published)

- `498c92953ea3ad60a75389aea53a9f501d8b126a` -- bead nexus-sh9v2 -- shipped in `v7.7.0` -- RDR-191 GATE-2: caller-supplied manifest collection, NOT NULL at the store (engine half: engine-service-v0.1.73)
- `b361a8106953c0bb586ab3aac969f904d3dff9df` -- bead nexus-rnqbw -- shipped in `v7.7.0` -- one protection semantics for the delete paths; manifest rows carry their own collection on the wire (engine half: engine-service-v0.1.74)
- `8c75a61a3fd1d65f61695263ea1b0961377c358d` -- bead nexus-sh9v2 -- shipped in `v7.7.0` -- F8c manifest guard passes the rows' own collection; rebuilt the GATE-2 tripwire (engine half: engine-service-v0.1.75 test coverage, no main-code touch)
