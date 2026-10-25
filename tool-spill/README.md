# conexus-tool-spill

An opt-in Claude Code mod. When a tool result in the main conversation is larger than a limit, the full text goes to a file and the model sees only the first few KB plus the file's path. A big `grep`, `git log` or file read then costs the main context a few KB, and the model can still read the rest of the file, or hand it to a subagent, when it needs it.

It is not installed with `conexus`. Nothing changes for anyone who does not install it.

## Turn it on

```
/plugin install conexus-tool-spill@nexus-plugins
```

or from a shell:

```
claude plugin install conexus-tool-spill@nexus-plugins
```

It takes effect in the running session. To turn it off, `claude plugin disable conexus-tool-spill`. The `nexus-plugins` marketplace is the one `conexus` and `sn` come from; add it first with `/plugin marketplace add Hellblazer/nexus` if you have not.

## What it does

It hooks `tool.call` and acts on the main conversation only. A subagent's calls are never touched, because heavy reads belong there.

| Tool | Result over the limit |
|---|---|
| `Bash` | `stdout` is cut to the first `head` bytes at a line end, with a note giving the size, the line counts and the path of the full output (stdout, then stderr after a `--- stderr ---` line). Output that Claude Code has already moved to a file of its own (past about 30 KB) is left to it, as are images. |
| `Read` | The text is cut the same way, with a note giving `totalLines` and the `offset` to read next. Nothing is written: the file is already on disk. |
| any other tool (`Grep`, MCP tools) | The call has run, and its result is withheld: the model gets the first `head` bytes and the path of the full text, as a denial. |
| `Agent`, `Task`, `TodoWrite`, `AskUserQuestion`, `Edit`, `Write`, `NotebookEdit` | Never touched. |

A result at or under the limit passes unchanged. An errored result passes unchanged.

## Options

Set in the install screen, in `/config`, or in settings under `pluginConfigs`. A change reloads the module.

| Option | Default | Meaning |
|---|---|---|
| `limit` | `4000` | Bytes. A result larger than this is cut. |
| `head` | `2000` | Bytes of a cut result the model still sees, trimmed back to a line end. Capped at `limit`. |

A value that is not a positive number falls back to the default.

## Where the files go

`<temp dir>/claude-tool-spill/<tool_use_id>.txt`, where the temp dir is the first of `$TMPDIR`, `$TEMP` and `$TMP` that is set, else `/tmp`. On macOS that is the per-user folder under `/var/folders`. The mod never deletes them; the operating system's temp cleanup does.

## Develop and test

```
claude plugin validate tool-spill
claude plugin test tool-spill
```

`tests/register.test.ts` holds the plugin's tests. `claude plugin test` needs the `claude` CLI, so it runs by hand and is not part of the repository's CI.
