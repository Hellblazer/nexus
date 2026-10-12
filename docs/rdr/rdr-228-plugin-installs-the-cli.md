---
title: "The Plugin Installs the CLI"
id: RDR-228
type: Feature
status: draft
priority: high
author: Sam
reviewed-by: self
created: 2026-10-12
accepted_date:
related_issues: [nexus-qkb9v]
related_rdrs: [RDR-126, RDR-143, RDR-174, RDR-197, RDR-218, RDR-224]
---

# RDR-228: The Plugin Installs the CLI

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

Drafted 2026-10-12 against develop `067155b83`, conexus 7.77.1. No product code
has changed.

**Terms used here.** The *plugin* is the `conexus` Claude Code plugin: the
skills, hooks and server definitions a user adds from the marketplace. The
*CLI* is the `conexus` Python package, whose programs are `nx`, `nx-mcp`,
`nx-mcp-catalog`, `nx-hook` and `nx-session-end-launcher`. An *MCP server* is a
program a Claude application starts and talks to over standard input and output
to get tools. A *hook* is a command the application runs at a fixed moment,
such as the start of a session. `uv` is the Python package manager the plugin
already uses to run its own scripts. A *generation* is one complete install of
the CLI in its own directory, `<tools>/gen-<stamp>`, with a pointer named
`current` that says which one is live (`src/nexus/_install/layout_core.py`).
The three *surfaces* are the Claude Code terminal program, the Code tab of the
Claude Desktop application, and Cowork (the Desktop application's task mode).

## Problem Statement

Adding the plugin does not give a user a working Nexus. The plugin's servers
and most of its hooks are programs from the CLI, and nothing installs the CLI.
A new user on macOS or Linux runs five terminal commands before the two plugin
commands (`uv`, `git`, `uv tool install conexus --python 3.12`,
`nx self install`, `nx init`), and a Windows user runs about nine. A Desktop
user who never opens a terminal cannot get there at all. RDR-218 records the
requirement that a CLI must not be an installation prerequisite. No platform
meets it today.

### Enumerated gaps to close

#### Gap 1: The plugin names programs it does not provide

`conexus/.mcp.json` starts the two Nexus servers by the bare names `nx-mcp` and
`nx-mcp-catalog`. `conexus/hooks/hooks.json` has six session-start entries that
run `nx-hook`, and the session-end entry runs `nx-session-end-launcher`. All 17
commands begin with an `nx` shell line. Every one of these is a program of the
CLI package. With the CLI absent the servers fail to start and each hook
reports "Executable not found in $PATH" (measured, Research Findings item 3).

#### Gap 2: A user without the CLI is told the opposite of the truth

In that state `version_lockstep_hook.py` tells the model that "an
extras-preserving upgrade has been dispatched in the background" and that no
action is needed. The upgrade it dispatches is `uv tool upgrade conexus`, which
does nothing when no install exists. The `uv` tool directory was empty
afterwards (measured). The one hook that warns "nx is missing" is itself an
`nx-hook` verb, so it cannot run in the case it exists for.

#### Gap 3: Nothing provisions the local service

A CLI on disk is still not a working Nexus. `nx init` downloads the engine (the
Java service that stores and searches), its Postgres bundle and two embedding
models, about 0.7 GB over two to five minutes, and starts the service. The
plugin does none of this, and the MCP server deliberately starts nothing
(`src/nexus/mcp/core.py`, the "NO first-run daemon install here" note).
`docs/desktop-deployment.md` says the Claude surfaces "auto-start what they
need on first launch", which is false.

#### Gap 4: Cowork starts each session with a 15 second wait

Cowork runs the Claude Code program bundled with the Desktop application and
passes it each plugin with server discovery turned off, because the Desktop
application starts the servers itself. Under that launch the plugin's
`mcp-connect-wait` hook waited its full 15 seconds and reported that `nx-mcp`
did not connect (reproduced with Desktop's own binary; not yet seen in a live
Cowork session).

## Relationship to Prior RDRs

- **RDR-143** (plugin and CLI version lockstep) rejected a plugin-driven
  `uv tool install conexus==X` on three grounds: it removed the `[local]` extra
  and with it local search, it blocked session start on a network install, and
  it overwrote a developer's editable install. This record reverses that
  rejection for the case RDR-143 did not cover, a machine with no CLI at all,
  and answers each ground in Decision Rationale. RDR-143's upgrade action for
  an existing install is unchanged.
- **RDR-174** (unified `nx init`) ruled that no autostart unit (the launchd or
  systemd entry that restarts the service at login) is written without the
  user's explicit yes. This record keeps that rule.
- **RDR-218** states the requirement this record implements.
- **RDR-126** rejected a hosted remote MCP endpoint and a bundled Python
  runtime. Both stay rejected here; see Alternatives.
- **RDR-224** (native Windows) supplies the Python statement of the generation
  installer, `generation_core.py`, which the Windows path of this design uses.

## Context

### Background

Sam installed the plugin on a clean Windows box on 2026-10-11 and found that
the Desktop application's chat and Cowork modes read plugins from the claude.ai
account, not from the terminal program's plugin directory, and that the CLI was
still required after that. He asked why the CLI is required and whether that
can be fixed, for all three surfaces.

### Technical Environment

`uv` is already a hard prerequisite of the plugin. Seven hook entries and the
`sequential-thinking` server run as
`uv tool run --directory ${CLAUDE_PLUGIN_ROOT} --no-config --quiet --python >=3.12 python <script>`.
A script launched this way is standard-library Python with no Nexus import.
The plugin already ships one such MCP server (`conexus/mcp/sequential_thinking.py`).

A first install today is two commands: `uv tool install conexus --python 3.12`
puts the CLI in `uv`'s own tool directory, and `nx self install` moves it to
the generation layout. An install into a new generation never writes under a
running process, so it is safe while sessions are live.

## Research Findings

### Investigation

Measured on Sam's laptop (macOS, Claude Desktop with bundled Claude Code
2.1.295, `uv` 0.8.0) and on a clean Windows 11 box, 2026-10-11. Full records:
T2 `nexus/install-friction-analysis-three-surfaces-2026-10-11`,
`nexus/install-friction-measurements-laptop-2026-10-11` and
`nexus/desktop-install-friction-qwent-2026-10-11`.
The 2026-10-12 runs are in `nexus/install-friction-desktop-live-conversation-2026-10-12`
and `nexus/install-friction-phase0-rdr228-2026-10-12`. Each discovery and
assumption below is also a research record, `nexus_rdr/228-research-1`
through `-14`, with its classification and method: eleven verified (eight
by running it, three by reading source or logs), two documented, and one
assumed (`-13`, replaced by the Windows run in `-14`).

### Key Discoveries

1. **Verified.** Cowork's agent loop is the Claude Code program. Old Cowork
   session records name its version and list plugins staged in a temporary
   directory. The Desktop application's own code passes each plugin as
   `--plugin-dir` or `--plugin-dir-no-mcp`, and turns hooks off only for a
   one-off query, not for a session.
2. **Verified by reproduction, not in a live Cowork session.** Running that
   bundled program with `--plugin-dir-no-mcp` on the account copy of the plugin
   registered 15 hooks from 5 plugins and ran every session-start hook. So a
   hook-driven install reaches Cowork. The last live Cowork session on the
   laptop was 2026-07-13, before conexus was in the account.
3. **Verified.** With `uv` present and the CLI absent (isolated home directory,
   `PATH` holding only `uv`): six "Executable not found" hook errors, the false
   lockstep message, and nothing installed.
4. **Verified.** The `uv` launch form costs nothing measurable. Medians of 20
   runs: bare `python` 26 ms, the `uv tool run` form 19 ms, an `nx-hook`
   dispatch 22 ms. First run with an empty `uv` cache: 68 ms.
5. **Verified.** The Desktop application starts plugin servers on the host with
   a login-shell `PATH`. On macOS it resolved `nx-mcp` to `~/.local/bin`; on
   Windows to `<tools>\current\bin\nx-mcp.exe`. Both Nexus servers connected on
   both boxes on 7.77.1.
6. **Verified.** The claude.ai account plugin store does not follow release
   tags. The account copy sat at 7.67.0 through 15 releases while the Desktop
   application synced every 20 minutes. It moved only when the marketplace was
   removed and added again, and that change reached every machine on the
   account.
7. **Verified, one live conversation, 2026-10-12** (Desktop 2.31226.1, which
   shows chat and Cowork as one view). A message asking for a Nexus search and
   a shell command was answered by a loop that does not run on the laptop. The
   search reached the `nx-mcp` process the Desktop application had started on
   the laptop the day before, through the application's bridge for local
   servers. The shell command ran in what the model called its cloud
   workspace. The Desktop log shows no local Claude Code process for the
   conversation and no session record was written, so no plugin hook ran. This
   contradicts what Discoveries 1 and 2 suggested for today's build: those
   describe the local agent sessions recorded up to 2026-07-13. Whether the
   application still starts a local session in some cases is not known; a
   workspace virtual machine did boot on the laptop a minute after the answer,
   for a reason the logs do not show.
8. **Documented.** Plain chat and the claude.ai web application ignore a
   plugin's local servers and hooks. Cowork refuses a plugin that has a
   top-level `bin/` directory. `${CLAUDE_PLUGIN_DATA}` is a per-plugin
   directory that survives plugin updates. Cowork does not prompt for a
   plugin's `userConfig` values.

### Critical Assumptions

- **CA-1 (Refuted for the conversation measured).** The draft assumed a
  Desktop session runs the plugin's hooks. In the live conversation of
  Discovery 7 no hook ran, because the loop was not on the laptop. The design
  now treats the stand-in server as the only channel that reaches every
  surface; see "How the question reaches the user".
- **CA-2 (Not applicable to that path).** With no hooks, the 15 second
  `mcp-connect-wait` did not run. It remains measured for the
  `--plugin-dir-no-mcp` launch of Gap 4, and the fix stays in scope for any
  local session that uses it.
- **CA-3 (Refuted, 2026-10-12).** The draft assumed `nx self install` could
  run from a throwaway `uv` environment
  (`uv tool run --from conexus==7.77.1 nx self install`). It refuses: "this nx
  is running from <uv cache path>, which is not a generation". The two
  documented commands do work from an empty home directory:
  `uv tool install conexus==7.77.1 --python 3.12` and then `nx self install`
  took 40 seconds with the packages already in `uv`'s cache and produced a
  working `nx`. That route leaves two full copies on disk, the generation
  (1.6 GB) and the `uv tool` tree it was converted from (1.7 GB), until a
  later install removes the second. The design adds a direct route; see "The
  setup process".
- **CA-4 (Verified in Claude Code; read from code for Desktop).** A
  twenty-line standard-library server that sets `instructions` and offers
  `setup` and `setup_status` was started by Claude Code 2.1.295. The model
  quoted the instructions text exactly and called `setup_status`. For
  Desktop, the application's bridge for local servers was read in its
  shipped code: it passes each tool's name, description and schema to the
  conversation and nothing else, so a server's `instructions` text does not
  arrive there. Not tested: a reconnect replacing the stand-in with the real
  server, and the stand-in under Desktop itself, which needs a released
  plugin.
- **CA-5 (Verified on native Windows, 2026-10-12; not under Desktop).** On
  the Windows 11 box (`uv` 0.12.23), a client speaking the MCP handshake
  started three servers through the plugin's `uv tool run` form. The stand-in
  script answered in 0.2 seconds with its `instructions` and two tools. A
  prototype launcher found `nx-mcp.exe` under `<tools>\current\bin`, started
  it as a child and relayed its input and output: the real server answered
  in 2.3 seconds with 44 tools and exited cleanly when its input closed. The
  same launcher, asked for a program that does not exist, served the
  stand-in. Not run: the same under Claude Code on that box (its command-line
  login is not available over ssh) and under Desktop, where the evidence is
  still that `sequential-thinking`, which uses this form, connects.
- **CA-6 (Verified, 2026-10-12).** `nx init --yes --no-autostart`, run in its
  own session with standard input closed, in an empty home directory on
  macOS: exit 0 in 46 seconds on a fast connection, service serving, with
  0.25 GB under the config directory and 0.53 GB of models.
- **CA-7 (Verified, one observation).** The shell tool in a Desktop
  conversation does not run on the user's machine, so a script run through it
  cannot install the CLI there (Discovery 7). A plugin server does run on the
  machine.

## Proposed Solution

### Approach

The plugin stops naming CLI programs directly. Every server and hook entry that
needs the CLI goes through one small launcher script that ships in the plugin
and runs under `uv`, as seven hooks already do. When the CLI is present the
launcher runs the real program. When it is absent, the first session says so
and asks one question that carries every choice: install with a local
service, install for the managed cloud, install the CLI only, or not now. One
answer starts one background process that does all of it. Nothing is installed
that the user did not agree to (Sam, 2026-10-12). A one-command installer script
ships with the same work for terminal users, and it is the one path that also
installs `uv`. For everyone else `uv` and the plugin are the only two things a
user installs.

### Technical Design

**The launcher.** `conexus/launch/nx_launch.py`, standard library only, no
Nexus import, pinned by a test that imports it with `nexus` made unimportable.
It is not under `bin/`, which Cowork refuses. It takes the name of a CLI
program and that program's arguments. It finds the program in this order: the
generation layout's `<tools>/current/bin`, then a `PATH` lookup that skips the
current directory (the existing `_exec_path.which_off_cwd`). The layout is
checked first because an application started before the install does not see a
`PATH` change until it restarts. When the program is found the launcher
replaces itself with it (`os.execv`; on Windows it spawns the program and
relays its exit code), so a running server has no wrapper between it and the
application.

**When setup is not complete.** Setup is complete when the CLI resolves and
either no setup status file exists (the user installed by hand) or the status
file says complete. Until then the launcher's behaviour depends on what was
asked for:

- *A hook verb.* With no CLI, exit 0 with one line on standard error. A
  missing CLI never blocks a prompt or a tool call. This absorbs the "no
  `nx-hook`" branch of `nx_hook_shim.py`. With a CLI, run the verb.
- *An MCP server.* Serve a stand-in server: answer the handshake, set the
  server instructions to one sentence saying Nexus is not set up yet, and
  offer two tools. `setup` takes `mode` and starts the work. `setup_status`
  reports the current stage, the log path and the next step. The stand-in does
  not try to swap itself for the real server inside one connection; when setup
  is done its status says to reconnect the server or start a new session.

The setup tools are on the stand-in server, not in a script the model runs
through its shell tool, for two reasons. A server runs on the user's machine on
every surface, where Cowork's shell tool runs in a sandbox (CA-7). And each
Claude application shows its own permission prompt before a plugin tool runs,
so the user sees and approves the install in the application's own interface
as well as in the conversation.

**The one question.** The four choices, as the user is shown them:

| Choice | What happens | Download |
|---|---|---|
| Local | Install the CLI and run a Nexus service on this machine | about 1.3 GB down, 2.4 GB on disk |
| Cloud | Install the CLI and connect it to a managed service you have an address and token for | about 0.7 GB down, 1.6 GB on disk |
| CLI only | Install the CLI and decide about a service later | about 0.7 GB down, 1.6 GB on disk |
| Not now | Nothing is installed | none |

The CLI is the larger part: 216 Python packages, 1.6 GB installed (measured
2026-10-12). The local service adds about 0.8 GB on disk. Earlier drafts of
this record called the CLI "about 0.1 GB", which was wrong.

**How the question reaches the user.** Two channels carry the same state and
the same table, because no one channel reaches every surface. In Claude Code
and the Code tab a session-start hook tells the model at the start of the
session. In a Desktop conversation no hook runs (Discovery 7), and the only
part of the plugin on the user's machine that the model can see is the
server. So the stand-in server also states it, twice. Its `instructions` text
says Nexus is not set up and that `/conexus:setup` or the `setup` tool starts
it; Claude Code delivers that text to the model (CA-4). Both tools'
descriptions say the same, and the descriptions are the part that reaches a
Desktop conversation, because Desktop's bridge passes tools and not
`instructions` (CA-4). There the question is asked when
the user first asks for something Nexus would do, not at the start.

**The setup hook.** A new session-start hook, `nx_setup_notice.py`, first in
the list, runs the same way as the launcher. It starts nothing. It returns in
milliseconds and tells the model the state, which is one of:

1. Setup is complete: say nothing.
2. Nothing started: tell the model that Nexus is not set up, give it the table
   above, and ask it to put the one question to the user. The model calls
   `setup` with the answer, or records "not now".
3. Setup is running (a lock file with a live process id under
   `${CLAUDE_PLUGIN_DATA}`, falling back to `~/.config/nexus`): report its
   stage.
4. A step failed: report the failure line and the log path. `setup` called
   again resumes from the failed step.
5. The user chose CLI only, or said not now: a marker records it, and later
   sessions say one line ("Nexus is not fully set up; run `/conexus:setup`")
   without asking again.

The model is asked to put the question once and to take no setup action
without an answer. That instruction is a request to a model, so it is not the
control. The control is that only a tool call starts anything, and the
application prompts for the tool call.

**The `/conexus:setup` command.** The way back after "not now" or "CLI only",
and the way to see progress. It reads the status, shows the same table with
the choices still open, and calls `setup` with the answer. When setup is
complete it says so and shows the `nx doctor` result.

**The setup process** (`setup`). One detached process per call, logging to one
file and writing its stage to a small JSON status file. The tool returns at
once with the stage; `setup_status` follows it. The steps, each skipped when
already done:

1. *Build.* `nx self install` gains a third starting point. Today it builds
   from a running generation or converts a `uv tool` install, and refuses
   anything else (CA-3). The new branch accepts a throwaway `uv` environment,
   so the build is one command and one copy on disk:
   `uv tool run --python 3.12 --from conexus==<plugin version> nx self install`.
   It builds `gen-<stamp>`, points `current` at it and writes the command
   shims. The version is the plugin's own, read from its `plugin.json`, so
   the first CLI matches the plugin by construction. A CLI release older
   than the one that carries the new branch refuses the one-command form, so
   the setup process falls back to the two documented commands when it does.
   Mode `cli-only` stops here.

   `nx self install` ends by pruning `uv`'s shared package cache. On a
   developer's machine that removed 23.4 GiB in the test for CA-3. For a new
   user the cache holds only what this install fetched, and the prune is the
   existing behaviour of every install, so it stays.
2. *Service, mode `local`.* `nx init --yes --no-autostart` through the new
   generation. It never writes an autostart unit. The final status says how to
   add one (`nx daemon service install --autostart`) and that without it the
   service must be started again after a reboot.
3. *Service, mode `cloud`.* The credentials page, below. No service download.
4. *Verify.* `nx doctor`; record pass or the failing rows, and mark complete.

**The credentials page.** A cloud token must reach the config file without
passing through the model, the conversation or a tool argument, and a Desktop
user has no terminal (Sam, 2026-10-12). So in mode `cloud` the setup process
serves one small web page and opens it in the user's browser at the start of
the run, while the build proceeds, so the user is not asked for anything
later.

- The process listens on the loopback address `127.0.0.1` only, on a port the
  operating system picks, and the page's address carries a random one-time
  path. A request without that path gets nothing.
- The page has two fields, service address and token, and one button. On
  submit the process checks the pair against the service's `/health` and
  `/version` routes, writes them to the Nexus config file with owner-only
  permissions, shows the result on the page and stops listening.
- It stops listening after ten minutes with nothing submitted, and `setup`
  called again opens a new page.
- The token is never written to the log or the status file. The status says
  only "waiting for credentials", "credentials saved" or "credentials
  rejected".
- When no browser can be opened, the status gives the page's address for the
  user to open by hand.

**The installer script.** `install.sh` and `install.ps1`, published on the
site, for a user who starts in a terminal. Typing the command is the consent
for the CLI, so it does not ask about that. It installs `uv` when `uv` is
absent, runs the same setup process as the plugin, and adds the plugin to
Claude Code when `claude` is on `PATH`. For the service it asks the same
question once on the terminal, before any download, or takes `--local`,
`--cloud` or `--cli-only`; with no terminal attached and no flag it builds the
CLI only and says so. `--cloud` reads the address and token on the terminal
with echo off. The script cannot reach the claude.ai account plugin store, so
a Desktop user still adds the plugin in the application.

**Entries that change.** `.mcp.json`: `nexus` and `nexus-catalog` become the
`uv tool run ... python ${CLAUDE_PLUGIN_ROOT}/launch/nx_launch.py nx-mcp` form.
`hooks.json`: the six `nx-hook` entries and the session-end entry go through
the launcher. The 17 commands keep their `nx` lines; a command typed before the
install finishes fails with the shell's own "command not found", and the
session-start message has already said why.

**Defects fixed in the same change.**

- `version_lockstep_hook.py` checks for the CLI before it claims an upgrade is
  in flight. With no CLI it says nothing and leaves the message to the
  setup hook.
- `mcp-connect-wait` returns at once when the session was started with server
  discovery off for this plugin, if CA-2 confirms the wait. The signal to key
  on is decided by the Phase 0 measurement.
- `docs/desktop-deployment.md` loses the "auto-start what they need" sentence
  and gains the real first-run sequence.

### Existing Infrastructure Audit

| Need | Exists | Where |
|---|---|---|
| Build an install with no Nexus importable | yes | `src/nexus/_install/*_core.py`, `install_generation.sh` |
| Safe install under live sessions | yes | the generation layout |
| Run a plugin script with no CLI | yes | the `uv tool run` form, 7 hooks |
| A standard-library MCP server in the plugin | yes | `conexus/mcp/sequential_thinking.py` |
| `PATH` lookup that skips the current directory | yes | `conexus/hooks/scripts/_exec_path.py` |
| Unattended provisioning | yes, unverified detached (CA-6) | `nx init --yes --no-autostart` |
| Keep plugin and CLI versions together afterwards | yes | RDR-143 lockstep action |
| Stand-in server with setup tools, setup hook, launcher, credentials page, `/conexus:setup`, installer script | no | this record |

### Decision Rationale

RDR-143's three objections, against this design:

- *It removed the `[local]` extra.* That hazard was an upgrade replacing an
  install that had the extra. A first install has nothing to lose, and the
  build step is `nx self install`, the same code every documented install
  uses. Upgrades still go through RDR-143's extras-preserving action.
- *It blocked session start.* The hook starts nothing, and the build is a
  detached process started by a tool call.
- *It overwrote a developer's install.* Setup is offered only when no CLI
  resolves at all, and a generation build never writes into an existing tree.

The first draft of this record installed on the first session by default
with an opt-out. Sam ruled that out on 2026-10-12: the plugin asks first. A
second draft asked twice, once before the CLI build and again before
provisioning. Sam asked for one question instead, and one is better: the user
decides once, with the sizes and times in front of them, and can then walk
away, where two questions leave the session waiting on a second answer a
minute in. The single question still separates the 0.6 GB local service from
the CLI, and still gives a managed-cloud user the choice before a local
service is downloaded. The part RDR-174 treats as needing consent, a unit that
runs at every login, stays behind its own explicit command.

## Alternatives Considered

### Alternative 1: The installer script alone

A shell script and a PowerShell script, with no change to the plugin. It cuts
the terminal steps to one and can install `uv`. It does not reach the account
plugin store, so a Desktop user still needs a terminal, and a user who adds the
plugin first still gets the errors of Gap 1. It does not meet RDR-218's
requirement alone. It ships as part of this design (Sam, 2026-10-12), beside
the plugin path.

### Alternative 2: A remote MCP endpoint on the managed service

The only route with no local install, and the only one that reaches plain chat
and the web application. It needs an HTTP MCP endpoint that does not exist
(the transport is standard input and output only), self-serve sign-up and
hosting, and it serves cloud tenants only. RDR-126 rejected it for local-first
reasons that still hold for local mode. It is a separate product decision.

### Alternative 3: Bundle the `.mcpb` extension in the plugin

The `.mcpb` (a Desktop extension package) needs `uv` just as the plugin does
and would add a second copy of the servers under a second tool namespace. No
step is removed.

### Briefly Rejected

- **The plugin installs `uv` itself.** One command line would have to work in
  every shell on every platform, the running application would not see the new
  `PATH`, and it means piping a remote script into a shell on the user's
  behalf. `uv` stays the one stated prerequisite of the plugin path. The
  installer script does install it, because there the user typed the command.
- **Install on the first session without asking, with an opt-out.** The first
  draft. Rejected by Sam; see Decision Rationale.
- **Two questions in sequence.** The second draft. Replaced by one question.
- **The token as a tool argument or typed into the conversation.** It would
  sit in the transcript and in the model's context. The credentials page keeps
  it out of both.
- **A private copy of the CLI inside the plugin's data directory.** Two
  installs drift, and the terminal `nx` a user later runs would be a different
  one from the plugin's.
- **Block the first session until the install finishes.** Hooks time out in 5
  to 30 seconds and a server must answer its handshake in about that long. The
  install takes minutes.

## Trade-offs

### Consequences

- A user installs `uv`, adds the plugin and answers one question. Five to
  nine terminal steps go away. A terminal user can run one command instead.
- The first session has no Nexus tools until setup ends, from under two
  minutes on a fast connection to about seven on a slow one for local mode, and needs a reconnect at the end. The Desktop application
  has no progress display; the status tool is the only view.
- A user who says no keeps a plugin whose tools do nothing, with one line per
  session naming `/conexus:setup`.
- The plugin now opens a listening port on the loopback address for up to ten
  minutes during cloud setup.
- Every server start and seven hook entries gain one `uv` launch, about 20 ms
  each by measurement.
- The plugin now carries a launcher that must keep working against every CLI
  version in the field, the same compatibility duty `nx_hook_shim.py` already
  has.

### Risks and Mitigations

- **The download fails or the disk is full.** The status file records the
  failing step and the log path; the next session reports it and does not loop.
- **Two sessions start at once.** One lock, taken with an atomic directory
  create, as the generation builder does. The second session reports the
  first's progress.
- **The account copy of the plugin is old** (Discovery 6). On a machine with no
  CLI the build installs the old plugin's version, which is consistent but
  stale. On a machine with a newer CLI the launcher uses the newer CLI. The
  install page keeps "remove and add the marketplace again" as the Desktop
  upgrade step until the store's behaviour is understood.
- **A Desktop user is never told.** With no hook and no `instructions`, the
  notice there is the two tool descriptions, which the model reads only when
  it considers the tools. The install page tells Desktop users to ask "set up
  Nexus" after adding the plugin. Whether Desktop offers plugin commands such
  as `/conexus:setup` in a conversation was not measured.
- **The model does not ask, or asks badly.** The hook's message is a request
  to a model. If the model stays silent, nothing is installed and the next
  session says the same thing; if it calls a setup tool unasked, the
  application's permission prompt is what the user sees. A user who has set
  the plugin's tools to always-allow has removed that prompt for themselves.
- **Another local program reads the credentials page.** Loopback only, a
  random one-time path, one submit, ten minutes. A program already running as
  the user can read the config file anyway, so the page adds no exposure
  beyond the file it writes.
- **The browser does not open** (a headless box, a remote session). The status
  carries the address; the installer script's terminal prompt is the other
  route.

### Failure Modes

- `uv` absent: nothing in the plugin runs, as today. The application shows the
  server as failed. Documentation is the only remedy.
- Build killed midway: the generation has no receipt, so nothing treats it as
  an install, and the next `setup` builds a new one.
- Stand-in server still running after the install: its status tool says to
  reconnect. It never serves a Nexus tool.

## Implementation Plan

### Prerequisites

- An isolated-home harness with `uv` and no CLI; `tests/e2e/run.sh` already
  isolates by home directory.

### Minimum Viable Validation

On a machine image with `uv` and nothing else: add the plugin, start one Claude
Code session, answer "local", wait, reconnect, and run one search that
returns a result. No terminal command other than adding the plugin. Separately,
on an image with nothing at all: run the installer script and get the same
result. The same on Windows, and from
the Desktop application with the account copy of the plugin.

### Phase 0: Measure

Done 2026-10-12, on the laptop. Results are in Critical Assumptions and
Discovery 7.

1. A live Desktop conversation: the loop is off the machine, no hook runs,
   plugin tools are bridged.
2. `nx self install` from a throwaway `uv` environment: refused. The design
   adds that branch.
3. A stand-in server in Claude Code: accepted, `instructions` delivered.
4. `nx init --yes --no-autostart` with no terminal: works, 46 seconds.

Still open, none of it blocking Phase 1: a reconnect swapping the stand-in
for the real server; plugin commands in a Desktop conversation; when Desktop
starts a local agent session; `nx self install` and `nx init` from nothing
on Windows. The launcher form itself was run on Windows (CA-5).

### Phase 1: Launcher and honest failure

The launcher, the entry changes in `.mcp.json` and `hooks.json`, the lockstep
message fix and the doc fix. No install yet. After this phase a user without
the CLI gets one true message and no errors.

### Phase 2: Setup in the plugin

The setup hook with its five states, the stand-in server's two tools, the
detached setup process with its status file, the credentials page and the
`/conexus:setup` command. The Cowork wait fix if CA-2 holds.

### Phase 3: Installer script

`install.sh` and `install.ps1`, sharing the build command and the status file
with Phase 2, tested in a container and on the Windows box.

### Phase 4: Install pages

Rewrite the install steps on the site and in `README.md`,
`docs/getting-started.md`, `docs/desktop-deployment.md` and
`docs/windows-install.md` around two routes: "run this one command" and
"install `uv`, add the plugin, answer one question". The manual commands move
to a troubleshooting section.

### Day 2 Operations

- The status file and log live under `${CLAUDE_PLUGIN_DATA}`. `nx doctor` gains
  a row that reads them and is not applicable when no setup has run.
- Upgrades are unchanged: RDR-143's action for the terminal plugin, remove and
  add for the account store.
- Removal is unchanged: `nx self` and `nx daemon service` verbs.

### New Dependencies

None. The launcher, the hook and the stand-in server are standard library.

## Test Plan

- The launcher imports and runs with `nexus` unimportable.
- Resolution order: layout before `PATH`; current directory never searched.
- Absent CLI, hook verb: exit 0, one line, for every verb in `hooks.json`.
- Setup not complete, server: the stand-in answers a real MCP handshake from
  the SDK client and lists exactly the two setup tools.
- A hand install (CLI present, no status file) always gets the real server.
- Setup hook: each of its five states, with the lock held by a live and by a
  dead process id. In no state does the hook start a process.
- Nothing is downloaded or written outside the status directory until `setup`
  is called; pinned by running a session start in an isolated home and listing
  what changed.
- Each mode runs exactly its steps: `cli-only` downloads no service files,
  `cloud` downloads no service files and serves the page, `local` serves no
  page.
- Credentials page: a request without the one-time path gets nothing; the
  listener binds loopback only; it closes after one submit and after the
  timeout; the token appears in neither the log nor the status file; a
  rejected pair is not written.
- `setup` after a failure resumes at the failed step.
- Installer script: with a terminal it asks once before any download; with
  none and no flag it builds the CLI only.
- A journey test in an isolated home with a local package index: session
  start, `setup` with `local`, the real server answers after reconnect.
- The existing wire snapshot and hook floor tests still pass.

## Validation

### Testing Strategy

Unit tests cover the launcher and the hook branches. The journey test covers
the build and provisioning against a local index so it needs no network. The
Minimum Viable Validation is run by hand on macOS, Windows and from the
Desktop application before release, because no test harness drives the Desktop
application.

### Performance Expectations

No more than 30 ms added per launcher call (measured 19 to 22 ms for the same
form). Setup hook under 100 ms in every state. Measured on a fast connection
with a warm package cache: CLI build 40 seconds, local provisioning 46
seconds. On a home connection with nothing cached, expect about two minutes
for the 0.7 GB CLI download and two to five for the service, from the
existing install measurements.

## Decisions Made

Sam, 2026-10-12:

1. The plugin asks before anything is installed. No install by default.
2. One question carries every choice; the asks are not spread over the session.
3. A cloud token is entered on a small local web page.
4. `/conexus:setup` is the way back after "not now".
5. The one-command installer script ships with this work.

## Open Questions for Sam

None open.

## Finalization Gate

### Contradiction Check

Two research findings contradicted earlier drafts and the design was changed
to match each. The live Desktop conversation ran no hook (Discovery 7), so the
setup notice is carried by the stand-in server as well as by the hook.
`nx self install` refuses a throwaway `uv` environment (CA-3), so the build
step adds that starting point and keeps the two documented commands as the
fallback. No contradiction between the findings and the design as it now
stands is known to the author.

One tension is accepted, not resolved: on Desktop the notice reaches the model
only through tool descriptions, so a Desktop user may not be told until they
ask for something Nexus does. The install page carries the instruction.

### Assumption Verification

| Assumption | State | How |
|---|---|---|
| CA-1 hooks run in a Desktop session | refuted | live conversation; design changed |
| CA-2 the 15 second wait on Desktop | not applicable to the measured path | live conversation |
| CA-3 `nx self install` from a throwaway environment | refuted | spike; design adds the branch |
| CA-4 stand-in server accepted | verified in Claude Code; read from code for Desktop | spike, source |
| CA-5 `uv` launch form on Windows | verified on native Windows; Desktop by log | spike, log |
| CA-6 unattended `nx init` | verified | spike |
| CA-7 Desktop shell is not on the machine | verified, one observation | live conversation |

Not verified before implementation, each named in Phase 0's "still open"
list: a reconnect replacing the stand-in with the real server; the stand-in
and the launcher under Desktop itself, which need a released plugin; plugin
commands in a Desktop conversation; `nx self install` and `nx init` from
nothing on Windows. The first is checked in Phase 2 before the stand-in ships.
The Desktop items are checked by the Minimum Viable Validation on the first
release that carries the launcher.

#### API Verification

| Call | Provider | Verification |
|---|---|---|
| `uv tool run --directory ... python <script>` as a server and hook command | `uv` 0.8.0 macOS, 0.12.23 Windows | Spike |
| `uv tool run --from conexus==X nx self install` | conexus 7.77.1 | Spike: refuses today |
| `uv tool install` then `nx self install` | conexus 7.77.1 | Spike |
| `nx init --yes --no-autostart` | conexus 7.77.1 | Spike |
| MCP `initialize` result field `instructions` | Claude Code 2.1.295 | Spike |
| Desktop bridge for local servers | Claude Desktop 2.31226.1 | Source search |

### Scope Verification

The Minimum Viable Validation is in scope and runs before the release that
carries Phase 2: on an image with `uv` and nothing else, add the plugin,
answer "local", reconnect, and get a search result with no other terminal
command. It runs on macOS and Windows and from the Desktop application. The
journey test in the Test Plan covers the same path without a network.

### Cross-Cutting Concerns

- **Versioning**: the first CLI is built at the plugin's own version. After
  that RDR-143's lockstep action keeps them together. The launcher must work
  against every CLI version in the field; a test pins it against the oldest
  supported release.
- **Build tool compatibility**: `uv` is the only tool. Measured on 0.8.0 and
  0.12.23.
- **Licensing**: N/A. No new dependency.
- **Deployment model**: the launcher, hook, stand-in and command ship in the
  plugin, through a client release or a plugin-only cut (RDR-197). The new
  `nx self install` branch ships in the CLI package, so it needs a client
  release. The installer scripts are published on the site.
- **IDE compatibility**: N/A.
- **Incremental adoption**: Phase 1 alone is shippable and changes nothing for
  a machine that already has the CLI except one extra `uv` launch per server
  start and hook.
- **Secret/credential lifecycle**: the cloud token is typed into a loopback
  page or a terminal prompt with echo off, written to the Nexus config file
  with owner-only permissions, and never logged, never placed in the status
  file and never passed through the model. Rotation and override are the
  existing `nx config` verbs.
- **Memory management**: N/A. The launcher replaces itself with the real
  program on POSIX and holds one idle parent process on Windows.

### Proportionality

The record is long for a launcher script because it reverses one prior
decision (RDR-143), implements a standing requirement (RDR-218) and touches
consent. The Technical Design could be shorter once Phase 2 is built; nothing
is flagged for trimming before then.

## References

- T2 `nexus/install-friction-analysis-three-surfaces-2026-10-11`
- T2 `nexus/install-friction-measurements-laptop-2026-10-11`
- T2 `nexus/desktop-install-friction-qwent-2026-10-11`
- `conexus/.mcp.json`, `conexus/hooks/hooks.json`
- `conexus/hooks/scripts/nx_hook_shim.py`, `version_lockstep_hook.py`
- `src/nexus/_install/layout_core.py`, `generation_core.py`, `install_generation.sh`
- `src/nexus/commands/self_cmd.py` (`nx self install`)

## Revision History

- 2026-10-12: Draft. From the three-surface install analysis and the laptop
  and Windows measurements of 2026-10-11.
- 2026-10-12: Sam's three decisions. The plugin asks before the CLI build and
  again before provisioning, so the session-start hook starts nothing and
  setup moves to tools on the stand-in server. The installer script moves from
  an alternative into the design as Phase 3.
- 2026-10-12: One question with four choices replaces the two sequential
  asks, and one `setup` tool replaces `setup_install` and `setup_provision`.
  Added the loopback credentials page for the cloud token and the
  `/conexus:setup` command. The stand-in server now serves until setup is
  complete, not only while the CLI is absent, so "CLI only" has a way
  forward on Desktop.
- 2026-10-12: One live Desktop conversation measured. The loop ran off the
  laptop, plugin tools were bridged from the laptop, and no hook ran. CA-1
  refuted, CA-7 verified. The stand-in server's `instructions` and tool
  descriptions now carry the setup notice as well as the hook.
- 2026-10-12: Phase 0 run. CA-3 refuted (`nx self install` refuses a
  throwaway `uv` environment), so the build step adds that branch and keeps
  the two documented commands as the fallback. CA-4 verified in Claude Code;
  Desktop's bridge passes tools only, so tool descriptions carry the notice
  there. CA-6 verified. Sizes corrected: the CLI is 1.6 GB installed, not
  0.1 GB.
- 2026-10-12: CA-5 run on native Windows: the `uv` launch form, a prototype
  launcher relaying to the real `nx-mcp.exe`, and the fall-through to the
  stand-in all work.
- 2026-10-12: Gate round 1: BLOCKED (1 Critical, 9 Significant, 1 ship-blocker(s)); commit `2a630e7c5`; critique `nexus_rdr/228-gate-critique-2026-10-12-r1`.
