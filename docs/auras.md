# Auras

> Status: shipped in conexus 8.3.0. Design of record: [RDR-249](rdr/rdr-249-declarative-auras.md). Reference: [`nx aura`](cli-reference.md#nx-aura), [configuration § Auras](configuration.md#auras).

## What an aura is

An aura watches one board subspace in the [tuple space](tuple-space.md) for you. When a matching post arrives, it runs a small read-only procedure and puts that procedure's one line of text in your session's threads entry, where you and the agent both see it. A later post can clear the line again.

An aura is a TOML file, `<name>.aura.toml`, beside the Lua procedure it runs, in the procedure library. The session's MCP server does all the work. Nothing runs in the engine, and the procedure can only read: it cannot write tuples, files or memory.

Every aura ships off. Nexus never turns one on for you.

## What ships

One aura ships: `ci_develop_red`. It watches `board/ci/nexus-develop--done`, the subspace where CI posts each finished develop run. When a run fails, it writes a block to your threads entry:

```text
## aura: develop CI red
state: develop red at 8b30c16: diagnose before the next push (worktrees rule 7)
```

The next green develop run removes the block. It is meant for nexus developers: a red develop sha is a finding to diagnose before anyone pushes on top of it.

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

**The channel.** When the aura's file sets `display.channel = true`, as `ci_develop_red` does, the session also gets a channel notice. It is a fixed template and never carries the procedure's text:

```text
aura ci_develop_red fired on board/ci/nexus-develop--done tuple <id>; see the "aura: develop CI red" block in your threads entry
```

That notice wakes an idle session the way any channel message does.

**Clearing.** An aura with a `[clear]` table removes its block when a post matching that table arrives on the same subspace. For `ci_develop_red` that is the next develop run whose outcome is `success`. A clear does not run the procedure.

Posts on the aura's subspace are not announced to the agent unless the session also subscribes that subspace itself; the aura's notice is the only one.

## When an aura fails

A procedure error, a limit, a missing field or a display error replaces the block's line with `aura error: <kind> on tuple <id>`. It never stops the session's other mail. Three failures in a row disable the aura for the rest of the session, and the line becomes `aura disabled after 3 failures: <kind>`. A disabled aura neither fires nor clears until the server starts again.

## Turning one off

Any of these, at the next server start:

- `nx config set auras.ci_develop_red=off`, or `off` in a repo's `.nexus.yml`.
- `NX_AURAS_OFF=ci_develop_red` (a comma-separated list) or `NX_AURAS_OFF=all` in the session's environment. This switch can only turn auras off, and it beats both config files. Use it for a CI job or any process that should run none.

`nx aura list` shows each aura as on or off and the level that decided it: `default`, `global`, `repo` or `env`. A value other than `on` or `off` is refused with the file and key named; it is never read as off.

Auras never load in a `claude -p` child process, even one that inherited its parent's session id.

## Checking aura files

```bash
nx aura list          # name, summary, trigger, on or off, and the deciding level
nx aura check [NAME]  # grammar, procedure, argument mapping, subspace fields
```

`nx aura check` parses each file against its closed set of keys, resolves the procedure and checks the argument mapping against the procedure's header, and checks the match and argument fields against the subspace's template. It refuses a `[clear]` that a post could meet together with the trigger, and two auras that write the same threads block. Checks that need the engine are skipped without it, and the output says which.

## Writing your own

An aura file sits beside its procedure in a library root: the shipped `src/nexus/procedures/`, or a repo's `.nexus/procedures/` (read only while `NX_PROCEDURES_REPO_ROOT=1`). The shipped pair is the example to copy:

```toml
aura = "ci_develop_red"
summary = "A red develop sha, posted to this session's threads entry."
procedure = "ci_red_notice"

[trigger]
subspace = "board/ci/nexus-develop--done"   # one concrete board/<topic> or board/ci/<topic>; no wildcards
match = { kind = "sha", outcome = "failure" } # equality on dimensions; absent means every post

[args]                                       # each procedure argument from dims.<f>, body.<f>, id, subspace, or = "literal"
sha = "dims.sha"
outcome = "dims.outcome"

[when]
field = "show"                               # a boolean in the procedure's result; absent means always show

[clear]
match = { kind = "sha", outcome = "success" } # optional; must differ from the trigger on some key

[display]
channel = true                               # optional fixed-template notice
threads = "develop CI red"                   # required: the block title, one line, at most 80 characters
```

The procedure returns a table with a `text` field (and the `when` field, if named). An optional `[limits]` table can lower the procedure limits for this aura. Any other key, a missing required key or a wrong type refuses the file. Run `nx aura check <name>` before turning it on.
