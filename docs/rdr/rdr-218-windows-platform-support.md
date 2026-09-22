---
title: "Windows Platform Support: a Low-Friction Plugin Install for the CLI and the Desktop"
id: RDR-218
type: Architecture
status: draft
priority: high
author: Sam
reviewed-by: pending
created: 2026-09-21
related_issues: [nexus-sa187, nexus-5dcky, nexus-t9klx, nexus-34f7r, nexus-1vc0n]
related_rdrs: [RDR-126, RDR-155, RDR-197, RDR-210, RDR-215]
---

# RDR-218: Windows Platform Support — a Low-Friction Plugin Install for the CLI and the Desktop

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside the template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

**Provenance.** On 2026-09-18 Sam ruled that Windows support is WSL2 only — no
native Windows engine, no Windows PostgreSQL bundle, no native Windows client
build (T2 `nexus/windows-support-research-of-record-2026-09-18`). WSL2 is the
Windows Subsystem for Linux version 2: a real Linux kernel running in a
lightweight virtual machine on Windows, able to run unmodified Linux binaries.
That decision's own MCP research concluded that "the only clean configuration
is the Claude Code CLI installed inside WSL2."

On 2026-09-21 that conclusion was falsified by measurement. Native Windows
Claude Code, with the conexus plugin and a natively installed conexus client,
talked to a nexus-service running inside WSL2 and completed a full write/read
round trip through the CLI, with the row independently read back from the Linux
side (T2 `nexus/windows-client-over-wsl2-service-trial-2026-09-21`). Both MCP
servers connected as native Windows processes; an MCP *tool* round trip was
attempted and is inconclusive, for a reason unrelated to Windows (Gap 3). So
the configuration the decision ruled out works at the CLI layer and is
unproven at the tool layer. The reason it now works is RDR-215, which removed
the last shell script from the plugin's hook tier three days earlier
specifically "so a native Windows client becomes viable" — speculative
groundwork at the time, validated here.

Sam then set the goal for this record, in his words: the target is "a claude
plugin for the windows CLI and the desktop (CoWork/Code)", with "as low
friction for our users as possible". He ruled out optimising for the CLI-only
experience and ruled out relying on a CLI for *bootstrapping*: "Updating, yes.
but not a hard req on installation." Then the wrinkle, which is the whole
design problem: "But we have the WSL2... :)"

This record exists because supporting that configuration reverses a locked
decision, and because the low-friction goal is not reachable by executing the
beads already filed — those fix defects found along the way, and none of them
answers what "install" should mean on Windows.

---

## Problem Statement


**The goal is a new capability, not a port.** A never-touches-a-terminal
install does not exist on any platform today. All three documented install
paths — `docs/desktop-deployment.md:62-80`, `README.md:72`, and
`web/index.html:225-241` — require a terminal FIRST (`uv tool install conexus`,
`nx self install`, `nx init`, `nx doctor`) and only then a double-click, and
`web/index.html:227` states outright that "The extension does not start the
service itself." So Sam's constraint that a CLI must not be an installation
prerequisite is not a Windows requirement that macOS already satisfies; it is
unmet everywhere, and Windows is where it becomes unavoidable because there is
no terminal story to fall back on.

A Claude Desktop or CoWork user on Windows cannot install conexus. Not "finds
it awkward" — there is no documented path, and `docs/getting-started.md:15`
tells them so by name: the bundled PostgreSQL "is built for those three targets
only, so an **Intel Mac** or **Windows** cannot run a local install: `nx init`
stops with an error naming how to get a bundle, rather than falling back to
anything."

The honest measure of the problem is what a working install actually cost when
one was built by hand on 2026-09-21. Every step below was performed, in order,
on a Windows 11 host with WSL2:

1. **Install WSL2 and a distro.** On this host the inbox `wsl.exe` was a stub
   that answered `wsl --install` with an instruction to run the command that
   just failed; `winget install --id Microsoft.WSL` failed
   `0x80070005 Access is denied` under an admin token; the working path was the
   official MSI installed with `msiexec /quiet /norestart`. Enabling the
   optional feature required a reboot.
2. **Create an unprivileged user inside the distro.** The distro's default user
   is root and `initdb` refuses to run as root, so `nx init` fails with
   `pg_provision_refused_root`. PostgreSQL's rule, not ours.
3. **Install `uv` inside the distro.**
4. **Install conexus with an explicit interpreter pin.** Ubuntu 26.04 ships
   CPython 3.14 and our torch pin has no `cp314` wheels, so a bare
   `uv tool install conexus` fails resolution outright (`nexus-sa187`).
   `--python 3.12` is mandatory.
5. **Run `nx init`.** Downloads the PostgreSQL bundle, the bge-768 ONNX
   embedding model (~416 MB), the ms-marco cross-encoder (~91 MB), and the
   native engine binary.
6. **`loginctl enable-linger` for that user.** Without it, systemd-logind tears
   the service down the moment its launching session ends. Observed three times
   before it was diagnosed.
7. **Start the service with `NX_SERVICE_BIND=0.0.0.0`.** The default bind is
   invisible to WSL2's localhost relay (Gap 2).
8. **Install the conexus client on the Windows side.**
9. **Read the service's port and token out of the WSL filesystem.** There is no
   mechanism for this (Gap 1).
10. **Get `NX_SERVICE_HOST` / `NX_SERVICE_PORT` / `NX_SERVICE_TOKEN` into the
    environment Claude Code inherits.**
11. **Keep the distro alive** against WSL2's 15-second distro and 60-second VM
    idle shutdown.

Eleven steps. Two need administrative rights, one needs a reboot, roughly
1.5 GB is downloaded, and step 9 has no mechanism at all rather than a manual
workaround. That is not an install; it is a project. For the population this
record targets — a Desktop user who may never open a terminal — it is a
non-starter, and Sam's constraint that a CLI must not be an installation
prerequisite rules out most of the obvious shortcuts.

#### Gap 1: a Windows client cannot discover the service it is supposed to use

The service binds an ephemeral port, freshly assigned on every start. Across
one afternoon it occupied 39325, 51293, 58475, 34315, 57907, 47801, 48275 and
38843. The port and the bearer token are published to an address file inside
the distro at `~/.config/nexus/storage_service_addr.<uid>`, which is where a
Linux client's discovery chain finds them.

A Windows client has no path to that file. `src/nexus/db/service_endpoint.py`
resolves an endpoint through a tiered chain whose first tier is explicit
`NX_SERVICE_HOST` / `NX_SERVICE_PORT` / `NX_SERVICE_TOKEN` environment
variables and whose later tiers read local lease files — none of which exist on
the Windows side of the boundary. So the only configuration that works today is
the one a human performs by hand: read the file inside the distro, copy two
values out, export them on Windows before launching Claude Code, and repeat
after every service restart.

Windows *can* read the distro's filesystem, over the `\\wsl$\<distro>\` UNC
path, so a reader is feasible. Nothing implements one, and the resolution chain
has no tier that would call it.

**This is stronger than "unimplemented": the lease tier cannot succeed on
Windows even in principle, and it fails for a reason nothing surfaces.**
Measured 2026-09-22 on qwentescence, from a native Windows client, at debug
level:

```
service_endpoint_lease_discover_failed error="module 'os' has no attribute 'getuid'"
```

The address file is named for the POSIX uid — `storage_service_addr.<uid>` —
so discovery calls `os.getuid()`, which does not exist on Windows at all. The
attempt raises `AttributeError` before it reaches the filesystem, and the
`\\wsl$\` path being readable is beside the point: there is no uid to build the
filename from. A Windows reader for that tier therefore needs a decision this
record has not taken — what identity names that file when the platform has no
uid — and not merely an implementation.

It also explains, retroactively, something the 2026-09-21 trial recorded as
friction without a cause: every service restart needed `NX_SERVICE_*`
hand-copied again. That was not a rough edge on an unfinished path. It was the
only path, because the tier beneath it can never fire.

This gap is load-bearing for every option below that keeps the service in WSL2.
It is also the only step in the eleven with no workaround at all, which makes
it the sharpest thing this record has to decide.

#### Gap 2: the service's default bind is invisible across the WSL2 boundary

The service binds loopback only, and on Linux that is an IPv6-family socket on
the IPv4-mapped address `::ffff:127.0.0.1`. WSL2's localhost relay — the
mechanism that lets Windows reach `localhost:<port>` inside the distro — does
not forward it.

Measured 2026-09-21 with controls evaluated at the same instant, against a
service confirmed live:

| listener inside the distro | Windows `127.0.0.1` | Windows `::1` |
|---|---|---|
| `python -m http.server`, `0.0.0.0` | reachable | — |
| `python -m http.server`, `127.0.0.1` | reachable | — |
| `python -m http.server`, `[::1]` | unreachable | reachable |
| nexus-service, default `[::ffff:127.0.0.1]` | unreachable | unreachable |
| nexus-service, `NX_SERVICE_BIND=0.0.0.0` | — | reachable, HTTP 200 |

A plain IPv4 loopback bind is forwarded; the service's dual-stack bind is not.
`NX_SERVICE_BIND` already exists as an override documented for containers
(`service/src/main/java/dev/nexus/service/NexusService.java:309`), and setting
it to `0.0.0.0` makes the service reachable — at the cost of binding past
loopback, which that same comment warns against: "Binding beyond loopback
exposes a ..." So the working configuration today trades a security property
for reachability, and it does so silently.

The likely correct fix is narrower than the workaround: make the loopback
default an IPv4-family socket, which the relay does forward, and keep
loopback-only semantics intact. That is a change to the Java service, so it
rides an engine tag and has a different release path from everything else in
this record.

#### Gap 3: the service does not survive, for two independent reasons

Two distinct failures, discovered in sequence, each of which alone makes a
local Windows install useless.

**Session teardown.** systemd runs as pid 1 in the distro, but lingering was
not enabled for the service's user, so logind killed the service when its
launching session ended. `loginctl enable-linger` fixes it and was verified to
fix it. Until it was diagnosed, this produced three separate false readings in
which the service appeared unreachable from Windows and the actual cause was
that it had already exited — a measurement error worth recording because the
next person will make it too.

**Distro idle shutdown.** WSL2 stops the distro 15 seconds after the last
session ends and the VM 60 seconds later, background processes and lingering
notwithstanding. Observed live: a service confirmed running vanished between
two probes minutes apart, taking PostgreSQL with it. The 2026-09-18 research
already prescribed the remedy — a Task Scheduler task at logon and startup
running a sleeper inside the distro, plus `[boot] systemd=true` and
`.wslconfig` `vmIdleTimeout=-1` — and none of it is implemented, documented, or
surfaced by `nx doctor`.

Three residues of the VM model cannot be fixed from inside the distro at all,
and the RDR should state them rather than let them be discovered later: host
sleep freezes the guest clock, which exposes T1 lease and T2 TTL arithmetic; VM
teardown is an unclean PostgreSQL shutdown, so WAL recovery becomes the normal
startup path rather than the exceptional one; and Windows Update reboots.

#### Gap 4: the plugin hangs Claude Code on Windows before any of this matters

With the conexus plugin enabled and no service endpoint configured, `claude -p`
on native Windows hangs indefinitely — past 100 seconds and past 150 seconds,
on a prompt whose entire body is `Reply with exactly: PLUGINPROBE`. Isolated
four ways on the same host within minutes: no plugin and no endpoint returns
promptly; plugin enabled and no endpoint hangs; plugin disabled returns
promptly; plugin enabled *with* the endpoint set returns promptly.

**That isolation named WHEN correctly and WHY wrongly, and the correction
matters for the remedy.** The original reading here — "a hook blocks when it
cannot resolve a service endpoint" — was an inference from the four probes,
not a mechanism, and measurement on 2026-09-22 falsified it. None of the
SessionStart hooks block: all six `nx-hook` verbs return in under 2.1 seconds,
the two `python3` entries fail loudly in 0.1s, both MCP servers initialize in
about a second and list 64 and 10 tools instantly, and `npx` fetches the
sequential-thinking server in 11.6s cold. What blocks is the TOOL CALL, and
there are two distinct blocking sites:

- `hook_stop_verification`, which `hooks.json` wires on Stop, blocks in
  `nexus/hooks/verification_config.py:97` `_git_common_root` — inside a
  `subprocess.run(["git", ...], capture_output=True, timeout=5.0)`, still
  there when sampled at 25 seconds. This is the site that produces the
  reported symptom, because Stop fires at the end of a `claude -p` turn — the
  model answers, and then the session sits there.

  **Why it outlives its own timeout is unexplained, and the obvious answer
  is wrong.** This record first said "the pipe-drain shape": the timeout
  kills the direct child and the drain that follows waits on a handle a
  grandchild still holds. A census of the 80 capture-plus-timeout sites in
  `src/nexus/` (`nexus-t10nc`) falsified that for THIS site specifically —
  `git rev-parse` with stdout piped is leaf-shaped, spawning no pager, no
  hook and no credential helper, so the write end was held by something that
  is not a descendant of that git at all. The generic story does cover the
  other exposed sites; it does not cover the one that was measured. A
  standing hypothesis, labelled as one: on Windows, concurrent spawning from
  threads can leak a pipe handle into a SIBLING process, and the hook path
  does run each tool in a worker thread. Testable directly, untested so far.
- `tuple_registry`, and other tools that construct a `T2Database`, block in
  `T2Database.__init__` importing numpy's C extension. The same import outside
  that process takes 0.08 seconds, including from a worker thread under an
  asyncio loop, and this one is unexplained.

Two consequences the first reading obscured. The endpoint is not what blocks:
`hook_stop_verification` touches no storage on the path that hangs, and
setting `NX_SERVICE_*` made the fourth probe fast for a different reason than
the one assumed. And the git-subprocess site is not obviously Windows-specific
— an unbounded pipe drain behind a bounded-looking timeout is a general shape,
and nothing measured says it cannot happen elsewhere.

The remedy follows from having two sites rather than one: a bound at the
hook-tool boundary, which covers both and covers whatever the third turns out
to be, rather than a repair to either blocking path. Hook tools are advisory
and the harness already carries its own `timeout` per entry, so a hook past
that budget can no longer affect anything except by holding the session open.
Measured on qwentescence against a wheel carrying the bound: the Stop hook
returns at its bound with the same empty result a crashed hook produces, where
before it never returned.

It reaches a Windows user first for a structural reason, whatever its cause.
On Linux and macOS a running local service exists or is started, so these
paths are barely reachable; on Windows there is no local service and there
cannot be one, so the unresolvable state is the *default* for every native
Windows user. It is therefore the first thing a new user meets, and everything
that works — the client, both MCP servers, the hook executables, and the MCP
tool surface itself, all verified on that same host — is behind it.
`claude mcp list` reports both conexus servers Connected right up until a
plain prompt hangs. Filed as `nexus-5dcky`.

#### Gap 5: the desktop bundle is a shim, not a bundle, and it excludes Windows by manifest

The `.mcpb` artifact is not a self-contained installer. It is `manifest.json`,
`pyproject.toml` and two small source files — roughly one to two kilobytes,
with zero binaries, zero wheels and no interpreter. `manifest.json:31` launches
a bare `uv`, and `mcpb/src/bootstrap.py:24-27` says so plainly: "uv runs this on
whatever Python it discovers". `docs/desktop-deployment.md:66-69` states the
prerequisite as "uv on host PATH", with macOS and Linux instructions and none
for Windows.

So the bundle cannot deliver a no-terminal install on any platform, and three
things block it on Windows specifically:

- `manifest.json:169-171` declares `compatibility.platforms = ["darwin",
  "linux"]`. Windows is gated out at install time, before any of this record's
  other gaps are reachable.
- `bootstrap.py:116` ends in `os.execvp`, and the comment at `:115-116` says
  the design depends on POSIX exec semantics for stdio pipe inheritance.
  Windows has no equivalent, and `os.execvp` there also ignores `PATHEXT`, so
  the bare `uv` would not resolve to `uv.exe`.
- `mcpb/pyproject.toml:13` depends on `conexus[local]>=7.56.0` — the `[local]`
  extra is mandatory because the bundle "cannot run the interactive `nx init`
  embedder choice" — and whether Windows wheels exist for that whole dependency
  closure is unverified anywhere in the tree.

The bundle also assumes a nexus-service already exists: it starts nothing, and
with no endpoint `src/nexus/db/service_endpoint.py:476-486` raises
`ServiceEndpointUnresolvableError` with no fallback. The genuinely dangerous
failure is not that error but its quiet cousin, recorded at
`docs/desktop-deployment.md:262`: a GUI-launched subprocess inherits no shell
environment, resolves a local bge-768 embedder against voyage-1024 collections,
and returns empty searches — a silent mis-mode rather than a refusal.

Two documentation drifts were found while establishing this and should be fixed
independently of this record: `docs/mcp-servers.md:3,14` claims the `.mcpb`
ships two MCP servers when `manifest.json` exposes only one, and the shipped
`entry_point` is `src/bootstrap.py` where `docs/rdr/rdr-126-*.md:190` specifies
`src/server.py`.

#### Gap 6: declaring a supported platform obliges a gate we do not have

CI runs thirty `ubuntu-latest` jobs and one `macos-14` job. There is no Windows
runner and no WSL runner anywhere in `.github/workflows/`.

Every Windows claim in this record, including all the measurements above, is a
hand-measurement on one host on one afternoon. That is the right evidence for a
research finding and the wrong evidence for a support commitment. Declaring a
third supported platform with no automated gate is the vacuous-gate doctrine
(`nexus-moht0`) applied to an entire platform: a sweep that found nothing to
check is a failure, not a pass.

It is worse than absent for the Desktop surface specifically:
`docs/desktop-deployment.md:213-257` records that the Desktop path currently has
**no validation gate at all**, both halves of its minimum-viable-verification
having been deleted (disposition `nexus-uvn3t`). So the surface this record
most wants to extend is the one with the least evidence behind it today.

The shape of the gate depends on which option below is chosen, which is an
argument for deciding the option first — but it cannot be deferred past the
decision, because "supported" without a gate is a claim we have no way to keep.
Note that "no Windows CI" does not by itself imply "add a Windows runner":
Phase 4 argues the gate splits, with only its boundary half needing real
Windows hardware and belonging in the release battery rather than a workflow.

---

---

## Relationship to Prior Records


**T2 `nexus/windows-support-research-of-record-2026-09-18`** is the decision
this record revisits. Its three research documents stand and are reused here:
the native-executable options pass, the 19 catalogued failure modes for
bridging stdio MCP servers across the Windows/WSL boundary, and the WSL2
service-persistence analysis whose prescriptions Gap 3 confirms by measurement.
What has changed is one premise, not the research: that document concluded
"hooks are bash invoked directly by Claude Code, so a wrapper only for
`.mcp.json` leaves every hook broken." RDR-215 landed the next day and made
that false.

**RDR-215** (plugin hooks as `nx` verbs) is the enabling work. It deleted every
shell script from the plugin so that "a native Windows client becomes viable",
in `src/nexus/hooks/verification_config.py:22`'s own words, and the goal it
named is the goal this record builds on. Its job is not finished: five
`hooks.json` entries still invoke a bare `python3`, which stock Windows does not
have (`nexus-t9klx`). Sam's ruling on 2026-09-21: "we need everything ported."

**RDR-126** is the record that deferred this one. Its own text, at
`docs/rdr/rdr-126-*.md:192`, defers "Windows Claude Desktop (daemon installer
has no Windows path; defer to a follow-up RDR)". RDR-218 is that follow-up, and
the deferral reason still holds: `src/nexus/daemon/installer.py:190-197`
branches on `launchctl` or `systemctl` with no third arm.

**RDR-155 P4b** is why the service side is hard. It consolidated T3 onto
pgvector inside PostgreSQL and deleted the last alternative substrate, so there
is no longer a storage mode that runs without PostgreSQL. Every option below
inherits that.

**RDR-197** (the independent plugin release channel) is the precedent for
shipping plugin-surface content on its own cadence, which matters if the
Windows plugin surface moves faster than the client.

**RDR-210** governs embedding posture per boot, which constrains what a
cloud-mode Windows client can assume about the engine it talks to.

---

---

## Context


### Background

conexus ships as two things that are easy to conflate and must be separated to
reason about this at all.

The **client** is a Python wheel: the `nx` CLI, the two MCP servers
(`nx-mcp`, `nx-mcp-catalog`), the hook runtime (`nx-hook`), and the session-end
launcher. It is pure Python with no compiled component of its own, and
`pyproject.toml` declares all five as `[project.scripts]` console scripts —
which means the installer emits a real `.exe` launcher per entry point on
Windows, with no cross-compilation and no build step of ours. This was verified
on 2026-09-21: `uv tool install` on Windows reported "Installed 5 executables",
and `nx-hook.exe` and `nx-session-end-launcher.exe` both ran and exited 0.

The **service** is a Java binary (`nexus-service`) in front of a bundled
PostgreSQL 17 carrying the pgvector extension. It owns all three storage tiers.
Neither piece has a Windows build, and the PostgreSQL bundle is the harder of
the two because a Homebrew or distro PostgreSQL is explicitly refused — there is
no host-fallback leg (`_NO_HOST_FALLBACK`, `src/nexus/db/pg_provision.py:257`).

The distinction matters because the client half of a Windows port is
essentially done and was demonstrated, while the service half is the entire
problem.

### Technical Environment

Verified on qwentescence, a Windows 11 Pro host (build 10.0.26200, AMD64) with
WSL2 (2.7.14.0, kernel 6.18.33.2) running Ubuntu 26.04.1 LTS, on 2026-09-21:

- Native Windows Claude Code 2.1.220, conexus plugin 7.56.0 installed and
  enabled, conexus client 7.56.0 installed natively via `uv tool install`
  (217 packages).
- `claude mcp list` on Windows: `plugin:conexus:nexus` and
  `plugin:conexus:nexus-catalog` both Connected.
- Service inside WSL2: engine `release_version 0.1.129` — the pinned identity —
  `embedding_mode onnx-local`, pgvector 0.8.2, `schema_latest_id
  grants-nexus-diag-5`, 460 changesets applied.
- A `nx memory put` / `nx memory get` round trip from the Windows client, with
  the row independently read back from the Linux side, proving it reached the
  engine's PostgreSQL rather than a local fallback.
- An MCP-tool round trip was attempted and is **inconclusive**: the run timed
  out at 400 seconds, and the distro shut down underneath it (Gap 3), so the
  timeout is not attributable to the MCP path. This is the one link in the
  chain that remains unproven.

Two Windows-side frictions worth carrying, neither of them ours:
`uv tool install` initially failed with "Failed to inspect Python interpreter
... untrusted mount point (os error 448)" because a stale uv trampoline sat at
`C:\Users\<user>\.local\bin\python3.exe`, fixed with
`--python-preference only-managed`; and the only `npm` on the distro's default
PATH is the *Windows* `node-v24.15.0-win-x64` binary reached through `/mnt/c`,
so any provisioning step that shells out to npm inside WSL gets a Windows
executable.

---

---

## Research Findings


### Investigation

Four threads, three of them dispatched as parallel surveys and folded in below;
the fourth is the hand-built install recorded in the Problem Statement.

**Thread 1 — the desktop distribution path. Complete; see Gap 5.** The
headline is that the `.mcpb` is a launcher shim rather than a bundle, that it
excludes Windows by manifest, and that no no-terminal install exists on any
platform. One further finding belongs here rather than in a gap: the repo
already contains a `winget install uv` instruction, at
`src/nexus/hooks/preflight_verb.py:128-142` — the only Windows onboarding
instruction anywhere in the tree — but it is reachable only from a Claude Code
hook, never from the bundle. Whatever this record decides, that instruction is
evidence the CLI-side Windows story was already being thought about in one
place and not joined up.

**Thread 2 — cloud-mode onboarding. Complete; see Alternative 1.** The decisive
finding is that cloud access is operator-issued and deliberately not
self-serve, which closes the option that would have been lowest-friction.

Two further findings belong here because they affect every option, not just A.
First, cloud mode *does* skip the expensive local provisioning — PostgreSQL
bundle, engine binary, both ONNX models, supervisor and autostart all fall
away (`src/nexus/commands/init.py:1067-1086` early-returns;
`managed_endpoint.py:6-10` "no local Java service and no local Postgres") — so
the substrate problem really is separable from the client problem. Second, and
cutting the other way, the *wheel's dependencies are mode-blind*:
`onnxruntime`, `tokenizers`, `torch`/`torchvision`, `mineru[pipeline]` and
`docling` all sit in `[project] dependencies`
(`pyproject.toml:62,67,116-131`), so even a client that will never embed
locally drags the entire heavy closure. That is what makes Gap 5's "are there
Windows wheels for the whole closure" question load-bearing rather than
incidental, and it is the same closure that already fails to resolve on
CPython 3.14 (`nexus-sa187`).

Three documentation drifts surfaced while establishing the above, each worth
fixing independently of this record: `docs/managed-onboarding.md:28,38-41`
still instructs `export NX_STORAGE_BACKEND=service`, which is now a no-op since
`service` is the only backend; `service/SECURITY.md` still carries the retired
"do not deploy in multi-principal environments" warning; and the docs name one
endpoint with no staging URL or environment matrix, which matches the known
single-environment posture (conexus-vbti) rather than contradicting it.

**Thread 3 — the engine and PostgreSQL option space. Complete.** Three
findings carry weight beyond their own option. First, the platform refusal has
exactly one choke point, `pg_bundle.py:54-72`, which is why an appliance
running `linux-amd64` artifacts pays none of it — the basis of the direction
taken. Second, the JVM-JAR run path is a sanctioned production path rather than
a test-only shim, which makes a future native Windows service materially
cheaper than the 2026-09-18 research implied. Third, the split of portability
is not where one would guess: the *engine* startup is largely portable already
(environment-variable config, a TCP listener, a `ProcessHandle` parent-death
watchdog, no POSIX Java APIs, no Windows mentions in any Liquibase changelog),
while the *Python supervisor* that manages it is POSIX throughout.

Two absences are worth recording because their absence is the finding: there is
no documented procedure for adding a platform target to the PostgreSQL bundle —
the survey inferred an eight-site edit list including the ABI-floor `case`
statements that hard-fail an unknown architecture — and no JAR is ever
published as a release asset, so the JVM path is reachable today only from a
locally built jar.

### Key Discoveries

**Verified by measurement** (all 2026-09-21, one host):

1. A native Windows client reaches a WSL2-hosted service and completes a real
   storage round trip, confirmed from both sides.
2. Both conexus MCP servers connect as native Windows stdio servers. Because
   the client is installed on Windows, nothing crosses the WSL boundary in the
   stdio path — only HTTP to the engine does, which sidesteps the entire class
   of stdio-relay failure modes the 2026-09-18 research catalogued.
3. WSL2's localhost relay forwards IPv4-family loopback binds and does not
   forward the service's dual-stack default (Gap 2 table).
4. `loginctl enable-linger` is necessary and sufficient for session-teardown
   survival; it is not sufficient against distro idle shutdown (Gap 3).
5. The plugin hangs a native Windows session when no endpoint is configured
   (Gap 4), isolated four ways.
6. The hook tier's executables work on Windows; five `python3` entries do not
   have an interpreter to run on.

**Corrected by measurement.** Two predictions made while surveying this were
wrong and are recorded so they are not re-derived. `os.fork` in
`src/nexus/_session_end_launcher.py` was predicted to break the SessionEnd hook
on Windows; it does not, because `main()` already guards with
`if not hasattr(os, "fork")` and falls through to a synchronous path, and the
module docstring names Windows at line 48. `nx-hook` was predicted to be a
problem; it exits 0. The codebase is further along on Windows than a first
reading suggests, and that is largely RDR-215's doing.

**Documented, not measured.** The 2026-09-18 research's claims about the
native-executable option space: that a Windows native engine image needs VS
Build Tools 2022 on a Windows host, that PG17+pgvector has no relocatable
Windows bundle available, and that Windows-on-ARM has no GraalVM native-image
or onnxruntime target. This record does not re-derive them; it treats them as
the cost inputs to Alternative 3.

### Critical Assumptions

**That the MCP tool surface works from a native Windows client.** Not
established. The connection is established and the CLI round trip is
established, but the one attempt at an MCP *tool* round trip timed out with the
substrate gone. Every option below assumes this works. It must be proven before
this record is gated, and it is cheap to prove once Gap 3 is handled.

**That "a lot of MCP drops" is not a Windows blocker.** A machine-wide sweep on
2026-09-21 across 345 conexus MCP log directories found seven files carrying the
transport-drop signature all-time, and zero in the window after the
`_sdk_patches` fix shipped (commit `92b7ad439`, in v7.55.3 and v7.56.0), with
the patch confirmed live in those sessions. The Windows-specific worry is
different: `nexus-dgvsz`'s starvation symptom shares one serialized stdio pipe
across nine `mcp_tool` hook entries with 5–10 second timeouts, and Windows adds
latency on that pipe (`nexus-34f7r`). Unmeasured on Windows.

**The two load-bearing mechanism claims are now VERIFIED by experiment**
(2026-09-21, qwentescence, WSL 2.7.14.0, kernel 6.18.33.2). They were asserted
first and tested afterwards, which is the wrong order; the record of the test
is below so nobody has to take the assertion on faith again.

*A `docker export` rootfs boots under WSL2 with systemd as pid 1.* An
`ubuntu:24.04` image with `systemd systemd-sysv dbus` installed, a baked
`/etc/wsl.conf` carrying `[boot] systemd=true` and `[user] default=nexus`, and
an unprivileged `nexus` user, cross-built `--platform linux/amd64` on an arm64
Mac, exported with `docker export` to a 104 MB tar, copied to the Windows host
and imported with `wsl --import`. Results: `IMPORT_EXIT=0`; `PID1=systemd`;
`WHOAMI=nexus`, so the baked default user applied; `ARCH=x86_64`, so the
cross-built rootfs runs on the AMD64 host; and the image marker confirmed the
booted distro was that image.

`systemctl is-system-running` reports **degraded**, and the single cause is
`kmod-static-nodes.service` ("Create List of Static Device Nodes"), which is
meaningless under WSL's own kernel. Nothing else failed. A first-boot warning —
"Failed to start the systemd user session" — is cosmetic: `systemd-logind` is
`active`, and **`loginctl enable-linger nexus` returns `Linger=yes`**, so Gap
3's session-teardown fix works inside a docker-built appliance.

*WSL2 mounts a separate virtual disk, and the data survives replacing the
image.* A 2 GB expandable VHDX created with `diskpart` (`New-VHD` is absent on
this host — no Hyper-V module), attached with
`wsl --mount --vhd <path> --bare`, appeared as `/dev/sde`, was formatted ext4
**inside the distro** — which is what the 2026-09-18 research requires of the
PostgreSQL data directory, as opposed to a 9P mount under `/mnt/c` — mounted,
and round-tripped a marker file.

Then the decisive test: the distro was `--unregister`ed and re-imported from
the same tar, simulating an image update, and the volume reattached.
`SURVIVED_MARKER=rdr218-data-volume` against a fresh rootfs. So "update the
service without toasting the data" is a measured property of this design, not
an aspiration.

**Still unverified, and it is the friction claim rather than the mechanism:**
whether `wsl --mount` and `wsl --import` work *without administrative rights*.
Both succeeded here, but the ssh session that ran them carries a full
Administrator token, so the experiment cannot distinguish "works" from "works
because elevated" — and Microsoft documents `wsl --mount` as requiring
elevation. This matters for the Desktop install story specifically and is the
next thing Phase 1 should settle.

**Also unverified, and smaller:** that a *fixed* port behaves under WSL2's
localhost relay the way ephemeral ones did — what was measured was the bind
*family*, not port fixedness; and that Windows wheels exist for the whole
conexus dependency closure, which the native-client half needs and which no
gate covers.

**That WSL2 itself is acceptable as a dependency.** Every option except C
requires it. On the one host tested, installing it required an MSI, admin
rights and a reboot, because the inbox stub and winget both failed. If that is
representative, "WSL2 is present" is not a safe assumption for a Desktop user
and belongs in the install flow rather than the prerequisites list.

---

---

## Proposed Solution


### Direction, set by Sam

Five options were surveyed. On 2026-09-21, having read the survey and the
cloud-access finding, Sam's ruling was: "let's focus on appliance as local is
the main target for now." So the **pre-built WSL2 appliance** is the direction
and local mode is the target population. The four alternatives are kept under
"Options considered and not taken" below rather than deleted — the cloud
option because the *reason* it closed constrains everything else, and the
native-Windows option because it is the fallback if the appliance shape fails
Phase 1.

### Why the appliance is cheaper than it looks

The finding that makes this tractable, and it is worth stating before the plan:
**the appliance requires no new platform target.**

`src/nexus/db/pg_bundle.py:54-72`'s `current_platform_tag()` is the single
choke point that refuses Windows — it knows `mac-arm64`, `mac-x64`,
`linux-amd64` and `linux-arm64`, and raises a `RuntimeError` naming Windows as
a "release N+1" follow-on. Every consumer routes through it
(`pg_bundle.py:129`, `binary_install.py:394`, `:611`), and the engine binary's
`asset_name()` raises there too, uncaught, before any download.

An appliance never asks that question. The image runs `linux-amd64` binaries
inside WSL2, so it consumes the PostgreSQL bundle and the engine binary exactly
as CI publishes them today, unmodified. No Windows PostgreSQL build, no Windows
native-image, no GraalVM-for-Windows, no eighth edit site in the release
workflow, and no touch to the ABI-floor `case` statements that hard-fail an
unknown architecture. The build cost that dominated Options B and C is simply
not incurred.

Two further facts make the substrate portable in the right direction. The
bundled PostgreSQL cluster is already TCP-only — `unix_socket_directories = ''`
(`src/nexus/db/pg_provision.py:903-914`) — so nothing depends on Unix domain
sockets. And `refuse_root()` already tolerates Windows by treating an absent
`os.geteuid` as "not root" (`:166-177`).

And the build is cheap for the same reason the artifacts are: a WSL2 root
filesystem is what `docker export` produces, so the image is a Dockerfile away
from assets CI already publishes, and most of it is testable on a Linux runner
(Phase 4).

### What the appliance is

A published WSL2 root-filesystem image, consumed by `wsl --import`, containing:

- The `linux-amd64` PostgreSQL 17 + pgvector bundle — the *binaries*. Not a
  provisioned cluster: a PostgreSQL data directory is coupled to binary
  version, locale and collation, so baking one post-`initdb` is a portability
  trap this design does not need to take. The cluster is created on first boot,
  onto the data volume below.
- The `linux-amd64` `nexus-service` engine binary.
- Both ONNX models — bge-768 (~416 MB) and the ms-marco cross-encoder
  (~91 MB) — pre-fetched, since these are the two steps of `nx init` that cost
  the most wall-clock and the most bandwidth.
- A Python interpreter and the conexus wheel. The service *supervisor* is
  Python (`src/nexus/daemon/storage_service_daemon.py`), so the image needs the
  client regardless of whether a user ever runs `nx` inside it.
- An unprivileged `nexus` user with lingering already enabled, and
  `/etc/wsl.conf` carrying `[boot] systemd=true` and that user as the default.
- A systemd unit for the service, so persistence is a property of the image
  rather than an instruction in a document.

That collapses steps 2 through 7 of the Problem Statement — the unprivileged
user, `uv`, the interpreter-pinned install, `nx init`'s downloads, lingering,
and the bind flag — into one download and one import.

### Code and data are separate, and both locations are already configurable

Sam, 2026-09-21: "can't we mount the data volume or something so we can update
the service without toasting all the data (or worse, back/up)". Yes, and it
needs no code change — the two directories involved are *already* environment
overrides:

- `NEXUS_CONFIG_DIR` (`src/nexus/config.py:619`, tier 1 of
  `nexus_config_dir()`) relocates the durable estate. Everything derives from
  it: the PostgreSQL cluster (`pg_provision.py:2125`, `config_dir /
  "postgres"`), the engine binary
  (`binary_install.py:359`, `config_dir / "service" / ...`), credentials, logs
  and leases.
- `NX_ONNX_MODEL_DIR` (`src/nexus/db/onnx_model_root.py:37,53`) relocates the
  ONNX models, which otherwise sit under `HOME/.cache/nexus/onnx_models`.

So the appliance splits cleanly into an immutable half that ships in the image
and a persistent half on a mounted volume:

| | lives in | contents |
|---|---|---|
| **Code** | the image, replaced on update | OS, Python, the conexus wheel, PostgreSQL binaries, the systemd unit, the `nexus` user with lingering, and — if the size answer goes that way — the ONNX models |
| **Data** | a mounted volume, never replaced | `NEXUS_CONFIG_DIR`: the PostgreSQL cluster, the engine binary, credentials, logs |

Three things fall out of that split, and the third is the one that matters
most.

First, updating the service stops being destructive by construction: re-import
a new image, re-attach the same volume, and the cluster is untouched. That is
the property Sam asked for, and it is also what makes a backup story optional
rather than urgent — though not unnecessary.

Second, the ONNX models become a genuine choice rather than a build constraint
(decision surface item 2). In the image they are ~500 MB of immutable content
that a re-import refreshes; on the volume the image is smaller and first boot
is slower. Either is one environment variable.

Third — **this resolves a contradiction an earlier draft of this record
carried.** That draft said the client inside the appliance updates in place
while "the engine and PostgreSQL change only when a new image is imported."
That was wrong: a local-mode box converges its engine from
`PINNED_SERVICE_TAG`, which derives from `REQUIRED_ENGINE_VERSION`, so an
in-place client update *does* move the engine by downloading a new binary. With
the split above the behaviour is coherent instead of contradictory: the engine
binary lives on the *data* volume, so a converged engine persists across
re-imports and the image's engine is only ever the initial seed. A re-import
cannot silently downgrade a converged engine, and the one-engine-identity-per-
release contract holds inside the appliance exactly as it does on any other
local-mode box.

### Publishing it, and downloading it during install

Sam, 2026-09-21: "can't we publish the appliance and download it in the install
process?" Yes, and the appliance inherits machinery that already exists rather
than needing its own. The PostgreSQL bundle and the engine binary are already
published as release assets and already acquired with verification — a sha256
check plus a Sigstore protobuf bundle, with no `cosign` binary required, and an
extract-time `bundle/.build_prefix` marker check
(`src/nexus/db/pg_bundle.py`, `src/nexus/daemon/binary_install.py`). An image
is one more asset on that path.

So "download during install" is not a new subsystem: it is the same
verified-asset acquisition the client already performs twice, pointed at a
third artifact. What genuinely is new is the *size* — decision surface item 1 —
and whether a plain signed tarball is an acceptable distribution channel or
whether the install wants winget or the Microsoft Store.

### The release lifecycle this rides

The appliance's contents are, almost exactly, the engine release's published
assets plus the two models. That is not a coincidence to exploit later; it is
the argument for where the appliance belongs: **cut on the engine cadence, as
an engine-release artifact.** The engine release already publishes 21 assets
across three architectures with a `promote-release` job that holds the release
as a draft until every one is present
(`scripts/promote_engine_release.sh:13-19`), which is exactly the shape an
image needs.

This also answers the update question Sam's constraint opens. He allowed a CLI
for updating but not for installing, and with the code/data split above the
model is: the *image* is replaced on the engine cadence, rarely and
deliberately; the *client* updates in place, which is an `nx` operation and
therefore permitted; and the *data* is never replaced by either, because it is
not in the image. Engine convergence rides the client update and persists on
the data volume, so the two cadences do not fight.

### The decision surface that remains

Ruling on the appliance does not settle these:

1. **Image size and distribution.** PostgreSQL plus both models plus the engine
   plus a Python runtime is comfortably over 1.5 GB uncompressed. Is that
   acceptable as a release asset on the engine cadence, and is a compressed
   tarball with a signature enough, or does it want a Store/winget channel?
2. **Whether the models belong in the image at all.** They are the bulk of it.
   Pre-fetched means a large image; fetched on first boot means a fast download
   and a slow first run, which is the tradeoff `nx init` makes today.
3. **The fixed port.** Which port, what happens on a collision, and whether a
   user running two appliances is a case worth supporting.
4. **Windows-on-ARM.** The appliance sidesteps GraalVM entirely, but a
   `linux-amd64` image on an ARM Windows host needs emulation. `linux-arm64`
   assets already exist, so a second image is cheap to assemble and expensive to
   gate. Out of scope for the first cut, but it should be named as out of scope
   rather than unconsidered.
5. ~~Which Windows host the release battery's new leg runs on.~~ **Settled
   (Sam, 2026-09-21): qwentescence. "qwentescence was born for this work."**
   It is already the host of record for every Windows measurement in this
   document and for v7.56.0's local-supervisor box class. What remains is
   operational rather than a design choice: it also serves a qwen backend on
   port 1235, so a reboot for Windows Update or a WSL restart takes that down,
   and the battery leg should say so where a release runner will read it.
6. **What happens to the WSL2 dependency itself.** The appliance assumes WSL2
   is present. On the one host measured, installing WSL2 needed an MSI, admin
   rights and a reboot because the inbox stub and winget both failed. Does the
   install flow attempt it, detect and instruct, or refuse?

### What proceeds regardless

`nexus-sa187` should not wait on this record. Ubuntu 26.04 ships CPython 3.14
and the torch pin has no `cp314` wheels, so `uv tool install conexus` fails
resolution on the current Linux LTS whether or not Windows is ever supported.
It is independently justified and it blocks fresh Linux installs today. It also
touches the appliance directly, since the image's own client install faces the
same resolver.

---

## Alternatives Considered


**Alternative 1 — cloud mode as the Windows default. CLOSED by research; recorded
because its closure is load-bearing for everything else.** No local substrate
at all, which would have been the lowest-friction option of the five. It fails
on access, not on technology: the service token is provisioned by an operator
out of band. `docs/managed-onboarding.md:12-14` states that `nx` "does not
self-serve signup or mint tokens"; RDR-166 named this its own Gap 1 and at
`:125-127` explicitly rejected building self-serve, citing "a conexus
self-serve API that may not exist". Both `nx service token issue` and
`nx tenant create` require an existing root bearer against a deployment you
already control. No signup route exists anywhere in `src/`,
`service/src/main`, `web/` or `docs/` — established by search, not inferred.

The client also cannot select a tenant even though the server is genuinely
multi-tenant: `_process_default_tenant()` returns the literal string
`"default"` (`src/nexus/db/http_vector_client.py:4561-4576`), every T2 store
hardcodes the same, and there is no tenant environment variable or config key.

A second, independent reason to be wary of this option even if self-serve
appeared: a cloud-only population has *zero local remediation*. The managed
handshake is the one surviving `>=` engine floor
(`src/nexus/engine_version.py:4-18`), it is enforced on the data path rather
than only in `nx doctor` — `get_http_vector_client()` probes once per process
and hard-fails, caching `INCOMPATIBLE` for the life of the process
(`http_vector_client.py:4740-4770`) — and its own error text tells the user it
"cannot be fixed locally". That couples every Windows user to the operator's
deploy cadence in a way local-mode users are not coupled.

**Alternative 2 — bundled client plus auto-provisioned WSL2.** Provision the WSL2
side programmatically at first run rather than shipping it pre-built. Every
step was performed from a script on 2026-09-21, so it is demonstrably
automatable. Not taken because the automation runs on the user's machine, which
is where the variance lives: the host measured had a stub `wsl.exe`, an
access-denied `winget`, a stale `uv` trampoline, and an `npm` that resolved
through `/mnt/c` to a Windows binary. It also inherits the whole of Gap 5 — the
`.mcpb` carries no runtime and no client today — and its machinery is testable
only against a real Windows host, which is exactly what CI does not have.

**Alternative 3 — a native Windows service.** A Windows PostgreSQL bundle plus a
Windows engine. Retained as the fallback if the appliance shape fails, and
cheaper than it first appears because of one finding: the engine has a
**sanctioned JVM-JAR run path**, not merely a test shim. `NEXUS_SERVICE_JAR`
(`src/nexus/daemon/storage_service_daemon.py:405-430`, argv assembly at
`:1019-1057`) makes `nx init --service` skip native-binary acquisition
entirely (`src/nexus/commands/init.py:189-197`, pinned by
`tests/daemon/test_jar_launch_opt_in.py:143-155`), the launch flags are only
`-Duser.timezone=UTC`, and the client's wire path is artifact-indifferent. So a
Windows service could ship a JRE and the JAR and sidestep GraalVM
native-image altogether. What remains genuinely hard is the PostgreSQL bundle,
and the Windows Python supervisor, which is POSIX throughout. Also noted: the
`native-libs-windows` pom profile (`pom.xml:663-670`) exists but hardcodes
x64 under an architecture-free activation, so it would silently select x64
libraries on Windows-on-ARM, and CI has never exercised it.

**Alternative 4 — staged hybrid, cloud first.** Superseded by Alternative 1's closure.
Without self-serve cloud access there is no cloud arm to stage behind.

---

## Trade-offs

### What the appliance buys

A first install that is one download plus one import rather than eleven manual
steps, most of which need a terminal. Persistence, lingering, the unprivileged
user and the bind flag all become properties of an artifact rather than
instructions in a document, which is the difference between a thing that works
and a thing that works when followed correctly. And a build that reuses the
`linux-amd64` artifacts CI already publishes, so no new platform target enters
the release surface.

### What it costs

**Size.** PostgreSQL binaries, the engine, a Python runtime and the wheel, plus
both ONNX models if they ride inside, is comfortably over 1.5 GB. The measured
probe image — Ubuntu plus systemd and nothing else — was already 104 MB, and
that is the floor rather than an estimate of the real thing.

**A fourth artifact class.** The repo already runs two release lifecycles and
one plugin channel. An image is a fourth thing to build, sign, publish, version
and eventually deprecate, and RDR-197's sunset trigger exists because artifact
classes outlive their usefulness quietly.

**A dependency we do not control.** Everything here rests on WSL2 behaving as
measured on one host at one version (2.7.14.0). WSL ships on Microsoft's
cadence, and three behaviours we depend on — the localhost relay's treatment of
bind families, `--mount --vhd`, and `/etc/wsl.conf` honouring `systemd=true` —
are implementation details rather than contracts.

**Two residues that no design here removes**, both from the VM model: host
sleep freezes the guest clock, which exposes T1 lease and T2 TTL arithmetic;
and VM teardown is an unclean PostgreSQL shutdown, so WAL recovery becomes the
normal startup path rather than the exceptional one.

### What we give up by not taking the alternatives

Rejecting a native Windows service keeps WSL2 as a hard prerequisite, and on
the one host measured, installing WSL2 itself needed an MSI, administrative
rights and a reboot. That cost does not disappear; it sits outside this
record's boundary. Rejecting cloud keeps the substrate problem, but it also
keeps local remediation, which a cloud-only population would not have.

---

## Implementation Plan

**Phase 1 — prove the shape, and close the one unproven link.** Build an image
by hand from today's published `linux-amd64` assets, `wsl --import` it on a
Windows host, and drive a native Windows Claude Code session against it. The
gate is the check that already exists for exactly this purpose:
`tests/e2e/post-publish-dispatch-check.sh` must end
`POST-PUBLISH DISPATCH CHECK PASSED` from a real Agent dispatch on Windows.
This phase also closes the Critical Assumption above — that the MCP *tool*
surface works from a native Windows client — which is the one link in the chain
still unproven, and which failed to prove only because the distro shut down
underneath the attempt.

No image-building automation in this phase. If the shape is wrong, it is wrong
before any CI work is spent on it.

The two mechanism questions this phase existed to de-risk are already
answered — a `docker export` rootfs boots with systemd as pid 1, lingering
works in it, a separate VHDX mounts as ext4 inside the distro, and the data
survives an unregister-and-re-import cycle (see Critical Assumptions). What
Phase 1 still owes is the *elevation* question: whether `wsl --import` and
`wsl --mount` work for a non-administrator, since the experiment ran under an
Administrator token and cannot tell the difference. A negative answer does not
reshape the architecture but does reshape the Desktop install flow.

**Phase 2 — the endpoint contract (Gaps 1 and 2).** With an appliance we
control, discovery mostly dissolves: the systemd unit pins a *fixed* port
rather than accepting an ephemeral one, so the port stops being a moving
target. What remains is the token, and it needs a one-time handoff rather than
a per-restart one — the appliance mints a token on first boot, and the Windows
side reads it once over `\\wsl$\<distro>\` and stores it in its own config.
That requires a new tier in `src/nexus/db/service_endpoint.py`'s resolution
chain; the chain has no tier today that could read across the boundary.

Gap 2 is fixed properly here rather than worked around: change the loopback
default to an IPv4-family socket, which WSL2's relay forwards, keeping
loopback-only semantics instead of trading them away with
`NX_SERVICE_BIND=0.0.0.0`. That is a Java change and rides an engine tag, so it
sequences with the appliance's own cadence rather than against it.

**Phase 3 — make the Windows client behave (Gaps 3 and 4).** The hang is first
because it is what a new user meets first (`nexus-5dcky`): whatever hook blocks
on an unresolvable endpoint must bound its wait and continue. Then the hook
tier: port the five `python3` entries to `nx-hook` verbs (`nexus-t9klx`, and
Sam has already ruled "we need everything ported") — no new executables are
needed, since they become verbs on an `nx-hook` that already ships and already
works on Windows. Then the process primitives (`nexus-34f7r`): `safe_killpg`
not catching the `AttributeError` Windows actually raises, and SessionEnd
having no fast path where POSIX has a double-fork.

Two incidental defects found during research belong in this phase because they
are in the same code and the same class:
`src/nexus/daemon/storage_service_daemon.py:1673-1683` imports `fcntl`
*directly*, bypassing the cross-platform `src/nexus/_locking.py` shim whose own
docstring at `:29-31` records that this exact pattern "broke the whole CLI on
Windows" once already; and `:451`'s error string claims a Java floor of ">= 21"
where `pom.xml:13,276` sets 25.

**Phase 4 — build it with Docker, and gate the half that does not need
Windows (Gap 6).** Sam's observation, 2026-09-21: the appliance is testable
"in any build we can run docker". That is right, and it goes further than
testing — a WSL2 root filesystem is what `docker export` produces, so one
Dockerfile can be both the build mechanism and the CI test target. The image CI
exercises is then byte-identical to the image a user imports, rather than a
fixture standing in for it, which is the property this repo already insists on
elsewhere (`nexus/feedback_fixture_mvv_is_not_the_live_path`: a fixture MVV
proves the scripts, not the deployment).

This splits Gap 6's gate along an honest seam.

*The substrate half runs on `ubuntu-latest` for free, and is where the
regressions will be.* Boot the image, assert PostgreSQL provisions as the
unprivileged user without hitting the root refusal, assert the engine's
`release_version` equals the pinned identity, assert the schema walk applies
its expected changeset count, assert both ONNX models are present, and drive a
full storage round trip from a Linux client inside the container. Container-
based end-to-end work is established practice here rather than a new
capability — `tests/e2e/migration-rehearsal/run.sh`,
`rehearse_shakeout.sh`, `tests/e2e/rdr208-mvv/run.sh` and the plugin-cut
rehearsal all already do it, and the engine release already builds on
`ubuntu-latest`.

*The boundary half genuinely needs Windows, and is thin.* `wsl --import`
itself, systemd under WSL2's own init, the localhost relay and bind family
(Gap 2), logind teardown and lingering, the distro idle shutdown (Gap 3), and
the Windows client with its MCP servers and hooks (Gap 4). None of that is
reachable from Docker, and it should be named rather than quietly folded into
the substrate gate.

**The boundary half does not belong in CI at all, and this repo already has
the right shape for it.** Sam, 2026-09-21: "we do not need to use a github
windows runner, do we?" No — and asking for one would run against this
project's own CI Cost Discipline, which restricts premium runners to
"release/tag artifact builds" and says "never in routine push/PR CI". A Windows
runner firing on every push is precisely the pattern those directives exist to
prevent, and GitHub-hosted `windows-latest` is a virtual machine on which
nested virtualisation for WSL2 is generally unavailable anyway, so it likely
could not run the appliance even if we paid for it.

The host is settled: qwentescence (Sam, 2026-09-21 — "qwentescence was born
for this work"), which is already the host of record for every Windows
measurement here and for v7.56.0's local-supervisor box class.

The precedent is exact. The release skill's §11d already requires a real
Agent dispatch "in a live Claude Code session on each box class (managed cloud,
local supervisor)", followed by `post-publish-dispatch-check.sh` against that
session — a human-run, per-box-class gate on real hardware at release time
rather than a CI job. That gate was run on both existing box classes for
v7.56.0 on 2026-09-21, the second of them on qwentescence. **Windows becomes
the third box class**, and the boundary half of this gate becomes a leg of the
release battery rather than a workflow.

That keeps the split clean: the substrate half gates every push for free on
`ubuntu-latest`, where regressions come from; the boundary half gates a
release on a real Windows host, where the only things it can catch live. A
battery leg that silently skips when no Windows host is available is the
vacuous-gate doctrine and must carry a max-skip assert, exactly as the other
legs do.

This is the phase that turns "supported" from a claim into something we can
keep, and this seam is the concrete reason the appliance beat Alternative 2:
install-time provisioning is testable only against a real Windows host, while
an image is testable almost entirely without one.

**Phase 5 — the install surface (Gap 5).** Lift `manifest.json`'s
`platforms` gate, decide what the `.mcpb` actually ships given that today it
carries no runtime and depends on POSIX `os.execvp` semantics, and write the
install path a Desktop user can follow. This is last because it is the phase
whose right answer depends on everything above, and because until Phase 1 lands
there is nothing worth documenting.

---

## Test Plan

The gate splits along the seam Phase 4 establishes, and each half has a
different owner and cadence.

### Substrate half — `ubuntu-latest`, every push

Runs in a container built from the same Dockerfile that produces the shipped
image, so the artifact under test is the artifact that ships. Each assertion is
chosen because its failure mode is silent rather than loud:

1. The image boots and PostgreSQL provisions **as the unprivileged user**. The
   root refusal (`pg_provision_refused_root`) is the exact failure a
   container-built image invites, since containers default to root.
2. The engine's `release_version` equals `REQUIRED_ENGINE_VERSION`. A drifting
   image is the "cut, gated, never pinned" failure the engine-identity contract
   exists to prevent.
3. The schema walk applies its expected changeset count, and the three outcome
   counts partition `pending_at_start` exactly — the `nexus-jl08t` identity.
4. Both ONNX models resolve from wherever `NX_ONNX_MODEL_DIR` points. A missing
   model degrades search silently rather than failing, which is the
   `docs/desktop-deployment.md:262` mis-mode one layer down.
5. A full storage round trip from a client inside the container: write, read
   back, and confirm the row reached PostgreSQL rather than a fallback.
6. `loginctl enable-linger` succeeds and `systemctl is-system-running` reports
   no failed unit other than `kmod-static-nodes.service`. Pinning the *known*
   failure is what makes a new one visible.

### Boundary half — release battery, real Windows hardware

A leg on qwentescence, run at release time beside the existing §11d box-class
checks, carrying a max-skip assert so an absent host fails rather than passes.

1. `wsl --import` of the published image, and `wsl --mount --vhd` of the data
   volume.
2. Data survives an image replacement: unregister, re-import, reattach, prior
   contents intact. Proven by hand on 2026-09-21; it is the design's
   load-bearing property, so it belongs in a gate rather than in a memory.
3. The endpoint resolves from Windows without hand-copied environment
   variables, once Phase 2 lands.
4. `tests/e2e/post-publish-dispatch-check.sh` ends
   `POST-PUBLISH DISPATCH CHECK PASSED` from a real Agent dispatch in a native
   Windows Claude Code session — the check that already exists for this purpose
   and the one that would have caught the 7.41.0 projector-dead-on-cloud class.
5. A plain `claude -p` returns rather than hanging, with and without an
   endpoint configured — the Gap 4 regression.

### What neither half covers, stated so it is not mistaken for coverage

Windows-on-ARM. WSL2 version drift. Host sleep and Windows Update reboots. The
unelevated-install question, until it is answered. And the `.mcpb` surface,
which has no validation gate at all today (`nexus-uvn3t`).

---

## Validation

### Testing Strategy

1. **Scenario**: the substrate suite above, against a deliberately root-owned
   data directory.
   **Expected**: red on assertion 1. A container-built image that provisions
   PostgreSQL as root is the most likely regression in this design, and a suite
   that cannot produce that failure is not testing for it.
2. **Scenario**: an image built from an engine tag older than
   `REQUIRED_ENGINE_VERSION`.
   **Expected**: red on assertion 2, naming both versions.
3. **Scenario**: the boundary leg with no Windows host reachable.
   **Expected**: red, not skip. A leg that skip-passes when its dependency is
   absent is the `nexus-moht0` vacuous-gate shape, which is precisely this
   record's Gap 6 complaint.
4. **Scenario**: image replacement with the data volume carrying a populated
   catalog rather than a marker file.
   **Expected**: the catalog survives, and the engine on the volume — not the
   image's seed engine — is the one that runs.

### Size and performance expectations

No latency target is set: the appliance changes how the service is installed,
not how it performs, and the service inside it is the same `linux-amd64` binary
local Linux users already run.

Two figures are worth holding as budgets rather than targets. The probe image
was 104 MB for Ubuntu plus systemd alone. And first-run cost is a *choice*
rather than a constant: models inside the image mean a larger download and a
fast first query, models fetched on first boot mean the reverse, and the
decision surface keeps that open deliberately.

---

## Open Questions


1. ~~Is a cloud service token self-service?~~ **Answered: operator-issued.**
   Alternative 1 is closed.
2. ~~Can the `.mcpb` carry a Python runtime?~~ **Answered: it carries none
   today**, and is gated to darwin/linux. See Gap 5.
3. ~~Can the engine run from a plain JAR?~~ **Answered: yes, and it is a
   sanctioned production path** (`NEXUS_SERVICE_JAR`). Kept as Alternative 3's
   cost reducer, not needed by the appliance.
4. Does the MCP *tool* surface work from a native Windows client? Still
   unproven; the one attempt was invalidated by Gap 3. Phase 1 closes it.
5. Is `wsl --import` of a published appliance image acceptable to install
   without administrative rights, given WSL2 itself is already present?
6. What is the update path for an appliance image, and does Sam's "updating via
   CLI is fine" extend to re-importing a distro?

---

## Finalization Gate

### Not yet run

This record is `status: draft`. The finalization gate has not run, and no
contradiction check is asserted here.

That omission is deliberate. RDR-217's own gate section records that its
no-contradictions clause "has made it twice before and been falsified both
times, once by gate round 1 and once by the fix check on that round's diff,
each of which found a contradiction this section had already declared absent."
Asserting cleanliness before the gate runs is the failure mode, so this section
stays empty until `/conexus:rdr-gate` fills it.

### Corrections already made during drafting

Recorded because the count is the honest measure of how much this document
moved before anyone reviewed it, and because three of the five came from Sam's
questions rather than from the drafting.

1. **The desktop bundle claim.** An early draft asserted the `.mcpb` could ship
   a Python runtime and the client. It ships neither — a 1–2 KB launcher shim
   requiring `uv` and a host Python, gated to `darwin`/`linux`. The claim had
   already been stated aloud before research contradicted it.
2. **The problem framing.** The draft treated no-terminal install as a Windows
   gap. It is unmet on every platform, so this is a new capability rather than a
   port, which changes what "low friction" is measured against.
3. **The update story contradicted the engine contract.** The draft said the
   engine changes "only when a new image is imported", contradicting local-mode
   convergence from `PINNED_SERVICE_TAG`. Resolved by the code/data split: the
   engine binary lives on the data volume, so convergence persists across
   re-imports.
4. **A baked PostgreSQL cluster.** The draft had the bundle arrive "already
   provisioned into a cluster". A data directory is coupled to binary version,
   locale and collation; the design now bakes binaries and runs `initdb` on
   first boot onto the volume.
5. **Two mechanism claims asserted before testing.** The `docker export` rootfs
   and the mountable data volume were written as established and verified
   afterwards. They hold, but the order was wrong, and the experiment record
   now sits in Critical Assumptions so the next reader need not take the
   assertion on faith.

---

## References

### Records

- T2 `nexus/windows-support-research-of-record-2026-09-18` — the decision this
  record revisits, and its three research documents.
- T2 `nexus/windows-client-over-wsl2-service-trial-2026-09-21` — the measured
  trial that falsified its central conclusion.
- RDR-126 — deferred "Windows Claude Desktop … to a follow-up RDR". This is it.
- RDR-155 P4b — consolidated T3 onto pgvector, which is why the service side is
  the hard half.
- RDR-166 — decided managed access is operator-provisioned; rejected self-serve.
- RDR-197 — the independent plugin release channel, and the sunset-trigger
  precedent for a new artifact class.
- RDR-215 — removed the plugin's shell tier "so a native Windows client becomes
  viable". The enabling work.

### Beads

- `nexus-sa187` — the torch pin fails resolution on Ubuntu 26.04. Proceeds
  independently of this record.
- `nexus-5dcky` — the plugin hangs `claude -p` on native Windows with no
  endpoint. Gap 4.
- `nexus-t9klx` — five bare-`python3` hook entries to port to `nx-hook` verbs.
- `nexus-34f7r` — Windows async and process-primitive gaps.

### Code the design depends on

- `src/nexus/db/pg_bundle.py:54-72` — `current_platform_tag()`, the single
  choke point that refuses Windows and that an appliance never asks.
- `src/nexus/config.py:619` — `NEXUS_CONFIG_DIR`, which relocates the durable
  estate onto the data volume.
- `src/nexus/db/onnx_model_root.py:37,53` — `NX_ONNX_MODEL_DIR`.
- `src/nexus/daemon/binary_install.py:359` — the engine binary under
  `config_dir`, which is why convergence persists on the volume.
- `service/src/main/java/dev/nexus/service/NexusService.java:309` —
  `NX_SERVICE_BIND` and its own warning about binding past loopback.

---

## Revision History

| Date | Change |
|------|--------|
| 2026-09-21 | Created as `draft`. Problem statement, six gaps, five options surveyed, cloud closed by research, appliance chosen (Sam). |
| 2026-09-21 | Reframed: no-terminal install is unmet on every platform, so this is a new capability rather than a port. Added the desktop-bundle gap. |
| 2026-09-21 | Added the code/data split (Sam) on the existing `NEXUS_CONFIG_DIR` and `NX_ONNX_MODEL_DIR` overrides; resolved the update-story contradiction; corrected the baked-cluster claim. |
| 2026-09-21 | Gate split into a substrate half on `ubuntu-latest` and a boundary half as a release-battery leg on qwentescence (Sam), rather than a Windows CI runner. |
| 2026-09-21 | Verified both mechanism claims by experiment: a `docker export` rootfs boots with systemd as pid 1 and lingering works; a mounted VHDX survives unregister-and-re-import. |
| 2026-09-21 | Added the structural sections this file was missing against RDR-217's shape. |
