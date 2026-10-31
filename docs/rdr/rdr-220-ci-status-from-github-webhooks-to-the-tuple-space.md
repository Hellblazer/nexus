---
title: "CI Status From GitHub Webhooks to the Tuple Space"
id: RDR-220
type: Architecture
status: closed
closed_date: 2026-10-31
priority: medium
author: Sam
reviewed-by: self
created: 2026-09-26
accepted_date: 2026-09-27
related_issues: [nexus-dotwy, nexus-r3ur5]
related_rdrs: [RDR-205, RDR-211, RDR-208]
---

# RDR-220: CI Status From GitHub Webhooks to the Tuple Space

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

## Problem Statement

Agent sessions need to know the state of CI: which workflow runs and jobs
are queued, in progress, passed, or failed, for the commit they just
pushed and for the commit a peer is about to push onto. Today the only
published state is two posts per develop run of one workflow. Everything
finer comes from polling GitHub, one loop per session, on one shared
token.

The tuple space (RDR-205) is the right transport: a board topic is a
one-to-many announcement that every subscribed session receives as a
channel ping (RDR-211), with no reader needing to run when the post is
written. The missing piece is the source: a generic path from GitHub's
own events into board posts, with no per-workflow wiring.

### Enumerated gaps to close

#### Gap 1: Status is per run, not per job

nexus-dotwy (2026-09-26) added two jobs to `ci.yml` that post
`ci-pending` when a develop run starts and `ci-verdict` when it ends.
A reader learns the conclusion of the whole run only after the slowest
job finishes. It cannot see that lint passed, that three of four test
shards are green, or that one shard is still running. The first real
verdict (run 36253520241) said `failure` with `failed:
["pytest-gate","test"]`; finding that one shard had timed out at 100%
took several GitHub API calls.

#### Gap 2: Every covered workflow needs hand-written jobs

The two jobs live inside `ci.yml` and know its job graph: the verdict
job's `needs` must list every other job, pinned by a test. Covering
`service-ci.yml`, the release workflows, and the nightly gates means
repeating that wiring in each file and keeping each `needs` list
current. A `workflow_run`-triggered observer cannot replace it, because
GitHub runs `workflow_run` workflows only from the default branch's copy
(`main`), so it would sit dormant for work on `develop`
(`ci-commit-coverage-audit.yml`'s header records this).

#### Gap 3: Readers still poll GitHub for anything the board lacks

Several sessions waiting on develop CI with `gh run watch` loops on one
token tripped GitHub's secondary rate limit on 2026-09-26: every Actions
API call returned 403 while `gh api rate_limit` showed the hourly quota
unused (T2 `nexus/github-api-usage-research-2026-09-26`). The board
removes the loops only for the facts it carries. As long as per-job and
per-workflow state is missing, sessions fall back to the API.

*Dropped 2026-10-31 (Sam; see Revision History). The reader fold that closed
this gap was retired on 2026-10-03, and readers now make one `gh api
.../check-runs` call per sha, one call at a time, as AGENTS.md rule 7 says.*

## Relationship to Prior RDRs

Searched the RDR index for: tuple, board, subscribe, channel, webhook,
GitHub, CI.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-205 (Linda tuple space over Postgres) | Precedent | Defines subspaces, templates, and the `board/<topic>` template this design writes to. No new tuple semantics are needed. |
| RDR-211 (board subscriptions, channel delivery) | Precedent | A board post already reaches every subscribed session as a channel ping, and a post is never claimed. This RDR adds a writer, not a delivery path. |
| RDR-208 (session-id mail addressing) | Adjacent | Addressing of sessions; unaffected. CI posts are addressed to topics, not sessions. |

## Context

### Background

nexus-dotwy shipped the in-workflow publisher (`scripts/ci_board_post.py`
plus `board-pending` and `board-verdict` jobs in `ci.yml`). It proved the
transport end to end on the live managed engine: the push of 7885b793c
posted `ci-pending`, every subscribed session received the ping, and the
`ci-verdict` post followed when the run ended. Sam chose, on 2026-09-26,
to generalise it to per-job status for every workflow through GitHub
webhooks (option A of three considered; see Alternatives).

### Technical Environment

- GitHub delivers repository webhooks, signed with HMAC-SHA256 over the
  raw body (`X-Hub-Signature-256`), for `workflow_run` and
  `workflow_job` events among others.
- The managed engine sits behind the conexus edge (nginx plus AuthFilter
  pass-through rules). The edge validates or passes through bearers per
  route.
- The engine exposes `POST /v1/tuples/out`; the board template caps a
  body at 1024 bytes and keeps a post seven days.
- Local-mode engines are not reachable from GitHub. The adapter below is
  what GitHub reaches, and it can write to any engine it can reach with a
  token, so a publicly hosted adapter can serve a managed engine, and an
  adapter on a developer's network can serve a local one.

## Research Findings

### Investigation

Five questions were investigated; each result is a T2 research record.

1. Edge admission for an in-engine receiver (`220-research-1`, conexus-4b
   reading conexus `AuthFilter`, the install-ping handler and `waf.tf`).
   The in-engine receiver was later rejected (Alternative 2); its ingress
   shape rules carry over to the adapter.
2. Adapter hosting (`220-research-2`): AWS Lambda behind a function URL
   in the conexus account.
3. Event volume against the board cap (`220-research-3`): measured from
   real develop runs, about 72 posts per push against the general board's
   500-row cap, which led to the `board/ci/<topic>` template.
4. Webhook payload fields (`220-research-4`): GitHub's machine-readable
   webhook schemas cross-checked against the adapter's field reads.
5. End-to-end security review of the whole chain (`220-research-5`),
   which found the fork job-event gap closed in conexus 5a0d3ad.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| GitHub webhook events (`workflow_run`, `workflow_job`) | No | Payload fields and actions to be confirmed. |
| conexus edge AuthFilter, WAF (in-engine receiver, now Alternative 2) | Yes (conexus-4b, 2026-09-26) | An unauthenticated POST is refused twice today: AuthFilter 401s any request without a bearer, and a WAF rule blocks any request without an Authorization header except exact-path exemptions (`/version`, `/v1/install-ping`). `/v1/install-ping` (conexus-n5n8) is the template: its own edge handler outside AuthFilter (POST only, exact path else 404, 4 KiB body cap, single-hop X-Forwarded-For) plus one more WAF exemption. Pass-through is untouched. |
| Engine tuple out route | Yes | `HttpTupleStore.out` posts `{subspace, keys, dims, body, nonce}`; ids derive from keys and nonce, so a replayed write lands on the same tuple. |

### Key Discoveries

- **Verified**: a board post written by CI reaches subscribed sessions as
  a channel ping with no polling (run 36253520241, both posts).
- **Verified**: a tuple's id derives from its keys and nonce, so a retried
  write is one tuple (`tests/scripts/test_ci_board_post.py`, since retired with the in-workflow publisher).
- **Documented**: `workflow_run` triggers fire only from the default
  branch's copy of a workflow.

### Critical Assumptions

- [x] `workflow_job` events carry job name, run id, run attempt, head
  sha, head branch, status, and conclusion, enough to post without an API
  call — **Status**: Verified — **Method**: Source Search (GitHub's
  machine-readable webhook schemas cross-checked against the conexus
  adapter's field reads; T2 `nexus_rdr/220-research-4`). Payload size is
  undocumented, so the 256 KiB adapter cap is a defensive default.
- [x] A publicly reachable host for the adapter exists that conexus can
  run, with a secret store for the webhook secret and the board token —
  **Status**: Verified — **Method**: Source Search (conexus-4b: AWS
  Lambda function URL, Secrets Manager, terraform under
  `infra/terraform`; T2 `nexus_rdr/220-research-2`)
- [x] Per-push event volume fits the topic's live-row cap with room for
  several pushes a day — **Status**: Verified FALSE for `board/<topic>`,
  resolved by a dedicated template — **Method**: Spike + Source Search
  (T2 `nexus_rdr/220-research-3`). About 72 posts per push against the
  board's 500-row cap: a normal day fills it, and the engine then refuses
  every write (HTTP 429) until rows expire. The `board/ci/<topic>`
  template below carries its own cap and retention.

The earlier edge-admission assumption (an HMAC-authenticated route on the
engine's edge) is withdrawn with the in-engine receiver; its research
(T2 `nexus_rdr/220-research-1`) still informs the adapter's own ingress
rules.

## Proposed Solution

### Approach

A small adapter service sits between GitHub and the engine. GitHub sends
`workflow_run` and `workflow_job` webhooks to the adapter. The adapter
verifies the signature, maps the delivery to a topic, and writes one board
post per state change through the engine's ordinary tuple API, with a
token that can only write board posts (nexus-r3ur5). The engine does not
change: to it, the adapter is one more client writing tuples. Readers
get pings from the board and read CI from check runs (the repo-script fold
was retired 2026-10-03). No workflow file changes.

### Technical Design

- **Adapter**: a standalone HTTP service, outside the engine and outside
  the `nx` package (Sam, 2026-09-26: "an adapter *to* the engine"). One
  route, `POST /github`, accepting GitHub's headers and raw body.
- **Host** (conexus-4b, T2 `nexus_rdr/220-research-2`): an AWS Lambda
  behind a function URL in the conexus account (us-east-1),
  terraform-managed. Runtime python3.13 with the standard library only
  (`hmac.compare_digest`, `json`, `urllib.request`): no dependencies, no
  lock file, one file zipped by terraform. One Secrets Manager secret
  (`conexus/dev/ci-board-adapter`) holds three fields: `webhook_secret`,
  `board_token`, and `github_token` (fine-grained, Actions read-only, for
  the fork check below). The IAM role is scoped to that one ARN and the
  function's log group. The adapter re-reads the secret every 300 seconds,
  so a re-seed needs no redeploy. Logs go to CloudWatch with 30-day
  retention. There is no reserved concurrency: the account Lambda quota is
  10 and AWS keeps 10 unreserved, so the shared account limit is the rate
  cap for now, with a throttles alarm (conexus-lgr8 raises the quota).
  The code, its tests, the
  terraform, and the GitHub-side webhook configuration live in the conexus
  repository (bead conexus-jewq). The terraform-pinned source hash is the
  provenance record; AWS code signing is deferred.
- **No WAF**: a function URL cannot sit behind AWS WAF. The signature is
  the control, and the shape checks below run in code. API Gateway can
  front it later if a WAF is wanted.
- **Ingress rules** (from the edge analysis in T2
  `nexus_rdr/220-research-1`, now applied to the adapter's own ingress):
  POST only; exact path; body read as raw bytes and verified before any
  parse, because the signature covers the exact bytes (a function URL
  delivers the body base64-encoded when `isBase64Encoded` is true, so it
  is decoded exactly once before the HMAC, with a test fixture on that
  path); non-identity `Content-Encoding` refused; body capped near
  256 KiB with 413 beyond; `X-GitHub-Event` allow-list of
  `workflow_run`, `workflow_job`, and `ping`. An allow-list of GitHub's published hook
  address ranges is optional defence in depth; the signature is the real
  control.
- **Signature**: HMAC-SHA256 over the raw body with the webhook secret,
  compared in constant time with `X-Hub-Signature-256`. Unsigned,
  mis-signed, or unmapped deliveries are refused before any write.
- **Template**: a new built-in tuple template `board/ci/<topic>` in the
  engine's resources (one YAML file plus one entry in
  `TemplateRegistry.RESOURCE_TEMPLATE_PATHS`; no engine code). Same shape
  as `board/<topic>` (keys `topic`; dims `from`, `kind`; take disabled;
  1024-byte body) with its own `max_live_rows: 5000` and
  `retention_seconds: 259200` (three days), sized from the measured volume
  (about 72 posts per push, T2 `nexus_rdr/220-research-3`). The engine
  resolves a subspace to a template by segment count, so the three-segment
  `board/ci/<topic>` never collides with the two-segment `board/<topic>`
  (`TemplateRegistry.resolve`), and the `board/` prefix keeps it
  subscribable and delivered by the existing channel path
  (`subscriptions.py` accepts any `board/` subspace; the engine keys no
  board behaviour on the template name). It ships in an engine tag.
- **Per-post lifetime**: the adapter sets `ttl_seconds` per state as a
  second guard: about 6 hours for `queued` and `in_progress`, the template
  retention for `completed`. Amended 2026-10-22 (conexus-efk8, conexus PR
  #582, live from 2026-10-22 12:13Z): `completed` gets 24 hours, because at the template retention the topic held about 4,600 of its
  5,000 live rows and bursts over the cap were refused and lost. The sizing
  above ("sized from the measured volume") did not hold in production; see the
  Revision History entry of that date.
- **Mapping**: the adapter configuration holds one engine URL, one
  board-only token and an allow-list of repositories; a delivery from a
  repository outside the list is refused before any write. Only the topic
  varies, per repository and branch: `board/ci/<repo>-<branch>`.
  Routing different repositories to different engines is deferred. `<repo>` is the
  lowercase repository name without the owner. `<branch>` replaces every
  character outside `[A-Za-z0-9._-]` with `-`, collapses runs of `-`, and
  strips leading characters until `[A-Za-z0-9]`
  (`TemplateRegistry.ADDRESS_SEGMENT_PATTERN`); the topic is capped at 80
  characters. So `develop` gives `board/ci/nexus-develop` and
  `feature/foo` gives `board/ci/nexus-feature-foo`. Only configured
  repositories and the allow-listed event types are accepted.
- **Post**: `dims` carry `from=github` and `kind=run|job`. The board
  template declares only those two dimensions, and the engine refuses an
  undeclared one (`TupleRepository.validateOutShape`), so the state rides
  in the body. The nonce is `X-GitHub-Delivery`, so a redelivered event is
  one post. The body is compact JSON: `state`
  (`queued|in_progress|completed`), workflow, job (for job events), sha,
  run id, attempt, conclusion, URL, within the template's 1024-byte cap.
  Every field is typed or restricted before it is written, because the
  text comes from GitHub (the prompt-injection boundary): sha is 40 hex
  or blank; run and attempt are integers or null; conclusion is GitHub's
  enum or null; URL matches only
  `https://github.com/<repo>/actions/runs/N[/job/N][/attempts/N]` or is
  blank; workflow and job names are cut to 64 characters of
  `[A-Za-z0-9 ._()/:#+-]`, anything else becoming `?`.
  Adding `state` to the board template later is an additive engine
  change; the adapter would then set both.
- **Retry**: GitHub does NOT redeliver a failed delivery on its own
  ("Handling failed webhook deliveries", docs.github.com), and a delivery
  not answered within 10 seconds fails. So the adapter retries the tuple
  write itself (three attempts, short backoff), inside a 9-second Lambda
  timeout split as a 2-second origin lookup plus a 6-second write budget,
  answers 2xx only after a write succeeds, and otherwise answers 5xx so
  the delivery shows red in GitHub's delivery log, and logs the delivery
  id. A manual or API redelivery keeps the same delivery id, so the nonce
  still makes it one post. Automatic recovery (a scheduled sweeper that
  lists failed deliveries and redelivers them) needs a fourth, broader
  secret, a GitHub token with webhook administration (the third field is
  already the read-only fork-check token), and is deferred. Since the fold's
  retirement, a gap shows in the adapter's alarms and GitHub's delivery log;
  the board is a ping source only, and readers take CI from check runs.
- **Replay**: GitHub signs no timestamp, so a captured delivery can be
  replayed. The delivery-id nonce makes an exact replay land on the same
  tuple while that tuple lives (24 hours for a completed post, three days
  before 2026-10-22; queued and in-progress posts expire after 6 hours). A replay after that writes a stale post. No verdict is read from the
  board any more (readers use check runs), so a replayed post can only send
  a misleading ping; its age shows it is stale.
- **Branches**: the allow-list defaults to `develop` and `main` and may not
  be empty (terraform refuses it), so the topic count stays bounded.
- **Observability** (conexus side): an hourly self-check proves the secret
  loads, the board token passes the edge, and the GitHub token reads runs
  (a 403 counts as healthy only with `SELF_CHECK_ACCEPT_403`, for the
  board-only token). Alarms go to `conexus-security-alerts`: post failed,
  origin unverified, signature refused, errors, throttles, slow (over 8
  seconds), self-check missing (heartbeat), and a branch-dropped burst. A
  dashboard and saved queries sit beside them. `activate.sh` is dry-run by
  default.
- **Engine and edge**: unchanged. The adapter's writes are ordinary
  bearer-authenticated `POST /v1/tuples/out` calls through existing
  pass-through, metered like any client.
- **Reader fold** (retired 2026-10-03 by aed1a2015; see Revision History): a repo-local script, `scripts/ci_status.py <sha>`,
  reads the topic with the ordinary tuple read, keeps the latest state
  per (workflow, job, attempt), and prints what is green, pending, and
  failed. It is a development-workflow helper beside
  `scripts/ci_board_post.py`, deliberately NOT an `nx` command or MCP
  tool (Sam, 2026-09-26). A session reads CI posts with `tuple_rd` like
  any other board. A read finds a sha's completed posts only while they
  live, 24 hours (three days before 2026-10-22), after which it returns
  nothing for that sha.
- **Coexistence**: the nexus-dotwy jobs keep running until the adapter is
  live, then retire.

### Security

The whole chain was reviewed end to end on 2026-09-26 (T2
`nexus_rdr/220-research-5`; detail in T2
`conexus/rdr-220-ci-board-whole-chain-security-review-2026-09-26`).

- **Untrusted input.** Post bodies carry GitHub-controlled strings. On a
  public repository a fork chooses its branch name and its workflow and
  job names. The adapter types or restricts every field: sha is 40 hex,
  run and attempt are integers, conclusion is GitHub's enum, the URL must
  match `https://github.com/<repo>/actions/runs/...`, and workflow and job
  names are cut to 64 characters of `[A-Za-z0-9 ._()/:#+-]`. A reader
  still treats a board body as data, never as instructions.
- **Fork job events (ship-blocker, fixed before activation).**
  `workflow_run` payloads carry the head repository, so fork runs are
  dropped. `workflow_job` payloads do not. Without a check, a fork PR on a
  branch named `develop` would post into `board/ci/nexus-develop`: its
  names reach the topic sessions trust, and because `max_live_rows` is
  enforced per subspace it could fill the cap so real posts are refused
  (HTTP 429), which GitHub never redelivers. The adapter therefore looks
  up each job's run with the GitHub API (`GET
  /repos/<repo>/actions/runs/<run_id>`), posts only when
  `head_repository` equals the repository, caches the answer per run, and
  fails closed on an API error. This needs a fine-grained, read-only
  Actions token. The repository setting "Require approval for all
  outside collaborators" is the second layer, and it is on: `GET /repos/Hellblazer/nexus/actions/permissions/fork-pr-contributor-approval`
  returned `{"approval_policy":"all_external_contributors"}` on 2026-09-26.
- **Topic count.** With the fork check, only same-repository branches
  post. The adapter's branch allow-list defaults to `develop` and `main`,
  and an alarm reports dropped unmapped branches.
- **Tokens.** The adapter secret holds three fields (webhook secret,
  board token, read-only GitHub token). The adapter's engine token is the board-only scope
  (nexus-r3ur5), a precondition for activation. The in-workflow
  `NX_BOARD_TOKEN` is readable only by push-to-develop jobs whose one
  third-party action is pinned by SHA; fork pull requests cannot reach it.
- **Tenancy.** The engine derives the tenant from the bearer and ignores
  tenant headers, so a post cannot be steered into another tenant.
- **Delivery to sessions.** A channel ping names only the subspace and
  tuple id, never the body, and the drain hook never renders board
  bodies. Board content reaches a model only through an explicit
  `tuple_rd`.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Board write | engine tuple out (`/v1/tuples/out`) | Reuse unchanged, as an ordinary client. |
| Topic capacity | `board/<topic>` template (500 live rows, 7 days) | Extend: add the `board/ci/<topic>` template with its own cap and retention; the existing template is untouched. |
| Board-only credential | nexus-r3ur5 (engine token scope) | Reuse: the adapter's token is exactly that scope. |
| Delivery to sessions | RDR-211 channel, `tuple_subscribe` | Reuse unchanged. |
| Status fold | `scripts/ci_board_post.py` `verdict_from_results` | Extended into `scripts/ci_status.py`; both retired 2026-10-03 (aed1a2015). |
| Per-run publisher | `ci.yml` board jobs | Replace once the adapter is live. |

### Decision Rationale

GitHub already emits every state change of every job of every workflow.
Consuming those events costs no runner time, needs no per-workflow code,
and reports each transition within seconds. The tuple space already
delivers one post to many sessions, and already accepts writes from any
authorized client. So the new part is an adapter from one protocol to the
other, and it belongs outside the engine: the engine stays a tuple space
with no knowledge of GitHub, the webhook secret never touches the engine
or its edge, and the adapter can be replaced or run per site without an
engine release.

## Alternatives Considered

### Alternative 1: A generic watcher job in each workflow

**Description**: one identical job per workflow reads the run's own job
list with the workflow's `GITHUB_TOKEN` and posts each transition.

**Pros**:

- No new ingress; works with a local-only engine reachable from runners.

**Cons**:

- Holds a runner for the whole run (about 25 minutes per develop run).
- Still one edit per workflow.
- Polls, at about 30-second resolution.

**Reason for rejection**: pays runner minutes to watch a run, against
CI Cost Discipline, and keeps per-workflow wiring.

### Alternative 2: The receiver inside the engine

**Description**: an engine route `POST /v1/hooks/github/{hook_id}` with a
registration table, HMAC verification in the engine, and a new edge
handler and firewall exemption (the first draft of this RDR).

**Pros**:

- No new service to host.

**Cons**:

- Puts GitHub-specific protocol handling and a new public, unauthenticated
  route into the engine and its edge.
- Ships in an engine tag and needs conexus edge and firewall work.

**Reason for rejection**: Sam, 2026-09-26: the receiver is an adapter to
the engine, so it has no reason to live in it.

### Briefly Rejected

- **A post step in every job**: the hand-weaving Gap 2 describes.
- **`workflow_run`-triggered observer workflow**: runs only from `main`'s
  copy, so dormant for `develop` work.
- **One central poller publishing to the board** (a scheduled job, or a
  session elected by a `lock/` tuple): the only other way to get CI state
  out of GitHub besides webhooks, since GitHub offers only push or pull.
  Dropped (Sam, 2026-09-26): it adds poll latency and API use, and the
  session-elected form reports only while some session runs.

## Trade-offs

### Consequences

- Positive: per-job status for every workflow with no workflow edits.
- Positive: zero GitHub API calls from readers for CI status. Retired with the fold on 2026-10-03: readers now make one `gh api .../check-runs` call per sha.
- Positive: no engine or edge change; the engine never sees GitHub.
- Negative: one more small service to host, with one secret of three
  fields (webhook secret, board token, read-only GitHub token).

### Risks and Mitigations

- **Risk**: a forged delivery writes false status.
  **Mitigation**: HMAC verification over the raw body before any write;
  refuse unsigned or unmapped deliveries.
- **Risk**: a leaked adapter token writes or reads beyond the board.
  **Mitigation**: the nexus-r3ur5 board-only scope.
- **Risk**: event volume floods the board.
  **Mitigation**: size against the live-row cap (assumption 3); a topic
  per repository and branch.

### Failure Modes

A dropped or refused delivery leaves a job's state stale on the board;
GitHub's delivery log shows the failure and allows redelivery. A down
adapter means no posts; the adapter's alarms show it, readers still have
check runs, and GitHub keeps the failed deliveries for redelivery.

The fork gate adds a GitHub-API dependency to every job event. For each
`workflow_job` delivery the adapter reads the run from GitHub
(`GET /repos/{repo}/actions/runs/{id}`, 2-second timeout) to confirm the
run's head is in the repository itself. Only definite answers are cached,
per warm container. When that read fails (a rate limit, an expired or
revoked token, a timeout), the adapter fails closed: it answers the
delivery `502 origin_unverified` and posts nothing, so that job's state is
missing from the board. GitHub does not redeliver on its own. Detection is
the `origin_unverified` alarm (and the hourly self-check for a token that
lost access); recovery is a manual redelivery from the webhook's delivery
log once GitHub answers again.

## Implementation Plan

### Prerequisites

- [x] All Critical Assumptions verified (see Critical Assumptions)
- [x] conexus-4b agrees on the adapter's host and secret store (T2 `nexus_rdr/220-research-2`)
- [x] Adapter job-event run-origin check (GitHub API, fail closed) merged and re-reviewed (conexus 5a0d3ad, re-review mutation-verified; follow-ups merged as ffe499f)
- [x] Outside-collaborator approval policy set on Hellblazer/nexus (Sam: `all_external_contributors`, set and read back)
- [x] nexus-r3ur5 (`board-ci` scope) shipped in an engine tag and deployed (engine-service-v0.1.134) (Sam decided the adapter waits for it; no interim tenant token)
- [x] Sam issues the fine-grained, read-only Actions token into 1Password (adapter activated 2026-09-27, T2 conexus/ci-board-adapter-activation-2026-09-27)

### Minimum Viable Validation

A push to `develop` produces, on `board/ci/nexus-develop`, one post per
job transition of `CI` and `service-ci`, and `scripts/ci_status.py <sha>`
shows each job's final state matching GitHub's check runs for that sha.
Passed on run efdc06800, before the fold was retired.

### Phase 1: Code Implementation

#### Step 1: `board/ci/<topic>` template (engine, this repository)

Done: 79a2386c7, shipped in engine-service-v0.1.133.

#### Step 2: Board-only token scope (engine, this repository, nexus-r3ur5)

A `board-ci` scope admitted only on `POST /v1/tuples/out` for subspaces
whose template is `board/ci/<topic>`. Done: shipped in engine-service-v0.1.134
(`service-tokens-006-board-ci-scope.xml`, `BoardCiTokenScopeTest`).

#### Step 3: Adapter (conexus repository, conexus-jewq)

Ingress rules, signature check, fork check, mapping, post shape, retry
contract, self-check and alarms. Done: conexus PR #394 and #396, merged
as ffe499f.

#### Step 4: Status fold script in `scripts/` (this repository)

Done: `scripts/ci_status.py`, c0a5cfef4; the MVV run efdc06800 used it.
Retired 2026-10-03 by aed1a2015 (cleanup step 8) together with its tests.

### Phase 2: Operational Activation

#### Activation Step 1: Deploy the engine tags

v0.1.133 (the template) and the next tag (the `board-ci` scope) are
deployed to the managed engine by conexus. Check: `tuple_registry` lists
`board/ci/<topic>`.

#### Activation Step 2: Seed and enable the adapter (conexus, each live step on Sam's go)

`activate.sh` apply, then seed with a `board-ci` token and the
fine-grained read-only GitHub token, then add the repository webhook,
then `--check`.

#### Activation Step 3: Retire the `ci.yml` board jobs

After the adapter's posts are confirmed on `board/ci/nexus-develop`,
remove the in-workflow publisher and move `AGENTS.md` worktree rule 7 to
the new topic. Done (5ac039222). Rule 7 later moved again, to check runs,
when the fold was retired (aed1a2015).

The two topics look alike but are different subspaces under different
templates. `board/ci-develop` (two segments, `board/<topic>`) holds the
in-workflow publisher's `ci-pending` and `ci-verdict` posts, one pair per
run. `board/ci/nexus-develop` (three segments, `board/ci/<topic>`) holds
the adapter's per-run and per-job posts. During the coexistence window a
session subscribes to whichever it reads; nothing forwards one into the
other. (The window ended with 5ac039222.)

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Adapter secret (`webhook_secret`, `board_token`, `github_token`) | In scope | In scope | In scope | In scope (`activate.sh --check`, hourly self-check) | N/A |
| Previous board token after a re-seed | In scope | In scope | In scope (`activate.sh --revoke-previous`) | In scope | N/A |
| Repository-to-topic mapping | In scope | In scope | In scope | In scope | N/A |

### New Dependencies

None. The adapter uses the Python standard library only.

## Test Plan

- **Scenario**: a correctly signed `workflow_job` completed delivery — **Verify**: one post with the job's conclusion.
- **Scenario**: the same signed delivery with `isBase64Encoded` true — **Verify**: verified after exactly one decode; one post.
- **Scenario**: the tuple write fails on every attempt — **Verify**: three attempts within the budget, then 5xx and the delivery id logged; a manual redelivery then writes exactly one post.
- **Scenario**: the same delivery id delivered twice — **Verify**: one post.
- **Scenario**: a bad or missing signature — **Verify**: refused, nothing written.
- **Scenario**: an unmapped repository — **Verify**: refused, nothing written.
- **Scenario**: a rerun of a failed job (new attempt) — **Verify**: the post carries the new attempt number (conexus `test_rerun_attempt_is_carried_in_body`).

## Validation

### Testing Strategy

1. **Scenario**: the template resolves apart from `board/<topic>` and
   carries its own cap and retention.
   **Expected**: `TemplateRegistryTest.ciBoardResolvesApartFromTheGeneralBoard`
   passes (engine suite).
2. **Scenario**: a `board-ci` bearer writes to `board/ci/<topic>` and is
   refused everywhere else.
   **Expected**: `BoardCiTokenScopeTest` and the `TokenAdminHandler` tests
   pass against a real engine.
3. **Scenario**: the adapter's signature, fork check, typed fields,
   retry contract and self-check.
   **Expected**: the adapter's pytest and tftest suites pass (conexus),
   with the fork gate mutation-checked.
4. **Scenario**: the fold reads adapter-shaped posts across pages.
   **Expected**: `tests/scripts/test_ci_status.py` passes against a real
   engine. Passed at c0a5cfef4; the script and its test were retired 2026-10-03.
5. **Scenario**: the Minimum Viable Validation on the live system.
   **Expected**: one post per job transition on `board/ci/nexus-develop`,
   and `scripts/ci_status.py <sha>` matching GitHub's check runs. Passed on
   run efdc06800, before the fold was retired.

### Performance Expectations

N/A. Volume, not speed, was the constraint, and it is sized in
`220-research-3`.

## Finalization Gate

### Contradiction Check

No contradictions found between the research findings and the proposed
solution. Two findings changed the design, and the design follows them:
the board cap finding (`220-research-3`) produced the `board/ci/<topic>`
template, and the security review (`220-research-5`) produced the fork
check. The in-engine receiver analysed in `220-research-1` is recorded as
a rejected alternative, not as the design.

### Assumption Verification

All three Critical Assumptions are resolved: payload fields verified
(`220-research-4`), hosting verified (`220-research-2`), and the volume
assumption verified FALSE for `board/<topic>` and resolved by the new
template (`220-research-3`). No assumption remains unverified.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| GitHub `workflow_job` / `workflow_run` webhooks | GitHub | Source Search (octokit webhook schemas + adapter reads) |
| GitHub `GET /repos/{repo}/actions/runs/{id}` (fork check) | GitHub | Source Search (adapter code, mutation-checked tests) |
| Engine `POST /v1/tuples/out` | nexus engine | Source Search + real-engine tests |

### Scope Verification

The Minimum Viable Validation was in scope and ran at Activation Step 2,
once the adapter was live (2026-09-27), with `scripts/ci_status.py`
compared against GitHub's check runs on run efdc06800. Activation is done.
The fold was retired afterwards, and Gap 3 is dropped.

### Cross-Cutting Concerns

- **Versioning**: the engine ships a template file (v0.1.133) and the
  `board-ci` scope (next tag); nothing ships in the `nx` client beyond the
  `--scope board-ci` choice. The adapter versions and deploys on its own;
  the fold was a repo script, retired 2026-10-03.
- **Secret/credential lifecycle**: one Secrets Manager secret with three
  fields, re-read every 300 seconds; rotation by re-seed and
  `activate.sh --revoke-previous`; the hourly self-check proves each field.
- **Deployment model**: one adapter per site; it writes to whichever
  engine its configuration names. Local-only setups have no CI board
  publisher; the in-workflow publisher was retired (5ac039222).
- **Incremental adoption**: the in-workflow publisher keeps running until
  the adapter is confirmed, then retires.

### Proportionality

The document is larger than the code it describes, because the security
review and the drift corrections are recorded in full. The Security
section and the Technical Design carry the decisions; the research
records carry the detail. No section needs trimming before acceptance.

## References

- T2 `nexus/github-api-usage-research-2026-09-26`
- `scripts/ci_board_post.py`, `.github/workflows/ci.yml` board jobs (both retired, 5ac039222)
- `web/ci-board.html` (now a redirect, f01ec0402)

## Revision History

- 2026-09-26: Gate round 1 — PASSED (0 Critical, 2 Significant, 0 ship-blocker(s)); commit `3059b5110`; critique `nexus_rdr/220-gate-critique-2026-09-26-r1`.
- 2026-09-26: Gate round 2 — PASSED (0 Critical, 1 Significant, 0 ship-blocker(s)); commit `0538478bc`; critique `nexus_rdr/220-gate-critique-2026-09-26-r2`.
- 2026-09-26: Gate round 3 — PASSED (0 Critical, 0 Significant, 0 ship-blocker(s)); commit `e575f38d8`; critique `nexus_rdr/220-gate-critique-2026-09-26-r3`.
- 2026-10-22: Post-accept amendment (conexus-efk8, conexus PR #582, merged as afe4c0e85 and live from 2026-10-22 12:13Z; the first CI burst after it posted 63 tuples with 0 failures, per the conexus session): the adapter sets `ttl_seconds` 86400 on completed posts instead of the template's three-day retention. `board/ci/nexus-develop` sat at about 4,600 of its 5,000 live-row cap (about 1,300 completed posts a day held three days, plus 6 hours of in-flight posts), so the template sizing in Technical Design and Critical Assumption 3 did not hold, and bursts over the cap were refused: 104 posts lost on 2026-10-20 and 626 on 2026-10-21 (counts from the conexus session), which GitHub does not redeliver. Consequences: completed history readable on the board (the reader fold, `tuple_rd`), and the exact-replay window for a completed post, become one day. The template's own retention and cap are unchanged (the comment in `board-ci.yaml` still describes the old sizing). The alternative conexus named is raising the template cap in an engine release, after which it would restore the three-day TTL.
- 2026-10-31: close audit (critique `nexus_rdr/220-closure-critique-2026-10-31`). Commit aed1a2015 (2026-10-03, cleanup step 8, no bead) deleted the reader fold `scripts/ci_status.py` and its tests, and AGENTS.md rule 7 now reads CI from `gh api .../check-runs`, one call per sha. So Gap 3 is open again. Whether to drop it or build a replacement reader is Sam's decision. The prerequisite boxes and the Step 2 status are brought current, and the fold's sites are marked retired.
- 2026-10-31: Sam ruled on Gap 3 (decision page, T2 `nexus/sam-decision-pass-2026-10-31`): dropped. Readers take CI from one `gh api .../check-runs` call per sha, as AGENTS.md rule 7 says. No replacement reader will be built. The board posts stay as an optional ping source.
