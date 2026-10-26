# conexus-tool-spill

An opt-in Claude Code mod. In the main conversation, a big `Bash` output or a big whole-file `Read` is cut to its head and its tail, and the full `Bash` text goes to a file. A `git log`, a test run or a file read then costs the main context a few KB, and the model can still read the rest of the file, or hand it to a subagent, when it needs it.

It is not installed with `conexus`. Nothing changes for anyone who does not install it.

## Turn it on

```
/plugin install conexus-tool-spill@nexus-plugins
```

or from a shell:

```
claude plugin install conexus-tool-spill@nexus-plugins
```

There is nothing to set up. The install lists four options as "not set", and `/plugin configure` shows their fields blank: Claude Code does not prefill declared defaults. Leave them blank and the defaults apply:

| Option | Blank means | What it changes, and why you might |
|---|---|---|
| `limit` | 4000 bytes | Output larger than this is cut. Raise it to see more whole; lower it to save more context. |
| `head` | 1400 bytes | How much of the start the model still sees. |
| `tail` | 800 bytes | How much of the end the model still sees; a test summary or a script's failure usually sits there. |
| `includeHeadless` | off | Whether `claude -p` runs are cut too. Off, because nobody is there to follow the note. |

A cut result ends its head with a line like `[tool-spill: 14 KB, 3001 lines; 2470 middle lines cut, 0 of them mention an error or failure. Full output: <path>. ...]`, which is how you can tell it is working. It loads when a session starts. To turn it off, `claude plugin disable conexus-tool-spill`. The `nexus-plugins` marketplace is the one `conexus` and `sn` come from; add it first with `/plugin marketplace add Hellblazer/nexus` if you have not.

**This plugin replaces any machine-local copy of the mod.** If you wrote a `tool-spill` mod under `~/.claude/mods/` (or any other `tool.call` hook that cuts output), remove it before installing: two loaded together would both act on the same result.

## What it does

It hooks `tool.call` and touches only `Bash` and `Read`, and only in the main conversation. A subagent's calls are never touched, because heavy reads belong there. Every other tool (`Grep`, MCP tools, `ToolSearch`, `Skill`, ...) passes untouched, whatever its size.

| Tool | Result over the limit |
|---|---|
| `Bash` | Each stream longer than `head` + `tail` is cut to the first `head` bytes and the last `tail` bytes, at line ends. A note between them gives the size, the line count, the number of middle lines cut, how many of those mention an error or failure (`error`, `fail`, `traceback`, `panic`, `exception`, `fatal`, `denied`, `not found`, `no such`, `cannot`, `refused`, `timed out`, `unable`; a count, not a verdict), and the path of the full output (stdout, then stderr after a `--- stderr ---` line), with a pointer to read a slice of it with the `Read` tool. A script's verdict on its last line, even with exit code 0, is in the tail. |
| `Read` (whole file) | The text is cut to the first `head` + `tail` bytes, with a note giving the line count, the `offset` to read next and a `limit` to use, and a reminder not to `Edit` text the model has not seen. Nothing is written: the file is already on disk. |

Passed through whole:

- a result at or under the limit (the limit counts UTF-8 bytes, and a cut never splits a character);
- an errored result or a denied call;
- a `Read` with an `offset` or a `limit`: the model sliced it itself, so following the note, or reading a slice of a spill file, costs nothing extra and is never cut again;
- a `Read` of an instruction file (`SKILL.md`, `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md`, `REGISTER.md`, or anything under a `skills/`, `agents/`, `commands/` or `rules/` directory, so a large source file under, say, `src/nexus/commands/` also passes whole; either path separator, any case);
- `Bash` output Claude Code has already moved to a file of its own (past about 30 KB: `persistedOutputPath`, `rawOutputPath`, or output that starts with its `<persisted-output>` marker; output that only quotes the marker further in is still cut), and image output;
- a `Bash` call whose command names the spill directory: it is reading a spill file back (`sed -n`, `head`, `grep` on it), and cutting it again would only spill the spill (Claude Code's own cap of about 30 KB still applies);
- every call a subagent makes;
- every call in a headless run, unless `includeHeadless` is on.

### Headless runs

A `claude -p` child (an `nx_answer` operator, a headless critic) is a main loop with no `agentId`, so the subagent rule does not cover it. The plugin tells the two apart by whether the session draws on any surface (`$.session.surfaces()` is empty in a plain `-p` run) and leaves headless runs alone by default: no one there can follow a note. Set `includeHeadless` to cut them too. A host that attaches a surface to an otherwise headless session (an SDK client with a UI) counts as interactive from then on.

## Options

Set in the install screen, in `/config`, or in settings under `pluginConfigs`. A change reloads the module.

| Option | Default | Meaning |
|---|---|---|
| `limit` | `4000` | Bytes. A `Bash` result (stdout and stderr together) or a `Read` larger than this is cut. |
| `head` | `1400` | Bytes kept from the start. |
| `tail` | `800` | Bytes kept from the end of a `Bash` stream. |
| `includeHeadless` | off | Also cut results in `claude -p` runs. |

A number that is not positive falls back to its default. When `head` + `tail` would not fit in `limit`, both are scaled down together to fit.

## Where the files go

`<temp dir>/claude-tool-spill-<user>/<tool_use_id>.txt`. The temp dir is the first of `$TMPDIR`, `$TEMP` and `$TMP` that is set, else `/tmp` (on macOS that is already a per-user folder under `/var/folders`). The user is the first of `$USER`, `$LOGNAME` and `$USERNAME` that is set, else `user`, made path-safe. The plugin cannot set file permissions: on a shared Linux `/tmp` the directory gets the process's default mode, so on a shared host point `TMPDIR` at a private directory. The mod never deletes spill files; the operating system's temp cleanup does. If the directory cannot be resolved or a file cannot be written, the result passes uncut.

## Develop and test

```
claude plugin validate tool-spill
claude plugin test tool-spill
```

`tests/register.test.ts` holds the plugin's tests. `claude plugin test` needs the `claude` CLI, so it runs by hand and is not part of the repository's CI. `tests/test_plugin_structure.py` does hold the manifest's option defaults equal to the module's constants.
