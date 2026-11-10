# RDR-231 post-mortem: note identity moves from chroma:// to nx-note://

## What the RDR set out to do

A note is a catalog document written by `store_put`, `nx store put` or `nx memory promote`, and its
`source_uri` is its identity. That identity was spelled `chroma://<collection>/<rest>`, the name of a
product nexus no longer uses, and every site that knew the scheme spelled it on its own. The RDR moved
every note to `nx-note://<collection>/<rest>`: the engine backfills stored rows once, turns `chroma://`
into `nx-note://` on every input route forever, rewrites the URI whenever a note's catalog row moves to
another collection, and shows only the stored spelling; the client mints and expects only `nx-note://`.

## Implementation status

Implemented. Epic nexus-bivz3 closed with all 30 children closed. The engine half shipped in
engine-service-v0.1.167 and the client half in 7.88.0 (2026-10-28), whose `REQUIRED_ENGINE_VERSION`
equals `NOTE_URI_MIN_ENGINE_VERSION`. The post-deploy census found 0 live `chroma://` note rows in
`catalog_documents`, `document_aspects` and `document_highlights` on every tenant, direct and export
form, with 417 notes now `nx-note://` (nexus-bivz3.23); the originating bug nexus-t7fbb closed on it.
Closed 2026-11-10 by linda: Sam chose "finish it" for RDR-231 in that day's grooming walk (relayed by
halcyon), under standing grant 2. The first close critique was `partial` (0 Critical, 4 Significant, all
the text lagging the code); the text was synced under standing grant 3 before the close.

## What diverged

**The backfill grew an export form.** The RDR's backfill rewrote `chroma://<c>/...` only. A
cross-collection import stores `nxexp://<target>/<original source_uri>`, so once the client minted
`nx-note://`, a re-import would have missed an unrewritten export row and made a duplicate. The backfill
and input canonicalization were extended to `nxexp://<target>/chroma://<c>/...` (Sam's ruling S6,
2026-10-26). The production census found 0 such rows. *Drift: scope underestimation.*

**Notes already moved kept a stale collection segment.** Moves before this RDR left some notes naming an
old collection. Repairing the segment inside the changeset would have collided with existing twins and
halted boot on installs that never ran a census, so the backfill changes the scheme only (S7) and a
separate, operator-run repair with a merge pass was built (nexus-bivz3.24, Decision 6). The census on
tenant nexus found nothing to repair, and no apply ran. *Drift: missing Day 2 operation.*

**The write refusal is best-effort, not a guarantee.** The RDR said the client refuses a note write when
the serving engine is below the floor, making "no twins" true by construction. An engine that reports no
version is not refused; the client logs and writes, and the backfill guard is the real stop. *Drift:
unvalidated assumption.*

**Operator aids nobody planned.** `nx daemon service note-uri-pairs` with psql access on every platform
(nexus-bivz3.28), `nx catalog merge`'s refusal to fold a chunk holder into a chunkless row (.27), and a
halt-and-recover diagnosis in `restart-stale` (.25), the last because Sam gated the deploy on a local
rehearsal of a halted boot. Additions, not reductions. *Drift: missing Day 2 operation.*

**The RDR text stopped following the work.** At acceptance the eight round 2 Significants went into the
beads, not the RDR, on purpose. The work then followed the beads, and the RDR kept the pre-implementation
SQL, unchecked assumptions and "assumed" research records until the close critique caught it.

## What to carry forward

- A "carry the gate findings in the beads" choice needs a matching step at the phase-exit crosswalk:
  write what changed back into the RDR then, not at close. Here the crosswalk bead handed the text sync
  "to Sam at close", and it waited two weeks.
- An identity-scheme change must list every stored form that embeds the identity, not only the column
  that holds it directly. The export form nests a note URI inside another URI.
- A data backfill that can only change one thing safely (the scheme) should say in the RDR what it leaves
  wrong and who fixes it, before the census, not after.
- A client-side refusal that depends on a version probe is a convenience. Name the server-side backstop
  in the same paragraph.
