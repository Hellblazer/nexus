# Nexus

**Permanent memory and search by meaning for Claude.** Nexus runs on your computer. You need no API key, no account, and no separate database install. What Claude learns in one session is there in the next.

[![CI](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/Hellblazer/nexus/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/conexus)](https://pypi.org/project/conexus/)
[![Python versions](https://img.shields.io/pypi/pyversions/conexus)](https://pypi.org/project/conexus/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

The package on PyPI is `conexus`. The command it installs is `nx`. The full install guide, with a version for each way of using Claude, is at [hellblazer.github.io/nexus](https://hellblazer.github.io/nexus/). The commands below are the same ones.

## Before you start

| Need | Why | Check |
|---|---|---|
| Python 3.12 or 3.13 | Python 3.14 does not work yet. If needed, uv downloads 3.13 for you. | `python3 --version` |
| [uv](https://docs.astral.sh/uv/) | Installs and runs the `nx` command. | `uv --version` |
| git | Nexus reads git information when it indexes a repository. | `git --version` |
| Node.js with npm | Required for the Claude Code plugin. Without it the plugin installs but its tools never appear, with no error message. | `node --version` |
| About 600 MB, a few minutes | The first run downloads the service program, a database, and the search model. | |

## Install

Run the steps in this order. Each step can be run again without harm.

**1. Install the `nx` command.** The second command moves it to the layout that Nexus manages.

```bash
uv tool install conexus
nx self install
```

If the terminal cannot find `nx` afterward, add `~/.local/bin` to your PATH and open a new terminal.

**2. Set up the storage service.** This downloads the service program, the database, and the search model, starts the service, and asks whether it should start at login. Answer yes.

```bash
nx init
```

`nx init --yes` answers for you. `nx init --no-autostart` skips the login item.

**3. Check the install.**

```bash
nx doctor
```

Every line must show ✓. One line may say "credentials not set". That is normal for a local install.

**4. Add the Claude Code plugin.** Check `node --version` first. Then start `claude` and type these two commands inside Claude Code, not in the terminal:

```
/plugin marketplace add Hellblazer/nexus
/plugin install conexus@nexus-plugins
```

Then run `/conexus:nx-preflight`. It checks that everything the plugin needs is present. The plugin uses the `nx` command from step 1, which is why step 1 comes first.

**5. Index a repository and search it.**

```bash
cd your-project
nx index repo .
nx search "how does retry work"
```

Indexing the same repository again skips files that did not change. For a large repository, add `--monitor` to see progress per file.

**Claude Desktop without Claude Code:** do steps 1 to 3, then download `conexus.mcpb` from the [latest release](https://github.com/Hellblazer/nexus/releases/latest) and double-click it. Claude Desktop adds it under Settings, Connectors, as "Conexus". If you already use the plugin in Claude Code, do not add the extension as well; Claude Desktop already sees the plugin's tools.

## Update

Two commands. Always run both.

```bash
nx self install   # install the new version next to the current one
nx upgrade        # update the service, the data, and the plugins
```

Do not update with `uv tool install conexus` or with `--force`. That removes the local search model, and search then returns nothing. If that happened, `nx self install` repairs it.

## Remove

```bash
nx uninstall                          # preview, changes nothing
nx uninstall --yes --remove-data      # stop the service, remove autostart, delete the data
uv tool uninstall conexus
rm -rf ~/.local/share/nexus
```

The full sequence, including exporting your knowledge first and removing the Claude integration, is on the site under [Remove Nexus completely](https://hellblazer.github.io/nexus/#uninstall).

## Problems

| Symptom | Do this |
|---|---|
| `nx: command not found` | Add `~/.local/bin` to your PATH and open a new terminal. If PATH is right, run `nx self install`. |
| Crash on startup, or an import error naming voyageai or Pydantic v1 | You are on Python 3.14. Run `uv python install 3.13`, then `uv tool install conexus --force --python 3.13`, then `nx self install`. |
| `nx doctor` says credentials not set | Normal for a local install. Only the cloud service needs a token. |
| `nx search` returns nothing | Run `nx doctor`. If the index was interrupted, run `nx index repo .` again. If you updated with `uv tool install`, see Update above. |
| Plugin installed but its tools never appear | The `nx` command or Node.js is missing. Run `/conexus:nx-preflight`; it says which. |
| Nothing above helps | Paste the [recovery runbook](https://gist.github.com/Hellblazer/08f0a615e3d73e47d8062bce4829b611) as the first message of a Claude Code session. It checks the install step by step and asks before changing any data. |

## What you installed

One service on your computer: Postgres 17 with pgvector, holding three stores. Scratch lasts one session. Memory holds project facts. Knowledge holds everything you index: code, documents, PDFs, and the decisions you record. The search model runs locally, so your data does not leave the machine.

Once a day the MCP server sends one anonymous message with six values: a random install id, the conexus version, the install mode, operating system, CPU type, and Python version. No hostname, paths, collection names, or content. `nx telemetry off` stops it; `nx telemetry status` shows the setting.

## Learn

- [Getting started](https://hellblazer.github.io/nexus/getting-started.html): eleven lessons, all done inside Claude Code.
- [Working with RDRs](https://hellblazer.github.io/nexus/rdr.html): record a decision before you build, in eight lessons.
- [Research with Nexus](https://hellblazer.github.io/nexus/research.html): what the store does at each step of research work.
- [The Nexus Tuple Space](https://hellblazer.github.io/nexus/tuple-space.html): how sessions, agents, and hooks coordinate.
- [CLI reference](https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md), [architecture](https://github.com/Hellblazer/nexus/blob/main/docs/architecture.md), [storage tiers](https://github.com/Hellblazer/nexus/blob/main/docs/storage-tiers.md), and the [docs tree](https://github.com/Hellblazer/nexus/blob/main/docs/README.md).
- [Managed service](https://github.com/Hellblazer/nexus/blob/main/docs/managed-onboarding.md), for a hosted deployment with server-side embeddings.

## License

Nexus, the `nx` command, and the plugin are free to use. The license covers the Nexus code only. Everything you make with it is yours: the repositories you index, the notes and memory Claude keeps, the documents you store, the RDRs you write, and anything Claude produces in a session are not covered by this license and carry no obligation from it.

The code is AGPL-3.0-or-later ([LICENSE](https://github.com/Hellblazer/nexus/blob/main/LICENSE)). That matters only if you modify Nexus itself and offer the modified version to others. Commercial licenses are available for organizations that need other terms; see [LICENSING.md](https://github.com/Hellblazer/nexus/blob/main/LICENSING.md).
