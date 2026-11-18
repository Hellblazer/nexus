# RDR-171 post-mortem: dchash document-offset spans that survive re-chunking

## What the RDR set out to do

A catalog link can point at part of a document through a *span*. None of
the four existing span forms could pin a passage (a sentence or a quotation)
and still resolve after the document was re-chunked: chunk-addressed spans move
with the chunk boundaries, and line spans are coarse and carry no stamp. And
`link_audit` no longer reported any span that had gone wrong. RDR-171 added a
fifth form, `dchash:<hash>:<start>-<end>`. Its offsets are code points in the
document's canonical text, and the hash is the SHA-256 of that text, so the
span ignores chunking and goes stale only when the text changes. The RDR also
made `link_audit` report stale `dchash:` spans. It covers text documents with
a readable source file; PDFs and stored notes are out of scope (Sam,
2026-11-16).

## Implementation status

Implemented. Steps 0-5 shipped to develop between 2026-11-16 and 2026-11-18
under beads nexus-qrg1r.5, .6, .1, .7 and .2; the last of them is 85e85336a
(CI green run 44323206269). The live validation (nexus-qrg1r.3) ran on a
throwaway local engine. Both spans read back after a forced re-index, and an
edited file's span was listed as `changed`. The review gate (nexus-qrg1r.4)
found no ship-blockers; its fixes landed under that bead. Reviews: T2
`nexus_rdr/171-review-*`.

## What diverged

**The re-chunk test did not re-chunk.** The headline claim is survival across
re-chunking. The Step 2 test passed no catalog and changed entry fields the
resolver never reads, so a regression that read the chunk manifest would have
passed it. The live validation could not cover the gap either, because each
test document indexed as one chunk. The survival held by construction, since
the resolver reads no chunks, but nothing tested it until the review gate. The
gate's fix re-chunks through a catalog that fails on any manifest or chunk
read.

*Drift: unvalidated assumption.*

**`nx catalog span` took tumblers only.** Every sibling command resolves a
title or URI. The docs example passed a title and would have failed. Every test
and the live validation used tumblers, so only the review gate caught it.

*Drift: missing cross-cutting concern.*

**The false "chash survives" claim lived in more places than the plan listed.**
Gate finding S-E named `docs/catalog.md`, `docs/cli-reference.md` and the
`--from-span` help. The same claim also sat in `docs/catalog.md`'s Link health
section, an exploration essay, a troubleshooting line, and a "(preferred)"
label on `chash:` in the link help. It took three review rounds to remove, one
site per round. Each round found the next site because the fix touched only the
files named, not every file the claim appeared in.

*Drift: scope underestimation.*

**Two acceptance tests were replaced by weaker ones.** Step 2 asked for
`nx catalog show` on a document with a malformed `dchash:` span. The bead
closed with a live check on a stale span instead, and the malformed case went
untested until the gate added it. A test placed in a module that mints a tenant
for every test pushed the tenant count to +1. It moved out before the push.

*Drift: none (test placement).*

**The Step 5 plan described code that had already changed.** Its bead asked to
move the status rules into one shared helper. Step 2 had already done that. It
also asked the audit to match a `resolve_path` fallthrough that `dchash_source`
does not have. The implementer followed the code, and the reviewers accepted
both departures.

*Drift: internal contradiction.*

## What the next person should take from it

A claim that holds "by construction" still needs a test that would fail if the
construction changed. Write the test against the thing the claim is about:
here, a catalog whose chunks change and whose chunk reads fail. When a fix
corrects a false sentence, grep the whole repo for the sentence before
committing, not just the files the finding names. A new command that takes a
document should accept what its siblings accept, and its tests should use the
same inputs as its docs example.
