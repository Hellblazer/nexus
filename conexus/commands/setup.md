---
description: Set up Nexus on this machine. Shows what is missing, asks, then installs the CLI and a service in the background.
---

# Set up Nexus

The way back after "not now" or "CLI only", the way to start a service that is down or repair a CLI that cannot start, and the way to follow a setup that is running. It uses the nexus server's two setup tools and nothing else: it runs no shell command, because it has to work on a machine where `nx` does not exist.

## Step 1: find out what is missing

Call the `setup_status` tool of the nexus server (in Claude Code its full name is `mcp__plugin_conexus_nexus__setup_status`) and read the answer.

If the nexus server offers no `setup_status` tool, the installed Nexus CLI is older than the release that carries the setup tools. Say that, and give the user these two commands as text, to run in a terminal. Do not run them yourself.

```
nx self install
nx upgrade
```

The first builds the current version and the second converges the data. On the older uv-tool layout (`nx self install` says this nx is not running from a generation) the first is `uv tool upgrade conexus`. Afterwards the user reconnects the `nexus` server or starts a new session, and runs `/conexus:setup` again. Stop here.

## Step 2: act on the answer

- **Nothing is missing.** Say so and stop. Do not call `setup`.
- **A setup run is in progress.** Report its stage and when to ask again. Do not call `setup`. Follow it with `setup_status`, no sooner than the interval the answer names.
- **Something is missing.** Say what, in the answer's words, and put the one question below. Do not call `setup` before the user has answered.
- **The last run failed or was interrupted.** Show the failure line and the log path from the answer. Ask the user first; if they agree, call `setup` again with the same choice the answer names, and it resumes from the failed step.

## The one question

ASK THE USER FIRST, once, and act only on the answer. Show only the choices that are still open for what is missing (the CLI is installed already: no CLI-only; it cannot start: CLI only repairs it by building it again):

| Choice | What happens | Download |
|---|---|---|
| Local | Install the CLI and run a Nexus service on this machine | about 1.3 GB down, 2.4 GB on disk |
| CLI only | Install the CLI and decide about a service later | about 0.7 GB down, 1.6 GB on disk |
| Not now | Nothing is installed | none |

With Local, also ask whether the service should start when the user logs in: yes registers a login unit, and no means the user starts the service again after each reboot.

## Step 3: call `setup` with the answer

Call the `setup` tool with `mode` set to `local`, `cli-only` or `not-now`. For `local` pass `autostart` true only if the user said yes to starting at login; leave it out otherwise.

The call returns at once and the work continues in the background for several minutes. Tell the user it has started, then follow it with `setup_status`, not in a loop and no sooner than the interval each answer names. When the answer says setup is done, the user reconnects the `nexus` server (in Claude Code: `/mcp`, the server, Reconnect) or starts a new session before the Nexus tools appear.

"Not now" installs nothing and stops the session-start question. This command still works whenever the user changes their mind.
