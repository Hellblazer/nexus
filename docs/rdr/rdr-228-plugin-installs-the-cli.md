---
title: "The Plugin Installs the CLI"
id: RDR-228
type: Feature
status: accepted
priority: high
author: Sam
reviewed-by: self
created: 2026-10-12
accepted_date: 2026-10-13
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
user who never opens a terminal cannot get there at all. Sam's requirement,
quoted in RDR-218, is that a CLI must not be an installation prerequisite.
No platform meets it today.

### Enumerated gaps

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
action is needed. The detached action it starts returns before it runs any
upgrade command, because it finds neither a generation nor a `uv tool`
install (`version_lockstep_action.py:428-431`). The `uv` tool directory was
empty afterwards (measured). The one hook that warns "nx is missing" is itself an
`nx-hook` verb, so it cannot run in the case it exists for.

#### Gap 3: Nothing provisions the local service

A CLI on disk is still not a working Nexus. `nx init` downloads the engine (the
Java service that stores and searches), its Postgres bundle and two embedding
models, about 0.6 GB over two to five minutes, and starts the service. The
plugin does none of this, and the MCP server deliberately starts nothing
(`src/nexus/mcp/core.py`, the "NO first-run daemon install here" note).
`docs/desktop-deployment.md` says the Claude surfaces "auto-start what they
need on first launch", which is false.

#### Gap 4: A local Desktop session waits 15 seconds (recorded, not closed here)

Desktop sessions recorded up to 2026-07-13 ran the Claude Code program bundled
with the Desktop application and passed it plugins with server discovery
turned off for those whose servers the application started itself
(Discovery 1). Reproducing
that launch with Desktop's own binary, the plugin's `mcp-connect-wait` hook
waited its full 15 seconds and reported that `nx-mcp` did not connect. The
one live conversation measured on today's Desktop build ran no local session
and no hook on the laptop (Discovery 7), so no user is known to pay this wait now. This
record does not change the hook for that launch. It does make sure its own
design never causes the same wait; see "The connect wait".

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
- **RDR-218** (Windows through a WSL2 appliance) is where the requirement was
  written down. Sam's words there, about the CLI: "Updating, yes. but not a
  hard req on installation." RDR-218 is superseded by RDR-224, which carries
  over its Gaps 4 to 6 and does not restate this ruling. On 2026-10-11 Sam
  asked why the CLI is required and asked for it to be fixed (Background), so
  this record treats the ruling as live and is now its home.
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
assumption CA-1 to CA-7 below is also a research record,
`nexus_rdr/228-research-1` through `-14`, with its classification and method: eleven verified (eight
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
   **Corrected 2026-10-13** (one live conversation, Phase 1 gate,
   `nexus_rdr/228-research-22`): the plugin's hooks do run for a Desktop
   conversation, in the cloud workspace and not on the laptop. That workspace
   has `uv` and no CLI, and sets `CLAUDE_CODE_REMOTE`. The setup hook's notice
   reached the model there and was false, because the servers on the laptop
   were working. "No hook ran" above holds for the laptop only.
8. **Documented.** Plain chat and the claude.ai web application ignore a
   plugin's local servers and hooks. Cowork refuses a plugin that has a
   top-level `bin/` directory. `${CLAUDE_PLUGIN_DATA}` is a per-plugin
   directory that survives plugin updates. Cowork does not prompt for a
   plugin's `userConfig` values.

### Critical Assumptions

- **CA-1 (Refuted for the conversation measured).** The draft assumed a
  Desktop session runs the plugin's hooks. In the live conversation of
  Discovery 7 no hook ran on the laptop, because the loop was not there. The design
  now treats the plugin's server as the only channel that reaches every
  surface; see "How the question reaches the user".
- **CA-2 (Not applicable to that path).** With no hooks, the 15 second
  `mcp-connect-wait` did not run. It remains measured only for the
  reproduced launch of Gap 4, which this record does not change.
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

- **CA-8 (Assumed).** Each Claude application asks the user before it runs a
  plugin's tool. It is verified on no surface, and a Claude Code user who has
  allowed all of the plugin's tools has turned it off for themselves. What
  rests on it is stated once, in "Consent". *Verify:* the Minimum Viable
  Validation notes on each surface whether a prompt appears for `setup`.
- **CA-9 (Assumed).** A setup process started detached from a server on
  Windows outlives that server. The repository's existing Windows server
  launcher ties its children to a job that kills them when it closes
  (`mcpb/src/bootstrap.py`), so this may be false for a process started the
  plain way. *Verify:* in Phase 2, before the setup process ships on Windows.

## Proposed Solution

### Approach

The plugin stops naming CLI programs directly. Every server and hook entry that
needs the CLI goes through one small launcher script that ships in the plugin
and runs under `uv`, as seven hooks already do. When the CLI is present the
launcher runs the real program, always. When it is absent, the first session
says so and asks one question that carries every choice: install with a local
service, install for the managed cloud, install the CLI only, or not now, and
for a local service whether it starts at login. One answer starts one
background process that does all of it. Nothing is installed
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
relays its exit code), so the launcher itself is not left between a running
server and the application on POSIX. `uv` is: it stays alive as the parent of
what it runs, on every platform (measured, uv 0.8.0), so the server is a
grandchild of the application and not a child. Before the hand-off the launcher changes directory to the
project, because the entries start it with the plugin root as its working
directory and the programs read the project from theirs: for a hook program
the payload's `cwd`, else `CLAUDE_PROJECT_DIR` (the rule `nx_hook_shim.py`
uses), and for a server `CLAUDE_PROJECT_DIR`, which Claude Code sets for a
plugin's server (measured, `nexus_rdr/228-research-17`).

**What is served, decided from facts.** The launcher decides from two facts
about the CLI program, both read from files: does it resolve, and, when it does,
can it start. No file the plugin writes takes part in that decision, so an
install done by hand at any time is used at the next launch.

- *The program resolves and can start.* Run it. This holds whatever the user
  answered earlier and whatever state a setup run was left in.
- *It does not resolve, and a hook verb was asked for.* Exit 0 with one line
  on standard error. A missing CLI never blocks a prompt or a tool call. This
  is the same outcome as the "no `nx-hook`" branch of `nx_hook_shim.py`,
  which stays for the three entries the shim still runs.
- *It does not resolve, and `nx-mcp` was asked for.* Serve the stand-in
  server: answer the handshake, set the server instructions to say Nexus is
  not installed, put the one question with its table of choices and name the
  commands that install it by hand, and offer the two setup tools below.
- *It does not resolve, and `nx-mcp-catalog` was asked for.* Serve a stand-in
  with no tools, so the application shows a connected server and the question
  is not offered twice.
- *It resolves and cannot start* (Sam, 2026-10-14: for example a generation
  whose Python `uv` pruned). "Cannot start" is decided from files alone, with
  no process, and it is claimed only on positive evidence. On Windows the
  `pyvenv.cfg` of the program's virtual environment names a `home` directory
  that is gone (the launchers are copies with no shebang and there are no
  links, so that is the whole of it). On POSIX a gone `home` is not evidence by
  itself, because the interpreter can still start through the venv's
  `bin/python` link (Sam, 2026-10-14, after the review of the first build
  measured it); it counts beside a `python*` link in the venv that points
  nowhere, which is the other thing a `uv` prune leaves, and a shebang that
  names an interpreter that is missing or not executable counts alone. The CLI
  states the same rule for `nx self install --bootstrap` and a test runs the
  two over one matrix, so they cannot disagree. Measured limit: whether a venv
  whose `home` is gone but whose link resolves then starts depends on the
  interpreter build (a system CPython finds its way back through the link; a
  `uv` standalone CPython does not find its library), and such a venv is
  judged runnable, so there the application shows a failed server. A hook verb gets one line on standard error and exit 0, as when the
  CLI is missing. `nx-mcp` gets the same stand-in as for no CLI, but it says
  "Nexus is installed but cannot start; setup repairs it" and the reason, in
  its instructions, in both tools' descriptions and in `setup_status`;
  `nx-mcp-catalog` gets the stand-in with no tools. A program the system
  refuses to execute when the launcher starts it is treated the same way. The
  build step of `setup` does not skip a CLI that cannot start, and
  `nx self install --bootstrap` counts a `current` generation, and a legacy
  `uv tool install conexus` tree, whose Python is gone as not installed, so it
  replaces it. A broken `current` generation is repaired even when a working
  legacy tree sits beside it: the lookup is layout-first, so the broken
  generation is the one the plugin runs and the one to replace. (A legacy
  tree on Windows is reached through a launcher copy in `uv`'s bin directory,
  which names no virtual environment, so the plugin does not judge it there.)

The stand-in is served only while there is no CLI or the CLI cannot start. It
does not try to swap itself for the real server inside one connection; when the
build is done its status says to reconnect the server or start a new session.
`setup_status` asks the lookup again at each poll, in both modes, of the same
program the launcher judged, because the setup record is never cleared and
outlives the install it describes: a `done` from a setup months ago says
nothing about a Python `uv` pruned since, or a tools directory removed since. A
live run is reported as it is, so a poll during the setup or the repair does not
offer it again. While the CLI cannot start, any other state keeps the statement
and the repair offer, with the record's report after them, in words that cannot
be read as a finished repair: a `done` on record "did not repair this", and a
`failed` run says the last run failed and gives the log. While no CLI is found,
the answer opens "not installed", and a `done` on record is an install that is
gone, so the one question is put again. Once the CLI can start, the answer is
the record and the reconnect step once (a `done` record's own next step is that
step; any other record's next step predates the CLI and is dropped). (One limit: when the launcher fell into the
cannot-start mode because the system refused to run a program the files say is
fine, nothing in the files can show the problem gone, so the poll then reads the
CLI as startable.) The hook's state 2 gives the same statement and offer.

**The setup tools.** Two tools, `setup` and `setup_status`, with the same
names, arguments and descriptions on the stand-in and on the real `nx-mcp`.
The real server registers both always, not only when something is missing.
A conditional registration would need two shapes of the committed tool list
and would go stale inside a running server once setup finished; two small
tools in every session is the cheaper cost. When nothing is missing,
`setup_status` says so and `setup` starts nothing. When no usable service is
found, the real server also says so through its instructions text, the
channel it already uses for another notice (`apply_stranded_notice`,
`src/nexus/mcp/_first_run.py`).

So "CLI only" is an ordinary state: the real server runs and says no service
is set up. A reboot that leaves the local service down is the same state with
a different sentence.

`setup` takes `mode` (`local`, `cloud`, `cli-only` or `not-now`) and, for
`local`, `autostart` (true or false; false when omitted, so the login unit is
never registered without an explicit yes). `not-now` installs nothing and
writes the marker described under "The setup hook". `setup_status` takes
nothing and reports the stage, the log path and the next step.

The setup tools are on a server, not in a script the model runs through its
shell tool, because a server runs on the user's machine on every surface and
the shell tool in a Desktop conversation does not (CA-7).

Registering the two tools on the real server touches the hand-kept lists
every new `nx-mcp` tool touches: the committed tool list
(`tests/fixtures/mcp_wire_snapshot.json`), the name set in
`tests/test_mcp_package.py`, and three tool counts (two in
`docs/mcp-servers.md`, one in the module docstring of
`src/nexus/mcp/core.py`). Both tools must also stay out of auto-approval. The
existing test `tests/hooks/test_permission_request_hooks.py` requires every
registered tool to be auto-approved unless its full name is in
`_MANUAL_APPROVAL_REQUIRED` with a paired "requires manual approval" test, so
`mcp__plugin_conexus_nexus__setup` and `mcp__plugin_conexus_nexus__setup_status`
are added there, as `daemon_uninstall` was.

**Consent.** Stated once. Nothing is installed without a `setup` call. The
call is made by a model that the hook, the server instructions and the tool
description have each told to ask the user first and to act only on the
answer. That instruction is the control this design owns, and it is a
request to a model, so it can fail. The application's own prompt before a
plugin tool runs is a second check that this record has not verified on any
surface (CA-8). If the Minimum Viable Validation finds a surface that shows
no prompt, consent on that surface rests on the model alone, and whether the
setup tools ship there is Sam's decision before release.

**A usable service.** One definition, used by the hook, the setup process and
the real server. A service is *usable* when the CLI would resolve an endpoint:
`resolve_service_endpoint()` in `src/nexus/db/service_endpoint.py` returns and
does not raise. It has four legs, in order: the `NX_SERVICE_URL` variable; a
saved `service_url` (`nx config set service_url`); `NX_SERVICE_HOST` with
`NX_SERVICE_PORT` (and `NX_SERVICE_TOKEN`); a live local supervisor lease (the
file a running local service keeps). The two address legs need a token as well:
a service token (`NX_SERVICE_TOKEN`, or the saved `service_token`), or the
token of a live lease, or a mint credential. A saved address with no token is
therefore not usable, because the CLI raises on it and the CLI is the program
that connects. The host and port leg needs a port and a token, each from the
environment or from a live lease, and a lease that carries no token is not
usable. The real server calls `resolve_service_endpoint`;
the hook, the setup process and the stand-in cannot import it, so the plugin
restates it as `endpoint_usable` in `conexus/hooks/scripts/_endpoint_resolve.py`,
and `tests/hooks/test_usable_service_agreement.py` holds the two to the same
answer on one fixture set per leg (Residual nexus-9i4d4 item 9). The plugin's
older `resolve_base_url` answers a different question, the base URL the other hooks
use with a credential policy of their own, and is not the definition. Resolving
reads files and the environment and makes no network request; in the hook the
file reads (the setup status and its error record, and the lease) retry a Windows
sharing violation for at most 30 ms together, counted from after the plugin
parts are imported, where the registry's own retry is two seconds. A hand-edited `config.yml` with the credentials in flow style is
read as the CLI reads it (a flat mapping of plain or quoted scalars, on one line
or several); a mapping nested in a document-wide flow mapping, an anchor or a
block scalar is not, and reads as no saved address. A lease just past its time
to live is kept while its owner process is alive (within ten times the lease's
time to live, on a loopback host), so a laptop that slept is not told "service
down". One difference remains, because the plugin makes no network request: the
CLI also asks the port to answer `/health`, so an owner that is alive and wedged
reads as usable to the hook and not to the CLI; a test names it. The setup
process does not share the difference: it decides whether to start the
service, so it asks with no grace and a lease past its time to live is not a
service there (`endpoint_usable(..., grace=False)`). A
local service is *provisioned* when `nx init` has left its files in the Nexus
config directory (the service binary under `service/`). A service whose
endpoint resolves but which does not answer, a cloud outage or a hung local
service, is not a setup state: every tool already reports that error, and this
record does not change it.

**The one question.** The choices, as the user is shown them:

| Choice | What happens | Download |
|---|---|---|
| Local | Install the CLI and run a Nexus service on this machine | about 1.3 GB down, 2.4 GB on disk |
| Cloud | Install the CLI and connect it to a managed service you have an address, token and tenant for | about 0.7 GB down, 1.6 GB on disk |
| CLI only | Install the CLI and decide about a service later | about 0.7 GB down, 1.6 GB on disk |
| Not now | Nothing is installed | none |

With Local, the same question asks one more thing: start the service when you
log in, yes or no. A yes is the explicit consent RDR-174 requires for the
login unit. A no means the service must be started again after each reboot,
and the question says so.

The CLI is the larger part: 216 Python packages, 1.6 GB installed (measured
2026-10-12). The local service adds about 0.8 GB on disk. The on-disk figures
do not count `uv`'s own package cache, which keeps the downloaded packages
until the user prunes it; its size after a first install is not measured. Earlier drafts of
this record called the CLI "about 0.1 GB", which was wrong.

**How the question reaches the user.** Two channels carry the same state and
the same table, because no one channel reaches every surface. In Claude Code
and the Code tab a session-start hook tells the model at the start of the
session. In a Desktop conversation the hooks run in a cloud workspace and
not on the user's machine (Discovery 7), so the hook cannot see the servers
and says nothing there, and the only
part of the plugin on the user's machine that the model can see is the
server. So the server that is running, stand-in or real, also states it,
twice. Its `instructions` text says what is missing and that `/conexus:setup`
or the `setup` tool fixes it; Claude Code delivers that text to the model
(CA-4). Both tools' descriptions say the same, and the descriptions are the
part that reaches a Desktop conversation, because Desktop's bridge passes
tools and not `instructions` (CA-4). There the question is asked when the user
first asks for something Nexus would do, not at the start.

**The setup hook.** A new session-start hook, `nx_setup_notice.py`, first in
the list, runs the same way as the launcher. It starts nothing and makes no
network request. In a remote workspace (`CLAUDE_CODE_REMOTE` is true) it
says nothing and reads nothing: the servers are not there, and neither is
the machine a setup would install on (Discovery 7). Elsewhere it reads the
facts above and the setup directory, and
reports the first state that matches, in this order:

1. A setup run is live (the lock's heartbeat is fresh): report its stage.
2. The CLI is found and cannot start (the launcher serves the stand-in for it;
   Sam, 2026-10-14): say that Nexus is installed but cannot start, give the
   reason, and say that `setup` repairs it by building the CLI again, after the
   user is asked first. A last run that failed or was interrupted is added.
3. The CLI resolves and a service is usable: say nothing. This holds
   whatever an earlier run left in the setup directory.
4. A setup run was interrupted (the lock's heartbeat is stale and the status
   is not terminal): say so, name the log, and say that `setup` called again
   resumes.
5. The last run failed (the status records a failure): report the failure
   line and the log path. `setup` called again resumes from the failed step.
6. No CLI: tell the model Nexus is not installed, give it the table, and ask
   it to put the one question to the user. The model calls `setup` with the
   answer. When the "not now" marker exists the hook does not ask; it says
   one line naming `/conexus:setup`.
7. The CLI resolves, no service is usable and a local service is provisioned:
   one line saying the service is down and that `/conexus:setup` starts it.
8. The CLI resolves, no service is usable and none is provisioned: one line
   saying no service is set up and naming `/conexus:setup`.

The "not now" marker is written by `setup` with mode `not-now` and removed by
any other `setup` call. It never changes what the launcher serves. A user who
declines and later installs by hand is in state 3.

The user's choice is honoured (Sam, 2026-10-14, "honour the choice"; this
replaces the first build's rule that the marker changed state 6 only, which left
the tool descriptions saying "not now" stops the asking while states 4, 5, 7 and
8 went on asking). With the marker, states 4, 5, 7 and 8 are each one quiet line
that names `/conexus:setup` (4 and 5 also name the log) and asks nothing, as
state 6 already was. A recorded CLI-only choice (the status file says mode
`cli-only` and the run is `done`) makes states 7 and 8 the same quiet line, because
a service that is not set up is what the user chose. A live run (1), a CLI that
cannot start (2) and a usable service (3) are not affected by either. A recorded
Local choice, or a failed CLI-only run, is not a choice to stop hearing about it.

**The setup directory.** One place for all of it, `setup/` under the Nexus
config directory (`~/.config/nexus` unless `NEXUS_CONFIG_DIR` or
`NX_CONFIG_DIR` moves it), where the plugin already keeps its lockstep
marker. It holds the status file (JSON, owner-only), the log, the "not now"
marker and the lock. The lock is a directory made with an atomic create, as
the generation builder claims a stamp, holding a heartbeat file that the
setup process rewrites every ten seconds. A lock whose heartbeat is more
than sixty seconds old is stale and the next `setup` removes it. The
heartbeat is new here: the generation builder has no liveness test, and a
process-id test is avoided because the standard library has no reliable one
on Windows. `${CLAUDE_PLUGIN_DATA}` is not used.

**The `/conexus:setup` command.** The way back after "not now" or "CLI only",
the way to start a service that is down, and the way to see progress. It
calls `setup_status`, shows the table with the choices still open, and calls
`setup` with the answer. When nothing is missing it says so. Its body uses
the setup tools only; unlike the 17 existing commands it has no `nx` shell
line, because it must work when `nx` does not exist. When the server offers
no setup tools, which means the CLI is older than the release that carries
them, the command says that and gives the two update commands as text.

**Where the setup process lives.** One copy, in the plugin:
`conexus/launch/nx_setup.py`, standard library only, no Nexus import. It
owns the lock, the status file, the build step and the credentials page, and
it runs CLI programs as child processes for everything that needs Nexus
(`nx init`, `nx config set`, `nx daemon service ...`, `nx doctor`). The
stand-in starts it directly. The real server reaches it through the
launcher: when the launcher hands off to the real `nx-mcp` it sets one
environment variable, `NX_SETUP_SCRIPT`, to that file's path, and the real
server's `setup` tool starts the file with its own Python. A real server
that was not started by this launcher has no such variable; its setup tools
then start nothing and answer with the terminal commands for the same work.
The plugin parts that name the real server's tools ship in the client
release that carries those tools, never in a plugin-only cut ahead of it.

**The setup process** (`setup`). One detached process per call, logging to one
file and writing its stage to the status file after each step. The tool
returns at once with the stage; `setup_status` follows it. A `setup` call
while a run is live starts nothing and returns that run's stage. The steps,
each skipped when already done:

1. *Build.* `nx self install` gains an explicit flag, `--bootstrap`, for a
   first install. Today the command builds from a running generation or
   converts a `uv tool` install, and refuses anything else, which is what
   protects a developer's checkout (CA-3; `self_cmd.py:237-242`). The flag is
   the only way into the new path, and nothing is inferred from where the
   process runs: with the flag the command installs the released package at
   its own version from the package index; without it every refusal stays as
   it is. The build is one command and one generation:
   `uv tool run --python 3.12 --from conexus==<plugin version> nx self install --bootstrap`.
   It builds `gen-<stamp>`, points `current` at it and writes the command
   shims into the user's bin directory. It writes a receipt with no extras,
   which is what the documented first install records
   (`uv tool install conexus --python 3.12`, `README.md`); local mode's
   embedding model belongs to the service and is fetched in step 2. It does
   not prune `uv`'s shared package cache, which a plain `nx self install`
   does at its end (Sam's decision, 2026-10-12). In the test for CA-3 that
   prune removed 23.4 GiB from a developer's cache. The version is the
   plugin's own, read from its `plugin.json`, so the first CLI matches the
   plugin by construction, and a plugin that carries the setup process is
   never older than the CLI release that carries the flag. There is no
   fallback to the two documented commands: they would run the prune and
   leave two copies. Mode `cli-only` stops here.
2. *Service, mode `local`.* When no local service is provisioned:
   `nx init --yes` if `autostart` is true, which registers the login unit
   (`_decide_autostart` in `src/nexus/commands/init.py`), and
   `nx init --yes --no-autostart` if not. When one is provisioned and down:
   `nx daemon service install --autostart` first if `autostart` is true,
   then `nx daemon service start`.
3. *Service, mode `cloud`.* Save what the credentials page accepted
   (below) with `nx config set`: `service_url`, then for a mint credential
   `mint_token` on standard input and `mint_tenant`, and for a static token
   `service_token` on standard input. Never both kinds. No service download.
4. *Verify.* `nx doctor`; record pass or the failing rows, and write a
   terminal status.

**The credentials page.** A cloud token must reach the config file without
passing through the model, the conversation or a tool argument, and a Desktop
user has no terminal (Sam, 2026-10-12). So in mode `cloud` the setup process
serves one small web page and opens it in the user's browser at the start of
the run, while the build proceeds, so the user is not asked for anything
later. Its requirements:

- The process listens on the loopback address `127.0.0.1` only, on a port the
  operating system picks, and the page's address carries a random one-time
  path. A request without that path gets nothing.
- The address is not put on a command line, where other users of the machine
  could read it. The setup process writes an owner-only file in the setup
  directory that sends the browser on to the address, asks the system to
  open that file, and deletes it when the page is first loaded. The address
  is in no other file, not in the log and not in a tool result.
- The page has three fields, service address, token and tenant, and one
  button. The token field is a password field with autocomplete off. The
  tenant is required for a mint credential and unused for a static token.
- A managed user holds a mint credential: a token bound to one tenant that
  mints short-lived data tokens, which the CLI does for every request
  (`mint_token` and `mint_tenant`; conexus, 2026-10-15). A static service
  token is the self-hosted case. The page takes both kinds (Sam, 2026-10-15).
- On submit the process checks what was typed at the address given.
  `/version` says the service answers and which version it is. The token is
  then sent to one authenticated, read-only route under `/v1`, `GET
  /v1/_whoami` (Sam, 2026-10-15): 401 refuses it; 200 is a working static
  token; 403 is a live mint credential, which the process proves with its
  tenant by minting one data token, `POST /v1/data-tokens/mint` with that
  tenant and a 60-second lifetime, the call the CLI itself makes. A 200 there
  accepts; a 403 says the token belongs to another tenant; a 429 says to try
  again in a moment. `/health` is not used: it is unauthenticated in the
  engine, and the managed edge answers it 401 for a mint credential (measured
  2026-10-15, T2 `nexus_rdr/228-research-37`). A redirect is never followed.
  An `http` address is accepted as well as `https` (Sam, 2026-10-12: no https
  requirement).
- A pair that passes is held in the setup process's memory, the page says
  it was accepted, and the process stops listening. The pair is saved in
  step 3, after the build, because saving needs the CLI. The token goes to
  `nx config set` on standard input, never on a command line.
- A pair that fails is not kept; the page says why and stays open for
  another try.
- When `NX_SERVICE_URL`, `NX_SERVICE_TOKEN`, `NX_MINT_TOKEN` or
  `NX_MINT_TENANT` is set in the setup process's environment, the page says
  that those take precedence over what it saves.
- Whether a browser opened cannot be known in advance. If the page has not
  been loaded two minutes after the request to open it, the status says so
  and names `nx config init` in a terminal as the other route; that wizard
  offers the same two kinds of credential and hides the token as it is typed
  (Phase 2 Step 5). The process
  stops listening ten minutes after it started listening, whatever happened.
- The status file's values for this step are exactly: "waiting for
  credentials", "page not loaded", "credentials rejected, waiting for another
  try", "credentials accepted", "credentials saved" and "timed out". A run
  that times out ends with a failed status; the next `setup` call with mode
  `cloud` opens a new page. A `setup` call while a run is live starts
  nothing, page included.
- The token is never written to the log or the status file.

**The installer script.** `install.sh` and `install.ps1`, published on the
site, for a user who starts in a terminal. Typing the command is the consent
for the CLI, so it does not ask about that. It installs `uv` when `uv` is
absent, builds the CLI with the same `--bootstrap` command, and adds the
plugin to Claude Code when `claude` is on `PATH`. For the service it asks the
same question once on the terminal, before any download, or takes `--local`,
`--cloud` or `--cli-only`, and `--autostart` or `--no-autostart`; with no
terminal attached and no flag it builds the CLI only and says so. `--local`
runs `nx init` with the matching flags and `--cloud` runs `nx config init`,
the existing terminal wizard. It does not use the plugin's setup process or
the setup directory, and it does not need to: the plugin decides what to say
from the facts the script leaves behind. The script cannot reach the
claude.ai account plugin store, so a Desktop user still adds the plugin in
the application.

**The connect wait.** `nx-hook mcp-connect-wait` waits up to 15 seconds for a
marker that only the real `nx-mcp` publishes
(`src/nexus/hooks/mcp_connect_wait.py`). With no CLI there is no `nx-hook`,
so the shim that runs this entry exits 0 and nothing waits. With a CLI the
real server runs and publishes the marker. One case still waits: a session
that starts while a build is finishing can launch the stand-in a moment
before the CLI appears and then find `nx-hook` when the hook runs. That
session waits 15 seconds once. It is accepted.

**Entries that change.** `.mcp.json`: `nexus` and `nexus-catalog` become the
`uv tool run ... python ${CLAUDE_PLUGIN_ROOT}/launch/nx_launch.py nx-mcp` form.
`hooks.json`: the six `nx-hook` entries and the session-end entry go through
the launcher. The three entries that run `nx_hook_shim.py` (`auto-approve`
twice and `mcp-connect-wait`) keep the shim, and the shim takes the launcher's
lookup order so both find a fresh install the same way. The shim's note for
a missing `nx-hook` tells the reader to run `nx self install`, which is wrong
when there is no CLI at all; it changes to the launcher's line. The 17 commands keep
their `nx` lines; a command typed before the install finishes fails with the
shell's own "command not found", and the session-start message has already
said why.

**Defects fixed in the same change.**

- `version_lockstep_hook.py` checks for the CLI before it claims an upgrade is
  in flight. With no CLI it says nothing and leaves the message to the
  setup hook.
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
| A notice through the real server's instructions text | yes | `apply_stranded_notice`, `src/nexus/mcp/_first_run.py` |
| A per-user marker directory that outlives plugin updates | yes | `~/.config/nexus`, where `cli_lockstep_marker` lives |
| Writing cloud credentials without a command-line secret | yes | `nx config set <key> --stdin` |
| A standard-library endpoint resolver in the plugin | yes, for the base URL only | `resolve_base_url`, `conexus/hooks/scripts/_endpoint_resolve.py`; "usable service" is `endpoint_usable` in the same file, new here (Phase 2 Step 4) |
| Stand-in server, setup tools on both servers, setup hook, launcher, `nx self install --bootstrap`, credentials page, `/conexus:setup`, installer script | no | this record |

### Decision Rationale

RDR-143's three objections, against this design:

- *It removed the `[local]` extra.* That hazard was an upgrade replacing an
  install that had the extra. The first build here records no extras, exactly
  as the documented first install does, so nothing is removed. This is a
  choice with a consequence: later upgrades reproduce the receipt, so a user
  who wants the extra adds it once with `nx self install --extras local`, as
  today. Upgrades still go through RDR-143's extras-preserving action.
- *It blocked session start.* The hook starts nothing, and the build is a
  detached process started by a tool call.
- *It overwrote a developer's install.* A generation build never writes into
  an existing tree, and the new path is behind an explicit `--bootstrap`
  flag, so `nx self install` run from a checkout still refuses. One case is
  not prevented by a check: a developer whose editable `nx` is not on the
  application's `PATH` looks the same as a machine with no CLI, because
  RDR-143's own test (no generation and no `uv tool` receipt) is true for
  both. That developer is asked the question like anyone else. A yes builds
  a generation and writes shims into the user's bin directory; a no changes
  nothing. No detector guards this case; the question does.

The first draft of this record installed on the first session by default
with an opt-out. Sam ruled that out on 2026-10-12: the plugin asks first. A
second draft asked twice, once before the CLI build and again before
provisioning. Sam asked for one question instead, and one is better: the user
decides once, with the sizes and times in front of them, and can then walk
away, where two questions leave the session waiting on a second answer a
minute in. The single question still separates the 0.6 GB local service from
the CLI, and still gives a managed-cloud user the choice before a local
service is downloaded. The part RDR-174 treats as needing consent, a unit that
runs at every login, is its own yes or no inside that question. An earlier
draft left it behind a separate terminal command, which meant a user with no
terminal had a dead service after the first reboot and nothing to tell them.

## Alternatives Considered

### Alternative 1: The installer script alone

A shell script and a PowerShell script, with no change to the plugin. It cuts
the terminal steps to one and can install `uv`. It does not reach the account
plugin store, so a Desktop user still needs a terminal, and a user who adds the
plugin first still gets the errors of Gap 1. It does not meet Sam's
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
  minutes on a fast connection to about seven on a slow one for local mode,
  and needs a reconnect once the CLI is built. The Desktop application has no
  progress display; the status tool is the only view.
- A user who says no keeps a plugin whose only tools are the two setup
  tools, with one line per session naming `/conexus:setup`.
- The real `nx-mcp` gains two tools in every session, 46 where there were
  44, whether or not anything is missing.
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
- **Two sessions start at once.** One lock in the setup directory. The second
  session's `setup` starts nothing and reports the first's stage.
- **A second session starts while setup is running.** Its hooks find a CLI
  that is built but not yet provisioned, and the real verbs run against it,
  `nx upgrade --auto` among them. Whether that interferes with a running
  `nx init` is not tested; Phase 2 Step 2 tests it, and the remedy if it does
  is for those verbs to exit early while the setup lock is held.
- **The setup process dies with its server on Windows** (CA-9). Phase 2
  Step 2 measures it before the Windows setup path ships.
- **A reboot leaves the local service down.** With start at login declined,
  this is expected: the server and the hook say the service is down, and
  `/conexus:setup` starts it (hook state 7).
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
- **The model does not ask, or asks badly.** If the model stays silent,
  nothing is installed and the next session says the same thing. If it calls
  `setup` unasked, what stands between that call and an install is the
  application's own prompt, which is unverified (CA-8); see "Consent".
- **Another local user or program reaches the credentials page.** Loopback is
  reachable by every user on a shared machine, so the one-time path is the
  only protection. That is why the address is kept off the command line and
  out of every file but one owner-only file that is deleted on first load.
  The page lives ten minutes at most.
- **The token is sent in the clear.** The page accepts an `http` service
  address (Sam, 2026-10-12), and the check sends the token to whatever
  address the user typed. A wrong or hostile address receives it. This is
  the same exposure as typing the pair into `nx config init`.
- **The browser saves the token.** Browsers commonly offer to save a
  password field's value whatever its `autocomplete` says, and a mint token
  does not expire and mints tokens of its own. Which markup suppresses the
  offer in Chrome, Safari and Edge is measured in the Minimum Viable
  Validation and the page takes the markup that does.
- **The browser does not open** (a headless box, a remote session). The page
  is served and never loaded; after two minutes the status says so and names
  `nx config init`, and the listener closes at ten minutes. The installer
  script's terminal route is the other way in.
- **A real server not started by this plugin's launcher** (the `.mcpb`
  extension, a hand-written server entry). It has the setup tools and no
  setup script, so the tools answer with terminal commands and start nothing.

### Failure Modes

- `uv` absent: none of the plugin's local servers or command hooks runs, and
  the application shows the servers as failed. This is wider than before
  Phase 1: the two Nexus servers and the six `nx-hook` entries named their
  programs directly and ran without `uv`, so a machine with the CLI installed
  some other way (`pip`, `pipx`) and no `uv` had them and now does not.
  `uv` is a hard requirement of the plugin (Sam, 2026-10-13). Documentation
  is the only remedy.
- A CLI program that is found and cannot start (a generation whose
  interpreter was removed): a hook entry writes one line and exits 0; a
  server entry gets the stand-in that says "Nexus is installed but cannot
  start; setup repairs it" with the reason, because "not installed" would be
  untrue; the setup hook says so (state 2). Phase 1 left this as a failed server
  (Sam, 2026-10-13); Phase 2 Step 4 reports it (Sam, 2026-10-14, Decisions Made 9).
  A venv whose `home` is gone but whose interpreter link resolves is judged
  runnable on POSIX and may then fail at start (Technical Design, "What is
  served").
- Build killed midway: the generation has no receipt, so nothing treats it as
  an install, and the next `setup` builds a new one.
- Stand-in server still running after the build: its status tool says to
  reconnect. It never serves a Nexus tool. The next launch runs the real
  server, because the CLI now resolves.
- Setup process killed midway: its heartbeat goes stale, hook state 4 reports
  it, and `setup` resumes.
- The CLI's Python was pruned by `uv` (the pipx#146 / uv#8028 class): the CLI
  is found and cannot start. The launcher serves the stand-in that says so,
  hook state 2 says so, and `setup` builds a new generation and moves `current`.
  The old generation stays until garbage collection.

## Implementation Plan

### Prerequisites

- An isolated-home harness with `uv` and no CLI; `tests/e2e/run.sh` already
  isolates by home directory.

### Minimum Viable Validation

Run before the first release that carries Phase 2, on macOS and Windows, in
Claude Code and from the Desktop application with the account copy of the
plugin. Each row starts from an image with `uv` and nothing else. Rows 1 to
3 and 5 use no terminal at all after the plugin is added; rows 4 and 6 use
one on purpose.

1. Answer "local, start at login". Reconnect. One search returns a result.
   Reboot; the search still works.
2. Answer "local, no". Reboot. The session says the service is down;
   `/conexus:setup` starts it, and offers start at login again.
3. Answer "CLI only". The real server runs and offers the setup tools; no
   15 second wait at the next Claude Code start.
4. Answer "not now". Install by hand in a terminal with the two documented
   commands and `nx init`. The next session runs the real server and says
   nothing.
5. Answer "cloud". The page opens; a wrong token is rejected and can be
   retried; a right one is saved once the build ends.
6. Kill the setup process from a terminal during the build. The next session
   reports an interrupted run and `setup` resumes it.

On each surface, note whether the application asked before `setup` ran
(CA-8), and on Windows whether the setup process outlived a closed session
(CA-9). The installer script has its own check, in Phase 3.

### Phase 0: Measure

Done 2026-10-12, on the laptop and the Windows box. Results are in Critical
Assumptions and Discovery 7.

1. A live Desktop conversation: the loop is off the machine, no hook runs
   on the machine, plugin tools are bridged.
2. `nx self install` from a throwaway `uv` environment: refused. The design
   adds the `--bootstrap` flag.
3. A stand-in server in Claude Code: accepted, `instructions` delivered.
4. `nx init --yes --no-autostart` with no terminal: works, 46 seconds.
5. The launcher form on native Windows: works.

Still open, none of it blocking Phase 1: a reconnect swapping the stand-in
for the real server; plugin commands in a Desktop conversation; when Desktop
starts a local agent session; `nx self install` and `nx init` from nothing
on Windows; the first launch on a machine with no Python 3.12, where `uv`
fetches an interpreter inside a hook's 5 second limit or does not; peak disk
use during the build and what `uv`'s cache holds afterwards; a stand-in with
no tools (the `nx-mcp-catalog` case); `nx init --yes` without
`--no-autostart`, `nx daemon service install --autostart` and
`nx config set --stdin` run unattended, each read from source or help text
only so far.

### Phase 1: Launcher, stand-in and an honest message

After this phase a user without the CLI gets a true message from the server
and the session-start hook, a one-line note from each other hook entry, and
no error. Nothing is installed by the plugin yet.

#### Step 1: The launcher

`conexus/launch/nx_launch.py` with its lookup order, the hook-verb exit and
the hand-off to the real program. `nx_hook_shim.py` takes the same lookup
order and the corrected no-CLI note. The bare-name spawn lint
(`tests/test_hooks_bare_exec_lint.py`) is extended to cover `conexus/launch/`.

#### Step 2: The stand-in, notice only

The stand-in for `nx-mcp` with its instructions text and both tools. In this
phase `setup` starts nothing: it answers with the three manual commands
(`uv tool install conexus --python 3.12`, `nx self install`, `nx init`) and the
install page, and `setup_status` reports "not installed". The stand-in for
`nx-mcp-catalog` with no tools. The reconnect from stand-in to real server is
tried by hand here.

#### Step 3: The setup hook, the no-CLI message only

The hook says nothing when the CLI resolves. With no CLI it says Nexus is
not installed and names the manual commands. It decides by whether `nx-mcp`
resolves, the fact the launcher serves the stand-in by, and it speaks on
every session-start event (startup, resume, clear, compact, fork), so a
resumed or forked session can hear it twice (accepted, Sam, 2026-10-13). No question, no setup
directory and no service check yet; those arrive in Phase 2 Step 4.

#### Step 4: Entries and defects

The `.mcp.json` and `hooks.json` entry changes, the lockstep message fix and
the `docs/desktop-deployment.md` fix.

One change to the CLI goes with the server entries. The hook that hands a new
session id to the running server on `/clear` and `/resume` found the server
as a direct child of the application's process
(`find_mcp_sibling_pids`, `src/nexus/session.py`). Behind `uv` the server is a
grandchild, the lookup found nothing, and the handoff stopped with no error.
The lookup now also looks through a child that is `uv` or a Python running
the launcher, and through nothing else, so one session still cannot reach
another session's server.

### Phase 2: Setup

One client release carries all of this phase: `--bootstrap` and the real
server's setup tools ship in the CLI package, and the plugin parts that name
them ship with it. The Minimum Viable Validation gates that release.

#### Step 1: `nx self install --bootstrap`

The flag, the receipt with no extras, no cache prune.

#### Step 2: The setup process and directory

`conexus/launch/nx_setup.py`: the detached process, the status file, the
lock with its heartbeat, and resume after a failure or an interruption. The
Windows detach measurement (CA-9) and the second-session interaction test
come first in this step.

#### Step 3: The setup tools, live

`setup` and `setup_status` do their work on the stand-in and are registered
on the real `nx-mcp`, which reaches the setup process through
`NX_SETUP_SCRIPT` and carries the instructions notice. The hand-kept tool
lists and `_MANUAL_APPROVAL_REQUIRED` are updated in the same change.

Phase 1 already ships `setup` and `setup_status` on the stand-in, with no
arguments, descriptions that say the tool installs nothing, and no
annotations. This step keeps the two names and changes the schema (`setup`
gains `mode` and `autostart`) and the descriptions. An application that
approved or remembered the Phase 1 tool by name now meets a different tool
under it. What it does then is a case of the question CA-8 asks and is not
measured here; the Minimum Viable Validation notes per surface whether the
application asks before `setup` runs, and does not yet start from a tool
remembered from Phase 1. Both tools stay without
annotations, as in Phase 1, so a remembered hint cannot differ from the new
one. After a reconnect in Phase 1 the real server has no setup tools at all
(`nexus_rdr/228-research-18`); from this step it has both.

#### Step 4: The question, the hook's states and `/conexus:setup`

The eight hook states over the usable-service definition, the `not-now` mode
and its marker, the start-at-login choice and the command.

#### Step 5: The credentials page

The loopback page for mode `cloud`, with every requirement in "The
credentials page" as a test, and `nx config init` taking both kinds of
credential with the token hidden as it is typed. The Cloud choice is not offered by a build that
lacks this step.

### Phase 3: Installer script

`install.sh` and `install.ps1`, tested in a container and on the Windows box.
Its check before it is published: on an image with nothing at all, run the
script, choose local, and get a search result.

### Phase 4: Install pages

Rewrite the install steps on the site and in `README.md`,
`docs/getting-started.md`, `docs/desktop-deployment.md` and
`docs/windows-install.md` around two routes: "run this one command" and
"install `uv`, add the plugin, answer one question". The manual commands move
to a troubleshooting section.

### Day 2 Operations

- The status file and log live in `setup/` under the Nexus config directory. `nx doctor` gains
  a row that reads them and is not applicable when no setup has run.
- Upgrades are unchanged: RDR-143's action for the terminal plugin, remove and
  add for the account store.
- Removal is unchanged: `nx self` and `nx daemon service` verbs.

### New Dependencies

None. The launcher, the hook and the stand-in server are standard library.

## Test Plan

- The launcher and the setup process import and run with `nexus`
  unimportable.
- Resolution order: layout before `PATH`; current directory never searched;
  the shim and the launcher agree. With no CLI the shim's note does not
  mention `nx self install`.
- No CLI, hook verb: exit 0, one line, for every verb in `hooks.json`.
- No CLI, `nx-mcp`: the stand-in answers a real MCP handshake from the SDK
  client and lists exactly the two setup tools. No CLI, `nx-mcp-catalog`:
  a stand-in with no tools.
- The CLI resolves: the launcher runs the real program in every state of the
  setup directory, including a "not now" marker, a failed status, a stale
  lock and no directory at all, and sets `NX_SETUP_SCRIPT`.
- The real `nx-mcp` registers `setup` and `setup_status` always. With no
  usable service its instructions text says so; with one it does not. With
  `NX_SETUP_SCRIPT` unset the tools start no process.
- `mcp__plugin_conexus_nexus__setup` and `mcp__plugin_conexus_nexus__setup_status`
  are in `_MANUAL_APPROVAL_REQUIRED`, each with its "requires manual
  approval" test, and in neither `_ALLOWED_TOOLS` nor `_ALLOWED_HOOK_TOOLS`.
  The wire snapshot, the registered-name set and the three tool counts are
  updated, and `tests/test_mcp_tool_count_docs.py` is run.
- Usable service: the hook's answer and the real server's answer agree on
  the same config directory for each resolver leg (environment variable,
  saved address, local lease) and for none.
- Setup hook: each of its eight states and the order between them, including
  a usable service beside a failed status (state 3 wins) and a CLI that
  cannot start beside a usable service (state 2 wins). In no state does the
  hook start a process or open a socket. The "not now" marker and a recorded
  CLI-only choice quiet the states listed under "The setup hook" and no other.
- A CLI that is found and cannot start: the launcher gives a hook program one
  line and exit 0 and a server the stand-in that says "installed but cannot
  start", on POSIX (a shebang to a missing interpreter; a `pyvenv.cfg` whose `home` is gone
  beside a dangling `bin/python`, and a gone `home` alone judged runnable) and in the Windows shape (a launcher `.exe` with no
  shebang and a `pyvenv.cfg` whose `home` is gone); `setup`'s build step runs
  for it and `nx self install --bootstrap` replaces the generation, and
  repairs a legacy `uv tool` tree whose Python is gone the same way. A
  `setup_status` poll while it still cannot start keeps the statement and the
  repair offer over a stale `done`.
- Until `setup` is called, a session start writes nothing under the Nexus
  config directory or the tools directory; pinned in an isolated home.
  (`uv` writes its own cache on the first launch, which is outside this pin.)
- Each mode runs exactly its steps: `not-now` writes the marker and starts
  no process; `cli-only` downloads no service files; `cloud` downloads no
  service files and serves the page; `local` serves no page.
- `autostart`: omitted means false. `local` with it true registers the unit
  on the unprovisioned path and on the provisioned-and-down path; with it
  false neither does. `local` on a provisioned, stopped service downloads
  nothing.
- `nx self install --bootstrap` from a throwaway environment builds one
  generation, writes a receipt with no extras and does not run the cache
  prune. Without the flag the same call still refuses.
- Lock: a second `setup` during a fresh heartbeat starts no process; a stale
  heartbeat is removed and the run resumes; `setup` after a failure resumes
  at the failed step.
- Credentials page, one test per requirement: a request without the one-time
  path gets nothing; the listener binds loopback only; the address appears on
  no command line, in no tool result and in no file but the opener file,
  which is owner-only and gone after the first load; the token appears in
  neither the log nor the status file and reaches `nx config set` on
  standard input; a rejected pair is not kept and the page stays open; an
  accepted pair closes the listener and is saved only after the build; the
  status takes only its six listed values; "page not loaded" appears at two
  minutes and the listener closes at ten; the environment-override notice
  appears when either variable is set; a wrong token fails the `/v1` route and
  a right one passes on a real local engine (the engine substrate) and on a
  small local service; the opener file is deleted when the listener closes
  without a load and, for a killed run, at the next start; a pair accepted by a
  run that is killed before it saves is lost, and the next `cloud` run opens a
  new page; Cloud is offered by both servers' tools, the table in the hook and
  `/conexus:setup` once this step is present.
- A journey test in an isolated home with a local package index: session
  start, `setup` with `local`, the real server answers after reconnect.
- Installer script: with a terminal it asks once before any download; with
  none and no flag it builds the CLI only.
- `/conexus:setup` contains no `nx` shell line, and says the CLI is too old
  when the server offers no setup tools.
- The server lookup for the session handoff finds a server behind `uv` and
  the launcher, built from the real `uv` and a real process listing; it does
  not look under a shell, a test runner or another Claude process; a space in
  an executable's path does not hide the server.
- The existing hook floor tests still pass.

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

Sam, 2026-10-12, after gate round 1:

6. Start at login is a yes or no inside the one question.
7. The credentials page has no https requirement. It and the installer
   scripts stay in this record.
8. The setup path does not prune `uv`'s package cache.

Sam, 2026-10-14:

9. A CLI that is found and cannot start (for example a generation whose Python
   `uv` pruned) is its own case. The launcher serves the stand-in that says
   "Nexus is installed but cannot start; setup repairs it"; the setup hook has
   a state for it, checked right after a live run and before a usable
   service; `setup` repairs it. This changes the launcher's rule from one
   fact to two.
10. The user's choice is honoured: "not now" and a recorded CLI-only choice quiet
    the setup hook's states that suggest a call to `setup` (one line naming
    `/conexus:setup`, no question), as "The setup hook" lists them. A live run, a
    CLI that cannot start and a usable service are not quieted. The tool
    descriptions say "not now" stops the asking, and now it does.

## Open Questions for Sam

None open.

## Finalization Gate

### Contradiction Check

Two research findings contradicted earlier drafts and the design was changed
to match each. The live Desktop conversation ran no hook on the user's machine (Discovery 7), so the
setup notice is carried by the server as well as by the hook.
`nx self install` refuses a throwaway `uv` environment (CA-3), so the build
step adds `--bootstrap`. Gate round 1 found a third: "setup complete" was decided from a
file the plugin wrote, which contradicted the record's own cases of a hand
install and "CLI only". What is served is now decided from whether the CLI
resolves, and the real server carries the setup tools. Its fix check found
that the release gate tested the credentials page while the page sat in a
later phase; the page is now Phase 2 Step 5.

One tension is accepted, not resolved: on Desktop the notice reaches the model
only through tool descriptions, so a Desktop user may not be told until they
ask for something Nexus does. The install page carries the instruction.

### Assumption Verification

| Assumption | State | How |
|---|---|---|
| CA-1 hooks run in a Desktop session | refuted | live conversation; design changed |
| CA-2 the 15 second wait on Desktop | not applicable to the measured path | live conversation |
| CA-3 `nx self install` from a throwaway environment | refuted | spike; design adds `--bootstrap` |
| CA-4 stand-in server accepted | verified in Claude Code; read from code for Desktop | spike, source |
| CA-5 `uv` launch form on Windows | verified on native Windows; Desktop by log | spike, log |
| CA-6 unattended `nx init` | verified | spike |
| CA-7 Desktop shell is not on the machine | verified, one conversation | live conversation |
| CA-8 applications ask before a plugin tool runs | assumed | noted per surface in the Minimum Viable Validation |
| CA-9 a detached setup process outlives its server on Windows | assumed | measured first in Phase 2 Step 2 |

Not verified before implementation, each named in Phase 0's "still open"
list: a reconnect replacing the stand-in with the real server; the stand-in
and the launcher under Desktop itself, which need a released plugin; plugin
commands in a Desktop conversation; `nx self install` and `nx init` from
nothing on Windows; three command forms read from source only. The first is checked in Phase 1 Step 2, when the stand-in is built. The
Desktop items are checked by the Minimum Viable Validation, which runs before
the first release that carries Phase 2.

#### API Verification

| Call | Provider | Verification |
|---|---|---|
| `uv tool run --directory ... python <script>` as a server and hook command | `uv` 0.8.0 macOS, 0.12.23 Windows | Spike |
| `uv tool run --from conexus==X nx self install` (no flag) | conexus 7.77.1 | Spike: refuses |
| `nx self install --bootstrap` | does not exist yet | Phase 2 Step 1 |
| `nx init --yes --no-autostart` | conexus 7.77.1 | Spike |
| `nx init --yes` (registers the login unit) | conexus 7.77.1 | Source search only |
| `nx daemon service install --autostart`, `nx daemon service start` | conexus 7.77.1 | Source search only |
| `nx config set service_token --stdin` | conexus 7.77.1 | Help text only |
| `resolve_base_url` in the plugin's `_endpoint_resolve.py` (the base URL; not the definition of a usable service) | conexus 7.77.1 plugin | Source search |
| `endpoint_usable` in the plugin's `_endpoint_resolve.py` | this record, Phase 2 Step 4 | Held to `resolve_service_endpoint()` by `tests/hooks/test_usable_service_agreement.py` |
| MCP `initialize` result field `instructions` | Claude Code 2.1.295 | Spike |
| Desktop bridge for local servers | Claude Desktop 2.31226.1 | Source search |

### Scope Verification

The Minimum Viable Validation is in scope and gates the release that carries
Phase 2, which includes the credentials page. Its six rows cover each answer
to the question, a hand install after declining, a reboot and an interrupted
run, on macOS and Windows, in Claude Code and from the Desktop application.
The installer script has its own check in Phase 3. The journey test in the
Test Plan covers the "local" path without a network.

### Cross-Cutting Concerns

- **Versioning**: the first CLI is built at the plugin's own version. After
  that RDR-143's lockstep action keeps them together. The launcher has no
  version-specific code: it finds a program by name and passes its arguments
  through, so it works against any CLI version that has that program.
- **Build tool compatibility**: `uv` is the only tool. Measured on 0.8.0 and
  0.12.23.
- **Licensing**: N/A. No new dependency.
- **Deployment model**: the launcher, hook, stand-in and command ship in the
  plugin. Phase 1 ships only in a client release, because its server entries
  need the CLI's lookup change (Phase 1 Step 4). Until Phase 2 is in the
  tree, a plugin-only cut (RDR-197) may carry the launcher, the lookup
  module, the shim's change and the notice script, and never `.mcp.json`,
  `hooks.json` or `version_lockstep_hook.py` ahead of that release. The
  `--bootstrap` flag and the real server's setup tools ship in the CLI
  package, so Phase 2 is one client release and its plugin parts never go
  out in a plugin-only cut ahead of it. Phase 2 changes files Phase 1 adds,
  and a deferred file is held back whole, so once any Phase 2 part is in
  the tree and the pinned release does not carry Phase 2, the launcher, the
  stand-in, the notice script and the entries that call them wait for that
  one client release with Phase 2's own parts; the shim's change
  may still go out in a cut, and the lookup module no longer can, because it
  carries `cannot_start` (Phase 2 Step 4). The cut script
  enforces this (`phase2_deferral_check`, nexus-sv2wx.13). The installer
  scripts are published on the site.
- **IDE compatibility**: N/A.
- **Incremental adoption**: Phase 1 alone is shippable: a machine with the CLI
  gains one extra `uv` launch per server start and hook and one resident `uv`
  process per server, and a machine without it gets a stand-in and one true
  message in place of errors. A plugin newer than its CLI loses the `/clear`
  and `/resume` handoff, with no message, until the CLI catches up. For a
  generation or a `uv tool` install the lockstep action upgrades it by the
  next session. Any other install (`pip`, `pipx`, an editable checkout), and
  any install whose upgrade fails, keeps the lost handoff until it is
  upgraded by hand.
- **Secret/credential lifecycle**: the cloud token, a mint credential for a
  managed user or a static token for a self-hosted engine, is typed into a
  loopback page or a terminal prompt with echo off, held in memory until the CLI is
  built, saved with `nx config set` on standard input, and never logged, never placed in the status
  file and never passed through the model. The page's address is in one
  owner-only file that is deleted on first load. An `http` service address is accepted, so the token can travel in
  the clear to the address the user typed. Rotation and override are the
  existing `nx config` verbs.
- **Memory management**: the launcher replaces itself with the real program
  on POSIX and stays as one idle parent process on Windows. On every platform
  `uv` stays as an idle parent of each server it started.

### Proportionality

The record is long for a launcher script because it reverses one prior
decision (RDR-143), implements Sam's standing requirement and touches
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
- 2026-10-12: Gate round 1: BLOCKED (1 Critical, 9 Significant, 1 ship-blocker(s)); commit `ea2593230`; critique `nexus_rdr/228-gate-critique-2026-10-12-r1`.
- 2026-10-12: Fixes for gate round 1, from `nexus_rdr/228-research-15`. What
  is served is decided from whether the CLI resolves, not from a status
  file; the real server carries the setup tools when no usable service is
  found; one setup directory; start at login joins the question; Phase 1
  carries the stand-in; `--bootstrap` flag with no extras and no cache
  prune; credentials page requirements; CA-8 and CA-9 added.
- 2026-10-12: Second fix for gate round 1, from `nexus_rdr/228-research-16`
  and fix check `nexus_rdr/228-fix-check-1705d2c4f`. The credentials page
  moves into Phase 2 so the release gate tests what that release carries;
  "usable service" is defined once over the endpoint resolver; the real
  server registers the setup tools always; `not-now` is a mode; `autostart`
  defaults to false and is honoured on the provisioned path; the setup
  process has one home in the plugin; the lock uses a heartbeat; the page's
  address goes through an owner-only opener file; no fallback build.
- 2026-10-12: Gate round 2: PASSED (0 Critical, 4 Significant, 0 ship-blocker(s)); commit `840f6b8a6`; critique `nexus_rdr/228-gate-critique-2026-10-12-r2`.
- 2026-10-13: Accepted. Residual dispositions: the Phase 1 release gate (BLOCKS-PLANNING) is carried by nexus-217c3 on Sam's choice; the 18 DISCOVER-AT-IMPLEMENTATION residuals are carried by nexus-j8ebc (4, Phase 1), nexus-9i4d4 (13, Phase 2) and nexus-omlpd (1, Phase 3). No change to the design, so no fix check.
- 2026-10-13: Implementation, Phase 1 Step 1 and 2. Added one sentence to
  "The launcher": it changes directory to the project before the hand-off.
  The record was silent, and with the entries' `--directory` form the
  programs would have taken the plugin root as the project. Sam accepted the
  reading and authorised the edit; the server half is measured in
  `nexus_rdr/228-research-17`. Sam also decided: a throwaway second
  marketplace repository carries a candidate to Desktop for both gates
  (nexus-sv2wx.1); each phase stays on its branch until its release bead,
  with a draft pull request for CI (nexus-sv2wx.2); the layout before `PATH`
  holds on every machine, with no override.
- 2026-10-13: Implementation, Phase 1 Step 4, on Sam's authorisation. The
  server entries put `uv` between the application and the server, which
  broke the CLI's direct-child lookup for the `/clear` handoff; Sam chose to
  fix the lookup. So Phase 1 is a client release and not a plugin-only cut
  (Deployment model, Incremental adoption), `uv` is recorded as a resident
  parent ("The launcher", Memory management), and the lookup change and its
  test are in Step 4 and the Test Plan. Step 2 says three manual commands,
  as the README gives them, where it said two.
- 2026-10-13: Phase 1 review, four answers from Sam. `uv` is a hard
  requirement of the plugin: without it the two Nexus servers no longer
  start, where before Phase 1 they did (Failure Modes). The lockstep action
  brings a newer plugin's CLI up only for a generation or `uv tool` install
  (Incremental adoption). A program that is found and cannot start is
  accepted as built for Phase 1 and goes to Phase 2 Step 4 (Failure Modes).
  The setup hook speaks on every session-start event (Phase 1 Step 3).
- 2026-10-13: Rebased onto develop for the push. The two gated commits were renumbered with the file unchanged, and the gate lines above now cite the new numbers; the T2 gate and fix-check records name both the old and the new.
- 2026-10-13: Phase 1 gate, Desktop on macOS, on Sam's decision. The
  plugin's hooks run for a Desktop conversation in its cloud workspace, and
  the setup hook told the model Nexus was not installed while the servers on
  the laptop worked (`nexus_rdr/228-research-22`). Discovery 7 is corrected:
  no hook runs on the user's machine. The setup hook now says nothing when
  `CLAUDE_CODE_REMOTE` is true ("The setup hook", "How the question reaches
  the user"); the server's tool descriptions stay the Desktop channel.
- 2026-10-14: On Sam's word, a CLI that is found and cannot start is a case of
  its own (Technical Design, "What is served"; hook state 2; Decisions Made 9).
  The hook has eight states; the first build had the "not now" marker change
  state 6 only, which the fix round below replaces.
- 2026-10-14: Fix round on the first Step 4 build, on Sam's word ("honour the
  choice"; the rest follows the review). The "not now" marker and a recorded
  CLI-only choice quiet the states that suggest a call to `setup` (hook states,
  "The setup hook"). On POSIX "cannot start" needs a dangling interpreter link
  or a bad shebang beside or instead of a gone `home` (Technical Design,
  "What is served"); `nx self install --bootstrap` repairs a legacy
  `uv tool` tree and a broken `current` the same way. The usable-service check
  reads flow-style credentials and keeps a lease past its time to live while its
  owner process is alive, and the hook's reads share one 30 ms budget.
  The on-disk sizes sit beside the download sizes in the tool descriptions.
  Failure Modes, the stand-in's lifetime, Deployment, the Reuse table and
  Sources now say what Step 4 built.
- 2026-10-14: Fix round 2 on the Step 4 build (review of the first fix round).
  `setup_status` in the cannot-start mode asked nothing of the machine and so
  showed a stale `done` as a finished repair, which hid the repair offer and
  told the user to reconnect, serving the stand-in again; it now asks the
  lookup again at each poll (Technical Design, "What is served"). The setup
  process's usable-service check grants no grace to a lease past its time to
  live, so a wedged supervisor does not make it skip the start step. The
  hook's 30 ms read budget starts after the plugin parts are imported. The
  words for a run that did not end well fit the run (interrupted, failed, nx
  doctor problems, cannot run on this host). Its verification found the same
  stale `done` when no CLI is found at all (an install removed since), so the
  poll asks the lookup in that mode too and puts the question again.
- 2026-10-15: Phase 2 Step 5 built (nexus-sv2wx.18). The token check is
  `GET /v1/_whoami`, the doctor's own static-token probe (a 403 there is a live
  mint-scoped credential, which the page refuses because it saves only a service
  token). The managed edge answers `/health` 403 to a request with no token
  (`nexus_rdr/228-research-36`), so the check sends the token to `/health` as
  well as to the `/v1` route and none to `/version`; a redirect is never
  followed. A timeout during the build lets the build finish and then ends the
  run `failed` at step `credentials`.
- 2026-10-15: The credentials page takes a mint credential (Sam, 2026-10-15).
  Every managed user holds one (conexus, 2026-10-15), and the page as built
  refused it. The page asks for address, token and tenant; the check is
  `/version`, then `GET /v1/_whoami`, then for a 403 one mint of a 60-second
  data token with the tenant; `/health` is dropped. A mint credential is saved
  as `mint_token` and `mint_tenant`, a static token as `service_token`, never
  both. `nx config init` gains the same two kinds and a hidden token prompt.
  The environment notice names `NX_MINT_TOKEN` and `NX_MINT_TENANT`. A
  browser offering to save the token is a risk measured in the Minimum Viable
  Validation.
  A build writes the lockstep marker only after `nx --version` of the CLI it
  left confirms the version it built; the lockstep hook treats a CLI that cannot
  start as absent; the bootstrap build names CPU torch on Linux.
