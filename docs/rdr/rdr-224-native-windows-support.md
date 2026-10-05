---
title: "Native Windows Support: Windows x64 Engine, PostgreSQL Bundle and Client"
id: RDR-224
type: Architecture
status: accepted
priority: high
author: Sam
reviewed-by: self
created: 2026-09-30
accepted_date: 2026-10-05
related_issues: [nexus-f9bgu, nexus-ijue9, nexus-lhr6a, nexus-vwfc0, nexus-efk2h, nexus-jevq5, nexus-zz2w7]
related_rdrs: [RDR-218, RDR-157, RDR-161, RDR-197]
---

# RDR-224: Native Windows Support: Windows x64 Engine, PostgreSQL Bundle and Client

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

Drafted 2026-09-30 against develop `8889bd50d`. No product code changed.

Amended 2026-10-05, after Phase 0 was decided and Phases 1 and 2 and most of
Phase 3 landed on develop. Where the text below says what was built, it states
the measured outcome and cites the T2 record; where it still says "inferred" or
"unmeasured", the item was not measured. The Revision History lists every change.

**Provenance.** On 2026-09-18 Sam ruled Windows support to be WSL2 only, and
RDR-218 (accepted 2026-09-22) designed a pre-built WSL2 appliance around that
ruling. WSL2 is the Windows Subsystem for Linux, version 2: a real Linux
kernel in a lightweight virtual machine on Windows. On 2026-09-29 Sam asked to
reconsider native Windows binaries, a spike on a Windows 11 workstation
(qwentescence) built and ran every native piece, and on 2026-09-30 Sam decided:
native Windows replaces the WSL2 direction. This record carries that decision
and the plan. It supersedes RDR-218's direction; see § Relationship to Prior
RDRs for what of RDR-218 survives.

## Problem Statement

nexus runs on Linux and macOS. On Windows, today, nothing runs natively: there
is no Windows build of the engine (the Java service, compiled to a single
native executable, that owns storage and embeddings), no Windows build of the
PostgreSQL bundle the engine stores its data in, and a Python client whose
process management assumes a POSIX operating system. RDR-218 answered this by
running the Linux artifacts inside WSL2. That answer puts a virtual-machine
boundary between Claude Code, which runs natively on Windows, and nexus, which
would run inside the VM. Three of RDR-218's six gaps exist only because of that
boundary (the client cannot discover the service across it, the engine's
loopback bind is invisible across it, and the VM stops when idle), and two
residues no design removes follow from the VM model (host sleep freezes the
guest clock; VM teardown is an unclean PostgreSQL shutdown). Native Windows
artifacts remove the boundary instead of bridging it.

The gaps below are what stands between today and a native Windows install
that works as well as the macOS one.

### Enumerated gaps to close

#### Gap 1: No Windows engine binary

The engine release matrix (`.github/workflows/engine-service-release.yml`) builds
linux-amd64, linux-arm64 and mac-arm64 only. Its binary-compatibility step fails
on any other architecture by design (`FAIL: unhandled arch ... no ABI floor
check defined`). There is no `windows-x64` leg, no Windows smoke, and no
Windows code-signing step.

#### Gap 2: No Windows PostgreSQL + pgvector bundle

The engine needs PostgreSQL 17 with the pgvector extension (vector similarity
search) and pg_trgm (trigram text matching). `scripts/build_pg_bundle.sh`
builds that bundle from source with autoconf and make, and makes it
relocatable (runnable from any directory) with `install_name_tool` on macOS and
`patchelf` on Linux. None of that runs on Windows. The client's platform
choke point refuses Windows outright (`src/nexus/db/pg_bundle.py`,
`current_platform_tag()`, which raises "Windows is a release N+1 follow-on").

#### Gap 3: The Python supervisor is POSIX-only

The client starts, finds, health-checks and stops the engine and PostgreSQL
through a supervisor in `src/nexus/daemon/`. It assumes POSIX throughout:

- it keys the service's identity on `os.getuid()`, which does not exist on
  Windows, so the supervisor fails before it starts anything; a grep finds 21
  code sites in 11 files (T2 `224-research-25`);
- it probes liveness with `os.kill(pid, 0)`, which on Windows sends a Ctrl+C
  event instead of probing;
- it stops processes with SIGTERM and process-group kills, which on Windows
  terminate immediately with no cleanup, the supervisor's own stop included
  (Gap 4);
- it identifies processes through `ps` and `/proc`;
- it registers autostart only through launchd and systemd, and refuses other
  platforms (`src/nexus/commands/daemon.py`);
- it names binaries without `.exe` (`pg_provision.py:193-198`) and injects
  `LD_LIBRARY_PATH`, which Windows ignores;
- it starts PostgreSQL with `pg_ctl` through `run_bounded` with piped output and
  a per-call Job Object (`pg_provision.py:634-641`, `bounded_subprocess.py:264-313`),
  and it takes the cluster's superuser name from `USER` or `LOGNAME`
  (`pg_provision.py:695`);
- it publishes its lease file with `os.replace` every second
  (`service_registry.py:523`, `DEFAULT_HEARTBEAT_INTERVAL` at `:72`), and
  upgrade replaces an installed executable and renames the PostgreSQL bundle
  directory (`binary_install.py:574-576`, `pg_bundle.py:235-244`); Windows
  refuses both. Measured at implementation: `os.replace` of the lease under
  concurrent readers raised `WinError 5` within 0.9 s, and `os.replace` over a
  running engine executable raised `WinError 5` (T2 `224-f9bgu19-conformance`,
  `224-f9bgu20-upgrade-stop`). The PostgreSQL bundle directory swap with a live
  `postgres.exe` was not run.

#### Gap 4: The engine has no graceful stop on Windows

On Linux and macOS the supervisor stops the engine with SIGTERM, and the
engine's shutdown hooks run. That matters: the ORT init gate
(`OrtInitGate`, nexus-o5xyx) exists because killing the engine during ONNX
Runtime initialisation crashed it, and it works by catching the signal. On
Windows, Python's `os.kill(pid, SIGTERM)` is `TerminateProcess`: no signal
reaches the engine, no shutdown hook runs, and PostgreSQL connections close
uncleanly.

The signal reaches the supervisor before it reaches the engine.
`nx daemon service stop` sends SIGTERM to the supervisor process
(`storage_service_daemon.py:3117`). The supervisor's handler sets a stop event
(`:2767-2771`), and the stop that follows marks the lease shutting down,
relinquishes it and stops the engine (`:2617`, `:2041-2074`). On Windows that
first SIGTERM is `TerminateProcess` too, so the supervisor never runs its stop
path. The engine's own signal handlers install at `Main.java:66`, before
schema migration (`:115`), ONNX Runtime initialisation (`:218`) and the HTTP
listener (`:355`), and the supervisor's readiness wait can last 20 minutes or
more (`storage_service_daemon.py:2096-2099`). An HTTP stop request has no
listener until `:355`, after both.

#### Gap 5: The plugin and desktop surface refuse or break on Windows

Three existing problems, each already filed, block a native Windows install:

- conexus hooks launched as `python3` do not fire on stock Windows, where no
  `python3` is on PATH (nexus-efk2h, recorded as an accepted trade-off on
  2026-09-24);
- `os.execv`/`os.execvp` on Windows spawns a child and exits the parent, which
  breaks `mcpb/src/bootstrap.py`'s promise that the MCP server's stdio lands on
  the server process;
- the desktop bundle's `mcpb/manifest.json` lists only darwin and linux in
  `compatibility.platforms`.

#### Gap 6: Everything we would ship for Windows is unsigned

Everything nexus would ship for Windows is unsigned today: the engine, the
whole PostgreSQL bundle, and four third-party DLLs the engine extracts at
runtime (§ Research Findings). Microsoft documents that Smart App Control (a
Windows 11 feature) checks every executable and DLL the operating system loads
and blocks unsigned code that has no cloud reputation, with no override.
We could not reproduce that: on two clean Windows 11 installs with Smart App
Control on, nothing unsigned was blocked, including a program downloaded by a
browser (§ Key Discoveries). The exposure is therefore documented but
unmeasured, not disproven: other builds and physical hardware were not tested. Enterprise application-control
policies (WDAC, AppLocker) need a trusted signature or an administrator
allowlist regardless. Signing is the only mitigation for all of these. Phase 0
Step 0.2 settled it on 2026-10-05: signing is deferred (nexus-dj01b), no route
is chosen and no identity validation has started, so the first Windows release
ships unsigned and Gap 6 is an accepted open risk (T2 `224-decisions`).

#### Gap 7: No gate on real Windows hardware

RDR-218's Gap 6 carries over: declaring Windows supported obliges a
check that runs on Windows. GitHub's hosted Windows runners can build and run a
native binary, which the WSL2 design could not rely on (nested virtualisation),
but a real-session check in a native Windows Claude Code session still needs a
real Windows box. RDR-218's version of that check ran
`tests/e2e/post-publish-dispatch-check.sh`, which no longer exists: it was
deleted with the RDR-184 ledger (`CHANGELOG.md:87`, nexus-0r1uz; T2
`224-research-26`). Phase 5 redefines the check against what exists.

## Relationship to Prior RDRs

Searched: the RDR index for "windows", "WSL", "native-image", "distribution",
"PG bundle", "relocatable".

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-218 (Windows via a WSL2 appliance) | Superseded | Its direction is replaced. Its rationale was Sam's 2026-09-18 WSL2-only ruling; that ruling is withdrawn (2026-09-30). Its Gaps 1-3 (discovery, bind, idle shutdown across the WSL boundary) dissolve; its Gap 4 (plugin hangs), Gap 5 (desktop bundle) and Gap 6 (a gate on real hardware) carry over here as Gaps 5 and 7. Disposition of its epic's beads is in § Implementation Plan, Phase 0. |
| RDR-157 (end-user distribution) | Origin | Deferred Windows to "release N+1" (bead nexus-f9bgu). Its Strategy B (build PostgreSQL and pgvector from source rather than repackaging a third-party build) is the proven default and this record keeps it. |
| RDR-161 (native-only local install) | Origin | Defined the native-binary install path (`nx daemon service install-binary`, cosign-verified assets) this record extends to Windows; also deferred Windows to nexus-f9bgu. |
| RDR-197 (plugin-only release channel) | Adjacent | Plugin-surface fixes for Gap 5 (hooks) can ship through the plugin channel without a client release. |

## Context

### Background

The 09-18 research of record (T3 `research-windows-executable-2026-09-18`,
parts 1 and 2) did not find native Windows impossible. It recommended WSL2
documentation as the cheap first step and a native spike as step 2: a GraalVM
native-image build on Windows, then PostgreSQL and pgvector with meson and
nmake. The WSL2-only ruling came first; the spike never ran. It ran on
2026-09-29 (§ Research Findings) and every native piece worked.

### Technical Environment

- Engine: Java, compiled with Oracle GraalVM 25.0.3 native-image (the CI pin),
  Maven, jOOQ, Liquibase, HikariCP, pgjdbc. Embeddings through ONNX Runtime
  1.20.0 and DJL HuggingFace tokenizers 0.30.0, both loaded through JNI (Java
  Native Interface) from native libraries embedded in the binary.
- PostgreSQL 17.5, pgvector 0.8.2, pg_trgm (version pins in the release
  workflow).
- Client: Python 3.12+, `uv`, the `conexus` wheel; plugin hooks run by Claude
  Code.
- Windows target: Windows 10/11 x64. Windows on ARM is out of scope: GraalVM
  native-image has no windows-aarch64 target and onnxruntime ships no
  Windows-ARM64 native library (09-18 research).
- Build host available: qwentescence (Windows 11 Pro, Ryzen AI MAX+ 395, 64 GB),
  now provisioned with Visual Studio Build Tools 2022 17.14 (MSVC x64 and the
  Windows 11 SDK 10.0.26100), Strawberry Perl, win_flex_bison, meson and ninja.
  It is also the release build host (Phase 0 Step 0.1): a self-hosted runner
  labelled `win-release`, for the Windows release legs only.

## Research Findings

### Investigation

A spike on qwentescence, 2026-09-29, against develop `7980de41f`, recorded on
bead nexus-f9bgu. Scripts are in `D:\spike` on that host. A read-only
portability audit of the repository the same day covered the engine, the PG
bundle, the Python client, the plugin, and the release workflow.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| GraalVM native-image 25.0.3 (Windows) | Spike | Builds the engine with MSVC; no source changes needed. |
| onnxruntime 1.20.0 jar | Yes (jar listing) | Ships `win-x64/onnxruntime.dll`, `onnxruntime4j_jni.dll`, and a 290 MB `onnxruntime.pdb` (debug symbols). |
| DJL tokenizers 0.30.0 jar | Yes (jar listing) | Ships `win-x86_64/cpu/tokenizers.dll` plus MinGW runtime DLLs (`libstdc++-6`, `libgcc_s_seh-1`, `libwinpthread-1`). |
| PostgreSQL 17.5 meson build | Spike | Builds with MSVC; installs under `include/postgresql` and `lib/postgresql` when the prefix lacks "postgres". |
| pgvector 0.8.2 `Makefile.win` | Yes + Spike | Assumes a flat layout (`$(PGROOT)\include\server`); works when given `pg_config`'s directories. |
| win_flex_bison (winget) | Spike | Its `Links` shim cannot find bison's data directory; parallel runs race on shared temp files. |

### Key Discoveries

- **Verified** — The engine compiles to `nexus-service.exe` with GraalVM
  native-image on Windows in about 80 seconds (peak RSS 12.5 GB), with the
  jOOQ sources generated on a Docker host, unchanged from the other platforms.
- **Verified** — With the nexus-lhr6a size fix (on develop, `32f6987b2`) the exe
  is 143 MB. Before the fix it was 803 MB, because every native library was
  embedded twice and the 290 MB `.pdb` was embedded too. Later `-Ob` builds
  measured 127 MB (Performance Expectations).
- **Verified** — The exe imports `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll` and
  the Universal CRT (`api-ms-win-crt-*`); everything else it imports ships with
  Windows (`dumpbin /dependents`).
- **Verified** — PostgreSQL 17.5 builds with meson and MSVC using the Linux
  bundle's lean options (no ICU, zlib, readline or OpenSSL), and pgvector 0.8.2
  builds as `vector.dll`. Three build-script requirements came out of it: put
  win_flex_bison's real package directory on PATH, generate the grammar and
  scanner files (20 targets) one at a time before the parallel build, and pass
  pgvector's `Makefile.win` the directories from `pg_config`.
- **Verified** — The bundle is relocatable: copied to a new directory with the
  build prefix removed, `initdb` ran, `CREATE EXTENSION vector` (0.8.2) and
  `pg_trgm` (1.6) worked, and an HNSW index query returned rows. Windows loads
  DLLs from the executable's own directory, so no equivalent of rpath patching
  was needed.
- **Verified** — The native exe boots against that relocated bundle, reports
  healthy in about 3 seconds, applies every changeset in the changelog (491 on
  2026-09-29; 508 effective and 512 raw, counting four inside XML comments, on
  2026-10-05, and the number keeps growing, so the tests read it from the
  changelog), and returns a 768-dimension bge embedding through the Windows ONNX
  Runtime and tokenizer libraries.
- **Verified** — `pg_ctl` must be run with its output redirected to a file
  (through `cmd`), not a pipe: it hands the pipe to the postgres process it
  starts, and a reader waiting for the pipe to close waits forever. The client
  started `pg_ctl` with piped output at the time of the spike
  (`pg_provision.py:634-641`). As built, the client starts it detached: a plain
  `Popen` with `CREATE_NEW_PROCESS_GROUP`, output to `pgdata/pg_ctl.out`, never
  `run_bounded` and no per-call Job Object (T2 `224-f9bgu18-windows-pg-start`).
- **Verified** — A backslash in a pom `<buildArg>` broke a resource exclude on
  the Windows build (`.*[.]pdb` excluded, `.*\.pdb` did not). The mechanism is
  not established; the pom now forbids backslashes in build args (nexus-vwfc0).
- **Documented** (source reading, audit) — The engine does not manage
  PostgreSQL itself (it connects over JDBC), uses no Unix sockets, no POSIX
  file permissions and no process spawning. Its only Windows gap is shutdown
  (Gap 4). The 09-29 `OrtInitGate` installed TERM, INT and HUP handlers; HUP
  throws on Windows and was caught, so startup was safe (observed in the spike's
  log). As built, the signal set differs per OS (Technical Design, Stop channel)
  and HUP is no longer requested on Windows.
- **Documented** (source reading, audit) — The client already has Windows
  groundwork: `_locking.py` falls back to `msvcrt`, `util/win_job.py` wraps
  Windows Job Objects (so child processes die with their parent),
  `util/process_group.py` and `bounded_subprocess.py` branch on Windows.
- **Documented** (09-18 research) — The conexus dependency closure has Windows
  wheels, bare and `[local]` (bead nexus-ijue9.18, closed).
- **Verified** (spike, 2026-09-30) — The VC++ runtime can ship app-local, but
  the set is four DLLs: `vcruntime140.dll`, `vcruntime140_1.dll`,
  `msvcp140.dll` and `msvcp140_1.dll`. With all four beside
  `nexus-service.exe`, the running engine loaded every one from its own
  directory (read from the live process's module list). With only the two
  `vcruntime` DLLs beside it, `MSVCP140.dll` and `MSVCP140_1.dll` still loaded
  from `System32`: the exe does not import them, the native libraries embedded
  in it (ONNX Runtime) do, so a dependency check of the exe alone misses them.
  `ucrtbase.dll` and `msvcp_win.dll` load from `System32` and ship with
  Windows 10 and later. T2 `224-research-4`.
- **Verified** (spike, 2026-09-30, clean VM) — On a fresh Windows 11 25H2
  install with no VC++ redistributable (Hyper-V VM `sac-test` on
  qwentescence), `nexus-service.exe` with the four app-local DLLs starts and
  reaches its own configuration check. The PG bundle as built does NOT run
  there: `postgres.exe` and `initdb.exe` exit `0xC0000135` (DLL not found).
  With the same four DLLs copied into the bundle's `bin` they run. The bundle
  worked on qwentescence only because Visual Studio is installed there. T2
  `224-research-13`.
- **Verified** (spike, 2026-09-30) — A file downloaded with Python's `urllib`,
  which is how `nx daemon service install-binary` downloads (`binary_install.py`), carries no
  Mark of the Web (the `Zone.Identifier` alternate data stream a browser
  writes). A 25 MB installer fetched from python.org had only its default data
  stream; a positive control with the stream written by hand was detected by
  the same check. T2 `224-research-5`.
- **Documented** — SmartScreen's reputation check applies to files that carry
  the Mark of the Web; a file without it is not screened by SmartScreen
  (Defender antivirus still scans it). T2 `224-research-6`.
- **Documented** — Smart App Control is a different mechanism and the no-Mark
  exemption does not reach it. It checks every executable and every DLL the
  loader loads, downloaded or not, and blocks unsigned code without cloud
  reputation, with no user override. A signature chaining to Microsoft's
  trusted root program, from an ordinary (OV) certificate, satisfies it. It
  starts in evaluation mode, Windows switches it to enforcement on devices it
  judges good candidates, and it switches itself off on detected developer
  and managed devices, so ordinary consumers are the exposed population; since
  April 2026 it can be turned back on after being turned off. T2
  `224-research-10`.
- **Verified** (spike) — What is signed today: nothing we build. Unsigned:
  `nexus-service.exe`; the PG bundle's `postgres.exe`, `initdb.exe`,
  `pg_ctl.exe`, `vector.dll`, `pg_trgm.dll`; the DJL tokenizer DLLs extracted
  at runtime to `%USERPROFILE%\.djl.ai\tokenizers\...` (`tokenizers.dll` and
  the MinGW `libstdc++-6.dll`, `libgcc_s_seh-1.dll`, `libwinpthread-1.dll`);
  the uv-managed `python.exe`. Signed by Microsoft: the ONNX Runtime DLLs
  extracted at runtime and the VC++ runtime DLLs. T2 `224-research-7`.
- **Verified** (spike) — Smart App Control is off on qwentescence, so no spike
  on that host says anything about how it treats our binaries. T2
  `224-research-8`.
- **Verified** (spike, 2026-09-30) — Smart App Control did not block any
  unsigned binary in our tests. On a clean Windows 11 Pro install (build
  26300, Microsoft's consumer ISO, Hyper-V VM `sac-pro`) Sam turned it On in
  Windows Security; code integrity then logged the enforcing policy
  (`VerifiedAndReputableDesktop`) as activated and user-mode enforcement went
  from audit to enforced. After that, launched by double-click from the
  desktop, all of these ran: a byte-tampered `whoami.exe` carrying a Mark of
  the Web, the same file without the mark, the unsigned `psql.exe`, and the
  unsigned `nexus-service.exe`. With the guest's network disabled, so that no
  cloud verdict was possible, fresh-hash unsigned copies also ran, marked and
  unmarked. No code-integrity block or reputation events were logged. An
  earlier run on the Enterprise Evaluation edition (build 26200, Smart App
  Control forced On through the registry) gave the same outcome. This
  contradicts the documented behaviour for these configurations. A real
  browser download was then tested in the same VM: a new unsigned program
  (104 KB, never seen before) downloaded by Edge, which wrote a genuine Mark of
  the Web on it (`ZoneId=3`), ran when double-clicked, again with no
  code-integrity events. Not tested: physical hardware, other builds, a
  download from a public internet host over https, and signed binaries. A
  Smart App Control run in these configurations therefore cannot tell a signed
  install from an unsigned one. T2 `224-research-14`, `224-research-15`,
  `224-research-16`.
- **Verified** (spike) — ONNX Runtime's Java loader extracts its DLLs into a new
  `%TEMP%\onnxruntime-java<random>` directory on every engine start and never
  removes it (5 left behind after 5 starts). T2 `224-research-9`.
- **Documented** — Signing options in 2026: Microsoft Artifact Signing
  (formerly Trusted Signing; organizations in the US, Canada, EU and UK,
  individuals in the US and Canada; 9.99 USD a month; a GitHub Action;
  identity validation takes days to weeks and cannot be hurried); an OV
  certificate (about 129 USD a year, with the private key in a hardware or
  cloud HSM, which CA rules have required since June 2023, so no key file in
  CI secrets); SignPath Foundation (free for OSI-licensed open source built in
  CI). An EV certificate no longer buys instant SmartScreen reputation. T2
  `224-research-11`.
- **Verified** (spike, 2026-10-05, qwentescence, Windows 11 build 26200) — The
  engine stops on `CTRL_BREAK`, once `BREAK` is a handled signal. A native-image
  child started with `CREATE_NEW_PROCESS_GROUP` runs its shutdown hooks within
  0.01 s of `CTRL_BREAK` and exits 149, whether its main thread sleeps, reads
  stdin, accepts on a socket or spins. Without a `BREAK` handler native-image
  ignores `CTRL_BREAK` and does not exit. The 09-29 engine, whose
  `EXIT_SIGNALS` are `TERM INT HUP`, ignored it during the Liquibase migration
  and while serving. A copy patched to `TERM INT HUP BREAK` exited in 0.08 s
  while serving, logged `shutdown_signal`, `service_stopped` and
  `own_backends_terminated count=10`, and booted cleanly again. A break after
  `schema_migration_pending` and before the lock exited in 0.01 s, with 0
  changelog rows, and the next boot applied every changeset (491 then). T2
  `224-research-17`, `224-research-18`.
- **Verified** (same spike) — A stop during a Liquibase changeset, here about
  changeset 202, exits at once and leaves `databasechangeloglock` with
  `locked=t`. The next boot logged `Waiting for changelog lock` for about 300 s
  and then failed. SIGTERM on POSIX has the same effect. T2 `224-research-19`.
- **Verified** (same spike) — The sender must share the target's console.
  `GenerateConsoleCtrlEvent` from a process with no console fails with error 6,
  and from a process on a different console it can return TRUE and deliver
  nothing. `FreeConsole`, then `AttachConsole` on the target pid, then
  `GenerateConsoleCtrlEvent(CTRL_BREAK)` to that pid delivers it from a
  same-session process, for a target started with `CREATE_NEW_PROCESS_GROUP`,
  with `CREATE_NO_WINDOW` added, or on a new hidden console. `AttachConsole` to a
  target in another Windows session fails with access denied. A
  `DETACHED_PROCESS` target has no console, `AttachConsole` fails with error 6
  and `CTRL_BREAK` cannot reach it. T2 `224-research-20`.
- **Verified** (same spike, with stand-ins for the supervisor and the engine) —
  A Task Scheduler `/IT` task in the logon session started `pythonw`, which
  started a Python supervisor stand-in with `CREATE_NEW_PROCESS_GROUP` plus
  `CREATE_NO_WINDOW`, which started a native-image engine stand-in with
  `CREATE_NEW_PROCESS_GROUP`. A second task in that session attached to the
  supervisor's console and sent `CTRL_BREAK`. The supervisor's `SIGBREAK` handler
  forwarded it to the engine, both ran their exit paths, and both processes were
  gone. A console CLI can attach, send and `AttachConsole(ATTACH_PARENT_PROCESS)`
  back with stdout still working, checked under the ssh conpty only. T2
  `224-research-21`.
- **Verified** (same spike) — A CPython 3.13 `SIGBREAK` handler does not run
  while the main thread blocks in `time.sleep(120)`, `sys.stdin.read` or
  `threading.Event.wait`, not within 10 s. It ran within the tick of a
  `time.sleep(1.0)` loop, 0.5 s measured, and within 0.02 s under
  `asyncio.run`. Processes spawned without `CREATE_NEW_PROCESS_GROUP` share the
  parent's group and receive the same `CTRL_BREAK`. T2 `224-research-22`.
- **Documented** — `CREATE_NEW_PROCESS_GROUP` disables Ctrl+C for the new
  group, and `GenerateConsoleCtrlEvent` requires the caller to share the
  console of the target process group (Microsoft Learn, as summarised by the
  spike, not re-fetched). T2 `224-research-23`.
- **Assumed** — Enterprise policies (WDAC, AppLocker) need a trusted signature
  or an administrator allowlist; Defender's machine-learning checks may flag
  an unsigned native binary that extracts DLLs. Neither is tested. T2
  `224-research-12`.

Measured at implementation, 2026-10-05, on qwentescence (native Windows 11, hand
runs, release-shaped `-Ob` native exe; nothing here ran on a workflow runner):

- **Verified** — The engine stops on `CTRL_BREAK` in every phase (T2
  `224-p1.1-stop-probe`). Before the changelog lock: exit 149 in 0.011 s, 0
  changelog rows, the next boot applied all 508. During a changeset (255 of
  508): exit 149 in 0.013 s, 254 rows, `databasechangeloglock` left locked
  (`locked=t`), and the next boot was still waiting on the lock at the probe's
  45 s cap (nexus-8sph2 tracks it). Serving: exit 149 in 0.076 s, logging
  `shutdown_signal`, `service_stopped` and `own_backends_terminated count=10`;
  the next boot applied no new changeset and was ready in 0.97 s. PostgreSQL
  logged no crash recovery in any phase. The engine never logged
  `ort_init_signal_gate_unavailable`.
- **Verified** — The real client, supervisor and engine stop under the topology
  of § Technical Design (T2 `224-f9bgu17-round1`, `224-f9bgu17round2`; started
  under a per-user logon-style task in session 1). A plain `nx daemon service
  stop` returned in 1.52 s with the supervisor, the engine and the venv launcher
  gone and the lease removed, PostgreSQL still running, and no `unclean_stop`;
  `--with-pg` took 2.88 s and 1.77 s, logged a fast shutdown, and the next start
  logged no crash recovery. From another Windows session the stop was REFUSED
  (exit 1, nothing signalled or killed, the service still up). With a
  stand-in engine that ignores the break, the grace expired and the Job Object
  killed it, both gone in 2.81 s, `unclean_stop` logged.
- **Verified** — An elevated session's `initdb` runs `postgres` with a
  restricted token (Administrators deny-only). `Path.mkdir(mode=0o700)` and
  Python 3.12+'s `tarfile` directory members create an owner-only ACL (SYSTEM,
  Administrators and the owner), so a bundle extracted under such a directory,
  or a data directory created in one, makes `initdb` die `0xC0000135` with no
  message. It needs an explicit inheritable ACE for the user's SID on both the
  extract root and the data directory; either alone fails. A control run with
  the grant disabled reproduces the failure (T2 `224-f9bgu18-windows-pg-start`,
  `224-p1.1-stop-probe`).
- **Verified** — Three lifecycle properties fail on Windows without a fix (T2
  `224-f9bgu19-conformance`). `os.kill` of an exited pid raises `OSError`
  (`WinError 87`), not `ProcessLookupError`. A kill returns before the process
  leaves the process table. A lease replaced under concurrent readers raises
  `WinError 5` in the writer even when the readers open with delete sharing
  (8689 writer errors in 3000 replaces), so the writer must retry. The fixes
  are in the shared primitive: a bounded retry (2 s, 2 to 10 ms backoff,
  Windows only) on the lease read, replace and unlink, a hard kill that
  tolerates a gone pid, and a wait for exit after each hard kill. The
  conformance file on Windows: 74 passed, 2 skipped, 2 xfailed, identical in
  three runs. The daemon suites, the process-group tests and the session-sweep
  tests: 991 passed, 0 failed, 33 skipped; with the plugin lease-read and
  owner-only-file tests added: 1049 passed, 0 failed, 38 skipped. Each skip is
  reasoned in the record. Before the fixes the same suites failed 150 tests,
  had one collection error and killed pytest twice (T2
  `224-f9bgu44-windows-tests`). Integration-marked tests, which need the engine
  and a JVM, did not run there.
- **Verified** — Upgrade (T2 `224-f9bgu20-upgrade-stop`): `os.replace` over a
  running engine exe raises `WinError 5`. With the old exe hard-linked aside,
  the replace over a running exe succeeds, but the aside copy cannot be deleted
  until the process exits, so stop-first stays the contract.
- **Verified** — Task Scheduler (T2 `224-f9bgu23-autostart`). The task's
  restart-on-failure fires only when the task's program fails to launch; exit
  codes 1, 3, 255, `0x80070005`, `0xC0000005` and `0xFFFFFFFF` produced no
  restart in 160 to 200 s. The task's process job is
  `KILL_ON_JOB_CLOSE|SILENT_BREAKAWAY_OK`, and children survive the launcher's
  exit and `schtasks /End`. A non-administrator token registers a logon task for
  its own SID with `InteractiveToken`, `LeastPrivilege` and no password. The
  SID works as the user id, where `DOMAIN\user` failed in an ssh session. A task
  XML file that declares UTF-8 is refused; pure ASCII with no encoding attribute
  is accepted. `Hidden` hides the task in the Task Scheduler interface only, not
  a window.

### Critical Assumptions

- [x] The engine builds as a Windows native executable with no source changes —
  **Status**: Verified — **Method**: Spike
- [x] ONNX Runtime and the DJL tokenizer load and run in the Windows native
  image — **Status**: Verified — **Method**: Spike (bge embed, 768 dims)
- [x] PostgreSQL 17.5 + pgvector 0.8.2 build with MSVC into a relocatable
  bundle that the engine migrates cleanly — **Status**: Verified —
  **Method**: Spike
- [x] The VC++ runtime can ship app-local beside `nexus-service.exe`, so users
  need no separate redistributable install — **Status**: Verified, revised —
  **Method**: Spike. The app-local set is four DLLs, not two (see Key
  Discoveries): two are loaded by the embedded native libraries, not by the
  exe. Confirmed on a clean Windows 11 VM with no redistributable installed.
  The PG bundle needs the same four DLLs in its `bin` (it fails with DLL not
  found without them). The packaged engine archive's smoke on qwentescence
  loaded all four from the engine directory (T2 `224-f9bgu9-windows-engine-leg`);
  that host has the runtime system-wide, so the packaged artifacts have not met
  a machine without it (Phase 5; § Test Plan). Microsoft's redistribution terms
  were read in full and signed off by Sam on 2026-10-05 (Phase 0 Step 0.6; T2
  `224-vcruntime-terms`, `224-vcruntime-terms-signoff`): app-local deployment is
  permitted beside the exe and in the PG bundle's `bin`, under eight conditions
  (§ New Dependencies).
- [x] A binary fetched by `nx daemon service install-binary` carries no Mark of the Web, so
  SmartScreen does not screen it — **Status**: Verified — **Method**: Spike
  (no `Zone.Identifier` on a `urllib` download; positive control detected)
  plus Documented. This does not settle whether signing is needed: Smart App
  Control ignores the Mark of the Web (Gap 6), and whether signing gates the
  first release is Phase 0 Step 0.2. An earlier revision of this record drew
  the wrong conclusion; see Revision History.
- [ ] Signing is deferred (Phase 0 Step 0.2, nexus-dj01b), so this stays open.
  If signing is adopted: signing every PE file we ship (our
  exe, the PG bundle, and the
  third-party DLLs, signed before they are embedded) makes nexus run on a
  Windows 11 machine with Smart App Control enforcing, and the signed
  GraalVM exe still runs — **Status**: Unverified — **Method**: Spike, which
  needs a real certificate from a trusted root (a self-signed certificate
  proves only that signing does not break the exe) and a test machine with
  Smart App Control on. This cannot be verified as stated, because unsigned
  binaries were not blocked in the first place on either clean install we
  tried (Key Discoveries): there is no failing case for signing to fix. What
  remains checkable once a certificate exists is that signing does not break
  the GraalVM exe or the embedded-library extraction. A Smart App Control run
  is not evidence that signing works, because unsigned binaries pass it too
  (Test Plan).
- [x] `CTRL_BREAK` stops a native-image engine on Windows once `BREAK` is a
  handled signal (Windows only, Technical Design), while serving and in the
  migration window before the changelog lock — **Status**: Verified —
  **Method**: Spike (T2 `224-research-17`, `-18`), then the release-shaped
  `-Ob` exe in all four phases (T2 `224-p1.1-stop-probe`). A stop during a
  changeset exits but leaves the changelog lock held (Failure Modes). The probe
  driver is in the repository (`scripts/engine_windows_stop_probe.py`), and the
  release-leg smoke asserts the serving-phase stop (Phase 1 Step 2).
- [x] A same-session CLI can stop a supervisor, and through it an engine, with
  `CTRL_BREAK` under the topology in § Technical Design — **Status**: Verified
  with the real supervisor and engine — **Method**: Spike with stand-ins (T2
  `224-research-20`, `-21`, `-22`), then the real client, supervisor and engine
  (T2 `224-f9bgu17-round1`, `224-f9bgu17round2`). Not covered: a stop from a
  visible-console `cmd` window (the stop CLI ran under a hidden console, which is
  also the logon task's shape), a real engine that ignores the break (stand-in
  only), and a logon trigger that actually fires.
- [x] A stop during ONNX Runtime initialisation, sent as `CTRL_BREAK`, exits
  without the o5xyx crash — **Status**: Verified on both branches of the gate,
  with caveats — **Method**: probes on the native Windows exe. Deferral branch:
  5 of 8 breaks landed inside initialisation, each was deferred 135 to 444 ms and
  exited 149 (T2 `224-p1.1-stop-probe`, row c). Timeout branch, the one o5xyx
  protects (`System.exit` while native initialisation is in flight): with the
  wait bound forced to 50 ms and to 1 ms through `NX_ORT_INIT_SHUTDOWN_WAIT_MS`,
  30 of 32 breaks landed in flight and 29 exited through
  `ort_init_shutdown_wait_timeout` with initialisation live, all with exit 149
  and no `hs_err`, `svm_err`, dump or Windows Error Reporting event (T2
  `224-p1.1-ort-init-timeout-probe`). Caveats: real initialisation longer than
  the 3 s bound is unmeasured (forcing the bound short overlaps exit with
  initialisation for 30 to 400 ms, not 3 s); one start on a fresh model path
  took 0.61 s, a proxy, not a cold disk read; `shutdown_signal` is not logged on
  the timeout rows, because the process was gone before the hook wrote it; the
  `-O2` release exe and the 5 s supervisor grace against the 3 s bound are
  unmeasured.
- [ ] Claude Code on native Windows runs the conexus plugin's hooks once they
  are launched by something present on stock Windows (`uv run` or the `py`
  launcher, per nexus-efk2h's proposed fix) — **Status**: Unverified —
  **Method**: Spike in a native Windows Claude Code session.
- [ ] The packaged engine archive and PG bundle run on a clean Windows machine
  with no VC++ redistributable — **Status**: Unverified — **Method**: a
  hand-run clean guest (Phase 0 Step 0.5). The clean-VM spike (above) used loose
  files, not the packaged archives, and the smokes ran on a host that has the
  runtime system-wide; the module-resolution check proves the shipped copies
  were used, not that the system copies are absent.
- [ ] The Windows release-workflow jobs (cosign, upload, cache restore, the
  cross-job bundle artifact, promotion against a 27-asset release) run on
  `win-release` — **Status**: Unverified — **Method**: a `workflow_dispatch` run
  of the release workflow before the first cut (Phase 0 Step 0.5). No workflow
  step has run on that runner; the scripts they call ran by hand.
- [ ] A host shutdown, logoff or sleep with PostgreSQL running recovers cleanly
  on the next start — **Status**: Unverified — **Method**: a measurement on a
  real host; none has been run (T2 `224-research-32`).

## Proposed Solution

### Approach

Ship three native Windows x64 artifacts through the existing release
machinery, and port the client's process management so they run under it:

1. **Engine**: a `windows-x64` job in `engine-service-release.yml` (a job of its
   own, not a matrix entry; Phase 1 Step 2), built with GraalVM on Windows,
   smoked against the Windows PG bundle, and published and cosign-signed as one
   archive. Cosign here is release provenance, a different thing from
   Authenticode signing, which is deferred.
2. **PostgreSQL bundle**: a Windows build path (meson + MSVC for PostgreSQL,
   `Makefile.win` for pgvector) producing `nexus-pg-windows-x64`, cached on
   exact inputs like the other bundles.
3. **Client**: `nx daemon service install-binary` and the supervisor learn
   Windows: platform tag, `.exe` names, identity, liveness, stop, and autostart.

Plus the engine change that lets it stop on Windows (`BREAK` added to its stop
signals on Windows only) and the plugin fixes RDR-218 already identified.

### Technical Design

**Stop channel (Gap 4).** The stop signal on Windows is `CTRL_BREAK`, sent to a
process group. It reaches two processes, the supervisor first and the engine
through it, and it exists from process start, which an HTTP request does not
(§ Alternatives Considered). POSIX keeps SIGTERM.

- *Engine.* As built, `OrtInitGate.exitSignals(osName)` returns a per-OS set:
  `TERM`, `INT`, `HUP` on POSIX, unchanged, and `TERM`, `INT`, `BREAK` on
  Windows (a Windows `os.name`, case-insensitive). The handlers install at
  `Main.java:66`, before migration, ONNX Runtime initialisation and the
  listener, so `OrtInitGate` and the shutdown hooks run as they do for SIGTERM.
  Measured: with `BREAK` handled the engine ran its shutdown path in 0.08 s while
  serving; without it native-image ignores `CTRL_BREAK` (T2 `224-research-17`,
  `-18`, `224-p1.1-stop-probe`). `Signal.handle` per runtime (T2
  `224-p1.1-break-signal-measurement`, `224-p1.1-stop-probe`): on Windows,
  native-image accepts `TERM`, `INT` and `BREAK` and rejects `HUP` as unknown,
  while a HotSpot JVM rejects `BREAK` (`Signal already used by VM or OS`), so a
  Windows JVM engine cannot be stopped this way; HotSpot JVMs on macOS arm64,
  Linux amd64 and Linux aarch64 reject `BREAK` as unknown. Hence Sam's rule:
  `BREAK` is requested on Windows only, the POSIX and cloud sets are unchanged,
  and `HUP`, which does not exist on Windows, is no longer requested there, so
  the unavailable-signal warning no longer fires. `BREAK` on a POSIX native-image
  build was not measured and is not requested there.
- *Supervisor.* A `SIGBREAK` handler sets the same `stop_requested` event the
  SIGTERM and SIGINT handlers set. The supervisor's existing stop
  (`StorageServiceSupervisor.stop`) then runs as on POSIX: mark the lease
  shutting down, relinquish it, stop the engine. Its main thread must not block
  in a single long call: a CPython `SIGBREAK` handler did not run during a 120 s
  sleep, a stdin read or `Event.wait`, and ran within 0.5 s inside a 1 s sleep
  loop (T2 `224-research-22`). The spawn-lock wait and the PostgreSQL start wait
  tick, and a break during a tick ends the start (`StartInterruptedError`). Two
  residuals stay: the `psql` provisioning chains and the `pg_ctl status` calls
  are bounded subprocess calls (30 to 60 s backstops, milliseconds in practice),
  and a break during one is seen only after it returns; the worst case is that
  the stopper hard-kills the supervisor after its grace and the Job Object kills
  the engine (T2 `224-f9bgu17-round1`). The engine is stopped by `CTRL_BREAK`,
  where POSIX sends SIGTERM to its group, keeping the `proc.wait` grace
  (`_GRACEFUL_STOP_TIMEOUT`, 5.0 s, against `OrtInitGate`'s 3 s default wait). The
  engine runs in a Job Object that holds only the engine, with
  `KILL_ON_JOB_CLOSE`: the supervisor terminates it after the grace if the
  engine ignores the break, and the operating system closes the job, and so
  kills the engine, if the supervisor dies. PostgreSQL is never in the job and
  the supervisor is in no job, so no breakaway flag is needed. A job that cannot
  be made degrades with a warning (`storage_service_job_object_unavailable`).
- *Topology.* The supervisor is spawned with `CREATE_NEW_PROCESS_GROUP |
  CREATE_NO_WINDOW`, never `DETACHED_PROCESS`, which leaves it without a console
  to attach to, on both spawn paths: the per-user logon task and
  `nx daemon service start`. The engine is spawned by the supervisor with
  `CREATE_NEW_PROCESS_GROUP` only, so it shares the supervisor's console and the
  supervisor's own `GenerateConsoleCtrlEvent` reaches it. The stopper is the `nx`
  CLI in the same Windows session (`util/win_console.py`, called through the
  primitive's `request_graceful_stop`): `FreeConsole`, `AttachConsole(supervisor
  pid)`, `GenerateConsoleCtrlEvent(CTRL_BREAK, supervisor pid)`, then `FreeConsole`
  again and `AttachConsole(ATTACH_PARENT_PROCESS)`. The second `FreeConsole` is
  required: without it the re-attach fails with access denied and the CLI stays on
  the supervisor's hidden console (measured, T2 `224-f9bgu17-round1`). The
  process sweep's kill (`terminate_pids`) refuses a pid in another session
  instead of killing it. On the real stack a venv `python.exe` trampoline sits
  above the supervisor; it exits with it.
- *Confirming a stop.* The send can return success and deliver nothing (T2
  `224-research-20`), so a stop is confirmed by the target's exit, never by the
  return value. The hard kill is the fallback, and it waits for the process to
  leave the process table, because on Windows a kill returns first (T2
  `224-f9bgu19-conformance`). A stop sent from another Windows session fails
  `AttachConsole` with access denied, and Sam ruled that this fails loud: the CLI
  prints REFUSED, names the owning session and the remedy ("run this from session
  N"), exits 1, and signals, kills and relinquishes nothing (T2 `224-decisions`,
  `224-f9bgu17round2`).

No new HTTP route is added to the engine. The draft's loopback shutdown request
is removed, so no operator gate (`RequestContext.isOperator`,
`RequestContext.java:42-44`), no wire-ledger entry and no cloud client-path-gate
entry are needed for it.

**Windows PG bundle (Gap 2).** A build script separate from
`build_pg_bundle.sh`, because the toolchain shares nothing with autoconf. It
runs meson with the Linux bundle's options, generates grammar and scanner
targets serially before the parallel build, builds pgvector against
`pg_config`'s reported directories, and installs to a prefix. Relocation needs
no patching; the relocation smoke (build prefix removed, `initdb`, extensions,
an HNSW query) is the gate, as on the other platforms. The smoke must run on a
machine without Visual Studio or the VC++ redistributable, or it passes on
DLLs the user will not have: that is how the missing runtime went unseen on
the build host. Neither candidate release host qualifies: qwentescence has
Visual Studio Build Tools (§ Technical Environment), and `windows-latest` is
inferred, not read, to have Visual Studio too. The clean-machine runs so far
were hand-run Hyper-V guests (`sac-test`, `sac-pro`; Key Discoveries). Phase 0
Step 0.5 decided it on 2026-10-05, with both options: a hand-run clean Windows 11
Hyper-V guest on qwentescence at release time, as
`tests/e2e/mac-signed-binary-gate.sh` is for macOS (a manual real-hardware gate,
outside CI; T2 `224-research-26`), and, in CI, an assertion that every module the
running exe and `postgres.exe` load resolves from the app-local directory
(`check_loaded_modules` in `scripts/pg_bundle_windows_smoke.py`, and the same
check in `scripts/engine_windows_smoke.py`). The assertion is a proxy: it proves
the shipped copies were used, not that Visual Studio or a system-wide runtime is
absent. As built, the relocation smoke passed from the packaged archive on
qwentescence (build prefix removed, `vector` 0.8.2, `pg_trgm` 1.6, an HNSW index
used, 6 s; T2 `224-f9bgu12-windows-pg-bundle`), and the clean-guest run of the
packaged archives has not happened. The smoke must extract the bundle the way the
client does (with the user-tree ACL grant of Key Discoveries), or it can fail
for a reason the client path does not share, or pass without proving it.

**Supervisor port (Gap 3).** Each POSIX assumption gets a Windows branch in the
shared primitive (`src/nexus/daemon/service_registry.py` and the conformance
suite, per the daemon-lifecycle hot rule), never one tier's copy:

| POSIX assumption | Windows replacement |
| --- | --- |
| `os.getuid()` scoping, 21 code sites in 11 files (Phase 3 Step 2) | the user's SID, through one function, `service_identity()` in `service_registry.py`: `str(os.getuid())` on POSIX, unchanged, and the SID string on Windows (anything that is not `S-1-...` raises `ServiceIdentityError`). The SID, not the login name, because it is stable across account renames, cannot be set through an environment variable, and its characters are safe in file, lock and endpoint names. `tests/test_service_identity_lint.py` fails on any other `os.getuid()` |
| `os.kill(pid, 0)` liveness | `OpenProcess` + exit-code query (ctypes), or psutil if added as a Windows-only dependency |
| SIGTERM / `killpg` stop | `CTRL_BREAK` to the supervisor, and `CTRL_BREAK` from the supervisor to the engine (stop channel above, as built); the engine-only Job Object kill as the backstop; PostgreSQL is left running by default, as on POSIX (`commands/daemon.py:909-915`), and stopped with `pg_ctl stop -m fast` under `--with-pg` |
| `ps` / `/proc` identity | process creation time + image path via the Win32 API (done, through one stdlib-only core at every `ps` and `/proc` site) |
| launchd / systemd autostart | a per-user Task Scheduler task at logon, run only while the user is logged on (`/IT`, the configuration spiked, T2 `224-research-21`), with restart on failure, a hidden window and no execution time limit; the last three settings are not read from any source (inferred, not read, T2 `224-research-32`). Measured at implementation (nexus-f9bgu.23, 2026-10-05, T2 `224-f9bgu23-autostart`): the task's restart-on-failure fires only when the task's program fails to launch, never on a non-zero exit (exit codes 1, 3, 255, 0x80070005, 0xC0000005 and 0xFFFFFFFF did not restart), so the task runs a launcher that stays as the task's process and respawns the supervisor 30 s after any non-zero exit; the window is hidden by pythonw and CREATE_NO_WINDOW, since the task's Hidden flag only hides it in the Task Scheduler UI |
| `.exe`-less names, `LD_LIBRARY_PATH` | platform-derived executable names (`PgBinaries.from_dir`, `pg_provision.py:193-198`); no library-path injection (`_bundle_lib_env`, `:462`) |
| `pg_ctl start` through `run_bounded`: piped output, per-call Job Object that closes when `pg_ctl` returns | `pg_ctl` started detached: a plain `Popen` that never goes through `run_bounded` or `contain`, so no per-call Job Object, with `CREATE_NEW_PROCESS_GROUP`, stdin from `DEVNULL` and output to `pgdata/pg_ctl.out`; `PgStartError` names `pg.log` and `pg_ctl.out` with credential-scrubbed tails. The postmaster's own process group is what keeps a plain stop from reaching it: a plain stop left it serving across two stops (T2 `224-f9bgu18-windows-pg-start`, `224-f9bgu17round2`) |
| cluster superuser from `USER` / `LOGNAME` | derived from the same SID as the scope: `nx_` plus the first 16 hex characters of the SHA-256 of the SID string (`windows_superuser_name`; `bootstrap_superuser` uses `service_identity()` on Windows), so two accounts on one machine get distinct superusers |
| lease file `os.replace` under open readers; executable and bundle replaced in place on upgrade | both fail on Windows (Key Discoveries). The lease read, replace and unlink retry a sharing violation for up to 2 s (2 to 10 ms backoff, Windows only, in the shared primitive), and a conformance property replaces the lease under concurrent readers. Upgrade stops first: `quiesced()` stops the service (and PostgreSQL only for a bundle swap with a cluster up), the file set is placed as one unit (`place_set_with_rollback`: each file one atomic `os.replace`, DLLs first and the exe last, bounded retry with 5 sleeps of 0.1 to 2 s, every file restored if any step fails), then it restarts only what it stopped, on failure too. A refused or surviving pid raises `ReplaceBlockedError` naming the session, kills nothing and replaces nothing (T2 `224-f9bgu20-upgrade-stop`, `224-f9bgu19-conformance`) |
| `chmod 0o600` token files | decided: hardened in the first release, not deferred. `nexus._winsec` (`open_private`, `restrict_to_owner`, `ensure_owner_only`, `owner_only_problem`) installs a protected DACL with one ACE, full control for the current user's SID, before any secret byte is written (a rename keeps the source's descriptor, so the temp file gets it). The reader tolerates SYSTEM, Administrators and OWNER RIGHTS and refuses any other trustee. The plugin's hook scripts cannot import nexus, so they carry a mirror that `tests/test_winsec.py` pins to the original |

**VC++ runtime.** The four app-local DLLs (`vcruntime140.dll`,
`vcruntime140_1.dll`, `msvcp140.dll`, `msvcp140_1.dll`) must sit beside the exe,
the PG bundle ships the same four in its `bin` directory, and the release leg's
dependency check runs `dumpbin /dependents` on the exe AND on every native
library it embeds, since the embedded libraries bring in DLLs the exe does not
import. Every other engine asset is one file, `nexus-service-<platform>`,
verified by one `.sha256` and one sigstore bundle and placed as a single file
(T2 `224-research-28`). Phase 0 Step 0.4 decided the Windows asset on
2026-10-05: one archive, `nexus-service-windows-x64.txz`, holding the exe, the
four DLLs and a third-party notice (the notice is P0.6 condition 4), verified by
one `.sha256` and one `.sigstore.json`. `install_binary` places the set as one
unit (`_place_engine_archive`; the placement rules are in the upgrade row above).
The dependency check, `check-deps` in `scripts/windows_engine_release.py`, reads
`service/target/embedded-resources.json`, extracts every embedded DLL from its
origin jar and runs `dumpbin /dependents` on each and on the exe, against a
closed allowlist: the four VC++ DLLs, the system DLLs measured on the 2026-10-05
build, and, for an embedded library only, a sibling extracted into the same
directory. On that build it passed for 8 binaries. The exe imports 24 DLLs, two
of them VC++ (`vcruntime140`, `vcruntime140_1`); `onnxruntime.dll` imports all
four, which is why an exe-only check is not enough (T2
`224-f9bgu9-windows-engine-leg`).

**Signing (Gap 6), deferred.** Phase 0 Step 0.2 deferred signing on 2026-10-05
(nexus-dj01b); the first Windows release ships unsigned and this is the design for
when it is adopted. Every PE file (Windows
executable or DLL) we ship is Authenticode-signed with an RFC 3161 timestamp, so
the signature outlives the certificate: the engine exe, every PG bundle
executable and extension DLL, and the unsigned third-party DLLs the engine
embeds. Signing the exe does not cover the DLLs it loads, because Smart App
Control checks each file. The third-party DLLs are signed BEFORE the native
build embeds them, since the runtime extracts exactly the bytes that were
embedded; that means the build takes the DJL tokenizer DLLs from a signed copy
rather than straight from the DJL jar, and the embedded-resources checker must
see that copy as the single origin. The ONNX Runtime and VC++ runtime DLLs are
already Microsoft-signed and ship as they are. The signing route (Artifact
Signing, SignPath Foundation, or an OV certificate with a cloud HSM) is chosen
when signing is adopted; whichever route, the key never sits in a CI secret
file. The VC++ DLLs are shipped unmodified and are not re-signed (P0.6 condition
2). Windows signing runs in its own job
with its own environment, not inside `build-publish`. That job declares
`environment: apple-signing` for the whole matrix (`engine-service-release.yml:231`),
and that environment's comment says "only a job that declares this environment
can read them" (`:213-215`), so a Windows signing step inside it would sit beside
the Apple secrets. The same comment judged splitting signing out of that job not
worth it for the three existing legs (`:223-230`); a Windows credential changes
that (T2 `224-research-29`).

**Runtime extraction hygiene.** ONNX Runtime's per-start temp directories (Key
Discoveries) are removed by a cleanup at engine boot; a fixed extraction path is
not possible. onnxruntime-java 1.20.0 calls `Files.createTempDirectory(
"onnxruntime-java")` unconditionally, before it reads `onnxruntime.native.path`,
and always extracts `onnxruntime_providers_shared.dll` into it, so that property
cannot avoid the directory, and a fixed location would need a global
`java.io.tmpdir` override that still leaves one directory per start. Its own
`deleteOnExit` loses on Windows because the loaded DLLs are locked. DJL's
tokenizers extract once into `~/.djl.ai/tokenizers/<version>-...` and reuse it, so
they do not accumulate. `OrtTempSweep.sweepAtBoot()` runs in `Main` after the
signal handlers install, on Windows only. It deletes `onnxruntime-java*`
directories, trying the loaded libraries (`onnxruntime.dll`,
`onnxruntime4j_jni.dll`) first, so a live peer's mapped DLLs refuse the delete and
its directory stays. A complete directory touched in the last 250 ms is skipped,
and an incomplete one under 30 s old (a flat 1 to 2 s bound failed, because the
engine boots in about 1 s and always found the previous stop's directory too
new). Measured on qwentescence, 5 start and stop cycles: 5 directories without
the sweep, 1 with it; a real `%TEMP%` holding 19 went to 1 after one start; with
engine A running, three starts of B kept A's directory and left A alive (T2
`224-p1.3-ort-temp-sweep`). The safety rests on the loaded-library names being
true for the pinned 1.20.0 jar; a test ties them to the committed jar listing,
so an ONNX Runtime bump that renames one fails it (T2 `224-review-p1p2-critique`,
Observation 1). The sweep also skips junctions and any reparse point.

**Release build host (Gap 1, Gap 7).** Phase 0 Step 0.1 chose qwentescence as a
self-hosted runner on 2026-10-05, over GitHub's hosted `windows-latest` (inside
the existing trust model, billed at twice the Linux rate). It was not an
extension of the hellmini precedent: AGENTS.md said CI runs on GitHub-hosted
runners only, that hellmini is the only self-hosted registration, that
qwentescence is "a test host reachable by ssh, not a runner", and that the
`qwen-linux` and `gtr-windows` routes were removed on 2026-10-03. The choice
re-adds a self-hosted runner and reverses that removal, so AGENTS.md's
§ Self-hosted runners and fork PRs changed in the same change. The rules are
hellmini's by analogy: release legs only, inside the release trust boundary,
never `pull_request`. The runner's label is `win-release` (no `qwen` or `llama`
appears in any Windows service or task name on that box), and Sam registers it
himself. Every Windows release job carries the repo variable `NX_WINDOWS_RELEASE_LEGS ==
'on'` as its job-level condition, so with the variable off no job queues for a
runner that does not exist (T2 `224-decisions`, `224-decisions-amendments`,
`224-f9bgu14-windows-release-legs`).

**CI placement (decided, Phase 0 Step 0.5).** At drafting no workflow under
`.github/workflows` mentioned Windows (13 files, T2 `224-research-29`).
Premium runners are for release and tag artifact builds, not routine push or
pull-request CI (`AGENTS.md:138-141`). Run only at tag time, the first run of any
change to this machinery is the real tag: the first real mac signing run failed
mid-release at codesign on `engine-service-v0.1.142`, which is why
`.github/workflows/mac-signing-rehearsal.yml` exists. P0.5a (2026-10-05) chose a
Windows rehearsal modelled on it:
`.github/workflows/windows-pg-bundle-rehearsal.yml`, on a develop push for
path-filtered inputs plus `workflow_dispatch`, never `pull_request`, behind the
same repo variable. It runs the composite action
`.github/actions/windows-engine-leg` (build, checks, package, smoke), the PG
bundle script and the relocation smoke, and the supervisor conformance job with a
junit floor (`scripts/check_junit_floor.py`) and a static census that allows at
most 4 Windows skips and requires at least 60 tests to run. The rehearsal does
not run the cosign steps, the release upload, the cross-job bundle artifact
handoff, the cache restore on the self-hosted runner or promotion against a real
27-asset release, and its path filter omits `engine-service-release.yml` and
`pg-bundle-cache-seed.yml`. Those are covered by the pre-cut `workflow_dispatch`
run of the release workflow in the cut checklist (T2 `224-decisions-amendments`;
T2 `224-review-p1p2-critique`, S6). No workflow step has run on `win-release`
yet.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Windows native build | `service/pom.xml` `native-libs-windows` profile | Reuse: already selects `win-x64` / `win-x86_64` libraries. |
| Embedded-resource guard | `scripts/check_native_embedded_resources.py` (nexus-vwfc0) | Extend: a `windows-x64` platform mapping; release-leg coverage is nexus-zz2w7. |
| Windows PG bundle | `scripts/build_pg_bundle.sh` | Replace for Windows only: separate script, same outputs and cache key shape. |
| Platform tag | `src/nexus/db/pg_bundle.py` `current_platform_tag()` | Extend: add `windows-x64` (done: it returns `windows-x64` on Windows). |
| Process containment | `src/nexus/util/win_job.py` | Reuse for the engine's Job Object (`KILL_ON_JOB_CLOSE`); the console attach, send and re-attach are new, in `src/nexus/util/win_console.py`. |
| File locking | `src/nexus/_locking.py` | Reuse (already msvcrt-aware). |
| Autostart | `src/nexus/daemon/installer.py` | Extend: `schtasks` as a third manager beside launchd/systemd, with `src/nexus/daemon/windows_autostart.py` (task XML render and the restarting launcher). Done. |
| Binary install | `src/nexus/daemon/binary_install.py` | Extend: Windows asset names and the one-archive layout (Phase 0 Step 0.4), placed as one unit with rollback; cosign verification unchanged. |

### Decision Rationale

The WSL2 appliance was chosen because native Windows looked expensive and
uncertain. The spike removed the uncertainty for the two expensive parts (the
engine and the PostgreSQL bundle): both work, the engine with no source
changes. What remains is ordinary porting in the client, and the client has
Windows groundwork already. Native also removes the costs the appliance could
not: the VM boundary, the per-boot keep-alive, the clock freeze on host sleep,
WAL recovery after every VM teardown, a 1.5 GB image as a fourth artifact
class, and a dependency on WSL behaviour Microsoft does not contract. It does
not remove crash recovery for every host shutdown: what a user-launched
`postgres.exe` receives at logoff, reboot and sleep-kill, and whether the next
start then recovers, was not measured (T2 `224-research-32`). Engine stops
measured so far left no crash recovery (T2 `224-research-19`), and neither did a
plain or `--with-pg` CLI stop of the real stack (T2 `224-f9bgu17round2`).

## Alternatives Considered

### Alternative 1: Continue RDR-218's WSL2 appliance

**Description**: Ship the pre-built WSL2 image RDR-218 designed.

**Pros**:

- Reuses the Linux artifacts unchanged; no new platform target.
- Several of its beads are done (fixed port, IPv4 bind, image size decision).

**Cons**:

- Keeps the VM boundary and everything built to bridge it (discovery,
  keep-alive, volume attach, the handoff file).
- Host sleep freezes the guest clock; VM teardown makes WAL recovery the normal
  start; WSL behaviour is Microsoft's implementation detail, not a contract.
- A fourth artifact class of 1.5 GB or more.

**Reason for rejection**: Sam's decision, 2026-09-30, after the spike showed the
native pieces work.

### Alternative 2: A JVM engine with a bundled JRE on Windows

**Description**: Ship the JAR and a JRE instead of a native executable (the
sanctioned `NEXUS_SERVICE_JAR` run path, RDR-218 Alternative 3).

**Pros**:

- Avoids native-image on Windows entirely.

**Cons**:

- Larger download, slower start, a second engine shape to support on one
  platform.

**Reason for rejection**: native-image works on Windows (Verified), so the
reason to avoid it is gone.

### Alternative 3: An authenticated loopback HTTP shutdown request (this record's first stop design)

**Description**: A new engine route that begins the shutdown the SIGTERM handler
begins, authorised by the engine's boot bearer token. The supervisor calls it on
Windows.

**Pros**:

- Needs no console, process-group or session arrangement.

**Cons**:

- No listener exists until `Main.java:355`, after schema migration (`:115`) and
  ONNX Runtime initialisation (`:218`), the window the signal gate exists to
  cover (`OrtInitGate.java:36-43`). `CTRL_BREAK` stopped the engine in the
  migration window and while serving (T2 `224-research-18`).
- It stops the engine, not the supervisor that `nx daemon service stop` signals
  (`storage_service_daemon.py:3117`); with the supervisor alive, its loop treats
  a dead engine as a failure and exits 3 for the OS to restart
  (`_supervise_until_stopped` docstring, `:2810-2822`).
- It adds a route to the engine binary every platform and the cloud share.

**Reason for rejection**: unreachable in the window it protects, and aimed at the
wrong process (T2 `224-research-18`, `-20`).

### Alternative 4: A named event the engine waits on

**Description**: A Windows named event the engine opens at process start, set by
the stopper.

**Pros**:

- Exists from process start, like a signal handler.

**Cons**:

- A second Windows-only mechanism in the engine beside the signal list; not
  spiked.

**Reason for rejection**: not needed for this topology. `CTRL_BREAK` reached both
the supervisor and the engine from a same-session sender (T2 `224-research-20`,
`-21`).

### Briefly Rejected

- **Cloud mode as the Windows answer**: closed by RDR-218 Alternative 1
  (no self-serve token issuance); unchanged.
- **Windows on ARM**: no GraalVM native-image target and no onnxruntime
  Windows-ARM64 library; revisit when upstream ships them.
- **A third-party Windows PostgreSQL build (EDB, zonky)**: RDR-157 showed zonky
  lacks `pg_config` and headers to build pgvector against, and Strategy B
  (from source) is the proven path.

## Trade-offs

### Consequences

- Positive: Windows users get the same install shape as macOS: `nx init
  --service` fetches a verified binary and PG bundle, and the supervisor runs
  them.
- Positive: RDR-218's WSL-boundary gaps disappear rather than being bridged.
- Negative: a fourth native build leg and a fourth PG bundle on every engine
  tag (release cost, and a Windows build host to keep healthy).
- Negative: a supervisor with Windows branches in its lifecycle primitive,
  which the daemon conformance suite must cover on Windows.
- Negative: the engine's stop-signal code gains a per-OS list, in source the
  Linux, macOS and cloud builds share (`OrtInitGate.exitSignals`). `BREAK` is
  requested on Windows only, so the POSIX and cloud sets are unchanged. A
  Windows JVM engine cannot be stopped by `CTRL_BREAK` (HotSpot refuses the
  handler); that run path was rejected for Windows (Alternative 2).
- Negative: a stop sent from another Windows session fails with access denied
  (T2 `224-research-20`); the CLI refuses it with a message naming the owning
  session and kills nothing.

### Risks and Mitigations

- **Risk**: Smart App Control, or an enterprise application-control policy,
  blocks unsigned code on some user machines. Documented by Microsoft; not
  reproduced in our tests on two clean Windows 11 installs.
  **Mitigation**: none in the first release. Phase 0 Step 0.2 deferred signing
  (nexus-dj01b), so this is an accepted open risk. When signing is adopted, sign
  every shipped PE file (Gap 6). Because the block could not be reproduced,
  signing is not shown to be a precondition for a working install.
- **Risk**: when signing is adopted, the signing identity takes weeks to
  validate and is on the critical path.
  **Mitigation**: choose the route and start validation when signing is adopted;
  nothing has started.
- **Risk**: a new publisher still sees SmartScreen's "unrecognized" warning on
  browser downloads until reputation accrues (EV no longer helps).
  **Mitigation**: the supported path (`nx daemon service install-binary`) writes
  no Mark of the Web, so SmartScreen does not screen it; keep the publisher identity
  stable across releases so reputation accumulates.
- **Risk**: enterprise application-control policies block nexus regardless.
  **Mitigation**: document the publisher to allowlist; not otherwise
  solvable.
- **Risk**: VC++ runtime missing on user machines.
  **Mitigation**: the four app-local DLLs beside the exe, in one archive (Critical
  Assumption 4, verified; Phase 0 Step 0.4), with a dependency check that covers
  the embedded native libraries, since those import two of the four. The packaged
  archives have not run on a machine without the runtime.
- **Risk**: an engine tag cut with `NX_WINDOWS_RELEASE_LEGS` off publishes 21
  assets and no Windows asset, becomes immutable, and gives a Windows client
  nothing to install; the recovery is another engine cut.
  **Mitigation**: the cut checklist and release-skill guard recorded with the
  P0.4 amendment (Phase 0 Step 0.4): the .42 pre-cut checklist, the
  engine-release skill's step 3f and `check_engine_release_floor.py
  --require-windows`.
- **Risk**: the Windows build host is a persistent machine inside the release
  trust boundary (qwentescence, chosen in Phase 0 Step 0.1).
  **Mitigation**: hellmini's rules by analogy, written into AGENTS.md: release
  legs only, never `pull_request` (as in
  `.github/workflows/mac-signing-rehearsal.yml`), every Windows release job behind
  the `NX_WINDOWS_RELEASE_LEGS` variable, and a fork-PR run is never approved by
  an agent. Like hellmini, it persists state between jobs. The releaser's Visual
  Studio product licence is part of the boundary (§ New Dependencies).
- **Risk**: the supervisor port regresses POSIX behaviour.
  **Mitigation**: the port lands in the shared primitive behind the existing
  conformance suite, which keeps running on Linux and macOS.

### Failure Modes

- The engine exe does not start: the supervisor reports the child's exit code
  and stderr, as on POSIX; a missing VC++ runtime shows as a loader error
  naming the DLL.
- PostgreSQL fails to start: `pg.log` and `pgdata/pg_ctl.out` carry the reason
  (the start path writes `pg_ctl`'s output to that file, never a pipe), and the
  error names both with credential-scrubbed tails. An elevated session whose
  bundle directory or data directory has an owner-only ACL fails `initdb` with
  `0xC0000135` and no message (Key Discoveries); the ACL grant prevents it.
- A stop does not make the supervisor or the engine exit within the grace: the
  send can return success and deliver nothing (T2 `224-research-20`), so the
  stop is confirmed by process exit. If the engine ignores the break, the
  supervisor terminates it through the Job Object that holds the engine after
  the grace, and a supervisor that outlasts its own grace is hard-killed by the
  stopper, which then waits for it to leave the process table. The event is
  logged (`unclean_stop`) so an unclean stop is visible, not silent.
- A stop is sent from another Windows session: `AttachConsole` fails with access
  denied (T2 `224-research-20`) and the CLI REFUSES: it names the owning session
  and the remedy, exits 1, and signals and kills nothing (T2
  `224-f9bgu17round2`). A refused upgrade or install behaves the same way.
- A stop arrives during a Liquibase changeset: the engine exits at once and
  leaves `databasechangeloglock` locked, and the next boot waited about 300 s
  and then failed (T2 `224-research-19`; that record does not say whether a
  supervisor was involved). POSIX
  SIGTERM has the same effect, so this is shared with POSIX and is not a Windows
  port defect. The supervisor releases a stale lock before each spawn of a
  bundled cluster (`storage_service_daemon.py:1543-1557`, called at `:2205`);
  whether that recovers a supervised Windows boot is not measured (T2
  `224-research-31`). Re-measured on the release-shaped engine alone: a break at
  changeset 255 of 508 exits 149 in 0.013 s, leaves 254 changelog rows and the
  lock held, and the next boot was still waiting on the lock at the probe's 45 s
  cap (T2 `224-p1.1-stop-probe`). Fixing it is outside RDR-224; tracked
  separately (nexus-8sph2).
- The supervisor exits non-zero or is killed under the logon task: the task's
  own restart-on-failure does not fire on an exit code, so the launcher the task
  runs respawns the supervisor 30 s later (longer than the 15 s lease TTL, pinned
  by a test). A deliberate stop exits 0 and ends the launcher and the task, so
  nothing resurrects it. Measured with a stand-in supervisor, a hard kill respawned
  at 30.8 s and an exit 3 at 31.8 s (T2 `224-f9bgu23-autostart`).
- An upgrade runs while the service is up: it stops first, and a held-open or
  unreplaceable file raises `ReplaceBlockedError` after 6 attempts over about
  3.9 s, with all five files restored to the old set and the service restarted if
  the upgrade stopped it (T2 `224-f9bgu20-upgrade-stop`).
- The host shuts down, the user logs off or sleeps with PostgreSQL running: a
  user-launched `postgres.exe` may get no `pg_ctl stop`, and the next start may
  run crash recovery. Not measured (T2 `224-research-32`).

## Implementation Plan

### Prerequisites

- [ ] The remaining Critical Assumptions verified: hooks in a native Windows
  Claude Code session, the packaged archives on a clean machine, and the
  Windows release-workflow jobs on `win-release` (§ Critical Assumptions). The
  stop on the real supervisor and engine and the stop during ONNX Runtime
  initialisation are verified.
- [x] The Phase 0 signing decision recorded: deferred (nexus-dj01b), no route
  chosen, so no identity validation gates the first release
- [x] Phase 0 decisions recorded (T2 `224-decisions`,
  `224-decisions-amendments`; Revision History)

### Minimum Viable Validation

On a clean Windows 11 x64 machine with no developer tools: `nx init --service`
installs the published `windows-x64` engine (`commands/init.py:229`) and PG
bundle (`:563`); `nx daemon service install-binary <tag>` installs the engine and,
by default, the PG bundle (`--pg-bundle`, `commands/daemon.py:777-785`;
`--no-pg-bundle` installs the engine alone). The supervisor starts both, a
store-then-search round trip returns the stored text, and
`nx daemon service stop --with-pg` from the same Windows session leaves no
supervisor, engine or postgres process behind, confirmed by process exit, and no
WAL recovery on the next start. A plain `nx daemon service stop` leaves PostgreSQL
running (`commands/daemon.py:909-915`). The next-start check covers a CLI stop
only; host shutdown is unmeasured (T2 `224-research-32`).

### Phase 0: Decisions and RDR-218 disposition

#### Step 0.1: Release build host

Record the choice between GitHub `windows-latest` and qwentescence as a
self-hosted runner. qwentescence is not a runner today, so that choice re-adds
one and reverses the 2026-10-03 removal (`AGENTS.md:148-163`).

**Decided 2026-10-05 (Sam):** qwentescence as a self-hosted runner, label
`win-release`, for release legs only, never `pull_request`, inside the release
trust boundary on hellmini's terms. AGENTS.md changed in the same change. Sam
registers the runner himself (T2 `224-decisions`, `224-decisions-amendments`).

#### Step 0.2: Signing route

Decide whether signing gates the first supported Windows release or follows
it: the documented Smart App Control block was not reproduced (Key
Discoveries), so an unsigned install is measured to work on the machines we
tested, and signing protects against configurations we could not test and
against enterprise policies. Either way, choose the route: Microsoft Artifact Signing (if
Sam, as an individual in the US or Canada, or a nexus organization in a
supported country qualifies), SignPath Foundation (if its open-source terms
fit), or an OV certificate held in a cloud HSM. Start identity validation
immediately after choosing: it takes days to weeks and cannot be hurried, so
it is on the critical path of Phase 5. Until the decision is recorded, the
signing steps in Phase 1 Step 2, Phase 2 Step 1 and the Test Plan are
conditional on it.

**Decided 2026-10-05 (Sam):** skipped for now and added later. No route is
chosen and identity validation has not started. The signing steps are not in the
first release and carry to nexus-dj01b (deferred); the two Phase 1 and Phase 2
signing beads were closed to it. Gap 6 is an accepted open risk.

#### Step 0.3: RDR-218 beads

Flip RDR-218 to `superseded` with `nx rdr set-status 218 superseded` (it writes
the supersedes edge). Close its appliance-only beads with this record as the
reason: ijue9.4, .5, .6, .8, .11, .13, .14, .15, .17, .19, .22, .23, .24, .31.
Re-parent to this record's epic the beads that carry over: ijue9.2 (dispatch
check from native Claude Code), ijue9.16 (the real-hardware gate), ijue9.20
(desktop bundle platform gate and `execvp`), ijue9.21 (desktop install path),
and nexus-efk2h, nexus-jevq5. Rewrite ijue9.2 and ijue9.16 against Phase 5
first: both name `post-publish-dispatch-check.sh`, which was deleted, and
ijue9.16's items 1 to 3 and 5 concern the WSL appliance (T2 `224-research-27`).

**Decided 2026-10-05 (Sam):** approved as proposed. RDR-218 is `superseded`, with
the supersedes edge to this record (`tests/catalog/test_rdr_dependency_edges.py`
pins the pair). The same round of decisions added a fifth Phase 5 assertion (Phase 5,
item 5).

#### Step 0.4: Windows asset layout and promotion

Decide two things. First, the layout of the Windows engine asset: an archive of
the exe and the four DLLs, or separate assets, and the `install_binary` change
that layout needs (Technical Design, VC++ runtime). Second, whether the Windows
asset set blocks promotion of the release or is attached after it.
`promote_engine_release.sh:26-31` expects 21 assets for the three existing
platforms and leaves the whole release a DRAFT when any is missing (`:41-44`),
so adding `windows-x64` to it means a failing Windows leg keeps every platform's
release draft. `check_engine_cut_riders.py:42-46` lists three names and no
Windows entry; an entry for a name that is absent from the release fails the
check (`:58-62`), so the name it lists follows the layout. Attaching late
leaves a published tag without a Windows asset, while AGENTS.md says one engine
identity per release on every install path (`AGENTS.md:264`); that a tag without
a Windows asset then cannot be the identity a Windows client pins is inferred,
not read (T2 `224-research-28`).

**Decided 2026-10-05 (Sam):** one archive holding the exe and the four DLLs
(`nexus-service-windows-x64.txz`), and the Windows assets BLOCK promotion: they
join `promote_engine_release.sh`'s expected set.

**Amendment, 2026-10-05 (Sam), after the Phase 1 and 2 review (T2
`224-review-p1p2-critique`, S4):** "block promotion" is implemented, and accepted,
as conditional on the repo variable `NX_WINDOWS_RELEASE_LEGS`. With it off, tags
and promotion behave as before and a release carries 21 assets. With it on, the
Windows engine archive and the PG bundle, with their checksum and sigstore files,
add 6 assets, 27 in all, and block promotion. The reason is that no runner is
registered, so a required Windows job would queue until it times out and hold
every platform's release in draft. The risk is a Windows-carrying cut made with
the variable off, which publishes an immutable tag with no Windows asset. The
guard that was agreed and landed with the review fixes: the pre-cut checklist
(variable on, a green `workflow_dispatch` run of the release workflow, a
clean-guest candidate run), a step in the engine-release skill, and
`check_engine_release_floor.py --require-windows`. Sam flips the variable after
he registers the runner (T2 `224-decisions-amendments`).

#### Step 0.5: Windows CI placement and clean-machine host

Decide where the supervisor conformance suite, the bundle-script check and the
relocation smoke run, and which host runs the clean-machine smoke (Technical
Design, Release build host and CI placement).

**Decided 2026-10-05 (Sam):** (a) CI placement: a Windows rehearsal workflow
modelled on `mac-signing-rehearsal.yml`, on a develop push for path-filtered
inputs plus `workflow_dispatch`, never `pull_request`
(`.github/workflows/windows-pg-bundle-rehearsal.yml`). (b) Clean-machine host: a
hand-run clean Windows 11 Hyper-V guest on qwentescence at release time, plus a CI
proxy that asserts app-local module resolution. Neither (b) half has covered the
packaged archives yet (Critical Assumptions).

#### Step 0.6: VC++ redistribution terms

Read Microsoft's terms for app-local deployment of the four DLLs and have Sam sign
off the reading (it is a reading, not legal advice).

**Decided 2026-10-05 (Sam):** signed off (T2 `224-vcruntime-terms`,
`224-vcruntime-terms-signoff`). The conditions are in § New Dependencies.

### Phase 1: Engine

#### Step 1: Windows stop signal (Gap 4)

`OrtInitGate.exitSignals(osName)` requests `BREAK` on Windows only, with `TERM`
and `INT`, and `TERM`, `INT`, `HUP` elsewhere (Technical Design). It was proved
with the o5xyx window probe on Windows, driven by `CTRL_BREAK`, in each phase the
Test Plan names: migration (before the lock and during a changeset), ONNX Runtime
initialisation (both branches of the gate) and serving; all measured (Critical
Assumptions; T2 `224-p1.1-stop-probe`, `224-p1.1-ort-init-timeout-probe`). The
probe driver first stayed on qwentescence; the review fixes committed it as
`scripts/engine_windows_stop_probe.py`, so the rows can be re-run when
`OrtInitGate`, `Main` or the native-image configuration changes. The recurring
coverage is `OrtInitGateTest`, which uses a stand-in signal installer, and the
release-leg smoke's stop assertion (Step 2). The `OrtTempSweep` boot cleanup (Technical Design, Runtime extraction hygiene)
belongs to this phase too.

#### Step 2: Windows release leg (Gap 1, Gap 6)

As built, a job of its own, `build-publish-engine-windows`, not a matrix entry:
the matrix job carries the `apple-signing` environment and bash and macOS steps,
and one matrix entry cannot be skipped alone. The job runs on `win-release`
behind `NX_WINDOWS_RELEASE_LEGS`, and its steps are the composite action
`.github/actions/windows-engine-leg`, which the rehearsal runs too: the native
build, the embedded-resources check for `windows-x64`, the dependency check (only
the VC++ runtime and system DLLs may be imported; it covers the embedded native
libraries as well as the exe), the package step (the archive with the four
app-local VC++ DLLs, layout in Step 0.4) and the smoke. The jOOQ sources come
from the Linux `jooq-codegen` job's artifact through `-Pprebuilt-jooq`, because
codegen needs Docker and the Windows runner has none; they are plain Java and the
same on every OS. On a tag, after the smoke, cosign signs the archive in the new
bundle format and the job uploads to the release. That produces a
`.sigstore.json` only, with no `.cosign.bundle`, and it is release provenance,
not Authenticode signing.

The smoke is a new Windows script, `scripts/engine_windows_smoke.py`.
`service/native-smoke.sh` starts a throwaway pgvector container with `docker run`,
which the Windows runner cannot do (inferred, not read, T2 `224-research-29`).
The new smoke boots the exe against the Windows PG bundle of the same workflow
run, handed over as the artifact `nexus-pg-windows-x64` by
`build-publish-pg-bundle-windows`, so Phase 2 Step 1 lands before it runs. It
asserts health, that every changeset in the changelog applied, a 768-dimension
embedding, and that the four VC++ DLLs loaded from the engine directory, then
stops the engine with `CTRL_BREAK`. The stop is an assertion (review finding S1,
T2 `224-review-p1p2-critique`): exit code 149 within a bound, `shutdown_signal` and
`service_stopped` in the engine log, then a second boot that reaches health with
no new changesets and stops the same way; a session that cannot deliver the break
fails the leg. The smoke extracts the PG bundle through the client's own ACL grant
and compares module paths in long form (S5). The smoke is narrower than `native-smoke.sh`: no cross-encoder rerank and no
Python client probes. A hand run on qwentescence passed: 8 binaries in the
dependency check, an archive of 33,687,976 bytes with VC++ redistributable
14.44.35112, health after 3.0 s, 508 of 508 changesets, a 768-dimension embedding
and the four DLLs from the engine directory (T2 `224-f9bgu9-windows-engine-leg`).
The workflow itself has not run.

Authenticode signing is not in the first release (Phase 0 Step 0.2, nexus-dj01b).
When it is adopted: sign the DJL tokenizer DLLs before the native build and the
exe after it, in a job and environment of their own (Technical Design, Signing),
and add a check that fails the leg if any shipped PE file lacks a valid signature
(`Get-AuthenticodeSignature` / `signtool verify /pa`).

### Phase 2: PostgreSQL bundle (Gap 2)

#### Step 1: Windows bundle build script and relocation smoke

As § Technical Design (`scripts/build_pg_bundle_windows.py`, with the
subcommands `build`, `refresh-runtime`, `verify`, `package` and `cache-key`).
Cached on exact inputs per CI Cost Discipline: the key is
`pg-bundle-windows-x64-<runner>-pg<PG>-pgvector<PGV>-img-native-<sha256 of the
script>`. The four VC++ DLLs are not a key input, so a cache-restored prefix runs
`refresh-runtime` before `package` (P0.6 condition 8), and the Build Tools, meson,
ninja and win_flex_bison versions on the one persistent runner also change
without changing the key. The full build took 100 s at `ninja -j8` on
qwentescence, the archive is 7,407,044 bytes, and the relocation smoke passed from
it in 6 s. The cache restore has never run. Authenticode signing of the bundle is
not in the first release (Phase 0 Step 0.2).

#### Step 2: Release and cache-seed legs

Add `windows-x64` to the PG-bundle release and to `pg-bundle-cache-seed.yml`
(as a job of its own on `win-release`, behind the variable), and to the release
promotion per Step 0.4. As built, with the variable on, promotion expects 27
assets and the Windows jobs must succeed; with it off, 21 and neither is required.
The workflow YAML is covered by `actionlint` and parsing tests only; no run exists
(T2 `224-f9bgu14-windows-release-legs`).

### Phase 3: Client (Gap 3)

#### Step 1: Platform tag and binary install

`current_platform_tag()` returns `windows-x64` (done); `nx init --service` and
`nx daemon service install-binary` fetch the Windows assets. No Windows release
is published, so that fetch has not run end to end on Windows.

#### Step 2: Supervisor port

The table in § Technical Design, in the shared primitive, with the conformance
suite extended to Windows. As built, every item below landed on develop: the 21
sites call `service_identity()` and a lint rejects any other `os.getuid()`; the
conformance properties passed on real Windows (74 passed, 2 skipped, 2 xfailed); a
junit floor in the rehearsal job keeps a vacuous run from passing. Two siblings
found along the way were fixed with them: the session sweep's orphan-tracker kill
and the plugin's lease read (T2 `224-f9bgu19-conformance`,
`224-f9bgu44-windows-tests`). The port covers:

- the 21 `os.getuid()` code sites in 11 files: `health.py:3546`,
  `upgrade_finish.py:1638`, `daemon/installer.py:202`, `:211`, `:507`, `:570`,
  `:603`, `:1128`, `daemon/storage_service_daemon.py:1052`, `:3089`,
  `hooks/mailbox_drain.py:1132`, `:1146`, `db/onnx_model_root.py:48` (already
  behind an `ImportError` arm), `db/service_endpoint.py:132`,
  `commands/init.py:360`, `commands/upgrade.py:415`, `commands/daemon.py:529`,
  `:1223`, `:1233`, `:1263`, `upgrade_ladder/preconditions.py:161` (T2
  `224-research-25`);
- the stop sequence and spawn flags in § Technical Design, including both
  supervisor spawn paths and the engine spawn;
- the PostgreSQL start path: `pg_ctl` with output redirected to a file, outside
  the per-call Job Object, with `.exe` names and a Windows superuser source (the
  SID-derived `nx_` name), and the user-SID ACL grant on the bundle extract root
  and the data directory (Key Discoveries);
- owner-only files: the token and credential files get a protected DACL (the
  `chmod 0o600` row), decided for the first release;
- conformance properties: stop returns only once the supervisor and the engine
  have exited, and the lease file is replaced under concurrent readers;
- an upgrade that stops the service before it replaces the installed executable
  or the PostgreSQL bundle directory (`binary_install.py:574-576`,
  `pg_bundle.py:235-244`).

#### Step 3: Autostart

A Task Scheduler implementation in `installer.py` and `windows_autostart.py`,
with the task settings in § Technical Design: logged-on user only, a hidden
window, no execution time limit. The task's restart-on-failure does not restart a
supervisor that exits non-zero (Key Discoveries), so the task's process is a
launcher that respawns the supervisor 30 s after any non-zero exit and ends on
exit 0. Verified by hand on qwentescence with a stand-in supervisor: install,
idempotent re-install, uninstall, out-of-band disable and delete caught by the
doctor row, a stop from a hidden session-1 task (T2 `224-f9bgu23-autostart`). Not
run: a logon trigger that actually fires (`schtasks /Run` stood in), a standard
(non-administrator) account, behaviour at sleep and logoff, and the real engine and
PostgreSQL under the launcher.

### Phase 4: Plugin and desktop (Gap 5)

Close nexus-efk2h (hook launcher), ijue9.20 (manifest platform gate and
`execvp`), then ijue9.21 (desktop install path). The `win32` platform token in
`mcpb/manifest.json` stays out of develop until the Windows client release
(nexus-f9bgu.43; T2 `224-decisions-amendments`).

### Phase 5: Gate (Gap 7)

A hand-run, release-time gate on a real Windows box, outside CI, modelled on
`tests/e2e/mac-signed-binary-gate.sh` and on the journey of
`tests/e2e/fresh-install-mvv.sh` (install, init, store, search; T2
`224-research-26`). No Windows script exists yet. ijue9.16 is rewritten for it;
the script `post-publish-dispatch-check.sh` is gone. In a native Windows Claude
Code session it asserts:

1. `nx init --service` installs the published `windows-x64` assets and the
   supervisor starts the engine and PostgreSQL;
2. an MCP tool round trip from the session: a store, then a search, returns the
   stored text. This is the link ijue9.2 records as unproven (T2
   `224-research-27`);
3. the hook launcher chosen under nexus-efk2h runs when Claude Code invokes it.
   What the gate observes to know a hook fired is chosen with that launcher: no
   file in the repository names one for Windows (inferred, not read);
4. `nx daemon service stop --with-pg` from the same Windows session leaves no
   supervisor, engine or postgres process, confirmed by process exit, and the
   next start's `pg.log` shows no crash recovery;
5. a plain `claude -p` returns rather than hanging (Sam, 2026-10-05; ijue9.16
   item 5; T2 `224-decisions`).

Non-vacuity: the script counts the assertions it ran and exits non-zero when that
count is below the number it declares, and when the Windows host is absent. A
skipped assertion fails; it does not pass.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Windows engine + PG bundle assets | In scope (release) | In scope | N/A (immutable tags) | In scope (cosign) | N/A |
| Autostart task | In scope (`nx daemon`) | In scope | In scope (uninstall) | In scope (`nx doctor`) | N/A |
| PostgreSQL after a host shutdown | N/A | In scope (`pg.log`) | N/A | Deferred (unmeasured, T2 `224-research-32`) | N/A |
| Windows build host (`win-release`) | Deferred | Deferred | N/A | In scope (the rehearsal workflow on develop pushes, and the pre-cut `workflow_dispatch` run of the release workflow; the runner isolation probes were deleted on 2026-10-03) | N/A |

### New Dependencies

- Build-time only: Visual Studio Build Tools 2022 (MSVC, Windows SDK),
  Strawberry Perl, win_flex_bison, meson, ninja on the Windows build host, and
  for the engine leg the VC redist folder, `dumpbin`, GraalVM 25.0.3 (fetched by
  `setup-graalvm`), about 12 GB free for the native build heap, and on the first
  run the bge model (416 MB). Python 3.13 through uv, git, `gh`, pwsh 7 and the
  Windows `tar` for the scripts. No Docker, bash or cosign preinstalled.
- Runtime: the VC++ runtime DLLs, redistributable under Microsoft's licence. Sam
  signed off the reading on 2026-10-05 (T2 `224-vcruntime-terms`,
  `224-vcruntime-terms-signoff`): app-local deployment is permitted beside the exe
  and in the PG bundle's `bin`, under eight conditions. (1) Take the four DLLs from
  the Visual Studio redist folder (`VC\Redist\MSVC\<version>\x64\Microsoft.VC143.CRT`),
  never `debug_nonredist`, never debug builds, never the build host's System32.
  (2) Ship them unmodified: no patching, rebasing or re-signing. (3) Ship them only
  as part of our program, never as a standalone asset. (4) Carry a third-party
  notice in each archive: the four DLLs are Microsoft's, are not covered by the
  AGPL, and are licensed under the Visual Studio 2022 Distributable Code terms.
  (5) A GA toolset, never preview or beta. (6) No Microsoft trademarks in names and
  no implied endorsement. (7) The AGPL is an Excluded License by definition, but the
  restriction is on distributing the source of the Distributable Code under it;
  unmodified object code beside AGPL code is aggregation. (8) Servicing is ours:
  refresh the DLLs from the current redist on every release and record the redist
  version shipped. The distribution right comes from a Visual Studio PRODUCT
  licence (Community or higher), not from Build Tools, which is a supplement. Sam
  qualifies for Community (an individual, and AGPL-3.0 is OSI-approved), so the
  identity that cuts a release with the Windows legs on must keep holding one.
  Uncertain, as the reading records: whether a notice file in an archive satisfies
  "require end users to agree", and whether a stricter reader sees the standalone
  PG bundle as distributing the runtime apart from our program.
- Possibly psutil as a Windows-only client dependency (Phase 3 decides).

## Test Plan

- **Scenario**: Windows release leg builds the engine — **Verify**: dependency
  check allows only VC++ runtime and system DLLs; embedded-resources check
  passes with one origin per native library. Status: both passed by hand on
  qwentescence at `-Ob`; the release leg builds at `-O2`, the pom default, and no
  `-O2` Windows build has been measured.
- **Scenario**: Windows PG bundle relocation — **Verify**: build prefix
  removed, `initdb`, `CREATE EXTENSION vector` and `pg_trgm`, HNSW query rows.
  Status: passed by hand from the packaged archive.
- **Scenario**: a stop, sent as `CTRL_BREAK`, in each engine phase on Windows.
  Measured outcome per phase, first with the 09-29 engine and a patched copy
  (T2 `224-research-18`, `-19`), then with the release-shaped native exe built at
  `-Ob` (T2 `224-p1.1-stop-probe`, `224-p1.1-ort-init-timeout-probe`); the
  `-O2` release exe is unmeasured:
  - migration, before the changelog lock is taken: the engine exits at once
    (149 in 0.011 s), the changelog has 0 rows, and the next boot applies every
    changeset in the changelog (508 at 2026-10-05);
  - migration, during a changeset: the engine exits at once and leaves
    `databasechangeloglock` locked. This is not a clean stop: the next boot
    waited about 300 s and failed (T2 `224-research-19`; Failure Modes). The test
    records that outcome and does not assert a clean one;
  - ONNX Runtime initialisation: the exit is deferred until initialisation ends
    (135 to 444 ms measured, inside the `OrtInitGate` wait of 3 s) and the engine
    exits 149 with no crash evidence; with the wait forced to 50 ms or 1 ms the
    timeout path fired 29 times, all exit 149 with no `hs_err`, `svm_err`, dump or
    Windows Error Reporting event. A real initialisation longer than 3 s is
    unmeasured;
  - serving: the engine exits (149 in 0.076 s), logs `shutdown_signal` and
    `service_stopped`, and the next boot is clean. The release-leg smoke asserts
    this stop (Phase 1 Step 2): exit 149 within a bound, both events in the log, a
    second boot with no new changesets that stops the same way.
- **Scenario**: `nx daemon service stop` from the same Windows session —
  **Verify**: the supervisor and the engine have both exited, confirmed by
  process exit and not by the send's return value.
- **Scenario**: clean Windows machine without the VC++ redistributable —
  **Verify**: the engine starts from the release asset. The host is a hand-run
  clean Hyper-V guest (Phase 0 Step 0.5); a loaded-module assertion on a host with
  Visual Studio is a proxy and does not prove the redistributable is absent. Not
  yet run on the packaged archives.
- **Scenario**: supervisor conformance suite on Windows — **Verify**: the same
  lifecycle assertions pass as on Linux and macOS, plus stop returning only once
  the supervisor and the engine have exited, and the lease file replaced under
  concurrent readers. It runs in the rehearsal workflow's conformance job (Phase 0
  Step 0.5), with a junit floor. Status: 74 passed, 2 skipped, 2 xfailed on real
  Windows, repeated.
- **Scenario**: Windows smoke in the release leg — **Verify**: the exe boots
  against the Windows PG bundle built in the same workflow run and handed over as
  an artifact. Status: passed by hand; the workflow has not run.
- **Scenario**: the Phase 5 gate on a real Windows box — **Verify**: its five
  assertions pass and its executed-assertion count equals the declared count.
- **Scenario**: Windows 11 machine with Smart App Control enforcing, fresh
  `nx init --service` — **Verify**: the engine, PostgreSQL and the extracted
  DLLs all load; no CodeIntegrity block events (IDs 3076/3077) in the event
  log. This scenario cannot show that signing works: unsigned binaries also
  passed it (Key Discoveries; T2 `224-research-14`, `-15`, `-16`). It shows only
  that an install is not blocked. No known-blocked binary was found to use as a
  canary, so signing's effect under Smart App Control is unverified (Critical
  Assumptions).
- **Scenario**: when signing is adopted (deferred, Phase 0 Step 0.2), a release
  leg with one shipped DLL left unsigned — **Verify**: the signature check fails
  the leg.

## Validation

### Testing Strategy

The Minimum Viable Validation is the acceptance proof. The Windows release leg
and the Phase 5 gate are the recurring proofs.

### Performance Expectations

Measured on qwentescence. The 09-29 spike: native build about 80 s, exe 143 MB,
health in about 3 s after start. The later builds at `-Ob` (the release leg builds
at `-O2`, the pom default, which is unmeasured, as are its build time, its peak
memory against the 12.5 GB cited here and its size): build 1 min 24 s, exe 127 MB
(127,148,032 bytes), engine archive 33,687,976 bytes (32.1 MiB), PG bundle archive
7,407,044 bytes. Boot to ready 0.96 to 1.06 s warm and 3.1 s on the first start
after a fresh model path with a migration; 508 changesets in about 1.5 s; a
serving stop in 0.07 to 0.08 s. The 55 MiB ceiling `check_engine_cut_riders.py`
holds the Windows archive to is an estimate. No further targets.

## Finalization Gate

### Contradiction Check

Gate round 1 (`nexus_rdr/224-gate-critique-2026-10-05-r1`) found
contradictions, and this revision changes them: signing was mandatory in the
design and optional in Phase 0 (it is now conditional on Phase 0 Step 0.2); the
stop design addressed the engine and not the supervisor that is signalled (now
`CTRL_BREAK` to the supervisor, which forwards it); and the Phase 1 smoke needed
the Phase 2 bundle (Phase 2 Step 1 now lands first). Decisions left open are in
Phase 0.

### Assumption Verification

Critical Assumptions 1-5 are verified by spikes (4 revised: four app-local
DLLs, not two, and the PG bundle needs them as well; 5 verified for
SmartScreen). The `CTRL_BREAK` stop on the engine is verified, and on the
supervisor path with stand-ins only. The stop during ONNX Runtime
initialisation and the hooks assumptions are unverified and are Prerequisites.
The signed-binaries assumption cannot be verified as written, because unsigned
binaries were not blocked in our tests. (Amendment, 2026-10-05: the stop on the
real supervisor and engine and the stop during ONNX Runtime initialisation are
now verified; the hooks, the clean-machine run and the release-workflow run
remain open. The gate text above records what the gate saw.)

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| native-image build on Windows | GraalVM 25.0.3 | Spike |
| `CREATE EXTENSION vector` on the Windows bundle | pgvector 0.8.2 | Spike |
| `OpenProcess` / exit-code liveness | Win32 API | Docs Only (Phase 3 verifies) |
| Task Scheduler per-user logon task | Windows | Verified by hand at implementation (`schtasks`, a non-administrator token; a logon trigger that fires was not run) |

### Scope Verification

The Minimum Viable Validation is in scope and is Phase 5's first run.

### Cross-Cutting Concerns

- **Versioning**: the Windows assets ride the existing engine tag
  (`engine-service-vX.Y.Z`). The engine change is a per-OS stop-signal list
  (`OrtInitGate.exitSignals`, `BREAK` on Windows only), and no HTTP route is
  added. Whether a Windows asset blocks promotion of that tag is decided and
  switchable (Phase 0 Step 0.4).
- **Build tool compatibility**: MSVC via Build Tools 2022; native-image does not
  cross-compile, so a Windows host is required.
- **Licensing**: VC++ runtime redistribution terms for app-local deployment of
  the four DLLs: signed off 2026-10-05 under eight conditions, and the releasing
  identity must hold a Visual Studio product licence (§ New Dependencies).
- **Deployment model**: `nx init --service` and
  `nx daemon service install-binary`, as on macOS and Linux.
- **IDE compatibility**: N/A.
- **Incremental adoption**: Windows stays unsupported until Phase 5's gate
  passes; phases land on develop without declaring support.
- **Secret/credential lifecycle**: signing is deferred (Phase 0 Step 0.2); when
  it is adopted, an Authenticode signing identity is required. Its key lives in a hardware or
  cloud HSM (Artifact Signing, or an OV certificate's cloud HSM, or SignPath's),
  never as a key file in CI secrets; renewal and revocation follow the chosen
  provider.
- **Memory management**: native-image build needs about 12.5 GB peak on the
  build host.

### Proportionality

Right-sized for an Architecture record that replaces an accepted direction.

## References

- Spike record: bead nexus-f9bgu comment, 2026-09-29.
- Size fix: nexus-lhr6a (`32f6987b2`), guards nexus-vwfc0 (`29653410b`),
  follow-ups nexus-zz2w7.
- Research of record: T3 `research-windows-executable-2026-09-18` (1/2, 2/2);
  T2 `nexus/windows-support-research-of-record-2026-09-18`.
- RDR-218 (`docs/rdr/rdr-218-windows-platform-support.md`), RDR-157, RDR-161.
- Source: `service/pom.xml` (`native-libs-windows`), `src/nexus/db/pg_bundle.py`,
  `src/nexus/daemon/`, `src/nexus/commands/daemon.py`, `mcpb/manifest.json`,
  `mcpb/src/bootstrap.py`, `.github/workflows/engine-service-release.yml`.

## Revision History

- 2026-09-30: created (draft).
- 2026-09-30: research round 1: Critical Assumptions 4 (revised to four
  app-local DLLs) and 5 verified by spikes on qwentescence; T2
  `224-research-1` through `-6`.
- 2026-09-30: research round 2 (signing). CORRECTION: round 1 concluded that
  Authenticode signing was deferrable because `nx install-binary` downloads
  carry no Mark of the Web. That holds for SmartScreen only. Smart App
  Control checks every executable and DLL regardless of the mark and blocks
  unsigned code, so signing is required: added Gap 6 (old Gap 6 is now Gap
  7), the signing design, Phase 0 Step 0.2's route choice, the signing
  checks, and a new unverified assumption. T2 `224-research-7` through
  `-12`; T3 `research-rdr-224-windows-signing-2026-09-30`.
- 2026-09-30: research round 3 (clean VM). The app-local runtime is confirmed
  on a clean install; the PG bundle needs the runtime DLLs too; the first
  Smart App Control test was inconclusive (no block ever produced). T2
  `224-research-13`, `-14`.
- 2026-09-30: research round 4 (Smart App Control on a consumer edition).
  Replaced the inconclusive result: with Smart App Control turned On through
  Windows Security on a clean Windows 11 Pro install, no unsigned binary was
  blocked, from the desktop or offline with fresh hashes. Gap 6 reworded from
  "blocks unsigned code" to what is measured versus documented; signing
  changed from a proven precondition to a Phase 0 decision. T2
  `224-research-15`. A follow-up browser download (Edge, real Mark of the Web)
  of a new unsigned program also ran: T2 `224-research-16`.
- 2026-10-05: Gate round 1 — BLOCKED (3 Critical, 10 Significant, 2 ship-blocker(s)); commit `c080de3d6`; critique `nexus_rdr/224-gate-critique-2026-10-05-r1`.
- 2026-10-05: Gate round 2 — PASSED (0 Critical, 2 Significant, 0 ship-blocker(s)); commit `107e0f958`; critique `nexus_rdr/224-gate-critique-2026-10-05-r2`.
- 2026-10-05: Amendment pass after Phase 0 and Phases 1 to 3 landed (source: the
  Phase 1 and 2 substantive critique, S7, `nexus_rdr/224-review-p1p2-critique`,
  and the per-bead records cited in the text). Phase 0 outcomes, all Sam's:
  P0.1 qwentescence as a self-hosted runner, label `win-release`, release legs
  only; P0.2 signing deferred to nexus-dj01b with no route chosen, so Gap 6 is an
  accepted open risk and the signing text is conditional on adoption; P0.3
  RDR-218 disposition approved (RDR-218 is `superseded`); P0.4 one archive of the
  exe and the four DLLs, Windows assets block promotion; P0.5a a Windows
  rehearsal workflow on the mac rehearsal's model; P0.5b a hand-run clean Hyper-V
  guest plus a CI module-resolution proxy; P0.6 the VC++ redistribution reading
  signed off with eight conditions and a Visual Studio product licence for the
  releaser (T2 `224-decisions`, `224-vcruntime-terms`,
  `224-vcruntime-terms-signoff`). Phase 0 also added a fifth Phase 5 assertion,
  a loud refusal of a cross-session stop, `BREAK` on Windows only, and token-file
  ACLs in the first release. Amendment to P0.4 (accepted by Sam, T2
  `224-decisions-amendments`): promotion blocks on the Windows assets only while
  the repo variable `NX_WINDOWS_RELEASE_LEGS` is on (21 assets off, 27 on).
- 2026-10-05: Corrections in the same pass. (1) The engine stop-signal set is
  per OS: `TERM INT BREAK` on Windows, `TERM INT HUP` unchanged on POSIX; `BREAK`
  is unknown to HotSpot JVMs on macOS and Linux and `HUP` is unknown on Windows,
  so `HUP` is no longer requested there (`224-p1.1-break-signal-measurement`,
  `224-p1.1-stop-probe`). (2) The changeset count is every changeset in the
  changelog, 508 effective and 512 raw on 2026-10-05, not 491. (3) The ONNX
  Runtime initialisation stop is verified on both branches (deferral, and the
  timeout path 29 times with a forced short wait), with the real-slow-init and
  `-O2` caveats kept (`224-p1.1-ort-init-timeout-probe`). (4) The VC++ terms are
  signed off, no longer "to be confirmed". (5) Extraction hygiene is a cleanup
  only, because ORT 1.20 creates the directory before it reads
  `onnxruntime.native.path` (`224-p1.3-ort-temp-sweep`). (6) The engine leg is a
  job of its own, not a matrix entry, and signs with cosign to a `.sigstore.json`
  only (`224-f9bgu9-windows-engine-leg`). (7) The Test Plan smoke shares a
  workflow run through an artifact, not one job. (8) Phase 0 Steps 0.1 to 0.5
  and the "open" passages carry their decisions, and Step 0.6 is added. (9)
  Performance: exe 127 MB and archive 32.1 MiB at `-Ob`, `-O2` unmeasured. (10)
  The Day 2 probe-workflow row named probes deleted on 2026-10-03; it now names
  the rehearsal workflow and the pre-cut dispatch run. Phase 3 facts added:
  Task Scheduler restart-on-failure fires only on a launch failure, so a launcher
  respawns the supervisor (`224-f9bgu23-autostart`); the stop channel as built,
  with a cross-session stop refused and nothing killed, an engine-only Job
  Object, and PostgreSQL outside any job and left running by a plain stop
  (`224-f9bgu17-round1`, `224-f9bgu17round2`); the SID-based service identity and
  the `nx_` plus 16 hex superuser (`224-f9bgu18-windows-pg-start`); owner-only
  DACLs; the elevated-token `initdb` `0xC0000135` hazard and its ACL grant; the
  lease retry and hard-kill fixes (`224-f9bgu19-conformance`); and upgrade
  stop-first with rollback (`224-f9bgu20-upgrade-stop`).
- 2026-10-05: Left unverified, and marked so in the text: hooks in a native
  Windows Claude Code session; the packaged archives on a clean machine; every
  Windows release-workflow step on `win-release` (none has run); a host shutdown,
  logoff or sleep with PostgreSQL running; signed binaries (deferred); a real
  logon trigger; a real engine that ignores the break.
- 2026-10-05: The Phase 1-2 review fixes landed after the pass above: the smoke's
  stop is asserted (S1), the smoke extracts with the client's ACL grant and
  compares long-form paths (S5), the probe driver is committed, `OrtTempSweep`'s
  loaded-library names are tied to the jar listing and it skips reparse points,
  the shipped VC++ DLLs are checked for a valid Microsoft signature, and the
  pre-cut guards exist (engine-release skill step 3f,
  `check_engine_release_floor.py --require-windows`). Still open: S3 (clean-guest
  candidate run, bead nexus-f9bgu.45) and S6 (the release workflow's Windows jobs
  have not run on `win-release`).
