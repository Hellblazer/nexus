# Nexus

**Permanent memory and search by meaning for Claude.** Nexus runs on your computer. You need no API key, no account, and no separate database install. What Claude learns in one session is there in the next.

[![CI](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/conexus)](https://pypi.org/project/conexus/)
[![Python versions](https://img.shields.io/pypi/pyversions/conexus)](https://pypi.org/project/conexus/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

**Install:** follow the [install guide at hellblazer.github.io/nexus](https://hellblazer.github.io/nexus/). It has a version for Claude Code, for the `nx` CLI alone, and for Claude Desktop, on macOS, Linux and Windows.

The package on PyPI is `conexus`. The command it installs is `nx`.

## Quick start

The common case: Claude Code on macOS or Linux, with [uv](https://docs.astral.sh/uv/) and git already installed.

```bash
uv tool install conexus --python 3.12   # install nx; --python 3.12 is required, Python 3.14 does not work yet
nx self install                         # move nx to the layout Nexus manages
nx init                                 # download and start the storage service; answer yes to start at login
nx doctor                               # every line must show ✓ ("credentials not set" is normal)
```

Then start `claude` and type these two lines inside Claude Code, not in the terminal:

```
/plugin marketplace add Hellblazer/nexus
/plugin install conexus@nexus-plugins
```

The [install guide](https://hellblazer.github.io/nexus/#before) has the requirements, the Windows path, the Claude Desktop extension, and how to index your first repository.

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
