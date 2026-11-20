# Post-Mortem: RDR-241 No Client Walks of the Managed Catalog

> Prose: see REGISTER.md in the parent directory. The reader is the next person
> about to make the same mistake: what we expected, what happened, what to
> check first next time.

## RDR Summary

Client code answered tenant-wide catalog questions by paging through every document
(`all_documents(limit=0)` and its siblings): 28 calls in 25 sites on `2e09b8cfc`. Against the
managed catalog, a walk is one request per page for every document in the tenant, and two of the
walks ran without anyone asking (MCP author search, operator dispatch's sensitive-path list). The
RDR set one rule, that the engine answers tenant-wide questions, and narrowed the work (Sam,
2026-11-16) to a ratchet that keeps new walks out (Gap 4), the unattended walks (Gap 3) and routes
plus client switches for the questions an engine can answer (Gap 1). Tenant-wide jobs (Gap 2) were
deferred to RDR-246's job mechanism.

## Implementation Status

Implemented, for the narrowed scope. Four engine routes (`/docs/by-author`, `/docs/by-meta`,
`/sensitive/locators`, `/docs/by-scheme`) shipped in engine-service-v0.1.197, live 2026-11-19 with
their flags true at the public edge. Their client switches shipped in v8.5.1, which pins that engine.
The walk-site table ends at 18 calls in 17 sites, the Phase 2 target. The close-time critique read
PARTIAL (0 critical, 4 significant). Sam's rulings for it are T2
`nexus/sam-ruling-rdr241-close-2026-11-19`.

---

## Implementation vs. Plan

### What Was Implemented as Planned

- The walk-site ratchet over `all_documents` calls, with a checked table of every surviving site and
  its class (AGGREGATE, BULK JOB, BOUNDED).
- Phase 1's switches to existing routes, then Phase 2's routes and switches, with an absent route
  reported as the cause (`EngineRouteUnavailable`: unsupported, probe failed, timeout, failed,
  malformed) and never answered by a walk.
- The spike before the routes (nexus-uwggq.2.1): every metadata and locator statement under 100 ms
  at 50,000 documents, so neither got an index.

### What Diverged from the Plan

- **A fourth route.** `nx doctor --check-references` was a Phase 1 switch to per-scheme prefix
  reads. A prefix read is case-sensitive and would have dropped `HTTPS://` rows the client checks
  today, so Sam chose an engine route that keeps exactly the client's `urlparse` answer (option C,
  2026-11-16). The switch moved to Phase 2.
- **The author fold.** The route first folded case with `pg_c_utf8`. Review found it disagreed with
  Python's `str.lower()` on the final sigma and U+0307, so the engine fold maps final sigma to sigma
  and drops U+0307 on both sides. The route is a superset of the client's rule; its count is an upper
  bound, and the client filter makes the answer exact (Sam's ruling, T2
  `nexus/sam-ruling-rdr241-author-fold-2026-11-19`).
- **No author index.** The spike's one slow statement, a broad term paged at offset 40,000 (about
  150 ms), is OFFSET cost that no index removes. Sam accepted it (option A); keyset paging is the
  named fix if deep paging ever matters.
- **The clients landed before the engine cut.** Halcyon called the landing after the 8.5 branch cut.
  A release floor, `CATALOG_PHASE2_ROUTES_MIN_ENGINE_VERSION` (0.1.197), stopped any client release
  from pinning an engine without the routes. Declaring the client commits non-additive in the wire
  ledger was proposed first and was the wrong tool: the ledger guards the other direction.
- **Gap 2's trigger fired before close.** The deferral named nexus-f3yqd.2.22 closed (RDR-246's
  Minimum Viable Validation). It closed on 2026-11-18 while activation still waited on Sam, so at
  close Sam moved the trigger to nexus-f3yqd.2.23 closed.

### Existing Infrastructure Reused Instead of New Code

- The existing list routes (`listDocuments`, `documentsByCollection`,
  `documentsByOwnerContentTypes`, `distinctDocCollections`) for Phase 1, after source search showed
  they return the same live rows the walks read.

### What Was Added Beyond the Plan

- A strict page check on the paged routes: a page must hold exactly `min(limit, count - offset)`
  documents, else MALFORMED, so a short page is never read as the end.
- The cloud-client-path gate's Leg A checks the route flags through the edge. It checked three
  until the close-time critique found the by-scheme flag missing; added at close.

### What Was Planned but Not Implemented

- Gap 2, tenant-wide jobs as client loops (14 of the 25 frozen sites, including the two upgrade
  rungs): deferred, trigger nexus-f3yqd.2.23 closed.
- The citation join in `link_generator._all_entries`: no route designed. A known gap Sam accepted,
  revisited when the citation join is next touched.
- Walk shapes other than `all_documents` calls (`by_content_type`'s unbounded `/list`, `find_all`'s
  loop, `link_audit`'s page loop): stated residual, not counted by the ratchet.

## Drift Classification

| Divergence | Category |
|---|---|
| A fourth route for `--check-references` | Scope underestimation |
| The author fold's final sigma and U+0307 | Framework API detail |
| Gap 2's trigger fired with no owner | Deferred critical constraint |
| Leg A checked three of four flags | Missing cross-cutting concern |
| Wire-ledger entries cited pre-rebase shas | Missing cross-cutting concern |
| Engine-backed tests in the default selection broke the tenant ratchet | Missing cross-cutting concern |

## RDR Quality Assessment

### What the RDR Got Right

- Narrowing before building. The three in-scope gaps shipped in four days; the gap that needs a
  mechanism waited for it.
- The ratchet first. Every later step lowered a checked count instead of claiming progress in prose.
- Naming the falsifiers before the runs: the spike's 100 ms rule and the golden diff's prediction were
  both written down first, so each result decided something.

### What the RDR Missed

- A list of every place that names the route flags. A new flag was added to the engine, the edge
  allowlist and the clients, and missed in the gate that proves the edge passes it.
- What happens when a deferral's trigger fires before the RDR closes. The text said "rewrite Gap 2"
  and named no owner, so the trigger fired silently.
- That the scanner's single callee is the boundary of Gap 4's claim. The blind spot was stated; its
  consequence for "nothing stops a new walk" was not.

### What the RDR Over-specified

- The design-time site table's per-site call counts, frozen at `2e09b8cfc`; the checked table in
  `src/nexus/tables/walk-sites.toml` is the live one.

## Key Takeaways for RDR Process Improvement

- When a change adds a capability flag, grep every gate that lists the flags in the same commit.
- A deferral's trigger needs an owner for the day it fires, or a check at close that it has not.
- A rebase changes the shas a wire-ledger entry cites. Re-read the ledger after any rebase of a
  branch with entries, and map each sha by patch id before a release.
