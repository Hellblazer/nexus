# RDR-254 post-mortem: identity when a path moves

## What the RDR set out to do

A catalog document is identified by its file's path, and a repository owner by
its folder's path. When either moved, nexus could not tell it was the same
thing. A moved PDF was registered a second time, and search returned both
copies (GH #1571). A renamed repository became a second owner and every file was
re-indexed (GH #1605). A repository moved under the same folder name kept every
document on a dead path. Two clones with the same folder name took one owner
over in turn. No command repaired any of these.

The RDR added engine routes that move a document or an owner to a new path and
carry what keys on the old row (links, sensitivity, highlights, aspects); a
client command over them that is a dry run unless given `--no-dry-run
--confirm`; hints printed during indexing when a new row or owner looks like a
moved one; and a rule that gives a second live clone its own owner,
`<folder>-<repo hash>`.

## Implementation status

Implemented, in four phases: engine move routes, the client command, move
hints, and the second-clone owner. Phases 1 to 3 landed on develop at
d124a736a3. Phase 4 lands with this post-mortem. The minimum viable validation
ran against a real engine for GH #1571 (8c70440a7b, fed4c979f8); GH #1605, a
renamed repository, is covered by the real-engine hint and owner-move journey
tests (34 passed with the MVV on the close branch). The
owner move was measured, not estimated: 15,442 row writes for owners of 7,200
and 3,100 documents, holding row locks for 1.7 to 8.4 s, with reads unblocked.

One residual is named in Risks and was accepted by Sam on 2026-11-21: two
clones registering their first owner at the same instant can both claim the
plain name. The loser splits itself to its own owner on its next run, so the
case heals with one full re-registration. Closing it needs an engine refusal
that can tell a moved clone (Gap 3, which the engine must re-root) from a racing
second clone (Gap 4), and that has no design yet.

## What diverged

**A late aspect write could land on the absorbed row.** Moving a document
absorbs its old row. An aspect extraction queued for the old row before the
move could finish after it and write its result against a document that no
longer had a live row, or against an alias. The RDR had not considered work in
flight during a move. The fix keeps the row's `doc_id` on an upsert for a
trashed or alias document (41a4edd0ae, d26ac3fcc2, 348cef2712).

*Drift: missing failure mode.*

**That fix opened a deadlock.** The aspect upsert's catalog read takes locks in
the order catalog row, then `document_aspects`. A collection rename and a
collection delete take them in the reverse order, so an unfenced upsert such as
`nx enrich aspects` running beside a rename of the same collection can
deadlock. Sam accepted the edge with a retry follow-up, and the retry is built:
the upsert runs under `DeadlockRetry`, four attempts with backoff, as the rename
and delete already did (97a0676f57, tests 53e1f30709). Lock order across every
writer of a table was not part of the design review.

*Drift: missing cross-cutting concern.*

**Renaming a second clone's owner broke a reader of the owner name.** The RDR
lifecycle stream derived the RDR project from the owner's name, so a second
clone named `<folder>-<hash>` read the wrong project. It now reads the basename
of the owner's recorded root. The owner-name census in the RDR listed
`owner_scope.py` but not this reader.

*Drift: missing cross-cutting concern.*

**The second-clone rule needed more cases than the design named.** Review of
Phase 4 found that a `<folder>-<hash>` suffix can already be held by another
owner (it now grows to the full root hash), that only a permission error should
count a root as present but unreadable, that a run whose own
root is missing must split rather than take the owner over, and that two
clones registering at the same instant race (the residual above). The design
described the steady state of two clones and not their first registrations.

*Drift: scope underestimation.*

**The curator hint was claimed to repeat when it did not.** Gate round 1's fix
check failed on a sentence saying the curator move hint repeats on every run.
It was corrected before acceptance.

*Drift: internal contradiction.*

## What held

The census of every table keyed on a path or document id, done before any
code, held: the move carries everything it lists, and the late aspect write
above was a timing problem on a listed table, not a missing one. Making the command a dry run by default, and having hints
print the dry run rather than act, meant no review finding concerned data a
wrong hint could damage.

## For the next RDR that moves identity

List every writer of each table the change touches and the order in which it
takes locks, and list in-flight work (queues, workers) that holds an id across
the change. Both of this RDR's engine findings came from those two lists, and
neither was in the design.
