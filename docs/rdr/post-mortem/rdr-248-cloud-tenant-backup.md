# Post-Mortem: RDR-248 Cloud-Mode nx backup: an Engine Tenant Export Saved to One File, with a Tested Restore into an Empty Tenant

> Prose: see REGISTER.md in the parent directory. The reader is the next person
> about to make the same mistake: what we expected, what happened, what to
> check first next time.

## RDR Summary

`nx backup` refused managed mode, and the exports that worked in cloud mode were partial and read
different moments. RDR-248 had the engine export one tenant from one snapshot, as a session of short
requests that fit the cloud edge's 60 s exchange and 20 MB body limits. The client writes one framed,
verified file. A second route pair loads such a file into an empty tenant, so the backup is proven by a
restore.

## Implementation Status

Implemented. Engine 0.1.192 (live 2026-11-15) carries the export and restore routes. The epic
(nexus-csayy, 10 beads) is closed. The first cloud export passed in run 2 (60 s, 633 MB). The first
cloud round trip passed after the edge fix (fresh export 56.6 s, restore into a throwaway tenant
555.5 s for 233,107 rows, re-export 57.9 s, compare PASS on 24 of 25 tables).

---

## Implementation vs. Plan

### What Was Implemented as Planned

- One snapshot per export, read as a session of short page requests inside the edge's limits.
- One framed file with a header, pages and a trailer, checked by `nx backup --check FILE`.
- Restore into an empty tenant only, with the planned remap of reassigned ids.
- The 60-minute export cap (Sam, 2026-11-11). The real export took about a minute and never reached it.

### What Diverged from the Plan

- **A page could hang**: the plan bounded the session (10 minutes idle, 60 minutes total) but not a
  single page. Under the native-image build's TLS, PgJDBC's skip path spun, and one page never
  returned. The fix added a 25 s wall per page. A native TLS check on 74a316006 passed: 3 walks of 31
  pages, slowest page 0.58 s. The 0.1.191 control still hangs on the same setup.
- **The edge dropped the part checksum**: CloudFront's origin request policy forwards only the headers
  it lists, so every restore part reached the engine without `X-Nexus-Part-Sha256`. The first restore
  failed with `400 missing_part_sha256`. conexus #710 added the header. The pre-deploy edge check
  covered the new routes, not the headers they need. The RDR (line 316) now names the header.
- **The verification step broke the RDR's own rule**: `test_nx_backup_pin` allows only the backup
  verb to import `nexus.backup.*` (nexus-f3yqd.4, no automatic backup). The round-trip compare needs
  that module's file reader. Placed in `scripts/` (326f2702f), it reddened develop. It now lives in
  `tests/e2e/` (eff561d94), beside the scale runs, and it only reads files.

### Existing Infrastructure Reused Instead of New Code

- None beyond what the RDR's audit already named.

### What Was Added Beyond the Plan

- **Three cases the comparison had to settle**: the plan's test (Test Plan, "per-table counts and row
  contents equal") and its note that trigger-maintained columns and reassigned ids change by design did
  not cover three cases. They were `tenant_id`, an id remapped inside a jsonb value
  (`run_summary.run.plan_id`), and the `catalog_meta` marker row. The compare
  (`tests/e2e/backup_roundtrip_compare.py`) lets `tenant_id` and integer ids differ, sends jsonb to
  review, and does not judge `catalog_meta`. Run 2 differed only in `tenant_id` and `plans.id`.
- **A table retired in flight**: a sibling bead (nexus-6pfgb.42, `telemetry-019-1`, shipped in 0.1.192)
  dropped `nexus.frecency`, one of the planned derived tables. The shipped registry exports 25 tables, not
  26.
- **The 25 s page wall**, above.

### What Was Planned but Not Implemented

- **Activation Step 2's measurements**: the plan wanted a tenant near nexus's size (about 540,000
  chunks), the response start of every page against a 5 s trigger, `NX_POOL_SIZE` against
  `max_connections`, and the oldest xmin during the import hold. The cloud runs used a 116,492-chunk
  tenant and recorded none of these. Sam accepted closing short of the target on 2026-11-15
  ("Accept, watch the 20 s line"): the index trigger is the engine's existing 20 s
  `export_page_over_budget` line, which is the only page timing the engine logs.
- **Cloud proof of the id remap and the soft references**: the cloud restore held rows in 8 of 25
  tables, and the only reassigned-id table with rows was `plans` (13 rows). The other remaps and every
  soft-reference rule are proven by the local round-trip and scale tests only.
- **The restore time limit at nexus's size**: scaled linearly from the 116,492-chunk run, a restore of
  `nexus` (about 540,000 chunks) takes about 2,600 s against `NX_IMPORT_LOAD_MAX` of 3,960 s, a 1.5 times
  margin, not the 2 times the bound was set for. The scaling is from one run, and the local scale run
  that set the bound used storage with fsync off. No restore near nexus's size has run in the cloud. The
  runbook (`docs/operations/backup-restore.md`, step 2) tells the operator to raise the bound before a
  restore over 500,000 chunks.
- **The 20 s watch is passive**: the index trigger is the engine's `export_page_over_budget` log line,
  which fires only after an export has already failed. Engine-release Step 6.2 now reads the engine log
  for it in each deploy window.

---

## Drift Classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| **Unvalidated assumption** | 1 | Restore load time measured on fsync-off local storage, assumed to hold on the cloud database | Yes, spike: one restore near nexus's size in the cloud |
| **Framework API detail** | 1 | PgJDBC's TLS skip spin under native image | Yes, spike: one native-image export page over TLS |
| **Missing failure mode** | 1 | No bound on a single page, only on the session | Yes, source search: list every blocking read and its bound |
| **Missing Day 2 operation** | 1 | chas lacked zlib1g-dev for the native build, worked around in user space | No, tooling |
| **Deferred critical constraint** | 1 | Activation Step 2 measured a tenant a fifth of nexus's size and recorded none of its promised figures; accepted by Sam | Yes, size the cloud tenant before the run |
| **Over-specified code** | 0 | | |
| **Under-specified architecture** | 1 | The comparison rules for `tenant_id`, jsonb-embedded ids and the marker row | Yes, write the acceptance test into the RDR |
| **Scope underestimation** | 0 | | |
| **Internal contradiction** | 1 | The backup pin forbade the reader the verification needed | Yes, source search: check the RDR's own pins against its verification plan |
| **Missing cross-cutting concern** | 1 | The edge forwards only listed request headers | Yes, source search: read the edge's origin request policy for every new header |

### Pattern References

No category has two or more instances.

---

## RDR Quality Assessment

### What the RDR Got Right

- Choosing an engine-side export over a client bundle of the existing exports. One snapshot is the only
  way the copies agree.
- Sizing every request to the edge's 60 s and 20 MB limits from the start. Both cloud runs finished
  inside them.
- Requiring a restore into an empty tenant as the proof of a backup. It found the edge header gap,
  which an export-only test could not have.

### What the RDR Missed

- A bound on each page, not only on the session.
- The edge's header policy as part of the wire contract.
- A column-level definition of a faithful restore.
- That its own backup pin covers any code that reads the backup format.

### What the RDR Over-specified

- Nothing known.

---

## Key Takeaways for RDR Process Improvement

1. **Bound every blocking call, not only the session**: a session limit does not stop one read that
   never returns. List each read that can block and name its timeout.
2. **Treat the edge as part of the wire contract**: for every new request header, read the edge's
   origin request policy, and test through the edge before the deploy, not only against the engine.
3. **Write the acceptance test into the RDR in full**: name every planned difference, including ids
   remapped inside json values and marker rows the operation writes. Otherwise the test's edge cases are
   settled after the fact.
4. **Check the RDR's own guards against its verification plan**: a rule that forbids a module's import
   also forbids the tool that verifies that module's output. Name where that tool lives.
