# RDR-169 Post-Mortem: Docuverse Storage, Reference-Only Chunks

**Closed** 2026-09-16 · **Accepted** 2026-06-25 · **Epic** nexus-izluc (22 beads, all closed) · **Shipped** engine-service-v0.1.116 and later, client 7.43.0 and later

## What the RDR set out to do

A consumer that indexes content nexus does not own (a vault, a DEVONthink
database, a remote document) had no way to register a chunk's identity,
embedding and address without also handing nexus the bytes. Six gaps: a
retention model with nullable content, a search-return shape that survives
absent content, a resolver registry keyed on the URI scheme, an
embed-without-store route, first-class span and source fields on the
bridge, and a staleness signal for content held elsewhere.

The RDR split the work by schema impact. The non-schema track (gaps 3 to 6)
could proceed at once. The schema track (gaps 1 and 2) was held for a
conexus co-design signal, because the retention column was to be a paired
change with the conexus ETL.

## Implementation status

Implemented. Both tracks landed on develop; the schema track shipped in
engine-service-v0.1.116 with the paired client 7.43.0.

## Implementation vs plan

### As planned

- Gap 1: `vectors-014` adds `retention` with a two-value check and drops
  `NOT NULL` on `chunk_text`, with a check that ties null content to the
  reference-only value (nexus-zw2em).
- Gap 2: `vectors-015` re-creates the plain and text-gated search functions
  with a retention output column; the Python `SearchResult.content` became
  optional (nexus-fams8, nexus-zw2em).
- Gap 4: `POST /v1/vectors/upsert-reference-only` with a dedicated insert
  path; an ordinary upsert over a reference-only row promotes it to full,
  and the reverse direction is refused (nexus-xvb6b, nexus-dtnpu).
- Gaps 3, 5, 6: the resolver registry, the bridge fields and the staleness
  signal landed in Phase A on 2026-06-25 (nexus-064jj, nexus-jkv85,
  nexus-5ev5s).

### Diverged

- **The target table moved under the RDR.** The accepted design named the
  per-dimension tables `chunks_{384,768,1024}`. By the time the schema track
  unblocked, RDR-191 had unified them into `nexus.chunks`. The developer
  retargeted at dispatch (nexus-zw2em).
- **The cross-repo pairing became moot.** The 78-day hold for the conexus
  signal (nexus-1tl7m, nexus-5cxwo) ended with conexus deciding its ETL did
  not need the paired column. The schema track shipped engine-only.
- **A schema change that admits null is a client change.** The released
  Python client sliced `content` and would have crashed on the first
  reference-only row in plain vector search. Code review caught it before
  the engine deployed; the None-guard shipped in the paired client.
- **Gap 3 was built and not wired.** The registry and two handlers existed
  from Phase A, but nothing called them on a read path, and the bead their
  javadoc pointed at closed through grooming. Found during the Phase B
  landing and wired as `POST /v1/vectors/resolve` (nexus-aphki).
- **Twelve functions missed the column.** The combined-query functions
  behind the four catalog-scoped search tools did not emit retention until
  `vectors-016` (nexus-4k1vz), found the same evening.

### Not implemented

- Gap 6 for `https://` and for the engine side. The staleness signal is
  real and tested for the four local schemes; the https reader always
  reports fresh, and the engine's resolver has no staleness field. The
  code cited a bead for the follow-up that had closed through grooming
  having done other work. Found by the close critique; nexus-oqenh owns it
  now. This is the one silent gap the phase reviews missed, next to the
  one they caught (the twelve combined-query functions).
- The third retention value `snapshot`. Deferred in the RDR itself pending
  a named consumer; none has appeared.

## Drift classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| Deferred critical constraint | 1 | table unification landed during the hold | No |
| Unvalidated assumption | 1 | the conexus ETL needed the paired column | No, decided on their side |
| Missing cross-cutting concern | 2 | client slicing null content; 12 combined-query functions | Yes, grep the read paths |
| Missing Day 2 operation | 2 | Gap 3 registry with no caller; Gap 6 follow-up cited a bead that closed on other work | Yes, a wiring test; yes, re-read the cited bead at close |

## What to check first next time

An accepted RDR that waits on another repo is a design against a tree that
keeps moving. Before dispatching the held track, diff the RDR's named
tables and routes against the current changelog, on the bead, not in the
developer's head. And when a change makes a nullable value reachable on
the wire, grep every client read of that field before calling the change
additive.
