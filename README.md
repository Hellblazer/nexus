# Nexus

**Permanent memory and search by meaning for Claude.** Nexus runs on your computer. You need no API key, no account, and no separate database install. What Claude learns in one session is there in the next.

[![CI](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/conexus)](https://pypi.org/project/conexus/)
[![Python versions](https://img.shields.io/pypi/pyversions/conexus)](https://pypi.org/project/conexus/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

**Install:** follow the [install guide at hellblazer.github.io/nexus](https://hellblazer.github.io/nexus/). It has a version for Claude Code, for the `nx` CLI alone, and for Claude Desktop, on macOS, Linux and Windows. On macOS, Nexus needs macOS 14 or later on Apple Silicon (the embedder runtime, onnxruntime, ships only macOS 14 wheels from 1.24 on). Windows 11 x64 is supported, with the limits listed in the [Windows install guide](https://github.com/Hellblazer/nexus/blob/develop/docs/windows-install.md#what-is-not-supported-or-not-measured).

The package on PyPI is `conexus`. The command it installs is `nx`.

## Install

There are two routes, and both end in the same install.

**Route one: run one command in a terminal.** It installs [uv](https://docs.astral.sh/uv/) if you do not have it, installs the `nx` command, adds the plugin to Claude Code when `claude` is on your PATH, and asks you one question before it downloads anything. With Local it asks a second, whether to start at login.

On macOS and Linux:

```bash
curl -LsSf https://hellblazer.github.io/nexus/install.sh | sh
```

On Windows, in a normal PowerShell window:

```powershell
irm https://hellblazer.github.io/nexus/install.ps1 | iex
```

To skip the question, add a flag: `--local`, `--cloud` or `--cli-only`, and with `--local` also `--autostart` or `--no-autostart`. On macOS and Linux put them after `sh -s --`, for example `curl -LsSf https://hellblazer.github.io/nexus/install.sh | sh -s -- --local --no-autostart`. The `iex` form cannot take flags, so on Windows run the script as a script block: `& ([scriptblock]::Create((irm https://hellblazer.github.io/nexus/install.ps1))) --local --no-autostart`. When the script asks whether the service should start at login, the answer is No unless you type `y`. The script itself does not need Git, but Claude Code fetches the plugin with Git, so on Windows install Git first if you want the script to add the plugin. If you press Enter at the question, or the script has no terminal, it installs the `nx` command only. `/conexus:setup` in Claude Code, or `nx init` (Local) or `nx config init` (Cloud) in a terminal, sets up a service later. On Linux the script asks `uv` for the CPU build of torch. If your `uv` is older than 0.9.19, run `uv self update` first, or the build downloads about 4.5 GB of GPU libraries that Nexus does not use.

**Route two: install uv, add the plugin, answer one question.** Install `uv` (and git, if you do not have it), then start `claude` and type these two lines inside Claude Code, not in the terminal:

```
/plugin marketplace add Hellblazer/nexus
/plugin install conexus@nexus-plugins
```

When the install finishes, run `/reload-plugins` or start a new session. In that session Claude tells you that Nexus is not installed and asks the question. If you answered Not now or want to answer later, type `/conexus:setup`. Setup runs in the background. When Claude says it is done, reconnect the `nexus` server (`/mcp`, the server, Reconnect) or start a new session.

In Claude Desktop, install `uv`, open Customize → Plugins, add the marketplace `Hellblazer/nexus`, install `conexus`, and say "Set up Nexus" in a conversation. When setup is done, quit Desktop fully and open it again; a new conversation keeps the old server. If you added `Hellblazer/nexus` before, remove it and add it again first. An older copy has no setup tool. Do not type `/conexus:setup` in Desktop; it lists the plugin's skills, not its commands (seen on Claude Desktop 2.42304.0 on macOS). To update the plugin in Desktop, remove the marketplace and add it again.

**The question.** Route two asks it with these four choices. The script asks the same question without Not now, because typing the command is your permission to install:

| Choice | What happens | Download |
|---|---|---|
| Local | Install the CLI and run a Nexus service on this machine | about 1.3 GB down, 2.4 to 3.1 GB on disk |
| Cloud | Install the CLI and connect it to a managed service you have an address, token and tenant for | about 0.7 GB down, 2.1 to 2.3 GB on disk |
| CLI only | Install the CLI and decide about a service later | about 0.7 GB down, 2.1 to 2.3 GB on disk |
| Not now | Nothing is installed | none |

The sizes on disk are the install itself, measured from nothing with conexus 7.81.0 on macOS, Linux and Windows; Windows is the high end. `uv` also keeps a download cache, about 2.4 GB on Linux, 3.6 GB on macOS and 4.8 GB on Windows after a first install. On Linux the cache and the install together measured 2.4 GB, not 4.4 GB, because they share files, so the cache is not all extra space. `uv cache clean` empties it but frees only the files the install does not share. The [install guide](https://hellblazer.github.io/nexus/#before) has the requirements, the Windows path, the Claude Desktop extension, and how to index your first repository.

## Install by hand

Use these commands when a route above fails, or when you want each step yourself. The common case is Claude Code on macOS or Linux, with `uv` and git already installed.

On macOS:

```bash
uv tool install conexus --python 3.12   # install nx; --python 3.12 is required, Python 3.14 does not work yet
nx self install                         # move nx to the layout Nexus manages
nx init                                 # download and start the storage service; answer yes to start at each login (route one defaults to no)
nx doctor                               # every line must show ✓ ("credentials not set" is normal)
```

On Linux, the first command also names the CPU build of torch. Without `--torch-backend cpu`, uv installs the CUDA build on x86-64: 4.3 GB of downloads and 8 GB on disk that Nexus never uses. The flag needs uv 0.9.19 or newer (`uv self update`), and uv prints a warning that it is experimental.

```bash
uv tool install conexus --python 3.12 --torch-backend cpu
nx self install
nx init
nx doctor
```

Then add the plugin with the two lines from route two.

## Update, remove, problems

- **Update:** run `nx self install`, then `nx upgrade`. Do not update with `uv tool install conexus` or `--force`: that removes the local search model and search returns nothing. Details: [Update Nexus](https://hellblazer.github.io/nexus/#update).
- **Remove:** [Remove Nexus completely](https://hellblazer.github.io/nexus/#uninstall) covers exporting your knowledge first, removing the Claude integration, and deleting the service and data.
- **Problems:** see [Problems](https://hellblazer.github.io/nexus/#problems). If nothing there helps, paste the [recovery runbook](https://gist.github.com/Hellblazer/08f0a615e3d73e47d8062bce4829b611) as the first message of a Claude Code session. It checks the install step by step and asks before it changes any data.

## What you installed

One service on your computer: Postgres 17 with pgvector, holding three stores. Scratch lasts one session. Memory holds project facts. Knowledge holds everything you index: code, documents, PDFs, and the decisions you record. The search model runs locally, so your data does not leave the machine.

Once a day the MCP server sends one message with six values: a random install id, the conexus version, the install mode, operating system, CPU type, and Python version. No hostname, paths, collection names, or content. The service also keeps a keyed fingerprint of the network address the message came from, never the address itself; see the [privacy policy](docs/privacy-policy.md). `nx telemetry off` stops it; `nx telemetry status` shows the setting.

## Learn

- [Getting started](https://hellblazer.github.io/nexus/getting-started.html): twelve lessons, all done inside Claude Code.
- [Working with RDRs](https://hellblazer.github.io/nexus/rdr.html): record a decision before you build, in eight lessons.
- [Research with Nexus](https://hellblazer.github.io/nexus/research.html): what you say to Claude at each step of research work, and what you see.
- [Research in Nexus](https://hellblazer.github.io/nexus/research-in-nexus.html): the thinking behind the method, what was borrowed from experimental science and what was left out.
- [The Nexus Tuple Space](https://hellblazer.github.io/nexus/tuple-space.html): how sessions, agents, and hooks coordinate.
- [Coordination](https://hellblazer.github.io/nexus/coordination.html): how sessions and agents coordinate through the tuple space, and which steps the hooks, the channel, and Claude each do.
- [How Agents Build and Ship Nexus](https://hellblazer.github.io/nexus/how-agents-ship.html): how agent sessions, the conexus instance, GitHub and a second build machine coordinate through the tuple space to build, test, review and release Nexus.
- [CLI reference](https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md), [architecture](https://github.com/Hellblazer/nexus/blob/main/docs/architecture.md), [storage tiers](https://github.com/Hellblazer/nexus/blob/main/docs/storage-tiers.md), and the [docs tree](https://github.com/Hellblazer/nexus/blob/main/docs/README.md).
- [Managed service](https://github.com/Hellblazer/nexus/blob/main/docs/managed-onboarding.md), for a hosted deployment with server-side embeddings.

## Push delivery into your session (Claude Code channels)

By default, a message that arrives while you are away — an agent's report, a peer session's reply — waits until your next prompt, when a hook delivers it. Claude Code's channel preview can push it into a running session instead, but only when the session is launched with a channel flag, every time; nothing Nexus installs can set that flag for you.

1. Launch with `claude --channels plugin:conexus@nexus-plugins` (no confirmation dialog once the plugin is on Claude Code's channel allowlist), or `claude --dangerously-load-development-channels server:nexus` (works everywhere the preview does, with a one-keystroke confirmation dialog on every launch).
2. For the dialog-free form, put the plugin on the allowlist: on macOS, write `/Library/Application Support/ClaudeCode/managed-settings.json` (admin-written) with `{"channelsEnabled": true, "allowedChannelPlugins": [{"marketplace": "nexus-plugins", "plugin": "conexus"}]}`.
3. Make it stick: add `alias claude='claude --channels plugin:conexus@nexus-plugins'` to your shell's startup file.
4. Check it worked: the startup screen shows "Channels (experimental) messages from plugin:conexus@nexus-plugins inject directly in this session · restart without --channels to stop"; `nx doctor`'s `tuples.channel_delivery` row reports the waiter's own status once it has run here (alive, last wake, messages announced and pending), or an informational "no record" line for a session whose waiter has not run yet.

Without the flag, mail still arrives at your next prompt through the drain hook — degraded, not broken. Channels are a Claude Code research preview, not available on Amazon Bedrock, Google Cloud Agent Platform, or Microsoft Foundry. See [Coordination](https://hellblazer.github.io/nexus/coordination.html#l5) and [Getting started](https://hellblazer.github.io/nexus/getting-started.html#push-delivery) for more.

## License

Nexus, the `nx` command, and the plugin are free to use. The license covers the Nexus code only. Everything you make with it is yours: the repositories you index, the notes and memory Claude keeps, the documents you store, the RDRs you write, and anything Claude produces in a session are not covered by this license and carry no obligation from it.

The code is AGPL-3.0-or-later ([LICENSE](https://github.com/Hellblazer/nexus/blob/main/LICENSE)). That matters only if you modify Nexus itself and offer the modified version to others. Commercial licenses are available for organizations that need other terms; see [LICENSING.md](https://github.com/Hellblazer/nexus/blob/main/LICENSING.md).
