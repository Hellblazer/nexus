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

## Shipped

- `f1c669b4792b43dc15d48dce344c48fa695bf287` -- bead nexus-zu4ma -- shipped in `v7.11.0` -- Bge768 padded-token-area sub-batching (engine half: engine-service-v0.1.81, deployed + cloud-gated 2026-08-19); client half (http_telemetry_store.py) rides v7.11.0, whose REQUIRED_ENGINE_VERSION=(0,1,82) floor carries the (0,1,81) bump (nexus-5uoxu conditions 2+4)

- `3b2901141627c98a4ad1c182bf021b44703d6d33` -- bead nexus-o8dil.33 -- shipped in `v7.8.0` -- catalog-030 (engine half: engine-service-v0.1.79); client half shipped 2026-08-17 in the paired release; the forced check was performed pre-deploy: v7.7.0's only callers were doctor diagnostics (degradation acknowledged via --ack-client-lag during the paired window, resolved the moment v7.8.0 published)

- `498c92953ea3ad60a75389aea53a9f501d8b126a` -- bead nexus-sh9v2 -- shipped in `v7.7.0` -- RDR-191 GATE-2: caller-supplied manifest collection, NOT NULL at the store (engine half: engine-service-v0.1.73)
- `b361a8106953c0bb586ab3aac969f904d3dff9df` -- bead nexus-rnqbw -- shipped in `v7.7.0` -- one protection semantics for the delete paths; manifest rows carry their own collection on the wire (engine half: engine-service-v0.1.74)
- `8c75a61a3fd1d65f61695263ea1b0961377c358d` -- bead nexus-sh9v2 -- shipped in `v7.7.0` -- F8c manifest guard passes the rows' own collection; rebuilt the GATE-2 tripwire (engine half: engine-service-v0.1.75 test coverage, no main-code touch)
