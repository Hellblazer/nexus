---
title: "Native Windows Support: Windows x64 Engine, PostgreSQL Bundle and Client"
id: RDR-224
type: Architecture
status: draft
priority: high
author: Sam
reviewed-by: self
created: 2026-09-30
accepted_date:
related_issues: [nexus-f9bgu, nexus-ijue9, nexus-lhr6a, nexus-vwfc0, nexus-efk2h, nexus-jevq5, nexus-zz2w7]
related_rdrs: [RDR-218, RDR-157, RDR-161, RDR-197]
---

# RDR-224: Native Windows Support: Windows x64 Engine, PostgreSQL Bundle and Client

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

Drafted 2026-09-30 against develop `8889bd50d`. No product code changed.

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
  directory (`binary_install.py:574-576`, `pg_bundle.py:235-244`); whether
  Windows refuses these while a reader or a running process holds the file is
  reasoned, not run (T2 `224-research-25`).

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
allowlist regardless. Signing is the only mitigation for all of these. Whether
signing is a precondition for declaring Windows supported is open: Phase 0
Step 0.2.

#### Gap 7: No gate on real Windows hardware

RDR-218's Gap 6 carries over: declaring Windows supported obliges a
check that runs on Windows. GitHub's hosted Windows runners can build and run a
native binary, which the WSL2 design could not rely on (nested virtualisation),
but a real-session check in a native Windows Claude Code session still needs a
real Windows box. RDR-218's version of that check ran
`tests/e2e/post-publish-dispatch-check.sh`, which no longer exists: it was
deleted with the RDR-184 ledger (`CHANGELOG.md:71`, nexus-0r1uz; T2
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
  embedded twice and the 290 MB `.pdb` was embedded too.
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
  healthy in about 3 seconds, applies all 491 Liquibase changesets, and returns
  a 768-dimension bge embedding through the Windows ONNX Runtime and tokenizer
  libraries.
- **Verified** — `pg_ctl` must be run with its output redirected to a file
  (through `cmd`), not a pipe: it hands the pipe to the postgres process it
  starts, and a reader waiting for the pipe to close waits forever. The client
  starts `pg_ctl` with piped output today (`pg_provision.py:634-641`).
- **Verified** — A backslash in a pom `<buildArg>` broke a resource exclude on
  the Windows build (`.*[.]pdb` excluded, `.*\.pdb` did not). The mechanism is
  not established; the pom now forbids backslashes in build args (nexus-vwfc0).
- **Documented** (source reading, audit) — The engine does not manage
  PostgreSQL itself (it connects over JDBC), uses no Unix sockets, no POSIX
  file permissions and no process spawning. Its only Windows gap is shutdown
  (Gap 4). `OrtInitGate` installs TERM, INT and HUP handlers; HUP throws on
  Windows and is caught, so startup is safe (observed in the spike's log).
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
  changelog rows, and the next boot applied all 491. T2 `224-research-17`,
  `224-research-18`.
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
  found without them). Microsoft's redistribution terms for app-local
  deployment are still to be confirmed (§ New Dependencies).
- [x] A binary fetched by `nx daemon service install-binary` carries no Mark of the Web, so
  SmartScreen does not screen it — **Status**: Verified — **Method**: Spike
  (no `Zone.Identifier` on a `urllib` download; positive control detected)
  plus Documented. This does not settle whether signing is needed: Smart App
  Control ignores the Mark of the Web (Gap 6), and whether signing gates the
  first release is Phase 0 Step 0.2. An earlier revision of this record drew
  the wrong conclusion; see Revision History.
- [ ] If Phase 0 Step 0.2 adopts signing: signing every PE file we ship (our
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
- [x] `CTRL_BREAK` stops a native-image engine on Windows once `BREAK` is in
  `OrtInitGate.EXIT_SIGNALS`, while serving and in the migration window before
  the changelog lock — **Status**: Verified — **Method**: Spike (T2
  `224-research-17`, `-18`). A stop during a changeset exits but leaves the
  changelog lock held (Failure Modes).
- [x] A same-session CLI can stop a supervisor, and through it an engine, with
  `CTRL_BREAK` under the topology in § Technical Design — **Status**: Verified
  with stand-ins for the supervisor and the engine, not with the real ones —
  **Method**: Spike (T2 `224-research-20`, `-21`, `-22`).
- [ ] A stop during ONNX Runtime initialisation, sent as `CTRL_BREAK`, exits
  without the o5xyx crash — **Status**: Unverified — **Method**: Spike (the
  o5xyx window probe, on Windows, driven by `CTRL_BREAK`).
- [ ] Claude Code on native Windows runs the conexus plugin's hooks once they
  are launched by something present on stock Windows (`uv run` or the `py`
  launcher, per nexus-efk2h's proposed fix) — **Status**: Unverified —
  **Method**: Spike in a native Windows Claude Code session.

## Proposed Solution

### Approach

Ship three native Windows x64 artifacts through the existing release
machinery, and port the client's process management so they run under it:

1. **Engine**: a `windows-x64` leg in `engine-service-release.yml`, built with
   GraalVM on Windows, smoked against the Windows PG bundle, and published and
   cosign-signed like the other legs.
2. **PostgreSQL bundle**: a Windows build path (meson + MSVC for PostgreSQL,
   `Makefile.win` for pgvector) producing `nexus-pg-windows-x64`, cached on
   exact inputs like the other bundles.
3. **Client**: `nx daemon service install-binary` and the supervisor learn
   Windows: platform tag, `.exe` names, identity, liveness, stop, and autostart.

Plus the engine change that lets it stop on Windows (`BREAK` added to its stop
signals) and the plugin fixes RDR-218 already identified.

### Technical Design

**Stop channel (Gap 4).** The stop signal on Windows is `CTRL_BREAK`, sent to a
process group. It reaches two processes, the supervisor first and the engine
through it, and it exists from process start, which an HTTP request does not
(§ Alternatives Considered). POSIX keeps SIGTERM.

- *Engine.* Add `"BREAK"` to `OrtInitGate.EXIT_SIGNALS`
  (`OrtInitGate.java:105`, today `TERM`, `INT`, `HUP`). The handlers install at
  `Main.java:66`, before migration, ONNX Runtime initialisation and the
  listener, so `OrtInitGate` and the shutdown hooks run as they do for SIGTERM.
  Measured: with `BREAK` added the engine ran its shutdown path in 0.08 s while
  serving; without it native-image ignores `CTRL_BREAK` (T2 `224-research-17`,
  `-18`). `installSignalHandlers` catches `IllegalArgumentException` for a
  signal name the platform does not know and logs a warning
  (`OrtInitGate.java:222-227`); whether `BREAK` is such a name on the Linux and
  macOS builds, which share this list, is inferred, not read.
- *Supervisor.* Register a `SIGBREAK` handler that sets the same
  `stop_requested` event the SIGTERM and SIGINT handlers set
  (`storage_service_daemon.py:2767-2771`). The supervisor's existing stop
  (`StorageServiceSupervisor.stop`, `:2617`) then runs as on POSIX: mark the
  lease shutting down, relinquish it, stop the engine. Its main thread must not
  block in a single long call: a CPython `SIGBREAK` handler did not run during a
  120 s sleep, a stdin read or `Event.wait`, and ran within 0.5 s inside a 1 s
  sleep loop (T2 `224-research-22`). The supervisor ticks at 1.0 s
  (`service_registry.py:72`) and polls its stop event every 0.5 s while waiting
  for readiness (`storage_service_daemon.py:182`); whether every other call in
  `start()` complies is inferred, not read. Where `_stop_service` and
  `_kill_after_readiness_failure` call `safe_killpg(pid, SIGTERM)`
  (`storage_service_daemon.py:2067`, `:1726`), Windows sends `CTRL_BREAK` to the
  engine instead (`win_job.send_ctrl_break`, `win_job.py:311`, which has no
  caller today) and keeps the `proc.wait` grace (`_GRACEFUL_STOP_TIMEOUT`,
  5.0 s, against `OrtInitGate`'s 3 s default wait, `OrtInitGate.java:102`).
- *Topology.* The supervisor is spawned with `CREATE_NEW_PROCESS_GROUP |
  CREATE_NO_WINDOW`, never `DETACHED_PROCESS`, which leaves it without a console
  to attach to. There are two spawn paths: the per-user logon task and
  `nx daemon service start` (`commands/daemon.py:603-610`, today
  `start_new_session=True`). The engine is spawned by the supervisor with
  `CREATE_NEW_PROCESS_GROUP` (`storage_service_daemon.py:1389`, today
  `start_new_session=True` and a `preexec_fn`; `isolation_popen_kwargs`,
  `process_group.py:189-205`, already returns the flag). The stopper is the `nx`
  CLI in the same Windows session: `FreeConsole`, `AttachConsole(supervisor pid)`,
  `GenerateConsoleCtrlEvent(CTRL_BREAK, supervisor pid)`, then re-attach to its
  parent console (T2 `224-research-20`, `-21`). `stop_storage_service` replaces
  its SIGTERM to the supervisor (`:3117`) with that sequence. Its direct signal
  to an engine pid for a lease with no `supervisor_pid` (`:3151`) and the process
  sweep (`sweep_matching_processes`) would need the same console attach
  (inferred, not read).
- *Confirming a stop.* The send can return success and deliver nothing (T2
  `224-research-20`), so a stop is confirmed by the target's exit, never by the
  return value; the hard kill (`KILL_SIGNAL`, `:3143`) stays as the fallback. A
  stop sent from another Windows session fails with access denied (T2
  `224-research-20`); what the CLI does then is a Phase 3 point.

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
were hand-run Hyper-V guests (`sac-test`, `sac-pro`; Key Discoveries). Which host
runs this smoke is a Phase 0 decision (Step 0.5). Two options exist: a hand-run
clean guest at release time, as `tests/e2e/mac-signed-binary-gate.sh` is for
macOS (a manual real-hardware gate, outside CI; T2 `224-research-26`), and, in
CI, an assertion that every module the running exe and `postgres.exe` load
resolves from the app-local directory. The assertion is a proxy: it does not
prove Visual Studio is absent.

**Supervisor port (Gap 3).** Each POSIX assumption gets a Windows branch in the
shared primitive (`src/nexus/daemon/service_registry.py` and the conformance
suite, per the daemon-lifecycle hot rule), never one tier's copy:

| POSIX assumption | Windows replacement |
| --- | --- |
| `os.getuid()` scoping, 21 code sites in 11 files (Phase 3 Step 2) | the user's SID or login name, one derivation used everywhere |
| `os.kill(pid, 0)` liveness | `OpenProcess` + exit-code query (ctypes), or psutil if added as a Windows-only dependency |
| SIGTERM / `killpg` stop | `CTRL_BREAK` to the supervisor, which forwards it to the engine (stop channel above); the Job Object kill as the backstop; PostgreSQL is left running by default, as on POSIX (`commands/daemon.py:909-915`), and stopped with `pg_ctl stop -m fast` under `--with-pg` |
| `ps` / `/proc` identity | process creation time + image path via the Win32 API |
| launchd / systemd autostart | a per-user Task Scheduler task at logon, run only while the user is logged on (`/IT`, the configuration spiked, T2 `224-research-21`), with restart on failure, a hidden window and no execution time limit; the last three settings are not read from any source (inferred, not read, T2 `224-research-32`) |
| `.exe`-less names, `LD_LIBRARY_PATH` | platform-derived executable names (`PgBinaries.from_dir`, `pg_provision.py:193-198`); no library-path injection (`_bundle_lib_env`, `:462`) |
| `pg_ctl start` through `run_bounded`: piped output, per-call Job Object that closes when `pg_ctl` returns | `pg_ctl` output redirected to a file, started outside the per-call Job Object; the Job Object's close would otherwise terminate the postmaster, reasoned from `process_group.py:255-266`, not run (T2 `224-research-30`) |
| cluster superuser from `USER` / `LOGNAME` | a Windows source for the same identity as the scope (`pg_provision.py:695`) |
| lease file `os.replace` under open readers; executable and bundle replaced in place on upgrade | a conformance property that replaces the lease under concurrent readers, and an upgrade that stops the service before replacing the executable or bundle; both unmeasured on Windows |
| `chmod 0o600` token files | an owner-only ACL, or a documented limitation if deferred |

**VC++ runtime.** The four app-local DLLs (`vcruntime140.dll`,
`vcruntime140_1.dll`, `msvcp140.dll`, `msvcp140_1.dll`) must sit beside the exe,
the PG bundle ships the same four in its `bin` directory, and the release leg's
dependency check runs `dumpbin /dependents` on the exe AND on every native
library it embeds, since the embedded libraries bring in DLLs the exe does not
import. How the engine asset carries them is open (Phase 0 Step 0.4): every
engine asset today is one file, `nexus-service-<platform>`, verified by one
`.sha256` and one sigstore bundle and placed as a single file
(`binary_install.py:332-338`, `:504-524`, `:565-576`; T2 `224-research-28`). A
Windows asset with DLLs beside the exe is an archive, as the PG bundle is a
`.txz` (`promote_engine_release.sh:29`), or separate assets, and either extends
`install_binary` beyond a single-file place.

**Signing (Gap 6), if Phase 0 Step 0.2 adopts it.** Every PE file (Windows
executable or DLL) we ship is Authenticode-signed with an RFC 3161 timestamp, so
the signature outlives the certificate: the engine exe, every PG bundle
executable and extension DLL, and the unsigned third-party DLLs the engine
embeds. Signing the exe does not cover the DLLs it loads, because Smart App
Control checks each file. The third-party DLLs are signed BEFORE the native
build embeds them, since the runtime extracts exactly the bytes that were
embedded; that means the build takes the DJL tokenizer DLLs from a signed copy
rather than straight from the DJL jar, and the embedded-resources checker must
see that copy as the single origin. The ONNX Runtime and VC++ runtime DLLs are
already Microsoft-signed and ship as they are. The signing route is a Phase 0
decision (Artifact Signing, SignPath Foundation, or an OV certificate with a
cloud HSM), and so is whether signing is in the first release; whichever route,
the key never sits in a CI secret file. Windows signing runs in its own job
with its own environment, not inside `build-publish`. That job declares
`environment: apple-signing` for the whole matrix (`engine-service-release.yml:231`),
and that environment's comment says "only a job that declares this environment
can read them" (`:213-215`), so a Windows signing step inside it would sit beside
the Apple secrets. The same comment judged splitting signing out of that job not
worth it for the three existing legs (`:223-230`); a Windows credential changes
that (T2 `224-research-29`).

**Runtime extraction hygiene.** ONNX Runtime's per-start temp directories
(Key Discoveries) need a fixed extraction path or a cleanup, so a long-running
install does not accumulate them.

**Release build host (Gap 1, Gap 7).** Two options, decided in Phase 0:
GitHub's hosted `windows-latest` (inside the existing trust model, billed at
twice the Linux rate), or qwentescence as a self-hosted runner. The second is
not an extension of the hellmini precedent. AGENTS.md says CI runs on
GitHub-hosted runners only, that hellmini is the only self-hosted registration,
that qwentescence is "a test host reachable by ssh, not a runner", and that the
`qwen-linux` and `gtr-windows` routes were removed on 2026-10-03
(`AGENTS.md:148-163`). Choosing qwentescence re-adds a self-hosted runner and
reverses that removal.

**CI placement (open, Phase 0 Step 0.5).** No workflow under `.github/workflows`
mentions Windows (13 files, T2 `224-research-29`), so where the supervisor
conformance suite, the bundle-script check and the relocation smoke run is
unstated. Premium runners are for release and tag artifact builds, not routine
push or pull-request CI (`AGENTS.md:138-141`). Run only at tag time, the first
run of any change to this machinery is the real tag: the first real mac signing
run failed mid-release at codesign on `engine-service-v0.1.142`, which is why
`.github/workflows/mac-signing-rehearsal.yml` exists (a rehearsal on push to
develop for path-filtered inputs, plus `workflow_dispatch`, never
`pull_request`). A Windows rehearsal modelled on it is one option; Phase 0
decides.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Windows native build | `service/pom.xml` `native-libs-windows` profile | Reuse: already selects `win-x64` / `win-x86_64` libraries. |
| Embedded-resource guard | `scripts/check_native_embedded_resources.py` (nexus-vwfc0) | Extend: a `windows-x64` platform mapping; release-leg coverage is nexus-zz2w7. |
| Windows PG bundle | `scripts/build_pg_bundle.sh` | Replace for Windows only: separate script, same outputs and cache key shape. |
| Platform tag | `src/nexus/db/pg_bundle.py` `current_platform_tag()` | Extend: add `windows-x64`. |
| Process containment | `src/nexus/util/win_job.py` | Reuse; `send_ctrl_break` (`win_job.py:311`) has no caller today and covers only the `GenerateConsoleCtrlEvent` call, the console attach is new. |
| File locking | `src/nexus/_locking.py` | Reuse (already msvcrt-aware). |
| Autostart | `src/nexus/daemon/installer.py` | Extend: a Task Scheduler implementation beside launchd/systemd. |
| Binary install | `src/nexus/daemon/binary_install.py` | Extend: Windows asset names and the asset layout (Phase 0 Step 0.4); cosign verification unchanged. |

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
measured so far left no crash recovery (T2 `224-research-19`).

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
- Negative: the engine's stop-signal list gains `BREAK`, in source the Linux,
  macOS and cloud builds share (`OrtInitGate.java:105`).
- Negative: a stop sent from another Windows session fails with access denied
  (T2 `224-research-20`).

### Risks and Mitigations

- **Risk**: Smart App Control, or an enterprise application-control policy,
  blocks unsigned code on some user machines. Documented by Microsoft; not
  reproduced in our tests on two clean Windows 11 installs.
  **Mitigation**: if Phase 0 Step 0.2 adopts signing, sign every shipped PE
  file (Gap 6). Because the block could not be reproduced, signing is not shown
  to be a precondition for a working install; whether it gates declaring
  Windows supported is a Phase 0 decision.
- **Risk**: if signing is adopted, the signing identity takes weeks to validate
  and is on the critical path.
  **Mitigation**: choose the route and start validation in Phase 0.
- **Risk**: a new publisher still sees SmartScreen's "unrecognized" warning on
  browser downloads until reputation accrues (EV no longer helps).
  **Mitigation**: the supported path (`nx daemon service install-binary`) writes
  no Mark of the Web, so SmartScreen does not screen it; keep the publisher identity
  stable across releases so reputation accumulates.
- **Risk**: enterprise application-control policies block nexus regardless.
  **Mitigation**: document the publisher to allowlist; not otherwise
  solvable.
- **Risk**: VC++ runtime missing on user machines.
  **Mitigation**: the four app-local DLLs beside the exe (Critical Assumption 4,
  verified; the asset layout is Phase 0 Step 0.4), with a dependency check that
  covers the embedded native libraries, since those import two of the four.
- **Risk**: the Windows build host is a persistent machine inside the release
  trust boundary (if qwentescence is chosen).
  **Mitigation**: none exists today. hellmini is the only self-hosted
  registration (`AGENTS.md:160-163`), so qwentescence would be a new runner. The
  rule to apply by analogy is hellmini's: release legs only
  (`AGENTS.md:151-153`), and never `pull_request`
  (`.github/workflows/mac-signing-rehearsal.yml:18`).
- **Risk**: the supervisor port regresses POSIX behaviour.
  **Mitigation**: the port lands in the shared primitive behind the existing
  conformance suite, which keeps running on Linux and macOS.

### Failure Modes

- The engine exe does not start: the supervisor reports the child's exit code
  and stderr, as on POSIX; a missing VC++ runtime shows as a loader error
  naming the DLL.
- PostgreSQL fails to start: `pg_ctl`'s log file (never a pipe, which the
  Phase 3 start path must stop using) carries the reason.
- A stop does not make the supervisor or the engine exit within the grace: the
  send can return success and deliver nothing (T2 `224-research-20`), so the
  stop is confirmed by process exit, and the supervisor falls back to the Job
  Object, which kills the process tree; the event is logged so an unclean stop
  is visible, not silent.
- A stop is sent from another Windows session: `AttachConsole` fails with access
  denied (T2 `224-research-20`) and the stop does not happen.
- A stop arrives during a Liquibase changeset: the engine exits at once and
  leaves `databasechangeloglock` locked, and the next boot waited about 300 s
  and then failed (T2 `224-research-19`; that record does not say whether a
  supervisor was involved). POSIX
  SIGTERM has the same effect, so this is shared with POSIX and is not a Windows
  port defect. The supervisor releases a stale lock before each spawn of a
  bundled cluster (`storage_service_daemon.py:1543-1557`, called at `:2205`);
  whether that recovers a supervised Windows boot is not measured (T2
  `224-research-31`). Fixing it is outside RDR-224; tracked separately.
- The host shuts down, the user logs off or sleeps with PostgreSQL running: a
  user-launched `postgres.exe` may get no `pg_ctl stop`, and the next start may
  run crash recovery. Not measured (T2 `224-research-32`).

## Implementation Plan

### Prerequisites

- [ ] The remaining Critical Assumptions verified: the stop on the real
  supervisor and engine (verified so far with stand-ins), a stop during ONNX
  Runtime initialisation, and hooks in a native Windows Claude Code session
- [ ] The Phase 0 signing decision recorded; if signing gates the first
  release, the route chosen and its identity validation complete
- [ ] Phase 0 decisions recorded

### Minimum Viable Validation

On a clean Windows 11 x64 machine with no developer tools: `nx init --service`
installs the published `windows-x64` engine (`commands/init.py:229`) and PG
bundle (`:563`); `nx daemon service install-binary <tag>` alone installs only
the engine (`commands/daemon.py:769`). The supervisor starts both, a
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

#### Step 0.5: Windows CI placement and clean-machine host

Decide where the supervisor conformance suite, the bundle-script check and the
relocation smoke run, and which host runs the clean-machine smoke (Technical
Design, Release build host and CI placement).

### Phase 1: Engine

#### Step 1: Windows stop signal (Gap 4)

Add `"BREAK"` to `OrtInitGate.EXIT_SIGNALS` (`OrtInitGate.java:105`); prove it
with the o5xyx window probe on Windows, driven by `CTRL_BREAK`, in each phase the
Test Plan names: migration, ONNX Runtime initialisation, serving.

#### Step 2: Windows release leg (Gap 1, Gap 6)

Add `windows-x64` to the native matrix: build, a Windows binary-dependency
check (only the VC++ runtime and system DLLs may be imported), the
embedded-resources check, cosign signing, and the four app-local VC++ runtime
DLLs with the asset (layout: Step 0.4). The dependency check covers the embedded
native libraries as well as the exe.

The smoke is a new Windows script. `service/native-smoke.sh` starts a throwaway
pgvector container with `docker run` (`:38-41`), and that a GitHub-hosted Windows
runner cannot run Linux containers is inferred, not read (T2 `224-research-29`).
It boots the exe against the Windows PG bundle, so Phase 2 Step 1 lands before
this smoke runs, and the bundle is built or fetched inside the same job.
`build-publish-pg-bundle` needs only `create-release`
(`engine-service-release.yml:788`), so that ordering needs no change to the
existing legs.

If Phase 0 Step 0.2 puts signing in the first release: Authenticode-sign the DJL
tokenizer DLLs before the native build and the exe after it, in a job and
environment of their own (Technical Design, Signing), and add a check that fails
the leg if any shipped PE file lacks a valid signature
(`Get-AuthenticodeSignature` / `signtool verify /pa`).

### Phase 2: PostgreSQL bundle (Gap 2)

#### Step 1: Windows bundle build script and relocation smoke

As § Technical Design; cached on exact inputs (version pins plus the script's
hash), per CI Cost Discipline. If Phase 0 Step 0.2 puts signing in the first
release, every executable and DLL in the bundle is Authenticode-signed before it
is packaged.

#### Step 2: Release and cache-seed legs

Add `windows-x64` to the PG-bundle release matrix and to
`pg-bundle-cache-seed.yml`, and to the release promotion per Step 0.4.

### Phase 3: Client (Gap 3)

#### Step 1: Platform tag and binary install

`current_platform_tag()` returns `windows-x64`; `nx init --service` and
`nx daemon service install-binary` fetch the Windows assets.

#### Step 2: Supervisor port

The table in § Technical Design, in the shared primitive, with the conformance
suite extended to Windows. The port covers:

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
  the per-call Job Object, with `.exe` names and a Windows superuser source;
- conformance properties: stop returns only once the supervisor and the engine
  have exited, and the lease file is replaced under concurrent readers;
- an upgrade that stops the service before it replaces the installed executable
  or the PostgreSQL bundle directory (`binary_install.py:574-576`,
  `pg_bundle.py:235-244`).

#### Step 3: Autostart

A Task Scheduler implementation in `installer.py`, with the task settings in
§ Technical Design: logged-on user only, restart on failure, hidden window, no
execution time limit.

### Phase 4: Plugin and desktop (Gap 5)

Close nexus-efk2h (hook launcher), ijue9.20 (manifest platform gate and
`execvp`), then ijue9.21 (desktop install path).

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
   next start's `pg.log` shows no crash recovery.

Non-vacuity: the script counts the assertions it ran and exits non-zero when that
count is below the number it declares, and when the Windows host is absent. A
skipped assertion fails; it does not pass.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Windows engine + PG bundle assets | In scope (release) | In scope | N/A (immutable tags) | In scope (cosign) | N/A |
| Autostart task | In scope (`nx daemon`) | In scope | In scope (uninstall) | In scope (`nx doctor`) | N/A |
| PostgreSQL after a host shutdown | N/A | In scope (`pg.log`) | N/A | Deferred (unmeasured, T2 `224-research-32`) | N/A |
| Windows build host (if self-hosted) | Deferred | Deferred | N/A | In scope (probe workflow) | N/A |

### New Dependencies

- Build-time only: Visual Studio Build Tools 2022 (MSVC, Windows SDK),
  Strawberry Perl, win_flex_bison, meson, ninja on the Windows build host.
- Runtime: the VC++ runtime DLLs, redistributable under Microsoft's license
  (app-local deployment permitted; confirm terms before shipping).
- Possibly psutil as a Windows-only client dependency (Phase 3 decides).

## Test Plan

- **Scenario**: Windows release leg builds the engine — **Verify**: dependency
  check allows only VC++ runtime and system DLLs; embedded-resources check
  passes with one origin per native library.
- **Scenario**: Windows PG bundle relocation — **Verify**: build prefix
  removed, `initdb`, `CREATE EXTENSION vector` and `pg_trgm`, HNSW query rows.
- **Scenario**: a stop, sent as `CTRL_BREAK`, in each engine phase on Windows.
  Expected outcome per phase, as spiked with the 09-29 engine and a patched
  copy (T2 `224-research-18`, `-19`), not yet with the release engine:
  - migration, before the changelog lock is taken: the engine exits at once, the
    changelog has 0 rows, and the next boot applies all 491 changesets;
  - migration, during a changeset: the engine exits at once and leaves
    `databasechangeloglock` locked. This is not a clean stop: the next boot
    waited about 300 s and failed (T2 `224-research-19`; Failure Modes). The test
    records that outcome and does not assert a clean one;
  - ONNX Runtime initialisation: expected, not measured, to defer exit up to the
    `OrtInitGate` wait of 3 s (`OrtInitGate.java:102`) and exit without a crash
    dump (the o5xyx window probe, driven by `CTRL_BREAK`);
  - serving: the engine exits in under a second, logs `shutdown_signal` and
    `service_stopped`, and the next boot is clean.
- **Scenario**: `nx daemon service stop` from the same Windows session —
  **Verify**: the supervisor and the engine have both exited, confirmed by
  process exit and not by the send's return value.
- **Scenario**: clean Windows machine without the VC++ redistributable —
  **Verify**: the engine starts from the release asset. The host is open (Phase 0
  Step 0.5); a loaded-module assertion on a host with Visual Studio is a proxy
  and does not prove the redistributable is absent.
- **Scenario**: supervisor conformance suite on Windows — **Verify**: the same
  lifecycle assertions pass as on Linux and macOS, plus stop returning only once
  the supervisor and the engine have exited, and the lease file replaced under
  concurrent readers. Where it runs is open (Phase 0 Step 0.5).
- **Scenario**: Windows smoke in the release leg — **Verify**: the exe boots
  against the Windows PG bundle built or fetched in the same job.
- **Scenario**: the Phase 5 gate on a real Windows box — **Verify**: its four
  assertions pass and its executed-assertion count equals the declared count.
- **Scenario**: Windows 11 machine with Smart App Control enforcing, fresh
  `nx init --service` — **Verify**: the engine, PostgreSQL and the extracted
  DLLs all load; no CodeIntegrity block events (IDs 3076/3077) in the event
  log. This scenario cannot show that signing works: unsigned binaries also
  passed it (Key Discoveries; T2 `224-research-14`, `-15`, `-16`). It shows only
  that an install is not blocked. No known-blocked binary was found to use as a
  canary, so signing's effect under Smart App Control is unverified (Critical
  Assumptions).
- **Scenario**: if Phase 0 Step 0.2 puts signing in the first release, a release
  leg with one shipped DLL left unsigned — **Verify**: the signature check fails
  the leg.

## Validation

### Testing Strategy

The Minimum Viable Validation is the acceptance proof. The Windows release leg
and the Phase 5 gate are the recurring proofs.

### Performance Expectations

Measured on qwentescence: native build about 80 s; exe 143 MB; health in about
3 s after start. No further targets.

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
binaries were not blocked in our tests.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| native-image build on Windows | GraalVM 25.0.3 | Spike |
| `CREATE EXTENSION vector` on the Windows bundle | pgvector 0.8.2 | Spike |
| `OpenProcess` / exit-code liveness | Win32 API | Docs Only (Phase 3 verifies) |
| Task Scheduler per-user logon task | Windows | Docs Only (Phase 3 verifies) |

### Scope Verification

The Minimum Viable Validation is in scope and is Phase 5's first run.

### Cross-Cutting Concerns

- **Versioning**: the Windows assets ride the existing engine tag
  (`engine-service-vX.Y.Z`). The engine change is one name in the shared stop
  signal list (`OrtInitGate.java:105`), and no HTTP route is added. Whether a
  Windows asset blocks promotion of that tag is open (Phase 0 Step 0.4).
- **Build tool compatibility**: MSVC via Build Tools 2022; native-image does not
  cross-compile, so a Windows host is required.
- **Licensing**: VC++ runtime redistribution terms for app-local deployment of
  the four DLLs to confirm.
- **Deployment model**: `nx init --service` and
  `nx daemon service install-binary`, as on macOS and Linux.
- **IDE compatibility**: N/A.
- **Incremental adoption**: Windows stays unsupported until Phase 5's gate
  passes; phases land on develop without declaring support.
- **Secret/credential lifecycle**: if Phase 0 Step 0.2 adopts signing, an
  Authenticode signing identity is required. Its key lives in a hardware or
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
