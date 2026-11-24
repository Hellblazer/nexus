# Auras

> Status: shipped in conexus 8.3.0. Design of record: [RDR-249](rdr/rdr-249-declarative-auras.md). Reference: [`nx aura`](cli-reference.md#nx-aura), [configuration § Auras](configuration.md#auras).

## What an aura is

An aura watches one board subspace in the [tuple space](tuple-space.md) for you. When a matching post arrives, it runs a small read-only procedure and puts that procedure's one line of text in your session's threads entry, where you and the agent both see it. A later post can clear the line again.

An aura is a TOML file, `<name>.aura.toml`, beside the Lua procedure it runs, in the procedure library. The session's MCP server does all the work. Nothing runs in the engine, and the procedure can only read: it cannot write tuples, files or memory.

Every aura ships off. Nexus never turns one on for you.

## What ships

Two auras ship: `ci_develop_red`, and `ci_develop_nudge`, which wakes next up when the owner has not acknowledged (below). Both are off until you turn each on by name. `ci_develop_red` is for sessions in the `push-train` group (see [Loading an aura for a group](#loading-an-aura-for-a-group)) and it watches `board/ci/<base>--owner` for each base its group serves (`nexus-develop` for nexus; see [One aura for every base a group serves](#one-aura-for-every-base-a-group-serves)), where the engine's owner stamper posts who owns a red branch. The stamper names one owner for each red, and a next-up session behind it, by identity id and, from engine 0.1.198, also by the seat's live alias (`owner_alias`, `next_alias`), which the notice shows for someone else's red; a row with no alias shows the id. The aura writes a block to your threads entry, worded for what this session has to do:

```text
## aura: develop CI red (nexus-develop)
state: Develop is red at 8b30c16, first red in your push. Diagnose it. Ack: nx ci ack 8b30c16.
```

The texts below say `Develop` because the base is `nexus-develop`: the first word is the base's branch, capitalised (`Main is red at ...` for `art-main`).

There are ten displays. `<owner>` is the owner's alias, else its identity id: from engine 0.1.198 the row carries the owner's live alias (`owner_alias`) beside the id, and the notice shows it; a row with no alias (an older engine, or an identity with none) shows the id. Who owns the red is always decided on the ids; the alias is a name to show.

| The row says | The block says | Wakes you |
|---|---|---|
| You own it, the red is from your push (`earliest_suspect`) | `Develop is red at <sha>, first red in your push. Diagnose it.` | yes |
| You own it, the leader takes it (`owner_unleased`, `owner_not_live`, `owner_not_member`, `no_record`, `chain_gap`, `stamper_error`) | `Develop is red at <sha>; the leader takes it (<reason>). Diagnose it.` | yes |
| You own it after a hand-off (`handoff`) | `Develop is red at <sha>; it was handed to you after no acknowledgement. Diagnose it.` | yes |
| You own it after the previous owner said it is not theirs (`disclaimed`, from `nx ci ack --not-mine`) | `Develop is red at <sha>; handed to you: <previous owner> said it is not theirs. Diagnose it.` | yes |
| Escalated because the red stayed open past the stall window (`escalated` with `stalled`), and it names you or no one | `Develop is red at <sha>, still open after N min; last activity: <who> <acknowledged\|pushed> at hh:mm UTC. Raise an ask to Sam.`, or with no sign of life `Develop is red at <sha>, still open after N min with no acknowledgement or fix push. Raise an ask to Sam.` | yes |
| The same stall, and you gave the last sign of life | `Develop is red at <sha>, still open after N min; your last activity: <acknowledged\|pushed> at hh:mm UTC. The leader is raising it with Sam.` | no |
| The same stall, and it names someone else | `Develop is red at <sha>; <owner> acknowledged it but it is still open after N min. The leader is raising it with Sam. Hold your pushes.` | no |
| Escalated, and it names you or no one (`escalated`) | `Develop is red at <sha>; no one acknowledged. Raise an ask to Sam.` | yes |
| You are next up | `Develop is red at <sha>; <owner: alias, else id> owns it. You are next up.` | no (see the nudge below) |
| Someone else owns it | `Develop is red at <sha>; <owner: alias, else id> owns it. Hold your pushes.` | no |

The first three owner displays end with the acknowledgement: the owner writes a `kind=ack` post with `sha=<the red sha>` to `board/ci/nexus-develop--owner`, with `nx tuple out` or the `tuple_out` MCP tool, and the stamper counts it when the row's sender-session stamp, mapped through the session index, names the owner. The engine never reads the row's `from`. The state line holds 200 characters, so the hint names the helper rather than the whole post: `Ack: nx ci ack <sha7>`. An owner who is not the one fixing it says so with `nx ci ack --not-mine <sha7> [--note TEXT]`, which posts a `kind=disclaim` row; the engine hands the red on at once when it reads one from the current owner (engine-service-v0.1.201 and later). `nx ci ack` resolves the prefix against the tips of the current red (the shas of its owner rows), refuses a caller who is not the red's current owner, and posts the row with `from=<your identity id>`, the full sha and a fresh nonce. A hand post (`nx tuple out board/ci/nexus-develop--owner --dim kind=ack --dim sha=<the red sha> --nonce <a fresh uuid>`, or the `tuple_out` tool) is restamped by the client: `from` on an ack of a red is always the caller's own identity id, never a name, and a caller with no identity is refused. `from` is a label people read (`nx group ack-times` does not read it), and the client's stamp of it is a hint (from engine 0.1.200 the engine stamps `from` itself as the identity the sender session maps to); an ack with a session alias in it (posted by hand on 2026-11-20) still stopped the hand-off, and only `ack-times` counted it as no ack; `ack-times` now counts an ack only when its sender-session stamp maps to the owner, so an ack with no stamp counts for nobody there, as in the engine. An unacknowledged red is handed to the next up after the group's deadline (20 minutes by default), and then to Sam as an escalation.

A session wakes (the channel notice below) only on a post that starts something: a row with `refresh: false` that makes it the owner, or an escalated row that names it or names no one. Next up is shown the red and not woken, because the owner usually acknowledges within two minutes (RDR-252 post-close audit S2). A hand-off that seats a session as owner wakes it through the owner display. A stall escalation (`escalated` with `stalled: true`) carries `open_minutes` (whole minutes since the red began, the `N` in its text) and, when there was any sign of life on the red, `stalled_sign` (`ack` or `push`), `stalled_owner` (who gave it) and `stalled_since` (when, a UTC instant shown as `hh:mm UTC`); all three are absent when there was none. It wakes the leader only, and the text names only what the engine knows: a push is never called an acknowledgement.

Until the `push-train` group is formed and serves a base, no session loads the aura, so a session with `auras.ci_develop_red=on` sees no reds at all. The old `--done`-based aura is gone; `nx aura check` warns about an aura on a concrete `--owner` subspace that is loaded by a group not serving its topic.

## Turning one on

Name it under `auras:` in your global config, `~/.config/nexus/config.yml`:

```yaml
auras:
  ci_develop_red: on
```

or from the shell:

```bash
nx config set auras.ci_develop_red=on
nx aura list        # ci_develop_red ... on  global
```

A repo's `.nexus.yml` can set the same key and wins over the global value either way, so `off` there turns off a global `on` for sessions started in that repo.

**The setting is read once, when the session's MCP server starts.** A session that was already running does not pick it up. Restart the session, or run `/mcp` and reconnect the conexus `nexus` server. The MCP log (`~/.config/nexus/logs/mcp.log`) then has an `auras_loaded` line with your session id and the aura names. If it says `auras_not_loaded`, its `reason` field says why (`none_turned_on` is the usual one).

Turning an aura on in the global config turns it on for every Claude session on the machine, from each one's next server start.

## What you see when it fires

**The threads entry.** The block goes into your own entry in T2 project `threads`, the one whose first line is `session: <your session id>` (the [cross-session board](agents/threads-board.md), shown in `nx console`, Threads tab). The block is always two lines: the heading `## aura: <name>` and one `state:` line of at most 200 printable ASCII characters. A later fire replaces it. The aura never creates an entry: with no entry carrying your session line, or with two, nothing is written and the server logs why.

**The channel.** When the aura's file sets `display.channel = true`, the session also gets a channel notice on every fire. It is a fixed template and never carries the procedure's text:

```text
aura ci_develop_red@nexus-develop fired on board/ci/nexus-develop--owner tuple <id>; see the "aura: develop CI red (nexus-develop)" block in your threads entry
```

That notice wakes an idle session the way any channel message does.

`display.channel` may also be the name of a boolean field of the procedure's result (`channel = "wake"`, as `ci_develop_red` does). The notice is then sent only when that field is `true`, so one aura can wake some sessions and leave the block alone for the rest. The text is still the fixed template. A result with no such field, or one that is not a boolean, is a failure like a missing `when` field: the block shows `aura error: result on tuple <id>`, no notice goes out, and three in a row disable the aura.

**`from = "engine"`.** `from` is a dimension the writer types, so a match on it alone would let any session post as the engine. A match pair `from = "engine"` therefore also requires the row to be one the engine wrote: the engine renders `sender_session` on every row, stamps it on a client's write, and writes its own rows with none. A row with a session stamped on it, or from an engine that does not render the stamp, matches neither the trigger nor the clear. A row from a client that holds no live session carries no stamp either, so a null stamp rules out a stamped forgery and does not by itself prove the engine wrote the row.

**Clearing.** An aura with a `[clear]` table removes its block when a post matching that table arrives on the same subspace. For `ci_develop_red` that is a `kind=clear` post, which the stamper writes when develop is green again. A clear does not run the procedure.

Posts on the aura's subspace are not announced to the agent unless the session also subscribes that subspace itself; the aura's notice is the only one.

## When an aura fails

A procedure error, a limit, a missing or mistyped result field, an argument source the post cannot supply or a display error replaces the block's line with `aura error: <kind> on tuple <id>`. The sources that cannot be supplied are a `dims.<f>` the post lacks, a `self.identity` when the session has none, and a body that is not a JSON object. A `body.<f>` that a JSON-object body lacks is not a failure by itself: the argument is left out and the procedure sees it absent. That is fine for an optional argument (`owner_alias` and `next_alias` of `ci_red_notice`, or a `next` that is null), and a failure for a required one, because the procedure's header then refuses the run (kind `args`). It never stops the session's other mail. Three failures in a row disable the aura for the rest of the session, and the line becomes `aura disabled after 3 failures: <kind>`. A disabled aura neither fires nor clears until the server starts again.

## Loading an aura for a group

An aura file may say which group it is for:

```toml
[load]
group = "push-train"      # one key; any other key refuses the file
```

A group-gated aura loads only in a session whose identity is a member of that group, and only while the group is live. Membership is RDR-253's: the server reads the identity's memberships once, when it starts, and then one group record for each group an aura names. The group is not live when its record has `ended_at` set, when its `ends` has passed, or when it has no record. A session that is not a member never subscribes to the aura's trigger. Joining or leaving a group does not change what is loaded: reconnect the MCP server (`/mcp`) afterwards.

A server that started with no identity loads no group-gated aura, and an aura with no `[load]` table loads as before. A Claude Desktop server has an identity only when it made the bind itself from the pending file, so otherwise it loads none. The one case that is not left until a reconnect: a server that loaded with no identity and whose identity lease later adopts its first one (any move from no identity to an identity: the bind from the pending file, or a token file a hook wrote) gates its group-gated auras once more at that moment (the reads are the same two, once), and loads the ones that pass; an identity the heartbeat reported before the load counts even when the token read at load missed it. A server that was bound when it loaded never gates again. Membership and group state are read once, at load: a group disbanded (or a seat that leaves it) while the session runs leaves the auras it already loaded in place until the MCP server reconnects, and a load that could not read the engine (`memberships_unreadable`, `group_unreadable`) stays that way until a reconnect, which `nx aura list` says on that aura's line. When an aura that is on is group-gated and this session's server wrote no state file, `nx aura list` says `not loaded: no state file` on its line. A read that fails (the engine is down, the group client is missing) loads none and gives the reason `memberships_unreadable` or `group_unreadable`.

`nx aura list` says, for each aura that is on, whether this session's server loaded it, and why: `loaded`, or `not loaded: not_a_member`, `unbound`, `group_disbanded`, `no_group_record`, `memberships_unreadable`, `group_unreadable`, with the group named. The server keeps this in `~/.config/nexus/auras/loaded-<claude pid>.<startedAt>.json`, written at load and rewritten when its heartbeat sees the identity change, and removed when it exits. `nx aura list` reads the file of its nearest Claude process and deletes a file whose process is gone (no registry file with that pid and `startedAt`, or a pid whose start time differs, which covers `/restart`). With no Claude ancestor (a shell outside any Claude session) it prints every file on standard error, none claimed: one line per file with its Claude pid, identity and server pid, then one line per aura with its load decision. The table and `--json` on standard output say nothing about loading then. After a cross-seat `/resume` or a take-over the loaded set and `self.identity` stay the old identity's until the server reconnects, and `nx aura list` prints the mismatch and says so on standard error.

## One aura for every base a group serves

A repository's red is a different board topic, `board/ci/<repo>-<branch>--owner`, and the group that owns a repository's reds lists that topic's base (`<repo>-<branch>`) in its `serves`. An aura that triggers on one concrete subspace would need one file per repository. A trigger can instead carry the placeholder `{serves}` once, inside its topic:

```toml
[load]
group = "push-train"                  # required with {serves}: the group whose serves list the placeholder expands

[trigger]
subspace = "board/ci/{serves}--owner"
match = { kind = "owner", from = "engine" }

[args]
branch = "self.branch"                # the branch of the base this copy is for, filled when the template expands

[display]
threads = "{branch} CI red ({base})"  # {base} is required, {branch} optional, no other brace
```

At load, for a session in the `[load]` group, the loader reads the group's `serves` and expands the file into one aura per base. Each is named `<aura>@<base>` (`ci_develop_red@nexus-develop`), triggers on its own concrete subspace, and writes its own threads block, so two bases never share one. `nx aura list` shows the template as loaded for the bases that expanded. The branch is the part of the base after its last hyphen (`develop` for `nexus-develop`, `main` for `recording-rig-main`); a base is `<repo>-<branch>` with no delimiter between them, so a branch that itself holds a hyphen reads as its last word. A group that serves exactly one base expands to one aura, as if the file had named that subspace. Both shipped auras are templates, and their notice texts name the branch, not "Develop".

The grammar takes `{serves}` exactly once in the topic of a `board/<topic>` or `board/ci/<topic>`, and refuses any other use of a brace in the subspace, a template with no `[load] group`, a threads value without `{base}` or with another brace, and `self.branch` in an aura that is not a template. `{serves}` is never read as a literal topic. A template whose group serves nothing loads nothing, and `nx aura list` says `not loaded: serves_nothing`; a base whose expansion the subspace or threads rules refuse is left out (`bad_base`). The group is read once at load, so a base added to `serves` later appears after the MCP server reconnects.

`nx aura check` validates a template against every base its group serves: each expansion must be valid, and the dictionary check runs on each and names the base in its findings. It refuses a template whose group has no record, is disbanded or serves nothing. When the group cannot be read, the serving check is reported as skipped and the dictionary check reads one expansion for a sample base.

## The session's own identity as an argument

An argument can come from the session itself:

```toml
[args]
me = "self.identity"      # the session's identity id, filled when the server loads the aura
```

`self.identity` is an argument source, not a reserved argument name: the procedure declares `me:string` like any other argument, and `nx aura check` accepts the source for a declared string argument and refuses it for another type. A procedure can compare it with the owner a row names. It is the identity the server held at load; a session that has no identity cannot fire an aura that uses it (the block shows `aura error: args`).

## Turning one off

Any of these, at the next server start:

- `nx config set auras.ci_develop_red=off`, or `off` in a repo's `.nexus.yml`.
- `NX_AURAS_OFF=ci_develop_red` (a comma-separated list) or `NX_AURAS_OFF=all` in the session's environment. This switch can only turn auras off, and it beats both config files. Use it for a CI job or any process that should run none.

`nx aura list` shows each aura as on or off and the level that decided it: `default`, `global`, `repo` or `env`. A value other than `on` or `off` is refused with the file and key named; it is never read as off.

Auras never load in a `claude -p` child process, even one that inherited its parent's session id.

## Checking aura files

```bash
nx aura list          # name, summary, trigger, on or off, the deciding level, and whether it loaded
nx aura check [NAME]  # grammar, procedure, argument mapping, subspace fields
```

`nx aura check` parses each file against its closed set of keys, resolves the procedure and checks the argument mapping against the procedure's header, and checks the match and argument fields against the subspace's template. It refuses a `[clear]` that a post could meet together with the trigger, and two auras that write the same threads block. Checks that need the engine are skipped without it, and the output says which.

`nx aura check` also warns, without refusing, about an aura triggered on a `board/ci/<base>--owner` subspace whose `[load]` group is not the group that serves `<base>`: no `[load]` table, a group with no record, a disbanded one, or one whose `serves` lacks `<base>`. No other aura is checked for this. When the group cannot be read the check is reported as skipped, never as passed.

## Writing your own

An aura file sits beside its procedure in a library root: the shipped `src/nexus/procedures/`, or a repo's `.nexus/procedures/` (read only while `NX_PROCEDURES_REPO_ROOT=1`). The shipped `ci_develop_red` is the shape to copy. This copy wraps the shipped `ci_red_notice` procedure, so it passes the arguments that procedure declares, and it takes the engine's owner and clear posts only:

```toml
aura = "my_develop_red"
summary = "A red develop sha and who owns it, posted to this session's threads entry."
procedure = "ci_red_notice"

[trigger]
subspace = "board/ci/nexus-develop--owner"       # one concrete board/<topic> or board/ci/<topic>; no wildcards
match = { kind = "owner", from = "engine" }      # equality on dimensions; absent means every post

[args]                                           # each procedure argument from dims.<f>, body.<f>, id, subspace, self.identity, or = "literal" (self.branch in a {serves} template)
sha = "dims.sha"
owner = "body.owner"
owner_alias = "body.owner_alias"        # engine 0.1.198 on; absent from a row with no alias, which the procedure then shows by id
next = "body.next"
next_alias = "body.next_alias"
refresh = "body.refresh"
reason = "body.reason"
stalled = "body.stalled"                # the stall and disclaim displays read these six,
stalled_owner = "body.stalled_owner"    # as the shipped ci_develop_red does; leave them out
stalled_since = "body.stalled_since"    # and the copy cannot render either
stalled_sign = "body.stalled_sign"
open_minutes = "body.open_minutes"
previous_owner = "body.previous_owner"
me = "self.identity"

[when]
field = "show"                                   # a boolean in the procedure's result; absent means always show

[clear]
match = { kind = "clear", from = "engine" }      # optional; must differ from the trigger on some key

[display]
channel = "wake"                                 # optional fixed-template notice: true, false, or a boolean field of the result
threads = "my develop CI red"                    # required: the block title, one line, at most 80 characters

# [load]                                         # optional: load only in a member of this group (see above)
# group = "push-train"
```

The procedure returns a table with a `text` field (and the `when` field, if named). An optional `[limits]` table can lower the procedure limits for this aura. Any other key, a missing required key or a wrong type refuses the file. Run `nx aura check <name>` before turning it on.
