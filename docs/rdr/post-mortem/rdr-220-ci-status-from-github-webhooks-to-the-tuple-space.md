# RDR-220 Post-Mortem: CI Status from GitHub Webhooks to the Tuple Space

**Closed** 2026-10-31 · **Accepted** 2026-09-27 · **Beads** 12, all closed (no epic) · **Shipped** engine-service-v0.1.133 (board-ci template) and v0.1.134 (board-ci token scope); the adapter in the conexus repo (conexus-jewq, PRs #394 and #396), live from 2026-09-27

## What the RDR set out to do

Sessions waiting on develop CI saw too little on the board: only a per-run
verdict, posted from inside `ci.yml`, so other workflows and per-job state
were missing (Gaps 1 and 2). For anything missing they polled GitHub, and on
2026-09-26 several `gh run watch` loops on one token tripped GitHub's
secondary rate limit (Gap 3).

The RDR proposed a GitHub webhook adapter, outside the engine, that posts
every job transition of every workflow to `board/ci/<topic>` with a token
that can only write board posts, and a repo script that folds those posts
into a per-sha status so readers never call GitHub.

## Implementation status

Implemented for Gaps 1 and 2; Gap 3 dropped. The engine template and token
scope shipped in v0.1.133 and v0.1.134, the adapter went live 2026-09-27,
the in-workflow board jobs were retired (5ac039222), and the Minimum Viable
Validation passed on run efdc06800 with the reader fold.

## Implementation vs plan

### As planned

- The `board/ci/<topic>` template and the `board-ci` scope, admitted only on
  `POST /v1/tuples/out` for that template.
- The adapter: signature check, fork check, branch allow-list, one post per
  job transition, retry contract, self-check and alarms.
- Retiring the `ci.yml` board jobs once the adapter's posts were confirmed.

### Diverged

1. **The reader fold was built and then deleted.** `scripts/ci_status.py`
   (c0a5cfef4) passed the MVV, then cleanup step 8 (aed1a2015, 2026-10-03)
   deleted it with its tests, and AGENTS.md rule 7 moved readers to one
   `gh api .../check-runs` call per sha. Nothing recorded the change in the
   RDR, which kept claiming the fold and "zero GitHub API calls from
   readers" until the 2026-10-31 close audit. Sam then ruled Gap 3 dropped:
   check runs stay the reader path and the board is a ping source.
2. **Completed posts live 24 hours, not three days.** The burst on 10-20 and
   10-21 lost 104 and 626 posts to the template's row cap, because GitHub
   never redelivers. The adapter now sets a 24-hour TTL on completed posts
   (conexus-efk8, 2026-10-22). The template comment still says three days.

### Drift classification

- 1: Unvalidated assumption (that readers would keep folding the board),
  then a decision made outside the RDR and not written back to it.
- 2: Missing failure mode (row-cap loss under a push burst).

## Lessons

- A cleanup that deletes a component an accepted RDR names as shipped must
  amend the RDR in the same change. Here the RDR was even amended 19 days
  after the deletion and still described the fold as live.
- An RDR whose premise moves to another mechanism (check runs) can still
  close as implemented for the gaps it did close, once the dropped gap is
  ruled and written into the Problem Statement where readers look.
