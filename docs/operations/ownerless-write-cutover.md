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
(conexus repo) booting the image with the production value catches only an INVALID value,
which fails before the push instead of at the redeploy. It does not catch a valid but wrong
one: `enforce` on the first deploy boots fine and refuses every legacy write. For that, the
conexus relay and arming checklist for the first deploy carries an assertion, run against the
booted image or the staged parameter before the push: `/v1/status` `ownerless_write_mode`
must equal `log-only`. **That assertion is a conexus-owned hold-the-push line, and nothing in
this repo checks it.** Owner: conexus. Step: image built and redeploy staged, before the paired
client tag is pushed. Evidence: the value they read, in their arming reply. It is not a field on
the `docs/release-arming/` attestation because that file records deploy facts conexus re-checks
at its own flip (image digest, parameter version): the mode is one more property of a deploy
that has not happened at tag push, so a nexus reader would only echo conexus's claim; a
required field no writer emits yet fails every paired tag until conexus's writer changes (a
repo this side cannot see or test), and an optional field asserts nothing; and
`--paired-deploy-auto` can skip the battery that would read it. The nexus-side backstop comes
after the harm window, not before it: the gate leg below, run after the first deploy. The code default (unset or blank is `log-only`) is pinned by the
engine's own test (`OwnerlessWriteRefusalTest`, `anUnsetModeBootsLogOnly_andAnExplicitEnforceBootsEnforce`);
what only the deployment can show is what conexus wired, so the check belongs on the
deployment's own `/v1/status`.

Local installs enforce from the first boot: the local launcher (`nx daemon service start`)
sets `NX_OWNERLESS_WRITE_MODE=enforce` itself unless the variable is already set, so a
local census run can export `log-only`. The first cloud deploy is the opposite: it must run
`log-only` (the engine's unset default), because conexus has one environment and it is the
live estate.

`GET /v1/status` reports `ownerless_write_mode`, `ownerless_writes_refused_total` and
`ownerless_writes_would_refuse_total`. The counters are since-boot: a redeploy resets them,
so read them and the log together. The live mode is asserted, not assumed:
`NX_EXPECTED_OWNERLESS_WRITE_MODE=log-only tests/e2e/cloud-client-path-gate.sh` after the
first deploy and `=enforce` after the flip (the `engine-release` skill, Step 6.1).

## Who is affected (measured 2026-10-01)

conexus-55's census of `POST /v1/vectors/store-put` and `/upsert-chunks` over
2026-09-01 to 2026-10-01: every request was WAF ALLOW, from **two source IP addresses**
(the operator's and one other), `Python-urllib/3.12` and `python-httpx/0.28.1`. Two
addresses is a floor on the machines, not the machine count. NAT can put several machines
behind one address, and each machine can run its own `nx-mcp` servers and hook-spawned `nx`.
Which machines share an address is **unverified**: nothing in this repo records the egress of
the Mac mini, the WSL appliance or qwentescence, so do not assume they share one or that they
do not; confirm each machine's public address against the two in the census. The population
to upgrade and restart is every machine behind those two addresses, so inventory machines and
not addresses.

The census also shows what the version header cannot tell you. The user agent carries no
client version, so `X-Nexus-Client-Version` is the only soak signal, and the final-cut nexus
client sends it on both transports (urllib and httpx). A caller that is not the nexus client
never sends it: the census has `curl` (6 `store-put`) and `python-httpx/0.28.1` (351
`upsert-chunks`), and a conexus-side or script caller that builds its own request logs
`client_version="absent"` (the engine quotes the value) for as long as it exists. Upgrading a package cannot fix that, so
`absent` does not mean "an old client". Step 3 dispositions each pair.

## Order of operations

1. **Deploy the cut with `log-only`.** conexus wires the knob before the cut deploys, as a
   Terraform parameter in the engine-redeploy SSM document (conexus-3jue's parameter),
   defaulting to `log-only`, next to the `NX_HNSW_MAX_SCAN_TUPLES` rollback lever. The
   engine's environment file is rendered at boot, so a hand edit on the host is lost.
   Assert the mode BEFORE the push (the `/v1/status` check in the previous section, in
   conexus's relay and arming checklist) and again after the deploy with the gate:
   `ownerless_write_mode` must read `log-only`, because a mis-wired parameter that reads
   `enforce` refuses every legacy write from every host at the first deploy, and only
   the pre-push check can stop that before it happens (a conexus-owned line, see "The knob":
   the post-deploy gate sees it only after the harm).
2. **Upgrade and restart every client.** On each machine behind the two addresses: upgrade
   conexus to the paired release, then restart every long-lived process that holds the old
   code. Upgrading the package on disk does not change a running process.
   - Each `nx-mcp` server is one per Claude Code session: reconnect it (`/mcp`) or quit and
     relaunch the session.
   - Hook-spawned and background `nx` processes: `nx daemon restart-stale` lists and
     restarts what predates the install and names the sessions only you can close.
   - Check with `nx doctor`: the `Process freshness` row must be green, and the
     `Ownerless writes` row shows the engine's counters.
3. **Soak, and disposition every caller.** Read `ownerless_writes_would_refuse_total` and the
   engine log (filter `event=ownerless_chunk_write_would_refuse`; **the CloudWatch log group
   is unverified**, confirm its name with conexus: `/conexus/dev/engine` appears in this repo
   only as an SSM parameter prefix, `docs/release-arming/README.md`, not as a log group. A
   wrong group errors loudly, but a wrong filter returns nothing and reads as "soak
   drained", which is why step 4 requires a positive control). Each line names the route, the tenant, the
   collection, the `user_agent` and `client_version`. The count stops moving once every
   writer is upgraded and restarted, **or** is a caller that cannot be fixed by an upgrade.
   Collect every distinct `(user_agent, client_version)` pair that appears during the soak
   and write down what each one is:

   | Pair | Disposition |
   |---|---|
   | nexus client, version at or above the paired release | counted only if a process still runs old code: find it with `nx doctor` on that machine |
   | nexus client, `client_version="absent"` | a client older than the cut: upgrade and restart it |
   | not the nexus client (`curl`, a conexus-side `httpx` caller, a script), `absent` | it never sends the header. Move it to the combined write (`/v1/catalog/manifest/write_many`, which writes the chunks and the owner rows together) or confirm it is retired. Do not wait for it to disappear |
   | a pair nobody can name | hold the flip until it is named |

   Every pair needs a written disposition before the flip. A pair that stays `absent`
   forever blocks the flip forever; only this step turns that into a decision.

   The log line is a sample, not a list. It is rate limited to one line per
   `route|tenant|collection` per minute (with `suppressed_since_last`), and it carries only
   the first unowned chunk's metadata and a sample of chashes, so it names one source per
   request. The counter is not limited; use it for counts and the log to find callers.
4. **Flip to `enforce`.** This step is its own bead, **nexus-z0o2p.40**, and it FOLLOWS the
   cut: the engine-release skill's Step 6.1 and Step 7 sign off the cut against `log-only` and
   do not wait for the flip, so a cut is never held open for it. Only with a positive control, because the engine logs only
   would-refuse lines and silence is also what a powered-off host looks like. Proceed
   when all of these hold, for Sam to confirm:
   - the WAF or ALB request counts for `store-put` and `upsert-chunks` are **non-zero for
     each of the two source addresses after the restart**, so a quiet log means "writers ran
     and nothing was ownerless" and not "nothing ran";
   - `Process freshness` is green on each machine behind each address;
   - the window since the restart is **at least as long as the longest writer cadence**, read
     from the census (for a weekly index job, a week; "a full day" is only enough if no
     writer is slower than daily);
   - every pair from step 3 has a disposition.

   **Export the since-boot counters first.** The flip redeploy resets them, and with them the
   soak evidence. Save the `/v1/status` body and the CloudWatch query result for the soak
   window to the release record (the bead's comment or a T2 note) before the redeploy.
   Then set the parameter to `enforce` and redeploy the **same tag**; no new tag is cut for
   the flip. Confirm `ownerless_write_mode` reads `enforce` with the gate in "The knob"
   (`NX_EXPECTED_OWNERLESS_WRITE_MODE=enforce`; that check belongs to nexus-z0o2p.40).
5. **If a writer turns up after the flip.** Its 422 error text says to upgrade conexus and
   restart `nx-mcp` and Claude Code sessions. To buy time, set the parameter back to
   `log-only` and redeploy the same tag; nothing else changes. The redeploy is not instant
   (it goes through conexus).
   **Writes refused in the window are lost, not queued.** A client does not retry a 422, and
   a background indexer fails that write in its own log. After the rollback:
   - bound the window: the flip redeploy time to the rollback redeploy time;
   - read `ownerless_writes_refused_total` from before the rollback redeploy for the count;
   - find the callers in the log (`event=ownerless_chunk_write_refused`), keeping in mind
     that it is sampled as in step 3, so it undercounts documents;
   - **re-index everything those clients wrote in the window**: re-run `nx index repo` for
     the repositories, and re-store the notes and re-run the `nx index md` and `nx index pdf`
     work, from each affected machine. Do not assume the log lists every refused file.

## Local-mode installs

A local install gets `enforce` at its first boot of the upgraded engine, with no soak: the
launcher sets it. The first write from a process that still runs the old code is refused.
So the restart instruction is part of the upgrade, not a follow-up: after upgrading, restart
every long-lived `nx-mcp` server (one per Claude Code session) and every hook-spawned `nx`
(`nx daemon restart-stale`, then `nx doctor`'s `Process freshness` row). The release's
CHANGELOG entry for the refusal and the `nx upgrade` section of the CLI reference carry the
same instruction. A local census run that wants to see the writers before refusing them
exports `NX_OWNERLESS_WRITE_MODE=log-only` before `nx daemon service start`.

## What the doctor row does and does not say

`nx doctor`'s `Ownerless writes` row warns when either engine counter is above zero. The
counters are since-boot and the engine exposes no per-client breakdown, so the row cannot
name the cause (an old client, a process still running old code, a non-nexus caller) and
cannot clear after the cause is fixed until the engine restarts. It does not warn that the
installed client is older than the paired release: an old client has no new doctor to run
it, and a stale local process is what `Process freshness` reports. That per-client signal
needs the engine to count would-refuse writes by `client_version` and report a
`last_would_refuse_at` timestamp in `/v1/status` (an additive change, nexus-z0o2p.39).

## What the published-client gate expects

`tests/e2e/published-client-write-gate.sh` runs the published client against the candidate
engine in both modes (`NX_GATE_OWNERLESS_WRITE_MODE=log-only` and `enforce`). With the
published client older than the paired release it expects exit 0 under `log-only` (and a
non-zero would-refuse count), and exit 2 under `enforce`
(`NX_EXPECTED_CLIENT_LAG=nexus-z0o2p.24`, accepted only when both journeys failed with the
refusal and the engine's refusal counter is at least 2 behind it). The `engine-release`
skill, Step 3c, carries the invocations.
