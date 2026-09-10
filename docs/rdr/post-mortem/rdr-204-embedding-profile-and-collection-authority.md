# Post-Mortem: RDR-204 Collections Stop Encoding Metadata in Their Names

> Prose: see REGISTER.md in the parent directory. The reader is the next person
> about to make the same mistake: what we expected, what happened, what to
> check first next time.

## RDR Summary

Collection names carried the embedding model, the owner and a version in their
segments, and every reader parsed them. RDR-204 made `catalog_collections` the
authority: an install-scoped `embedding_profile` (content type to model and
dimension) with an `embedding_models` seed, a backfill walk that classifies
every existing collection (live, disputed, dormant, quarantine) in one
changeset with its constraints, engine reads that resolve the model from the
row, and a client census that retires name parsing to a floor of sites where
the name is the CLI's own input. Phase 4, opaque names for new collections,
was rejected.

## Implementation Status

**Implemented, Phases 1 to 3 and Day 2.** Phase 1 shipped in
engine-service-v0.1.109 with conexus 7.37.0 on 2026-09-08. Phase 2 landed on
develop the same day. Phase 3 and Day 2 closed on 2026-09-09 and shipped in
conexus 7.38.0 paired with engine-service-v0.1.111. The three follow-ups from
the ship record went out in 7.39.0. Closed 2026-09-10. Epic nexus-ft04v,
every child closed.

## What Differed From the Plan

- **The census floor is 52, not zero.** The RDR's first Phase 3 wording read as
  "retire every name parse". The census found four classes of site that must
  read a name: mint-time, where the name is the user's input; write-model
  resolution; filter-by-row-absence; and the backfill, reconcile and doctor
  allowances. The RDR was amended to name the floor and the classes
  (164fc06b2) rather than the sites narrowed to fit a zero.
- **Phase 4 was rejected, not deferred.** After the 7.38.0 shakeout the
  opaque-name phase was judged to buy nothing the row-as-authority had not
  already bought. The RDR records the grounds and the reopen trigger.
- **An engine tag was burned.** v0.1.110 was tagged and never deployed; the
  quarantine-sibling dimension resolution it carried was wrong at the engine
  and had been masked by a client pre-registration. v0.1.111 superseded it with
  the root cause fixed at the engine and the client mask deleted.
- **The gate took nine rounds.** Each round's fix landed at the raised site and
  not at the sibling carrying the same fact. That loop, repeated on RDR-205, was
  measured on 2026-09-10 and traced to the fix-check rule itself; the consensus
  fix-check rule (nexus-dxksa) came out of it.

## What To Check First Next Time

- Before writing "retire every X", run the census and read the classes; the
  floor is a design fact, not a failure.
- A client fix that makes an engine 422 disappear is a mask, not a fix; find
  the engine cause before the tag.
- An accepted RDR is not edited to fit the code; when the census said 52, the
  amendment was the ruling, made once, at the fact's every site.
