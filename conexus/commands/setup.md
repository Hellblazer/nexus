---
description: Set up Nexus on this machine. Shows what is missing, asks, then installs the CLI and a service in the background.
---

# Set up Nexus

The way back after "not now" or "CLI only", the way to start a service that is down or repair a CLI that cannot start, and the way to follow a setup that is running. It uses the nexus server's two setup tools and nothing else: it runs no shell command, because it has to work on a machine where `nx` does not exist.

## Step 1: find out what is missing

Call the `setup_status` tool of the nexus server (in Claude Code its full name is `mcp__plugin_conexus_nexus__setup_status`) and read the answer.

If the nexus server offers no `setup_status` tool, first check that the server is connected: in Claude Code look at `/mcp`. A server that is disconnected, failed or still starting offers no tools at all, and that is not an older CLI. Tell the user what `/mcp` shows; if the server is not connected, have the user reconnect it (`/mcp`, the server, Reconnect) or start a new session, then call `setup_status` again.

Only when the server is connected and still offers no `setup_status` tool, the installed Nexus CLI is older than the release that carries the setup tools. Say that, and give the user these two commands as text, to run in a terminal. Do not run them yourself.

```
nx self install
nx upgrade
```

The first builds the current version and the second converges the data. On the older uv-tool layout (`nx self install` says this nx is not running from a generation) the first is `uv tool upgrade conexus`, and a third command, `nx self install`, then moves the box onto the generation layout and keeps PDF support (the upgrade alone leaves a slim tree). Afterwards the user reconnects the `nexus` server or starts a new session, and runs `/conexus:setup` again. Stop here.

## Step 2: act on the answer

- **Nothing is missing.** Say so and stop. Do not call `setup`.
- **A setup run is in progress.** Report its stage and when to ask again. Do not call `setup`. Follow it with `setup_status`, no sooner than the interval the answer names.
- **The local service is installed and not running.** Say so, in the answer's words. Ask whether to start it and whether it should start when the user logs in; do not show the table, because starting it downloads nothing. If the user agrees, call `setup` with `mode` `local` and `autostart` as they answered.
- **Something is missing.** Say what, in the answer's words, and put the one question below. Do not call `setup` before the user has answered.
- **The last run failed or was interrupted.** Show the failure line and the log path from the answer. Ask the user first; if they agree, call `setup` again with the same choice the answer names, and it resumes from the failed step.

## The one question

ASK THE USER FIRST, once, and act only on the answer. Show only the choices that are still open for what is missing (the CLI is installed already: no CLI-only; it cannot start: CLI only repairs it by building it again, and Cloud or Local repair it and then set up a service):

| Choice | What happens | Download |
|---|---|---|
| Local | Install the CLI and run a Nexus service on this machine | about 0.8 GB down (0.9 GB on Linux), 2.5 to 2.9 GB on disk |
| Cloud | Install the CLI and connect it to a managed service you have an address, token and tenant for | about 0.45 GB down (0.6 GB on Linux), 1.8 to 2.2 GB on disk |
| CLI only | Install the CLI and decide about a service later | about 0.45 GB down (0.6 GB on Linux), 1.8 to 2.2 GB on disk |
| Not now | Nothing is installed | none |

These sizes include PDF support (docling, MinerU and torch). They were measured on 7.85.0 on macOS and Linux (2026-10-24); Windows was not measured. They are not a promise. Slim (`setup` with `slim` true) leaves PDF support out: about 0.25 GB less to download on macOS, 0.37 GB on Linux, and 0.85 GB less on disk on macOS and 1.3 GB on Linux, and the first PDF indexed then installs it. Offer slim only if the user asks to leave PDF support out.

With Local, also ask whether the service should start when the user logs in: yes registers a login unit, and no means the user starts the service again after each reboot.

With Cloud, setup opens a page in the user's browser where they type the service address and token themselves. Never ask for the token, and never put it in a message or a tool call: it must not pass through this conversation. Tell the user the page is coming and that they fill it in there.

## Step 3: call `setup` with the answer

Call the `setup` tool with `mode` set to `local`, `cloud`, `cli-only` or `not-now`. For `local` pass `autostart` true only if the user said yes to starting at login; leave it out otherwise. Pass `slim` true only if the user asked to leave PDF support out; leave it out otherwise.

The call returns at once and the work continues in the background for several minutes. Tell the user it has started, then follow it with `setup_status`, not in a loop and no sooner than the interval each answer names. When the answer says setup is done, the user reconnects the `nexus` server (in Claude Code: `/mcp`, the server, Reconnect) or starts a new session before the Nexus tools appear.

"Not now" installs nothing and stops the session-start question. This command still works whenever the user changes their mind.
