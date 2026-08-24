---
title: "Indexing Lifecycle: Give the Corpus a Nameable Source Revision — Index the Mainline Ref from the Git Object Store, Diff-Driven and Opt-In"
id: RDR-199
type: Architecture
status: draft
priority: high
author: Sam
created: 2026-08-23
accepted_date:
related_issues: [nexus-ws67k]
---

# RDR-199: Indexing Lifecycle — Give the Corpus a Nameable Source Revision

## Problem Statement

The indexed corpus is a function of one machine's mutable working directory.
Nothing records which repository state produced it, so nothing can regenerate,
verify, or reason about it.

Sam, 2026-08-23: *"right now, it's whatever is in the local clone, which cannot
be correct — at least not correct all the time."* That calibration is the
precise form of the defect. **The corpus is not necessarily wrong. It is
unknowable.** It may be an accurate index of `develop`; it may be an index of a
half-finished feature branch plus three uncommitted edits. There is no
`(repository, ref, commit)` you can name that would reproduce it, and no field
you can read to find out which of those you have.

Every downstream symptom follows from that one missing fact.

### Enumerated gaps to close

1. **No source revision.** A catalog document carries `source_uri` (its
   identity) and `source_mtime` (a POSIX timestamp from the indexing machine's
   filesystem). It does not carry the *revision of the source* it was indexed
   from. `head_hash` exists but lives on the **owner** (the whole repository),
   not per document and not per ref.
2. **Staleness is decided by mtime, not by content or revision.** Measured over
   the last twelve `develop` commits: each touched **1–5 files**, while index
   runs re-processed **182–441** of a ~2151-file tree. Branch switches and
   worktree checkouts rewrite mtimes wholesale, so the staleness scan sees
   hundreds of "changed" files that are byte-identical.
3. **No branch dimension anywhere.** Grep of the catalog and indexing path finds
   zero branch fields; the only `branch=` occurrences are unrelated structlog
   labels. Whichever ref indexes last overwrites the single shared corpus.
4. **Uncommitted state is indexable.** The indexer walks the filesystem, so
   local edits and untracked-but-unignored files enter the shared corpus.
5. **The trigger is ambient, not declared.** Any commit in any checkout starts a
   full index. 433 runs in 14 days (~31/day); nine of them in one day targeted
   ephemeral worktrees, from three different sessions.
6. **Outcomes are invisible.** The dispatch is detached (`& disown`) with output
   redirected to a log nobody reads. A WAF-403 abort ran for **four days and 151
   runs** before anyone noticed the corpus had stopped updating (nexus-1jtob).

## Relationship to Prior RDRs

This RDR does not introduce a new substrate. Every primitive it needs already
exists; what is missing is one field and a changed trigger.

- **RDR-018 (closed), "Replace nx serve Polling Server with Git Hooks"** —
  installed the post-commit hook this RDR revises. Its decision was sound for
  its question (stop polling); it did not address *what* a commit should cause.
  This RDR narrows that hook's trigger; it does not restore polling.
- **RDR-096 (closed), "URI-Based Source Identity"** — established `source_uri`
  as the persistent identity, storing non-file schemes (`chroma://`, `https://`)
  verbatim. **This is the seam this RDR extends.** RDR-096 gave every document a
  stable *identity*; it did not give it a *revision*.
- **RDR-169 (accepted), "Docuverse Storage: Reference-Only Chunks"** — the
  URI-resolver and embed-without-store surface for content nexus references but
  does not hold. A source-revision field is the same shape of problem for remote
  content: "has the thing behind this URI changed?" Sam's instruction to fold
  the ref model into remote-content support lands here, not in a parallel
  mechanism.
- **RDR-181 (closed), "Server-Side Embed-Skip on Re-Index"** — **materially
  bounds this RDR's cost claim, and the claim must be stated correctly.** The
  engine now skips embedding for a chunk whose `(tenant, collection, chash)` it
  already holds; the client-side `skip_existing` flag is deprecated because the
  behaviour moved server-side and is unconditional. So re-processing 441 files
  does **not** re-embed 441 files' worth of Voyage tokens. The waste is
  client-side chunking, hashing and HTTP round-trips plus a server-side hash
  lookup — real, and the reason runs project at 64–131 minutes, but **not**
  embedding spend. Any cost argument in this RDR that implies otherwise is
  wrong.
- **RDR-101 (closed) / RDR-104 (closed)** — immutable document identity and
  incremental catalog projection. The incremental machinery exists; it is driven
  by the wrong input.
- **RDR-137 (closed)** — catalog as the canonical repo→collection authority, and
  the origin of the per-owner `head_hash` column this RDR must generalize.
- **RDR-193 (draft)** — server-side catalog reconciliation. Overlaps on *where*
  index-time diffing runs. Sequence after this RDR: RDR-199 defines what a diff
  is computed *against*; RDR-193 can then move that computation server-side.

## Context

### Background

The hook fires on every commit, resolves `REPO_TOP` with
`git rev-parse --show-toplevel`, and dispatches `nx index repo "$REPO_TOP"`
detached. `--show-toplevel` names a *checkout*, so a linked worktree indexes
itself as though it were a repository. An interim guard (nexus-ws67k, shipped
2026-08-23) now skips linked worktrees. **That guard is a stopgap and explicitly
not this design.**

### Technical Environment

`nexus` has two stable branches, `main` and `develop`. Feature branches are
routine and frequently materialized as `git worktree` checkouts, which this
repository's own guidance recommends for isolation. Hooks live in the common git
directory, so installing once arms every present and future worktree.

## Research Findings

### Investigation

Census of `~/.config/nexus/index.log`, 2026-08-10 to 2026-08-23, per-run rather
than per-line:

```
 433  runs in 14 days (~31/day)
 256  reached "Done."              (59%)
 166  aborted with a traceback     (38%)  -- 165 of them one WAF-403 class
  13  neither (killed, or pgrep-skipped)
   9  worktree-targeted in a single day, from three sessions
```

Commit footprint versus index footprint, last twelve `develop` commits: commits
touched 1, 5, 3, 5, 3, 3, 2, 2, 1, 1, 1, 1 files. Index runs re-processed 441,
360, 306, 291, 250, 186, 182 files.

### Key Discoveries

- **`head_hash` is per-owner.** `repos.py:159` reads it from the owner row.
  There is one commit sha for an entire repository, shared by every document.
- **A corpus document briefly carried a `head_hash` for a commit that existed in
  no branch.** On 2026-08-23 the corpus was stamped `87928a989` from a
  feature-branch index run; that commit was later rebased away. The recorded
  provenance pointed at nothing.
- **`skip_existing` is deprecated client-side** because RDR-181 moved the skip
  into the engine unconditionally. This is what bounds the cost claim above.

### Critical Assumptions

- **A1.** Reading blobs from the git object store (`git cat-file`,
  `git archive`) is not materially slower than reading the worktree for the
  volumes involved. UNVERIFIED — measure before Phase 1.
- **A2.** `main` and `develop` overlap closely enough that indexing only the
  mainline loses no retrieval value that anyone relies on. UNVERIFIED — measure
  the actual document-level divergence between the two refs.
- **A3.** Nobody depends on searching their own uncommitted edits. UNVERIFIED
  and the most likely to be wrong; see Trade-offs.
- **A4.** The diff between two commits is a sufficient description of what to
  re-index. Holds for content, but chunking is also a function of the *chunker
  version* — a pipeline-version bump must still force a full pass (RDR-029
  already versions this).

## Proposed Solution

### Approach

1. **Give every document a source revision.** Add a `source_revision` field
   alongside `source_uri`: an opaque, scheme-defined string. For a git-backed
   source it is the commit sha. For an `https://` source it is an ETag or
   content digest. For `chroma://` it is whatever that store versions by. This
   is the single field the whole design rests on, and it generalizes to remote
   content by construction rather than by extension.
2. **Read content from the git object store, not the worktree.** Makes the
   result reproducible and makes uncommitted state structurally unindexable
   rather than merely discouraged.
3. **Index the mainline ref; track the others.** One content corpus, indexed
   from one declared mainline ref. Other declared refs are recorded as
   bookkeeping — `last_indexed_revision` per ref — without a separate content
   namespace. `main` and `develop` overlap near-completely, so per-ref
   namespaces would roughly double storage for near-zero retrieval gain.
4. **Opt in to what is indexed.** A declared ref set in configuration. The
   default becomes silence: a ref nobody declared is never indexed. This inverts
   today's "index unless guarded".
5. **Diff-driven work.** `git diff --name-status <last_indexed_revision>..<target>`
   yields adds, modifications, deletes and renames exactly. Renames become
   metadata updates rather than re-embeds. Fall back to a full pass only on a
   pipeline-version bump or a missing baseline.
6. **Execution stays local.** Sam, 2026-08-23: *"wrt 5, its local. let's not get
   ahead of ourselves."* The hook remains the trigger. Reproducibility comes
   from the object-store read and the recorded revision, not from moving hosts.
7. **Surface the outcome.** A run that aborts must be visible without reading a
   44 MB log. Minimum: record last-indexed revision per declared ref where
   `nx doctor` can compare it against the ref's current tip, so staleness is a
   readable fact rather than an assumption.

### Decision Rationale

Locality plus a recorded revision is sufficient for the stated problem. A stale
local clone still produces a *nameable* result — `nexus@develop@<sha>` — and the
gap between that and the ref's real tip is then a computable number instead of
an unknown. Correctness here comes from naming the input, not from relocating
the computation.

## Alternatives Considered

### Alternative 1: Index in CI on the ref advance

Indexing becomes a pipeline stage: one run per ref advance regardless of how
many clones exist, a named commit as input, and a red job instead of a silent
four-day blackout.

**Rejected for now, by Sam's call**, as getting ahead of the problem. It also
adds a cloud dependency to a flow that currently works offline and delays
freshness from seconds to a CI cycle. Worth revisiting *after* this RDR ships,
because items 1, 2 and 5 are exactly the prerequisites that would make a CI
implementation trivial — the input becomes a commit sha and the work becomes a
diff.

### Alternative 2: Per-ref content namespaces

Each declared ref gets its own collections, fully separating `main` from
`develop`. Rejected: ~100% content overlap, so it roughly doubles storage and
index time to distinguish refs that differ by a release boundary.

### Alternative 3: Keep the worktree guard and stop there

The shipped interim. Rejected as the terminal state: it removes nine runs a day
but leaves identity, staleness, and the trigger untouched. The corpus stays
unknowable.

### Briefly Rejected

- **Debounce/coalesce per repository.** Reduces run count without making any run
  reproducible. Treats the symptom.
- **Disable auto-indexing entirely.** Most decisive on cost, but freshness
  degrades silently and users of `nx search` are not told the index stopped
  moving — trading a visible cost for an invisible one.

## Trade-offs

### Consequences

- The corpus becomes reproducible and attributable.
- Index work drops toward the size of the actual diff.
- **Uncommitted edits stop being searchable.** Today, because the indexer walks
  the worktree, work-in-progress *is* findable via `nx search`. That behaviour
  disappears. For a corpus shared across sessions and machines this is arguably
  the bug being fixed, but it is a real capability change and A3 should be
  tested with actual usage, not assumed.
- Freshness is tied to *commit*, not to *save*.

### Risks and Mitigations

- **Baseline migration.** Existing documents have no `source_revision`. Treat
  absent as "unknown baseline" and force one full pass per declared ref; do not
  guess a revision.
- **Renames.** Git rename detection is heuristic. A mis-detected rename that
  becomes a metadata update instead of a re-index silently keeps stale content.
  Mitigate by verifying content hash on any rename before skipping the re-index.

### Failure Modes

- A declared ref that never advances looks identical to a working index that has
  nothing to do. The staleness surface (item 7) is what distinguishes them, and
  it is therefore not optional.

## Implementation Plan

### Prerequisites

- Measure A1 (object-store read cost) and A2 (`main` vs `develop` divergence)
  before any code.

### Minimum Viable Validation

Index one declared ref twice at the same commit and assert the second run does
zero work; advance the ref by a one-file commit and assert exactly one document
is re-indexed. Both assertions fail today.

### Phase 1

`source_revision` field, populated for git-backed sources; per-ref
`last_indexed_revision` bookkeeping.

### Phase 2

Object-store reads and diff-driven selection, gated behind the declared-ref set.

### Phase 3

Retire the interim worktree guard's *reason for existing* — under a declared-ref
model, a worktree on an undeclared branch is skipped by the rule, not by a
special case. Keep the guard until then.

## Test Plan

Every assertion executes against real git repositories, with a positive control
proving the check can fail. This is a standing requirement in this repo and is
the reason three separate false negatives were caught on 2026-08-23 (a
CloudWatch window against offset-stamped datapoints, a terraform namespace
interpolation, and the `catalog_search(file_path=...)` filter of nexus-fhim9).
**A zero is a claim about the instrument until the instrument has been shown to
return non-zero.**

## Validation

- Re-indexing at an unchanged revision does zero work.
- A one-file commit re-indexes exactly one document.
- An uncommitted edit is never indexed.
- Every document carries a `source_revision` that names a commit reachable from
  a declared ref.
