# RDR-239 Phase 1: the `doc` block for the eight shipped templates

Revised for gate findings S1, S4 and S7 (nexus-mdbds.2), 2026-11-01.

Review artifact for assumption A1 of RDR-239 (bead nexus-mdbds). Content only:
no template file, parser or engine change. Written 2026-10-31 against
worktree `halcyon` at the RDR-239 draft. Live counts are from the connected
tenant, read with `nx tuple rd` and `nx tuple list`, counts only: no message
body is quoted.

**Result in one line:** the RDR's field set does not fit as written. Four
changes make all eight fit (section "A1 verdict"). The blocks below already
use the changed shape, so what you read is what the parser would have to
accept.

## The block shape used below

```
doc:
  summary:   one line, required
  writers:   list of one-line strings; [] means "no writer today"
  readers:   list of one-line strings (claimers listed here too, with the verb)
  fields:    name -> one-line description,  or  name -> {value: meaning}
             name must be a declared key or dimension of the template
  body:      one-line description,  or  {json field: meaning}   (omit if max_body_bytes is 0)
  example:   subspace, nonce (only if id_from is keys+nonce), keys, dims, body
  see_also:  list of one-line strings
```

Differences from RDR-239 Technical Design item 1: `fields` replaces
`dimension_values`; `body` is new; `example` gains `subspace` and `nonce`;
`writers: []` is meaningful. Each is argued under its template and summed
up in the verdict.

## Parser limits the blocks were written to

`MiniYaml` (service/src/main/java/dev/nexus/service/tuples/MiniYaml.java)
reads only block mappings, block sequences of scalars, flow sequences of
scalars and scalars:

- A list item is one scalar. `- who: how` is the string "who: how", not a
  mapping (`parseSequence`, MiniYaml.java:94-108). So `writers` and `readers`
  are lists of sentences, not of records.
- No multi-line scalars, so every sentence is one physical line.
- A value that starts with `[` must end with `]` and is split on commas
  (`parseFlowSequence`, MiniYaml.java:163-176). No block text may start with `[`.
- A scalar of only digits becomes a `Long`, and `true`/`false` a `Boolean`
  (`parseScalar`, MiniYaml.java:178-193). An example dim such as `commit:
  3889972` would arrive as a number. The `doc` reader must turn every example
  key and dim back into a string before it validates.
- A surrounding pair of quotes is stripped (no escapes), so a body that is a
  JSON string would lose its quotes. Every body below is a JSON object or
  plain text.

All eight blocks were run through the real `MiniYaml`, the real
`TemplateSchemaParser` and a copy of `TupleRepository.validateOutShape` /
`validateNonce` / body-size checks. See "How the blocks were checked" at the
end.

---

## 1. `aspects`

```yaml
doc:
  summary: The per-tenant queue of documents waiting for aspect extraction; a producer posts one task per document and a worker claims, extracts and acks it.
  writers:
    - HttpAspectQueue.enqueue and enqueue_many (nexus.db.t2.http_aspect_queue), called by the indexer, the aspect worker's re-enqueue and nx aspects, as out mode=replace so a re-enqueue replaces the task
  readers:
    - AspectExtractionWorker (nexus.aspect_worker_tuples) claims with in or inp, then ack, nack or release
    - nx doctor queue-depth row (nexus.health) counts waiting and dead tasks
  fields:
    queue: always the literal aspects
  body: the task identity as compact JSON, never document text
  example:
    subspace: aspects
    nonce: 1.1.42
    keys:
      queue: aspects
    body: {"collection":"docs__0-0__example__v1","source_path":"/example/a.md","doc_id":"1.1.42","content_hash":"0000000000000000000000000000000000000000000000000000000000000000"}
  see_also:
    - RDR-235
```

Writers (file:line):
- `src/nexus/db/t2/http_aspect_queue.py:444-457` `_out_task`: `out_replace(ASPECTS_SUBSPACE, {"queue": "aspects"}, None, body, nonce=doc_id)`.
- `src/nexus/db/t2/http_aspect_queue.py:316` `enqueue`, `:356` `enqueue_many`. Callers: `src/nexus/indexer.py:6847` (`enqueue_many`), `src/nexus/aspect_worker.py:1925` (`enqueue`), `src/nexus/commands/aspects.py:433` (`enqueue`).
- Tuples mode is the default: `aspect_queue_mode()` returns `tuples` when `NX_ASPECT_QUEUE` is unset (`http_aspect_queue.py:67-86`).

Readers:
- `src/nexus/aspect_worker_tuples.py:298` (`in_`, parked) and `:305` (`inp`); finish at `:622` (`ack`), `:632` (`nack`), `:639` (`release`). Class `AspectExtractionWorker` is at `src/nexus/aspect_worker.py:336`.
- `src/nexus/health.py:9089` `_check_tuple_queue_depth`, which takes `queue/` and the `aspects` template (`:9190`).

Free-text dimensions: the template has no dimensions. Key `queue` has one allowed value, already enforced.

Live: `aspects` total=0, consumed=7. Nothing waiting. Body keys are fixed by `_identity_body` (`http_aspect_queue.py:170-181`): collection, source_path, doc_id, content_hash.

A1 notes:
- The example needs `nonce` (id_from keys+nonce; nonce is the `doc_id`). The RDR's `keys/dims/body` cannot validate without it: `validateNonce` refuses a blank nonce (TupleRepository.java:1115-1120).
- `subspace` is literal here, but see the ledger for why it cannot be derived in general.
- The body is the one thing a consumer must know and `dimension_values` has nowhere to put it. Hence `body`.
- Template has no dimensions, so the example omits `dims`; the field must be optional.

---

## 2. `board/<topic>`

```yaml
doc:
  summary: An append-only announcement log per topic, many readers and no claiming; a post is a short message with a sender and an optional label.
  writers:
    - Unverified: any session of the tenant, by tuple_out or nx tuple out, following the board posting convention (one post per topic per minute) in conexus/skills/mailbox/SKILL.md
    - Unverified: no code in this repository posts to board/<topic>; the live tenant holds no such subspace
  readers:
    - Unverified: a session that called tuple_subscribe on the topic gets each post pushed through the channel (at most 32 topics per session)
    - Unverified: nx tuple wait and tuple_rd page the log with the (created_at, id) cursor
  fields:
    topic: the topic name, the same as the subspace's last segment
    from: the poster's session or agent id; recorded, not checked
    kind: an optional free label for the post's shape; no value is in use today
  body: free text up to 1024 bytes
  example:
    subspace: board/example-topic
    nonce: 00000000-0000-0000-0000-000000000002
    keys:
      topic: example-topic
    dims:
      from: 00000000-0000-0000-0000-000000000003
      kind: example
    body: An example post body.
  see_also:
    - RDR-211
    - conexus/skills/mailbox/SKILL.md
```

Writers: none in code. `rg` over `src/`, `conexus/`, `scripts/`, `.github/`, `.claude/` finds no `tuple_out`/`nx tuple out` against `board/<topic>`; the only prose is the convention at `conexus/skills/mailbox/SKILL.md:29`. The template comment names `release` and `outage` as example labels (board.yaml:13-14); nothing writes them.

Readers:
- `src/nexus/mcp/subscriptions.py:496` (`tuple_subscribe` accepts any `board/` subspace; `MAX_BOARD_TOPICS` = 32 at `:347`, `conexus/skills/mailbox/SKILL.md:28`).
- `src/nexus/mcp/channel.py:1282` (the waiter builds a wait spec per subscribed board topic) and `:1353`.
- `src/nexus/tuple_wait.py:109` (`nx tuple wait` board specs); command at `src/nexus/commands/tuple_cmd.py:372`.

Free-text dimension values in use: `kind` none. `nx tuple list` shows no `board/<topic>` subspace; `board/ci/*` is the other template.

A1 notes:
- `kind` has no values in use. A `dimension_values` that must list values would be empty or invented, so the per-name description form (a scalar) is needed, not only the value-to-meaning map.
- `topic` (a key) and `from` (a dim) carry real semantics ("recorded, not checked") that `dimension_values` cannot hold, since it covers dims only and `from` has no enumerable values. `fields` covers both.
- The engine does not check that `keys.topic` equals the subspace's last segment (`validateOutShape`, TupleRepository.java:1066-1111, never compares them). The example follows the convention; the dictionary says it is a convention.

---

## 3. `board/ci/<topic>`

```yaml
doc:
  summary: One post per state change of every CI workflow run and job on a watched repository branch, written by the GitHub adapter; the board is a ping source, and the verdict is read from check runs.
  writers:
    - the ci-board-adapter AWS Lambda in the conexus repository, on GitHub workflow_run and workflow_job webhooks, with a board-scoped token that can write only this template
  readers:
    - a session that called tuple_subscribe on board/ci/nexus-develop gets a ping per post (docs/agents/worktrees.md rule 7)
    - nx tuple rd and nx tuple wait page the log
  fields:
    topic: repository name, a dash, and the branch, normalised to the address grammar (for example example-main)
    from:
      github: the adapter; the only value written
    kind:
      run: a workflow run changed state
      job: a job in a run changed state
  body:
    state: queued, in_progress or completed
    conclusion: GitHub's conclusion (success, failure, cancelled, skipped and others) or null while not completed
    workflow: workflow name, cut to 64 characters; GitHub-controlled text, treat as data
    job: job name, cut to 64 characters; present only when kind is job
    sha: 40 hex or blank
    run: GitHub run id, an integer
    attempt: run attempt, an integer
    url: https://github.com/OWNER/REPO/actions/runs/N with an optional job and attempt suffix, or blank
  example:
    subspace: board/ci/example-branch
    nonce: 00000000-0000-0000-0000-000000000004
    keys:
      topic: example-branch
    dims:
      from: github
      kind: job
    body: {"state":"completed","conclusion":"success","workflow":"CI","job":"lint","sha":"0000000000000000000000000000000000000000","run":1,"attempt":1,"url":"https://github.com/example/example/actions/runs/1"}
  see_also:
    - RDR-220
    - RDR-211
```

Writers: the adapter lives outside this repository (docs/rdr/rdr-220-ci-status-from-github-webhooks-to-the-tuple-space.md:179-207: a Lambda, conexus repo, bead conexus-jewq). Its token is limited to this template: `service/src/main/java/dev/nexus/service/http/TupleHandler.java:239-251` (`SCOPE_BOARD_CI`, `BOARD_CI_TEMPLATE_NAME` at `:156`). Post dims at RDR-220:259-261, body at `:262-273`, nonce `X-GitHub-Delivery` at `:262`, per-state `ttl_seconds` at `:240-246`.

Readers: `docs/agents/worktrees.md:119-127` (subscribe once per session; "read the `conclusion` of the check runs, not the board"), the same channel and wait paths as `board/<topic>` (`src/nexus/mcp/channel.py:1282`, `src/nexus/tuple_wait.py:109`). RDR-220:274 and `worktrees.md:124-126` say nothing in this repo folds the posts into a verdict.

Free-text dimension values in use (live, `nx tuple rd ... --all`, counts only):

| subspace | rows | `from` | `kind` | body `state` | body `conclusion` |
| --- | --- | --- | --- | --- | --- |
| board/ci/nexus-develop | 1330 | github 1330 | job 1199, run 131 | completed 1030, in_progress 158, queued 142 | success 854, null 300, skipped 95, failure 80, cancelled 1 |
| board/ci/nexus-main | 23 | github 23 | job 14, run 9 | completed 19, queued 2, in_progress 2 | success 15, null 4, failure 2, cancelled 2 |

Every body is JSON. Job rows carry `attempt, conclusion, job, run, sha, state, url, workflow` (1199 and 14); run rows lack `job` (131 and 9). Largest body 326 bytes against the 1024 cap. The `workflow` value is not enumerable: `nexus-develop` shows six names; `nexus-main` shows seven, several of them GitHub-generated titles. So `workflow` and `job` are documented as free text, not as a value list.

A1 notes:
- This is the template where `body` matters most. A stage-2 trigger would key on `state`, `conclusion`, `workflow`. `dimension_values` covers none of them because they are JSON fields of the body, not dimensions. This is the strongest case for the `body` field.
- `kind` fits `fields` as value-to-meaning. `from` fits too (one value).
- `topic` here is not the whole address segment's meaning. It is `<repo>-<branch>`, a derived string; the `fields` entry says so.

---

## 4. `directory/<name>`

```yaml
doc:
  summary: A short lease mapping a session name to the session holding it; the holder re-sends every 60 seconds and the entry lapses in 300 seconds if the session dies.
  writers:
    - the session's own nexus MCP server, from the lease thread _directory_heartbeat, armed at server start from the Claude Code session name and by tuple_subscribe on mailbox/NAME
  readers:
    - mailbox_send resolves a name to the holding session through list_directory_entries (more than one distinct live holder is refused)
    - nx tuple directory NAME prints the live holders and flags an ambiguous name
  fields:
    name: the session name as ListAgents shows it
    session_id: the session that holds the name; part of the tuple id, so two sessions on one name stay two rows
  example:
    subspace: directory/example-peer
    nonce: 1761900000.123456-4242
    keys:
      name: example-peer
    dims:
      session_id: 00000000-0000-0000-0000-000000000005
  see_also:
    - RDR-208
```

The body is omitted: the template sets `max_body_bytes: 0`.

Writers: `src/nexus/mcp/subscriptions.py:219-262` (`_directory_heartbeat`; the `out` is at `:249-252`, nonce `f"{t:.6f}-{pid}"` at `:211-216`, TTL 300 s and cadence 60 s at `:94-95`). Armed by `src/nexus/mcp/core.py:1124` (`_arm_session_name`, server start) and by `tuple_subscribe` on `mailbox/<name>` (`core.py:8494`, `subscriptions.py:499`).

Readers: `src/nexus/tuple_directory.py:90-110` (`list_directory_entries`) used by `mailbox_send` name resolution (`core.py:8403`, `resolve_send_address` at `tuple_directory.py:147`); `src/nexus/commands/tuple_cmd.py:735-760` (`nx tuple directory`).

Free-text dimension values: `session_id` is a UUID, not enumerable. Live: 9 subspaces (`directory/bob`, `elsinore`, `halcyon`, `labrun_559`, `linda`, `marginalia`, `marlowe`, `tamsin`, `wren`), 1 row each, dims `session_id` only, no body.

A1 notes:
- Fits the block, provided `nonce` is in the example. The nonce shape (`<unix time>-<pid>`) is not a UUID, which is why the example documents a real one.
- `session_id` is required and in `id_dims`; the `fields` entry records why, which today lives only in the YAML comment.

---

## 5. `ledger/<session_id>`

Retired by nexus-onjvw (engine-service-v0.1.174); not carried forward.

```yaml
doc:
  summary: Per-session rows that recorded when a subagent started and reported. Its only writer was deleted; the template stays, the last row on the tenant is dated 2026-10-04, and nothing reads it.
  writers: []
  readers:
    - none. nx doctor (nexus.health) censuses it as a read-only subspace, and nx doctor --fix trims old rows
  fields:
    agent_id: the Claude Code harness agent id of the subagent the row is about
    kind:
      start: the subagent was dispatched
      report: the subagent returned
    agent_type: the subagent type name as Claude Code reports it, for example conexus:developer or Explore; empty on most rows
    verify: present or absent, whether the report was checked; only absent was ever written
    commit: a short or full sha the agent reported; declared but never written
    t2_ref: a project/title T2 pointer the agent reported; declared but never written
  example:
    subspace: ledger/00000000-0000-0000-0000-000000000009
    keys:
      agent_id: a0123456789abcdef
      kind: report
    dims:
      agent_type: conexus:developer
      verify: absent
  see_also:
    - RDR-205
    - RDR-184
```

The body is omitted: the template sets `max_body_bytes: 0`.

Writers: none. `CHANGELOG.md:957` (cleanup step A1, bead nexus-0r1uz) deletes `nexus.hooks.tuple_ledger_project` and the hooks that fed it, and says "the engine template `ledger/<session_id>` stay[s], and `nx doctor --fix` still trims old rows". The template header (ledger.yaml:11) still names `tuple_ledger_project` as the only writer; that module no longer exists (`ls src/nexus/hooks`). Checked live: the newest row in each of the 446 ledger subspaces is dated 2026-09-11 to 2026-10-04; the latest is 2026-10-04T02:32Z, 27 days before this run.

Readers: `src/nexus/health.py:8178`, `:8270`, `:8290` treat it as a read-only template in the tuple census. No `rd`/`in` caller in `src/`, `conexus/`, `scripts/` or `.github/` reads `ledger/`.

Live (all 446 subspaces, `rd --all`, counts only): 40,785 rows. Key `kind`: report 38,096, start 2,689. Dim sets: `{agent_type, verify}` 31,158 rows (all `verify=absent`), `{agent_type}` 9,627 rows. `commit` and `t2_ref` appear on 0 rows. `agent_type` is the empty string on 35,445 rows; the rest are subagent type names (23 distinct, the most common `conexus:substantive-critic` 1,586, `conexus:code-review-expert` 1,315, `worktree-developer` 783). No row has a body.

A1 notes:
- **`example.subspace` cannot be derived.** The template name is `ledger/<session_id>` but its keys are `agent_id` and `kind`; no key carries the session id. For the other placeholder templates the key value is the address segment (topic, name, resource, to, queue), so the example's subspace could be derived from its keys. For the ledger it cannot, so the example must name its subspace.
- **`kind` is a key here, a dim elsewhere.** `dimension_values` (dims only) would miss it. `fields` covers keys and dims.
- **An enumeration is the wrong shape for `agent_type`.** It is free text whose value set is the set of agent definitions installed on a machine. The scalar description form fits; a value-to-meaning map would be wrong the day a new agent is added.
- **`writers: []` has to mean "none today".** A lint that requires a non-empty `writers` would force an invented writer on a template whose writer was deleted. The lint should require that the key is present, and the parser should treat an empty flow list as a valid value (MiniYaml returns an empty list for `[]`, MiniYaml.java:169-171).
- **A header comment can go stale in the other direction too.** ledger.yaml:11 still names a deleted writer. This is evidence that Gap 2 is real: the YAML comments are not a reliable source of who writes.
- Dormant status is carried in `summary`. A separate `status` field was considered and left out (see verdict).

---

## 6. `lock/<resource>`

```yaml
doc:
  summary: One row per resource and one holder at a time; take the row with in, renew while working, end with release. The row expires on its own if the holder dies.
  writers:
    - scripts/git-push-develop.sh creates the row with nx tuple out for the resource ci-develop-push, which is idempotent
    - any session wanting mutual exclusion on a named resource, per conexus/skills/peer-messaging/SKILL.md
  readers:
    - scripts/git-push-develop.sh takes the lock with nx tuple in (lease 900 seconds) and ends it with nx tuple release
    - a session holding a lock renews with tuple_renew and ends with tuple_release; ack is refused on a lock claim
    - nx doctor (nexus.health) flags a claim renewed far past its lease
  fields:
    resource: the thing being locked, for example ci-develop-push
    from: optional; the holder's name for diagnostics, nothing depends on it
  example:
    subspace: lock/example-resource
    keys:
      resource: example-resource
    dims:
      from: example-peer
  see_also:
    - RDR-211
    - conexus/skills/peer-messaging/SKILL.md
```

The body is omitted: the template has no `max_body_bytes`, but no caller sends one.

Writers/readers (file:line):
- `scripts/git-push-develop.sh:212-213` (subspace and resource), `:515` (`tuple out ... --key resource=...`), `:522` (`tuple in ... --lease-s 900`), `:250` (`tuple release`). The header at `:56-73` explains the use.
- `conexus/skills/peer-messaging/SKILL.md:77` names `tuple_in`, `tuple_renew`, `tuple_release` as the cross-session mutual-exclusion primitive.
- `src/nexus/health.py:8412-8468` and `:8535` (`tuples.lock_held`: a claim held more than 4x the lease).

Live: one subspace, `lock/ci-develop-push`, 1 row, unclaimed, `attempts=3`, dims `{from}`, no body. The script sends no dims (`git-push-develop.sh:515`), so the `from` on that row came from some other writer; I did not find which. It confirms `from` is optional and unchecked.

A1 notes:
- Fits. `readers` is the awkward word: the holders take the row with `in`, which is a claim, not a read. The strings carry the verb, so it works, but a reader of the rendered dictionary sees "readers" next to a take-enabled template. Keep the name (the RDR chose it) and let the first reader string say "claims".
- The lifecycle (in, renew, release, never ack) is the template's meaning and is the thing a script author needs most. It fits in `summary` and the reader strings because it is short; `summary` was kept to one line by moving the rest to `readers`.
- A lock without `from` still validates, so the example may omit `dims`; it keeps `from` to show the optional dim.

---

## 7. `mailbox/<address>`

```yaml
doc:
  summary: A per-address inbox for session-to-session and agent-to-agent messages; a sender posts, the addressee claims, acts and acks, and an ack can carry the reply.
  writers:
    - the mailbox_send MCP tool, which resolves a name to a session id through the directory and mints a fresh nonce per call
    - tuple_ack with a reply, which writes the answer into the requester's mailbox in the ack's own transaction
    - tuple_out or nx tuple out directly, for a sender that already knows the address
  readers:
    - the mailbox-drain hook claims, acks and renders the session's mailbox on every UserPromptSubmit
    - the session's nexus MCP server waits on mailbox/SESSION_ID and pushes arrivals through the Claude Code channel
    - nx tuple wait, and tuple_in for a manual claim
  fields:
    to: the address, a session id or an agent id; the same as the subspace's last segment
    from: the sender's session or agent id; a claim the engine does not check, and mailbox_send fills it in
    kind:
      request: ask the addressee to do something and reply
      ack: the reply to a request, written by tuple_ack
      reply: a late answer sent as a new message after the request was acked as tracked
    correlation_id: pairs a reply with its request
    address_kind:
      agent: to is a subagent id
      session: to is a session id
  body: free text up to 4096 bytes
  example:
    subspace: mailbox/00000000-0000-0000-0000-000000000000
    nonce: 00000000-0000-0000-0000-000000000006
    keys:
      to: 00000000-0000-0000-0000-000000000000
    dims:
      from: 00000000-0000-0000-0000-000000000001
      kind: request
      correlation_id: req-0001
      address_kind: session
    body: Please review the diff on feature/example and reply with the findings.
  see_also:
    - RDR-205
    - RDR-208
    - conexus/skills/mailbox/SKILL.md
```

Writers:
- `src/nexus/mcp/core.py:8403` `mailbox_send`; the write is at `:8460` (`f"mailbox/{address}", {"to": address}, dims, body, nonce=str(uuid.uuid4())`), dims `from`, `address_kind`, optional `kind` and `correlation_id` at `:8454-8459`.
- `src/nexus/mcp/core.py:8163` `tuple_ack` with `reply`; `_default_reply_from` at `:8100-8124` fills `from`.
- Direct `tuple_out` (`core.py:7927`) and `nx tuple out` (`src/nexus/commands/tuple_cmd.py:75-130`).

Readers:
- `src/nexus/hooks/mailbox_drain.py:1-50` (docstring: claims, acks and renders on every `UserPromptSubmit`); the plugin copy is registered at `conexus/hooks/hooks.json:331`.
- `src/nexus/mcp/channel.py:1084-1100` (catch-up read of each subscribed mailbox) and `:1448` (push); `src/nexus/tuple_wait.py:106` (`nx tuple wait`).

Free-text dimension values (`kind`): in use per `conexus/skills/mailbox/SKILL.md:24` (`request`, `ack`) and `conexus/skills/peer-messaging/SKILL.md:60` (`reply`). The drain hook renders a message with no `kind` as `note` (`src/nexus/hooks/mailbox_drain.py:805`); that is a display default, not a written value, so it is left out of `fields`. `address_kind` values are enforced by the template: `agent`, `session` (`instance` was retired, mailbox.yaml:8-14).

Live: 49 mailbox subspaces, every row consumed (counts 1 to 30 each, for example `mailbox/0f3c9023-...` 30); no unconsumed row exists, so `rd` returns nothing and no `kind` or `address_kind` count can be taken from the tenant. The values above come from code and skills.

A1 notes:
- Fits with `nonce` (mailbox_send mints a UUID; `tuple_ack`'s reply nonce is the hex of the request id, set by the engine, so a reply example would not carry one. The example is a plain send).
- The queryable "values in use" is unavailable for any take-enabled template once its rows are consumed. The RDR's census join (Gap 4) will show zero for a mailbox, and the dictionary text is then the only record of what `kind` means. That raises the value of writing `kind` values by hand.
- `to` is the address, mirrored; the `fields` entry says it.

---

## 8. `queue/<name>`

```yaml
doc:
  summary: A named work queue; a producer posts one task per unit of work, any free worker claims the oldest, and the worker acks, nacks (counts a failure) or releases (hands it back).
  writers:
    - Unverified: any producer, by tuple_out or nx tuple out; no code in this repository posts to queue/<name>, and the live tenant holds no such subspace
  readers:
    - Unverified: a worker claims with tuple_in or tuple_inp, then tuple_ack, tuple_nack or tuple_release
    - Unverified: nx doctor (nexus.health) warns when the available backlog of a queue grows
  fields:
    queue: the queue name, the same as the subspace's last segment
    from: the producer's session or agent id; recorded, not checked
    kind: an optional free label for the task's shape; no value is in use today
    correlation_id: lets the worker's ack reply reach the producer's mailbox, and lets the producer match a reply to its request
  body: the task, free text up to 4096 bytes
  example:
    subspace: queue/example-work
    nonce: 00000000-0000-0000-0000-000000000008
    keys:
      queue: example-work
    dims:
      from: 00000000-0000-0000-0000-000000000007
      kind: review
      correlation_id: req-0002
    body: Review the diff on feature/example.
  see_also:
    - RDR-211
    - RDR-206
```

Writers: none in code. `rg` for `queue/` over `src/`, `conexus/`, `scripts/`, `.github/`, `.claude/` finds only health-check code (`src/nexus/health.py:9093-9209`) and comments. The aspect queue moved to its own template (RDR-235, `aspects`).

Readers: `src/nexus/health.py:9089-9209` `_check_tuple_queue_depth` (warns above `_TUPLE_QUEUE_DEPTH_WARN_AVAILABLE` = 1000, `:8028`). The claim verbs are generic MCP tools (`core.py:8062` `tuple_in`, `:8163` `tuple_ack`, `:8279` `tuple_release`).

Live: `nx tuple list` shows no `queue/` subspace. No value counts exist.

A1 notes:
- Same as `board/<topic>`: `kind` has no values in use, so the scalar form is needed.
- Two of the eight templates (board, queue) are documented from convention alone, with no live writer. The block can describe the shape and the intended use, but `writers` and `readers` there say "any" and cannot be verified. The RDR now says so: for a template with no in-repo user, the dictionary entry is an intent record, and its `writers` and `readers` strings start with `Unverified: `.
- The body size note: the template sets no `max_body_bytes`, so the global 4096 cap applies (`TupleLimits.MAX_BODY_BYTES`).

---

## A1 verdict

**The closed field set as the RDR wrote it does not fit all eight. Four
changes fix it, and the block stays small and closed.**

| # | Change | Why (the template that forced it) |
| --- | --- | --- |
| 1 | `example` gains `subspace` | The ledger's subspace (`ledger/<session_id>`) shares nothing with its keys, so it cannot be derived. Without a subspace the engine cannot resolve the template to validate against (`TupleRepository.prepareOut`, TupleRepository.java:556). One explicit field is simpler than a derivation rule with one exception. |
| 2 | `example` gains `nonce`, required exactly when `id_from` is `keys+nonce` and refused otherwise | Six templates (aspects, board, board/ci, directory, mailbox, queue) are keys+nonce, and `validateNonce` refuses a blank nonce (TupleRepository.java:1115-1120). The nonce is its own field of the write, not a key or a dim (`NONCE_OWN_FIELD`). The RDR's `keys/dims/body` cannot pass the check "same as `out()`". |
| 3 | `dimension_values` becomes `fields`: name to a one-line description, or name to `{value: meaning}`; the name must be a declared key or dimension | (a) The ledger's `kind` is a key. (b) `from` and `topic` carry meaning but have no enumerable values. (c) `agent_type`, board `kind` and queue `kind` are open: a scalar description fits, an enumeration would be empty or wrong. (d) The boot check that the name is declared catches a typo, as `id_dims` does today (TemplateSchemaParser.java:63-75). |
| 4 | New `body`: a one-line description, or `{json field: meaning}` | `board/ci`'s body is the data a stage-2 trigger reads (`state`, `conclusion`, `workflow`), and `aspects`' body is the task identity. Neither is a dimension, so the RDR's block has no place for them. Omitted when `max_body_bytes` is 0 (ledger, directory). |

Smaller points that need no field:

- `writers: []` must be valid and mean "none today" (ledger). The lint should check the key is present, not that the list is non-empty.
- `readers` also lists claimers (lock, mailbox, queue, aspects). The strings name the verb. No rename needed.
- Lists are sentences, not records, because `MiniYaml` has no list of mappings. A structured `writers` (workflow, job, verb) would need a parser change beyond `doc`. Stage 2 can live with strings; if it cannot, that is a stage-2 cost to name now.
- Example dims and keys must be coerced to strings after `MiniYaml` (digits become `Long`, `true`/`false` become `Boolean`).
- A `status` field (active, dormant) was considered for the ledger and left out: `summary` plus `writers: []` carries it, and a status enum is one more thing to keep true.
- Two templates (board, queue) have no in-repo writer, so their `writers` and `readers` are intent, not evidence. The lint cannot tell. The RDR says so (Approach item 4, the `Unverified: ` mark, and Drift).
- The census join (Gap 4) shows zero rows for any take-enabled template whose rows are consumed (all 49 mailboxes), so the dictionary text is the only record of `kind` there.
- Template header comments are not a reliable source for "who writes": ledger.yaml:11 names a deleted module.

A2 is untouched by this pass (no engine run).

## How the blocks were checked

The eight blocks were extracted from this file and each was run through a
throwaway Java driver in the scratchpad (not in the repository), compiled
against the real `MiniYaml`, `TemplateSchema`, `TemplateSchemaParser` and
`TupleLimits`. For each template the driver:

1. parsed the template YAML and the `doc` block with `MiniYaml`;
2. checked the `doc` field set is the closed set above and that every `fields`
   name is a declared key or dimension;
3. parsed the template with `TemplateSchemaParser.parse`;
4. replayed `validateOutShape`, `validateNonce` and the body-size and
   per-field-size checks of `TupleRepository.prepareOut` on the example, after
   resolving the example's subspace to the template by segment count and the
   address grammar.

First run: 8 of 8 templates examined, 350 checks, 0 failures, 0 skipped.
Positive control: three deliberate defects in a scratch copy (a dropped nonce,
a key value outside its enum, a `fields` name that is not declared) produced
three failures. The driver and the scratch copy live outside the repository.
