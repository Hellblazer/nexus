# Ownerless-write refusal: cutover runbook (RDR-223 Phase 3 Step 2)

The final engine cut (RDR-223 plus RDR-192, one tag) makes the engine refuse a chunk
write that no document owns. This page is the order of operations for turning that on
without breaking a client that has not been upgraded. Beads: nexus-20onx (this
runbook), nexus-z0o2p.24 (the engine refusal, the client version header and the log
line), nexus-9a6io (the published-client gate that expects the refusal).

## What is refused

`POST /v1/vectors/upsert-chunks` and `POST /v1/vectors/store-put` answer 422 with
`reason: ownerless_chunk_write` when any chash in the request has no live manifest row in
the collection. Every client released before the RDR-223 Phase 2 migration writes that
way for `nx store put`, `nx index md` and the other note and document writers, so those
clients stop working against an enforcing engine until they are upgraded **and
restarted**. A client from the paired release writes the chunks and the owner rows in one
request and is never refused.

## The knob

The engine reads `NX_OWNERLESS_WRITE_MODE` once, at boot:

| Value | Behaviour |
|---|---|
| unset or blank | `log-only` |
| `log-only` | write as before; count it; log `ownerless_chunk_write_would_refuse` |
| `enforce` | refuse with 422; count it; log `ownerless_chunk_write_refused` |
| anything else | the engine **refuses to start** |

The last row matters for the cloud environment file: a typo there is an outage, and the
emergency lever is one of the two accepted values, never a third. `deploy/engine/image-smoke.sh`
(conexus repo) must boot the image with the production value so a bad value fails before
the push, not at the redeploy.

Local installs enforce from the first boot: the local launcher (`nx daemon service start`)
sets `NX_OWNERLESS_WRITE_MODE=enforce` itself unless the variable is already set, so a
local census run can export `log-only`. The first cloud deploy is the opposite: it must run
`log-only` (the engine's unset default), because conexus has one environment and it is the
live estate.

`GET /v1/status` reports `ownerless_write_mode`, `ownerless_writes_refused_total` and
`ownerless_writes_would_refuse_total`. The counters are since-boot: a redeploy resets them,
so read them and the log together.

## Who is affected (measured 2026-10-01)

conexus-55's census of `POST /v1/vectors/store-put` and `/upsert-chunks` over
2026-09-01 to 2026-10-01: every request was WAF ALLOW, from **two IP addresses** (the
operator's and one other), `Python-urllib/3.12` and `python-httpx/0.28.1`. The population
to upgrade and restart is two hosts. The user agent carries no client version, so
`X-Nexus-Client-Version` is the only soak signal, and the final-cut client sends it on both
transports (urllib and httpx).

## Order of operations

1. **Deploy the cut with `log-only`.** conexus wires the knob before the cut deploys, as a
   Terraform parameter in the engine-redeploy SSM document (conexus-3jue's parameter),
   defaulting to `log-only`, next to the `NX_HNSW_MAX_SCAN_TUPLES` rollback lever. The
   engine's environment file is rendered at boot, so a hand edit on the host is lost.
2. **Upgrade and restart every client.** On each of the two hosts: upgrade conexus to the
   paired release, then restart every long-lived process that holds the old code. Upgrading
   the package on disk does not change a running process.
   - Each `nx-mcp` server is one per Claude Code session: reconnect it (`/mcp`) or quit and
     relaunch the session.
   - Hook-spawned and background `nx` processes: `nx daemon restart-stale` lists and
     restarts what predates the install and names the sessions only you can close.
   - Check with `nx doctor`: the `Process freshness` row must be green, and the
     `Ownerless writes` row shows the engine's counters.
3. **Soak.** Read `ownerless_writes_would_refuse_total` and the engine log (CloudWatch group
   `/conexus/dev/engine`, filter `event=ownerless_chunk_write_would_refuse`). Each line names
   the route, the collection, the `user_agent` and `client_version`; `client_version=absent`
   is a client older than the cut. The line is rate limited to one per route and collection
   per minute, the counter is not. The count stops moving once every writer is upgraded and
   restarted.
4. **Flip to `enforce`.** Set the parameter to `enforce` and redeploy the **same tag**.
   No new tag is cut for the flip. Recommended criterion, for Sam to confirm: no
   would-refuse line for a full day of normal use on both hosts after the restart.
5. **If a writer turns up after the flip.** Its 422 error text says to upgrade conexus and
   restart `nx-mcp` and Claude Code sessions. To buy time, set the parameter back to
   `log-only` and redeploy the same tag; nothing else changes.

## What the published-client gate expects

`tests/e2e/published-client-write-gate.sh` runs the published client against the candidate
engine in both modes (`NX_GATE_OWNERLESS_WRITE_MODE=log-only` and `enforce`). With the
published client older than the paired release it expects exit 0 under `log-only` (and a
non-zero would-refuse count), and exit 2 under `enforce`
(`NX_EXPECTED_CLIENT_LAG=nexus-z0o2p.24`, accepted only with the engine's refusal counter
behind it). The `engine-release` skill, Step 3c, carries the invocations.
