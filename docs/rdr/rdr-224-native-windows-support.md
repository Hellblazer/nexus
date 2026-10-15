---
title: "Native Windows Support: Windows x64 Engine, PostgreSQL Bundle and Client"
id: RDR-224
type: Architecture
status: closed
closed_date: 2026-10-15
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
  `224-f9bgu20-upgrade-stop`). The PostgreSQL bundle swap over a running stack
  was measured later, on 2026-10-14. Until then `install-binary` placed the
  bundle archive but never extracted it, so the runs of 2026-10-12 and the
  earlier engine swap of 2026-10-14 left the running PostgreSQL in place (nexus-f9bgu.56,
  fixed in `38c0c035d`). With the fix, on the Home guest, `install-binary` went
  from `engine-service-v0.1.160` to `v0.1.159` and back over the running stack:
  each step re-extracted the bundle (the `postgres.exe` creation time moved),
  the stack was healthy 1.2 s and 1.1 s afterwards, and PostgreSQL ran no crash
  recovery (T2 `224-dll-rules-and-install-binary-proof-2026-10-14`). The two
  tags carry the same `postgres.exe`, so a swap to a different PostgreSQL
  binary is not measured.

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

Amendment 2026-10-12: all three are fixed in code and shipped (Phase 4,
"How Phase 4 ended"). The third bullet's fix does not give a Windows user a
working Desktop install: the bundle cannot be installed on the Claude Desktop
build measured on 2026-10-11, and chat and Cowork get conexus from the account
marketplace instead.

#### Gap 6: Everything we would ship for Windows is unsigned

Everything nexus would ship for Windows is unsigned today: the engine, the
whole PostgreSQL bundle, and four third-party DLLs the engine extracts at
runtime (§ Research Findings). Microsoft documents that Smart App Control (a
Windows 11 feature) checks every executable and DLL the operating system loads
and blocks unsigned code that has no cloud reputation, with no override.
Measured on 2026-10-14 on Windows 11 Home build 26300 with Smart App Control on
(T2 `224-research-35`): the install steps completed, and `nx init --service`
then failed with `WinError 4551` ("An Application Control policy has blocked
this file"). CodeIntegrity blocked the unsigned PostgreSQL bundle
(`pg_config.exe`, `initdb.exe`), two `.pyd` files of the unsigned CPython that
uv installs, and PortableGit DLLs during its self-extraction. The engine was
never reached. On 2026-10-15, on the same Home checkpoint with Smart App
Control on, Windows blocked `nx.exe` itself at install, while uv, its Python,
Claude Code and Git ran (T2 `224-sac-detection-guest-runs-2026-10-15`). Two
earlier runs in September saw no block. So three runs with Smart App Control on
gave three different outcomes, and the cause is not known (§ Key Discoveries).
A machine with Smart App Control on is an unsupported configuration (Sam's
decision). In the two runs that blocked, the blocked files included files nexus
does not ship (uv's Python, the `nx.exe` launcher), so signing nexus's own
binaries alone would not have helped there.

Since nexus-f9bgu.57 nexus detects the state: `nx init` refuses with the
install guide's message before it downloads the PostgreSQL bundle when Smart
App Control is on, and `nx doctor` has a Smart App Control row. Where Windows
has no Smart App Control (the registry value does not exist), the state is
absent and `nx doctor` shows no row. The detection is on develop and ships in
the next client release after 7.80.0, not in 7.80.0. On the 2026-10-15 run with
Smart App Control on, Windows blocked `nx.exe` before nx could run, so neither
the refusal nor the row could report there. With Smart App Control in
evaluation the candidate passed the gate 5 of 5, so the guard does not refuse
there. In a second run that day `nx.exe` ran, the reader (through the tool environment's Python) returned `on`, and `nx init` refused with the new message within a second, with no download; on the evaluation checkpoint the reader returned `evaluation`.

Enterprise application-control policies (WDAC, AppLocker) were measured on
2026-10-13 and 2026-10-15: with their default rules enforced the install fails,
and with an administrator's path rules for the install's folders under the
user's profile the gate passes 5 of 5 with nothing signed (§ Key Discoveries;
`docs/windows-install.md` lists the rules). Path rules trust whatever is placed
in those folders, and an administrator who wants publisher rules instead needs
signed binaries. Phase 0 Step 0.2 settled it on 2026-10-05: signing is deferred
(nexus-dj01b), no route is chosen and no identity validation has started, so the
first Windows release ships unsigned and Gap 6 is an accepted open risk (T2
`224-decisions`). Sam confirmed that on 2026-10-14, after the Smart App Control
result: no signing until a user requires it (nexus-dj01b comment; Step 0.2
amendment).

#### Gap 7: No gate on real Windows hardware

Decided at close, 2026-10-15 (Sam): a clean Hyper-V guest satisfies this gap's
clean-machine gate (Phase 0 Step 0.5). Physical hardware and host sleep are
accepted residuals (T2 `nexus_rdr/224-close-decisions-2026-10-15`, item 3;
nexus-f9bgu.53 comment of 2026-10-15).

RDR-218's Gap 6 carries over: declaring Windows supported obliges a
check that runs on Windows. GitHub's hosted Windows runners can build and run a
native binary, which the WSL2 design could not rely on (nested virtualisation),
but a real-session check in a native Windows Claude Code session still needs a
real Windows box. RDR-218's version of that check ran
`tests/e2e/post-publish-dispatch-check.sh`, which no longer exists: it was
deleted with the RDR-184 ledger (`CHANGELOG.md:87`, nexus-0r1uz; T2
`224-research-26`). Phase 5 redefines the check against what exists.

Every Phase 5 gate run so far was on a Hyper-V guest on one host,
qwentescence: Windows 11 Enterprise Evaluation, an unactivated consumer Pro and
a consumer Home. Phase 0 Step 0.5 chose a Hyper-V guest as the clean-machine
host. What that leaves unmeasured: host sleep (a Hyper-V guest offers no sleep
state, nexus-f9bgu.53 item 4), physical hardware, and Microsoft and domain
accounts (all runs used local accounts).

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
- Windows target: Windows 11 x64. Every measured guest is Windows 11 x64
  (builds 26100 to 26300). Windows 10 is not measured and not supported; it has
  no Smart App Control, so `nx doctor` shows no Smart App Control row there.
  Windows on ARM is out of scope: GraalVM
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
- **Verified** (gate run, 2026-10-14): with Smart App Control on, local-mode
  nexus could not be set up on this run. On the Home guest `nx-home-win11` (Windows 11 Home
  build 26300), Smart App Control was set on through
  `VerifiedAndReputablePolicyState` (2 to 1) and `CiTool --refresh`; two cold
  starts both read state 1, `SmartAppControlState` read On, and CodeIntegrity
  event 3099 logged `VerifiedAndReputableDesktop` as activated. A standard user
  followed `docs/windows-install.md` with a 7.79.0 candidate wheel. The install
  steps completed. `nx init --service` then failed: "Postgres provisioning
  failed: [WinError 4551] An Application Control policy has blocked this file".
  CodeIntegrity events 3033 and 3077 recorded 8 blocks: `pg_config.exe` and
  `initdb.exe` of the unsigned PostgreSQL bundle, started by uv's `python.exe`;
  `_multiprocessing.pyd` and `_elementtree.pyd` of the unsigned CPython uv
  installs, loaded by that `python.exe`; and PortableGit DLLs loaded by its
  `ln.exe` during self-extraction (its install still finished). The unsigned
  engine was never reached. On this run, signing the engine and the bundle
  (nexus-dj01b) would not have been enough, because uv's interpreter is
  unsigned too and was blocked as well. T2
  `224-research-35`, `224-smart-app-control-on-2026-10-14`.
- **Verified** (spike, 2026-09-30), and not explained by the result above:
  Earlier runs saw no block. On a clean Windows 11 Pro guest (build 26300,
  Hyper-V VM `sac-pro`, since removed) Sam turned Smart App Control on in
  Windows Security; CodeIntegrity logged 3099 and user-mode enforcement status
  went from 1 to 2. Then an unsigned `nexus-service.exe`, an unsigned
  `psql.exe` and a tampered `whoami.exe`, each double-clicked from the desktop
  or started from a remote session with the network off, all ran, and an
  unsigned program downloaded by Edge with a real Mark of the Web ran too. No
  CodeIntegrity block events were logged. The Enterprise Evaluation build 26200,
  with Smart App Control forced on through the registry, gave the same outcome.
  T2 `224-research-14`, `224-research-15`, `224-research-16`. The cause of the
  difference from 2026-10-14 is not known. What is measured about evaluation
  mode: the Home guest's normal checkpoint (`clean-baseline-home-2026-10-14`),
  where `VerifiedAndReputablePolicyState` is 2 (evaluation), logged no
  CodeIntegrity block events in the 7.78.0 runs (T2 `224-home-guest-2026-10-14`)
  and passed the gate 5 of 5 on published 7.79.0 and 7.80.0, so nothing the gate
  ran was blocked there (T2 `224-published-7.79.0-gate-2026-10-14`,
  `224-published-7.80.0-gate-2026-10-15`, Home row). Whether the September runs were in evaluation mode is inferred, not
  measured: the 2026-10-14 record calls them "probably evaluation mode", but
  3099 was logged and the enforcement status read 2 in September too. The two
  runs differ in edition (Pro and Enterprise
  Evaluation, against Home), in how Smart App Control was turned on (Windows
  Security or a registry value, against `VerifiedAndReputablePolicyState` plus
  `CiTool --refresh` and two cold starts), in what was blocked or not (a
  program started by double-click or a remote session, against processes
  started by, and DLLs loaded into, uv's `python.exe`), and in date. Research-15
  is superseded by research-35 as the record of what Smart App Control does to
  a nexus install.
- **Verified** (gate run, 2026-10-15): a third run with Smart App Control on
  gave a third outcome. Same Home guest and Smart App Control checkpoint as
  2026-10-14 (`clean-baseline-home-sac-on-2026-10-14`), standard user, a
  candidate wheel (7.80.0 plus the Smart App Control detection, nexus-f9bgu.57).
  The install step failed: uv 0.13.1, the tool environment's Python 3.12.15,
  Claude Code 2.1.301 and Git ran, but `nx --version` failed with "Program
  'nx.exe' failed to run: An Application Control policy has blocked this file".
  So `nx init` and `nx doctor` never ran. On 2026-10-14 `nx.exe` ran on the same
  checkpoint and the block came inside `nx init`. Three runs with Smart App
  Control on therefore had three different outcomes: in September nothing was
  blocked, on 2026-10-14 the block came inside `nx init`, on 2026-10-15
  `nx.exe` was blocked at install. Why they differ is not known (not measured
  candidates for the last two: a different launcher layout, or the reputation of
  new launcher bytes). On the evaluation checkpoint the same candidate passed the
  gate 5 of 5. In a second run that day `nx.exe` ran, the reader (through the tool environment's Python) returned `on`, and `nx init` refused with the new message within a second, with no download; on the evaluation checkpoint the reader returned `evaluation`. T2 `224-sac-detection-guest-runs-2026-10-15`.
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
- **Verified** (spike, 2026-10-13 and 2026-10-15, clean guests, standard
  account): Enterprise policies (AppLocker, WDAC) block the unsigned install
  under their default rules, and an administrator allowlist of path rules for
  the install's folders is enough: the gate passed 5 of 5 under enforced
  AppLocker executable rules and under WDAC's `DefaultWindows_Enforced` policy,
  with nothing signed. Under AppLocker, DLL rules enforced and `nx self
  install`, upgrade and removal were also measured and passed with added path
  rules, and enforced script rules stop the uv installer (nexus-f9bgu.53 item
  9). Not measured under WDAC: script enforcement on, DLL rules, update and
  removal. T2 `224-research-34`, which supersedes the policy half of
  `224-research-12`.
- **Assumed** — Defender's machine-learning checks may flag an unsigned native
  binary that extracts DLLs. Not tested. T2 `224-research-12`.

Measured at implementation, 2026-10-05, on qwentescence (native Windows 11, hand
runs, release-shaped `-Ob` native exe; nothing here ran on a workflow runner):

- **Verified** — The engine stops on `CTRL_BREAK` in every phase (T2
  `224-p1.1-stop-probe`). Before the changelog lock: exit 149 in 0.011 s, 0
  changelog rows, the next boot applied all 508. During a changeset (255 of
  508): exit 149 in 0.013 s, 254 rows, `databasechangeloglock` left locked
  (`locked=t`), and the next boot was still waiting on the lock at the probe's
  45 s cap (fixed later by nexus-8sph2, live since `engine-service-v0.1.156`;
  Failure Modes). Serving: exit 149 in 0.076 s, logging
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
  Python 3.12+'s `tarfile` directory members create an owner-only ACL (CPython
  3.12.4 and later; earlier 3.12 releases ignore the mode on Windows, and
  `requires-python` is `>=3.12`, so nothing pins the minimum) (SYSTEM,
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
  that host has the runtime system-wide. The published packaged archives then
  ran on the clean guest with no runtime installed (the packaged-archive
  assumption below; T2 `224-gate-run-7.72.1-2026-10-07`). Microsoft's redistribution terms
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
- [deferred: nexus-dj01b] Signing is deferred (Phase 0 Step 0.2 and its
  2026-10-14 amendment, nexus-dj01b). If signing is adopted: signing every PE
  file we ship (our exe, the PG bundle, and the third-party DLLs, signed before
  they are embedded) makes nexus run on a Windows 11 machine with Smart App
  Control enforcing, and the signed GraalVM exe still runs. **Status**:
  Falsified for Smart App Control, unverified for the exe. **Method**: gate run
  with Smart App Control on (T2 `224-research-35`). Signing our files would not
  have been enough on the runs that blocked: on 2026-10-14 Smart App Control
  also blocked `.pyd` files of the unsigned CPython that uv installs, and on
  2026-10-15 it blocked the `nx.exe` launcher, so an install that signed every
  file nexus ships would still have failed there. The blocking behaviour varied
  between runs (§ Key Discoveries; the September runs saw no block). A machine
  with Smart App Control on is an unsupported configuration. What remains
  checkable once a certificate exists is that signing does not break the GraalVM exe or the embedded-library
  extraction; that needs a real certificate from a trusted root (a
  self-signed certificate proves only that signing does not break the exe).
- [x] `CTRL_BREAK` stops a native-image engine on Windows once `BREAK` is a
  handled signal (Windows only, Technical Design), while serving and in the
  migration window before the changelog lock — **Status**: Verified —
  **Method**: Spike (T2 `224-research-17`, `-18`), then the release-shaped
  `-Ob` exe in all four phases (T2 `224-p1.1-stop-probe`). A stop during a
  changeset exits but leaves the changelog lock held (Failure Modes). The probe
  driver is in the repository (`scripts/engine_windows_stop_probe.py`), and the
  release-leg smoke asserts the serving-phase stop (Phase 1 Step 2).
- [waived: accepted at close 2026-10-15, see Delivered versus the Problem Statement]
  A same-session CLI can stop a supervisor, and through it an engine, with
  `CTRL_BREAK` under the topology in § Technical Design — **Status**: Partially
  verified — **Method**: Spike with stand-ins (T2 `224-research-20`, `-21`,
  `-22`), then the real client, supervisor and engine (T2 `224-f9bgu17-round1`,
  `224-f9bgu17round2`). Verified: the plain stop and `--with-pg` from a session-1
  launcher, the cross-session refusal, and a stand-in engine that ignores the
  break. The shapes below that stay open were accepted at close as residuals
  (T2 `nexus_rdr/224-close-decisions-2026-10-15`):
  (a) a stop from a visible-console `cmd.exe` or Windows Terminal window. Round 2
  ran the stop CLI under `pythonw` with a `CREATE_NO_WINDOW` console and stdout
  to files, so `AttachConsole(ATTACH_PARENT_PROCESS)` had no console parent to
  return to, and a dead standard handle would not have shown. The re-attach to a
  real parent console was shown only in the ssh conpty spike. Measured in fix
  round A (T2 `224-review-p3-fixes-a`, stand-in supervisor): from a `cmd.exe`
  console and from a Windows Terminal tab, `nx daemon service stop` printed its
  result into the console buffer (read back with `ReadConsoleOutputCharacterW`),
  exited 0, and a follow-up echo worked, both before and after the change; the
  dead-handle hazard did not reproduce. The attach and send now run in a short
  helper process (`python -m nexus.util.win_console`, `DETACHED_PROCESS`), so the
  CLI never detaches its own console. Covered with a stand-in supervisor only;
  (b) a real engine that ignores the break (stand-in only);
  (c) a logon trigger that actually fires. Measured on 2026-10-06 on the clean
  guest: after each sign-out and restart, automatic sign-in and the logon task
  brought the full stack back (nexus-f9bgu.46, T2
  `224-f9bgu46-session-end-measurement`), and since nexus-f9bgu.51 the stack
  stops cleanly at that sign-out or restart (T2 `224-f9bgu51-guest-verification`);
  (d) a same-session denial (an elevated target stopped from a non-elevated
  shell, or another user's service). Measured in round A for a High-integrity
  session-0 target stopped from a Medium visible console: REFUSED on stderr with
  the account-and-elevation remedy (the helper cannot read a High-integrity
  target's session, so the session-numbered text is not shown), exit 1, nothing
  killed. Another user's service is unmeasured;
  (e) a lease pid that is not the console group's root:
  `GenerateConsoleCtrlEvent` takes a process group id. Measured in round A: the
  lease's `supervisor_pid` is the real `python.exe`, a child of the venv
  trampoline that is the group root, and a break sent to it was delivered 3 of 3
  (exit in 0.9 to 1.1 s); the real stop CLI made one helper call, to that pid,
  and the sweep found nothing left, so the lease path delivered the break. Why a
  non-root pid works is undocumented, so a launcher layer other than the venv
  trampoline is untested.
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
  the timeout rows, because the process was gone before the hook wrote it; these
  probes ran on the `-Ob` exe, and the initialisation-phase stop of the `-O2`
  release exe and the 5 s supervisor grace against the 3 s bound are unmeasured.
  The `-O2` release exe's serving-phase stop is asserted by the release-leg smoke
  and by gate assertion 4 on every published run (Test Plan).
- [x] Claude Code on native Windows runs the conexus plugin's hooks once they
  are launched by something present on stock Windows (`uv run` or the `py`
  launcher, per nexus-efk2h's proposed fix) — **Status**: Verified (2026-10-07)
  — **Method**: Phase 5 gate assertion 3 on published conexus 7.72.1 in the
  clean guest's first, cold Claude Code 2.1.292 session: six conexus hook runs,
  none failed, `nx-hook session-start` and the `uv run ... nx_hook_shim.py`
  launcher both produced output (T2 `224-gate-run-7.72.1-2026-10-07`). On
  7.72.0 the cold session had `nx-hook preflight` cancelled at 6.0 s against
  its 5 s budget; fixed in 7.72.1 (nexus-3cydr).
  Amendment 2026-10-12: the launcher quoted above is the 7.72.1 form and no
  longer ships. Since 7.74.0 (`856223b3f`, 2026-10-07, after that gate run)
  every uv hook entry is `uv tool run --directory ${CLAUDE_PLUGIN_ROOT}
  --no-config --quiet --python >=3.12 python <script>`. Evidence for the shipped
  form: a real Claude Code 2.1.292 session on the clean guest, all hooks
  succeeded and a planted `.venv` ran 0 times (T2 `224-phase4-fix-C-windows`);
  and `tests/hooks/test_hook_launcher_uv.py` runs natively in the Windows
  rehearsal job. The Phase 5 gate has since passed 5 of 5 on the shipped form,
  on published conexus 7.78.0, 7.79.0 and 7.80.0 (T2
  `224-gate-run-7.78.0-2026-10-12`, `224-published-7.79.0-gate-2026-10-14`,
  `224-published-7.80.0-gate-2026-10-15`); since 7.79.0 the hooks start through
  RDR-228's `launch/nx_launch.py` under uv.
- [x] The packaged engine archive and PG bundle run on a clean Windows machine
  with no VC++ redistributable — **Status**: Verified (2026-10-06/07) —
  **Method**: the clean guest `nx-clean-win11` (Phase 0 Step 0.5): the
  candidate-asset run (T2 `224-f9bgu45-clean-guest-candidate-run`), then Phase 5
  gate precondition P0 (no `vcruntime140*`/`msvcp140*` outside the .NET
  `_clr0400` copies, no VC++ runtime keys or uninstall entries) and assertion 1,
  which installed the published `engine-service-v0.1.149` windows-x64 archives
  on both init paths and served from them (T2 `224-gate-run-7.72.1-2026-10-07`).
  Earlier text, kept for the record: the hand-run clean guest was the method. The clean-VM spike (above) used loose
  files, not the packaged archives, and the smokes ran on a host that has the
  runtime system-wide; the module-resolution check proves the shipped copies
  were used, not that the system copies are absent.
- [x] The Windows release-workflow jobs (cosign, upload, cache restore, the
  cross-job bundle artifact, promotion against a 27-asset release) run on
  `win-release` — **Status**: Verified — **Method**: the
  `engine-service-v0.1.149` tag ran them: its published, non-draft release
  carries `nexus-service-windows-x64.txz` and `nexus-pg-windows-x64.txz` with
  their `.sha256` and `.sigstore.json` files, and `nx init --service` on the
  clean guest downloaded and signature-verified both (gate assertion 1; T2
  `224-engine-cut-v0.1.149`, release run 37428611456). The method first planned
  was a `workflow_dispatch` run of the release workflow before the first cut
  (Phase 0 Step 0.5).
- [waived: accepted at close 2026-10-15, see Delivered versus the Problem Statement]
  A host shutdown, logoff or sleep with PostgreSQL running recovers cleanly
  on the next start — **Status**: Partially verified — **Method**: measured on
  the clean guest (nexus-f9bgu.46, fixed by nexus-f9bgu.51; T2
  `224-f9bgu51-guest-verification`): sign-out and restart with the stack running
  stop the engine and PostgreSQL cleanly (`database system is shut down`,
  `postmaster.pid` removed), and the next boot starts with no crash recovery.
  Sleep is unmeasured (T2 `224-research-32`): a Hyper-V guest offers no sleep
  state, so it needs a physical machine (nexus-f9bgu.53 item 4). Sam accepted it
  at close as a residual (T2 `nexus_rdr/224-close-decisions-2026-10-15`).

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
  `224-f9bgu17round2`). The same refusal covers a same-session denial (an
  elevated target stopped from a non-elevated shell, or another user's service):
  the text then names no session and says to run the stop as the account, and with
  the elevation, that started the service (`_refused_stop_lines`). That case is
  unmeasured.

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
used, 6 s; T2 `224-f9bgu12-windows-pg-bundle`), and the published packaged
archives ran on the clean guest with no VC++ runtime installed in every Phase 5
gate run since 2026-10-07 (precondition P0 and assertion 1; T2
`224-gate-run-7.72.1-2026-10-07`). The smoke must extract the bundle the way the
client does (with the user-tree ACL grant of Key Discoveries), or it can fail
for a reason the client path does not share, or pass without proving it.

**Supervisor port (Gap 3).** Each POSIX assumption gets a Windows branch in the
shared primitive (`src/nexus/daemon/service_registry.py` and the conformance
suite, per the daemon-lifecycle hot rule), never one tier's copy:

| POSIX assumption | Windows replacement |
| --- | --- |
| `os.getuid()` scoping, 21 code sites in 11 files (Phase 3 Step 2) | the user's SID, through one function, `service_identity()` in `service_registry.py`: `str(os.getuid())` on POSIX, unchanged, and the SID string on Windows (anything that is not `S-1-...` raises `ServiceIdentityError`). The SID, not the login name, because it is stable across account renames, cannot be set through an environment variable, and its characters are safe in file, lock and endpoint names. `tests/test_service_identity_lint.py` fails on any other `os.getuid()` |
| `os.kill(pid, 0)` liveness | `OpenProcess` + `WaitForSingleObject(handle, 0)` through ctypes, no psutil (`pid_alive` in `service_registry.py`, nexus-f9bgu.25) |
| SIGTERM / `killpg` stop | `CTRL_BREAK` to the supervisor, and `CTRL_BREAK` from the supervisor to the engine (stop channel above, as built); the engine-only Job Object kill as the backstop; PostgreSQL is left running by default, as on POSIX (`commands/daemon.py:909-915`), and stopped with `pg_ctl stop -m fast` under `--with-pg` |
| `ps` / `/proc` identity | process creation time + image path via the Win32 API (done, through one stdlib-only core at every `ps` and `/proc` site) |
| launchd / systemd autostart | a per-user Task Scheduler task at logon, run only while the user is logged on (`/IT`, the configuration spiked, T2 `224-research-21`), with restart on failure and no execution time limit, and the task's `Hidden` flag left false; the settings are not read from any source (inferred, not read, T2 `224-research-32`). Measured at implementation (nexus-f9bgu.23, 2026-10-05, T2 `224-f9bgu23-autostart`): the task's restart-on-failure fires only when the task's program fails to launch, never on a non-zero exit (exit codes 1, 3, 255, 0x80070005, 0xC0000005 and 0xFFFFFFFF did not restart), so the task runs a launcher that stays as the task's process and respawns the supervisor 30 s after any non-zero exit; no window opens because the launcher runs under `pythonw` and spawns with `CREATE_NO_WINDOW`; the task's `Hidden` flag only hides the task in the Task Scheduler UI, and the XML sets it false |
| `.exe`-less names, `LD_LIBRARY_PATH` | platform-derived executable names (`PgBinaries.from_dir`, `pg_provision.py:193-198`); no library-path injection (`_bundle_lib_env`, `:462`) |
| `pg_ctl start` through `run_bounded`: piped output, per-call Job Object that closes when `pg_ctl` returns | `pg_ctl` started detached: a plain `Popen` that never goes through `run_bounded` or `contain`, so no per-call Job Object, with `CREATE_NEW_PROCESS_GROUP`, stdin from `DEVNULL` and output to `pgdata/pg_ctl.out`; `PgStartError` names `pg.log` and `pg_ctl.out` with credential-scrubbed tails. The postmaster's own process group is what keeps a plain stop from reaching it: a plain stop left it serving across two stops (T2 `224-f9bgu18-windows-pg-start`, `224-f9bgu17round2`) |
| cluster superuser from `USER` / `LOGNAME` | derived from the same SID as the scope: `nx_` plus the first 16 hex characters of the SHA-256 of the SID string (`windows_superuser_name`; `bootstrap_superuser` uses `service_identity()` on Windows), so two accounts on one machine get distinct superusers |
| lease file `os.replace` under open readers; executable and bundle replaced in place on upgrade | both fail on Windows (Key Discoveries). The lease read, replace and unlink retry a sharing violation for up to 2 s (2 to 10 ms backoff, Windows only, in the shared primitive), and a conformance property replaces the lease under concurrent readers. Upgrade stops first: `quiesced()` stops the service (and PostgreSQL only for a bundle swap with a cluster up), the file set is placed as one unit (`place_set_with_rollback`: each file one atomic `os.replace`, DLLs first and the exe last, bounded retry with 5 sleeps of 0.1 to 2 s, every file restored if any step fails), then it restarts only what it stopped, on failure too. A refused or surviving pid raises `ReplaceBlockedError` naming the session, kills nothing and replaces nothing (T2 `224-f9bgu20-upgrade-stop`, `224-f9bgu19-conformance`) |
| `chmod 0o600` token files | decided: hardened in the first release, not deferred. `nexus._winsec` (`open_private`, `restrict_to_owner`, `ensure_owner_only`, `owner_only_problem`) installs a protected DACL with one ACE, full control for the current user's SID, before any secret byte is written (a rename keeps the source's descriptor, so the temp file gets it). The reader tolerates SYSTEM, Administrators and OWNER RIGHTS and refuses any other trustee. The plugin's hook scripts cannot import nexus, so they carry a mirror that `tests/test_winsec.py` pins to the original |
| package upgrade: `uv tool upgrade conexus` rewrites the venv in place; `nx self install` runs bash; `current` is a symlink; shims are `#!/bin/sh` files | measured on qwentescence (nexus-f9bgu.47): upgrading over a running `nx-mcp` deletes site-packages, then fails on the locked `Scripts\python.exe` (os error 5), leaving `nx` broken until every process exits and it is reinstalled; a running uv launcher can be neither renamed, replaced nor deleted. Replacement: `nx self install` on Windows runs `_install/generation_core.py` (stdlib-only, no bash): each upgrade builds a new generation venv beside the running one and flips a `current` directory junction; each generation carries `<gen>\bin` with copies of its own launchers, and the user PATH (HKCU) holds `tools\current\bin` ahead of uv's bin directory, so no running file is ever replaced and a process started through it reports its real generation as `sys.prefix`. Migration from a uv tool install is resumable; GC compares folded paths and keeps a generation it cannot fully delete. POSIX and the `.sh` scripts are unchanged (T2 `224-f9bgu47-upgrade-measurement`, `224-f9bgu47-windows-shims-decision`, `224-f9bgu47-windows-e2e`) |

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
(nexus-dj01b), and Sam confirmed on 2026-10-14 that there is no signing until a
user requires it (Step 0.2 amendment). The first Windows release ships unsigned
and this is the design for when signing is adopted. It covers the files nexus
ships and nothing else, so it would not have made the measured Smart App
Control runs work: on 2026-10-14 the unsigned CPython that uv installs was
blocked too, and on 2026-10-15 the `nx.exe` launcher (T2 `224-research-35`,
`224-sac-detection-guest-runs-2026-10-15`). What it would serve is an administrator who wants publisher
rules instead of path rules under AppLocker or WDAC. Every PE file (Windows
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
T2 `224-review-p1p2-critique`, S6). The release workflow's Windows jobs first ran
on `win-release` for the `engine-service-v0.1.149` cut on 2026-10-06, which
published all 27 assets (T2 `224-engine-cut-v0.1.149`).

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
class, and a dependency on WSL behaviour Microsoft does not contract. Native
did not by itself remove crash recovery at session end: measured on the clean
guest, sign-out and restart killed the engine and crashed PostgreSQL
(nexus-f9bgu.46). The supervisor now stops both cleanly at sign-out and restart,
and the next boot runs no crash recovery (nexus-f9bgu.51, T2
`224-f9bgu51-guest-verification`). Sleep is unmeasured (T2 `224-research-32`). Engine stops
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

- **Risk**: Smart App Control blocks unsigned code on user machines where it is
  on. Measured 2026-10-14: `nx init --service` fails, and the blocks include the
  unsigned CPython that uv installs as well as the PostgreSQL bundle (T2
  `224-research-35`).
  **Mitigation**: detection, not a fix (nexus-f9bgu.57). `nx init` reads the
  state (`VerifiedAndReputablePolicyState` under
  `HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy`) and, when it is on, refuses
  with the install guide's message before it downloads the PostgreSQL bundle.
  `nx doctor` has a Smart App Control row on Windows: on is a failure in local
  mode and a warning in cloud mode (its effect there is not measured);
  evaluation is a warning, because Windows can switch it on later (expected,
  not observed; the warning can only speak before the switch, nexus-f9bgu.58);
  a state that cannot be read is a warning; off passes; where the registry
  value does not exist (Windows 10, older Windows 11) there is no row. The
  detection is on develop and ships in the next client release after 7.80.0,
  not in 7.80.0. On the Home guest on 2026-10-15, with Smart App Control on,
  Windows blocked `nx.exe` at install, so neither the refusal nor the row could
  report there (T2 `224-sac-detection-guest-runs-2026-10-15`); on 2026-10-14
  `nx.exe` still ran and the block came inside `nx init`, and why the two runs
  differ is not known. With Smart App Control in evaluation the candidate passed
  the gate 5 of 5, so the guard does not refuse there. In a second pair of runs the same day, the reader, run through the tool environment's Python, returned `on` on the Smart App Control checkpoint and `evaluation` on the other. On the Smart App Control checkpoint `nx.exe` ran that time, and `nx init` refused with the new message within a second, with no download and no `WinError 4551`. So the refusal and the reader work on real Windows, and whether Windows blocks `nx.exe` itself varied between two runs on the same checkpoint. A machine
  with Smart App Control on stays an unsupported configuration, and
  `docs/windows-install.md` says so. Signing nexus's binaries alone would not
  have fixed the measured runs, and Sam decided on 2026-10-14 that
  there is no signing until a user requires it (Phase 0 Step 0.2 amendment,
  nexus-dj01b).
- **Risk**: when signing is adopted, the signing identity takes weeks to
  validate and is on the critical path.
  **Mitigation**: choose the route and start validation when signing is adopted;
  nothing has started.
- **Risk**: a new publisher still sees SmartScreen's "unrecognized" warning on
  browser downloads until reputation accrues (EV no longer helps).
  **Mitigation**: the supported path (`nx daemon service install-binary`) writes
  no Mark of the Web, so SmartScreen does not screen it; keep the publisher identity
  stable across releases so reputation accumulates.
- **Risk**: enterprise application-control policies (AppLocker, WDAC) block
  nexus under their default rules (measured, T2 `224-research-34`).
  **Mitigation**: an administrator adds path rules for the install's folders
  under the user's profile; with them the gate passed 5 of 5 under both, with
  nothing signed. `docs/windows-install.md` lists the rules. Publisher rules
  would need signed binaries (nexus-dj01b).
- **Risk**: VC++ runtime missing on user machines.
  **Mitigation**: the four app-local DLLs beside the exe, in one archive (Critical
  Assumption 4, verified; Phase 0 Step 0.4), with a dependency check that covers
  the embedded native libraries, since those import two of the four. The
  published archives ran on the clean guest with no VC++ runtime in every Phase 5
  gate run (precondition P0; T2 `224-gate-run-7.72.1-2026-10-07`).
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
- A stop is sent from another Windows session, or is denied for another reason
  (an elevation or user mismatch; unmeasured): `AttachConsole` fails with access
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
  cap (T2 `224-p1.1-stop-probe`). It was fixed outside RDR-224 by nexus-8sph2,
  closed and live since `engine-service-v0.1.156`: the engine's schema migrator
  holds a session-level advisory lock for the walk and releases a stale
  `databasechangeloglock` row it finds under that lock (`ebd9c4deb`).
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
- The host restarts or the user signs out with the stack running: measured on
  the clean guest, this killed the engine and crashed PostgreSQL, and the next
  start ran crash recovery after a stall of about 33 s (nexus-f9bgu.46). The
  supervisor now handles `WM_ENDSESSION`, stops the engine and stops PostgreSQL
  fast in-process, and the next boot runs no crash recovery (sign-out 0.46 s,
  restart 0.20 s; nexus-f9bgu.51, T2 `224-f9bgu51-guest-verification`). Host
  sleep is unmeasured (T2 `224-research-32`).
- Windows switches Smart App Control from evaluation to on after nexus is
  installed: expected, not observed (bead nexus-f9bgu.58 measures it). The
  expected effect, from the 2026-10-14 and 2026-10-15 runs with it on (T2
  `224-research-35`, `224-sac-detection-guest-runs-2026-10-15`), is that the
  unsigned PostgreSQL bundle, uv's unsigned Python or `nx.exe` itself are
  blocked at the next start, so the local stack stops working. `nx doctor` warns
  while the state is evaluation (nexus-f9bgu.57), so the warning can only speak
  before the switch; after it, `nx.exe` may not run at all.

## Implementation Plan

### Prerequisites

- [x] The remaining Critical Assumptions verified: hooks in a native Windows
  Claude Code session, the packaged archives on a clean machine, and the
  Windows release-workflow jobs on `win-release` (§ Critical Assumptions;
  verified 2026-10-07 by the Phase 5 gate on published 7.72.1). The
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
only. Sign-out and restart were measured apart from it (nexus-f9bgu.46, .51; no
crash recovery since .51); host sleep is unmeasured (T2 `224-research-32`).

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
it. At the time of the decision the documented Smart App Control block had not
been reproduced (T2 `224-research-15`); it was reproduced on 2026-10-14 (see
the amendment below). Either way, choose the route: Microsoft Artifact Signing (if
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

**Amendment, 2026-10-14 (Sam):** no signing until a user actually requires it
(nexus-dj01b comment, 2026-10-14). The comment's basis has three parts. Smart
App Control on blocks uv's unsigned Python whatever nexus signs, so signing
would not make such a machine work: measured (T2 `224-research-35`). "Evaluation
mode blocks nothing": this rests on the Home guest's evaluation-state runs. The
state value on its normal checkpoint is 2. Before the comment, the 7.78.0 runs
there logged no CodeIntegrity block events (T2 `224-home-guest-2026-10-14`) and
a candidate wheel passed the gate 5 of 5 (nexus-f9bgu.53 item 2); since then the
gate passed there 5 of 5 on published 7.79.0 and 7.80.0 (T2
`224-published-7.79.0-gate-2026-10-14`, `224-published-7.80.0-gate-2026-10-15`,
Home row). That the September runs were in evaluation mode is inferred, not
measured (Key Discoveries). AppLocker works
with path rules: measured (T2 `224-research-34`). A
signature matters only for publisher-rule allow lists, which were not measured,
so signing is revisited when a user reports needing one. (The comment also named
WDAC as unmeasured; WDAC was measured the next day and passed with path rules and
nothing signed, T2 `224-research-34`.) A machine with Smart App
Control on is an unsupported configuration. Its blocking behaviour varied
between runs (Key Discoveries), and in the runs that blocked, signing nexus's
binaries alone would not have fixed it. nexus-dj01b stays deferred.

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
proxy that asserts app-local module resolution. The guest half has covered the
published packaged archives in every Phase 5 gate run since 2026-10-07 (Critical
Assumptions). Later gate runs added two more Hyper-V guests on the same host,
consumer Pro and consumer Home (T2 `224-pro-guest-gate-2026-10-13`,
`224-home-guest-2026-10-14`).

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
The workflow job ran on `win-release` for the `engine-service-v0.1.149` cut and
published the archive, 39.6 MiB against the 55 MiB ceiling (T2
`224-engine-cut-v0.1.149`).

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
it in 6 s. The release job ran on `win-release` for the `engine-service-v0.1.149`
cut and published `nexus-pg-windows-x64.txz` (T2 `224-engine-cut-v0.1.149`);
whether that job restored the prefix from the cache is not recorded. Authenticode signing of the bundle is
not in the first release (Phase 0 Step 0.2).

#### Step 2: Release and cache-seed legs

Add `windows-x64` to the PG-bundle release and to `pg-bundle-cache-seed.yml`
(as a job of its own on `win-release`, behind the variable), and to the release
promotion per Step 0.4. As built, with the variable on, promotion expects 27
assets and the Windows jobs must succeed; with it off, 21 and neither is required.
The workflow YAML is covered by `actionlint` and parsing tests (T2
`224-f9bgu14-windows-release-legs`). The release legs first ran for
`engine-service-v0.1.148`, whose Windows PG job failed in the cosign installer
(left a draft), and then for `engine-service-v0.1.149`, which published all 27
assets and was promoted (T2 `224-engine-cut-v0.1.149`). A run of
`pg-bundle-cache-seed.yml`'s Windows job is not recorded in the sources cited
here.

### Phase 3: Client (Gap 3)

#### Step 1: Platform tag and binary install

`current_platform_tag()` returns `windows-x64` (done); `nx init --service` and
`nx daemon service install-binary` fetch the Windows assets. `engine-service-v0.1.149`
published the first Windows assets on 2026-10-06 (T2 `224-engine-cut-v0.1.149`),
and every Phase 5 gate run since installs them through `nx init --service`.
`install-binary` with the published assets was measured on the guest on
2026-10-12 and 2026-10-14 (nexus-f9bgu.53 item 1). Those runs found that it
never extracted the PostgreSQL bundle it installed (nexus-f9bgu.56); since the
fix it does, including over a running stack (Gap 3).

#### Step 2: Supervisor port

The table in § Technical Design, in the shared primitive, with the conformance
suite extended to Windows. As built, every item below landed on develop: the 22
sites (see the list) call `service_identity()` and a lint rejects any other `os.getuid()`; the
conformance properties passed on real Windows (74 passed, 2 skipped, 2 xfailed); a
junit floor in the rehearsal job keeps a vacuous run from passing. Two siblings
found along the way were fixed with them: the session sweep's orphan-tracker kill
and the plugin's lease read (T2 `224-f9bgu19-conformance`,
`224-f9bgu44-windows-tests`). The orphan-tracker fix is unreachable from the
production sweep on Windows (`sweep_orphan_resource_trackers` returns 0 before it
lists anything, T2 `224-f9bgu44-windows-tests` item 6); it serves direct callers
only. The port covers:

- the `os.getuid()` code sites: 21 were listed at planning and implementation
  found a 22nd (`hooks/mailbox_drain.py:1191`, hidden behind a `getattr`). The
  lint also keeps six raw `os.getuid()` sites exempt, none reachable on Windows
  (five `launchctl` domain strings in `installer.py` and one `pwd` lookup in
  `db/onnx_model_root.py`), besides the helper's own two. The planning list, in 11
  files: `health.py:3546`,
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
  `pg_bundle.py:235-244`). At implementation this ran against a stand-in
  executable only (T2 `224-f9bgu20-upgrade-stop`); the real bundle swap over a
  running stack was measured on 2026-10-14, after the nexus-f9bgu.56 fix (Gap
  3). A placement that succeeds sweeps the `.nx_old_*` keep directories earlier
  blocked upgrades left behind;
- the aspect-worker daemon, which the Phase 3 critique found nobody had ported
  (T2 `224-review-p3-critique` S4, `224-review-p3-fixes-b`). The store path starts
  it, so the Phase 5 round trip spawns it. It is spawned with the supervisor's
  flags (its own process group, no console window), handles `SIGBREAK`, waits in
  ticks of 1 s so the handler runs, and `nx daemon restart-stale` stops it through
  `request_graceful_stop` instead of `os.kill(SIGTERM)`, which is
  `TerminateProcess` on Windows and skips its drain. `nx daemon service stop` does
  not stop it, on Windows as on POSIX (the worker belongs to the store path, not
  to the service), so assertion 4 counts only the supervisor, the engine and
  PostgreSQL. Measured on qwentescence from a session-1 launcher with a stand-in
  worker and queue: with the new flags the worker's console window handle is 0,
  where the pre-fix spawn gets a visible one; `restart-stale` stopped it in 0.54 s
  with the drain run and the lease gone; from another session the stop was
  refused and the worker kept running.

#### Step 3: Autostart

A Task Scheduler implementation in `installer.py` and `windows_autostart.py`,
with the task settings in § Technical Design: logged-on user only, no execution
time limit, `Hidden` left false (no window opens because the launcher runs under
`pythonw` and spawns with `CREATE_NO_WINDOW`, not because of the flag). The task's restart-on-failure does not restart a
supervisor that exits non-zero (Key Discoveries), so the task's process is a
launcher that respawns the supervisor 30 s after any non-zero exit and ends on
exit 0. Verified by hand on qwentescence with a stand-in supervisor: install,
idempotent re-install, uninstall, out-of-band disable and delete caught by the
doctor row, a stop from a hidden session-1 task (T2 `224-f9bgu23-autostart`). Measured
later on the clean guests: the logon trigger firing after sign-out and restart, and
the clean stop at both (nexus-f9bgu.46, .51); the real engine and PostgreSQL under
the launcher (gate assertions 1, leg A, and 4); a standard account (T2
`224-gate-run-standard-user-7.78.0-2026-10-13`). Not measured: behaviour at sleep.

### Phase 4: Plugin and desktop (Gap 5)

Close nexus-efk2h (hook launcher), ijue9.20 (manifest platform gate and
`execvp`), then ijue9.21 (desktop install path). The `win32` platform token in
`mcpb/manifest.json` stays out of develop until the Windows client release
(nexus-f9bgu.43; T2 `224-decisions-amendments`).

#### How Phase 4 ended (amendment 2026-10-12)

Source: the Phase 4 critique (T2 `224-review-p4-critique`, finding S3). Text and
evidence only; no status change.

- **Hook launcher (nexus-efk2h, closed).** Three forms were written and two shipped. `uv run
  --no-project` with a PEP 723 block (`dfa8ad852`, shipped in 7.72.0) ran a
  `.venv` planted in the project. `uv run --directory ${CLAUDE_PLUGIN_ROOT}
  --no-project` (`daa6ae73c`, never released alone) stopped that but ran a
  `.venv` in any ancestor of the plugin cache, the home folder and `C:\`
  included. The shipped form, since 7.74.0
  (`856223b3f`), is `uv tool run --directory ${CLAUDE_PLUGIN_ROOT} --no-config
  --quiet --python >=3.12 python <script>`; a tool environment never reads a
  `.venv`. The efk2h close reason still describes the first form.
- **Desktop bundle launch (nexus-ijue9.20, closed).** `os.execvp` is replaced on
  Windows by a child that inherits the stdio handles, inside a kill-on-close Job
  Object (`4aa3eb754`). The bundle launches as `uv tool run ... python
  src/bootstrap.py` and is its own uv workspace root (`fc66a9ea2`, 7.74.0).
- **Platform token.** `win32` entered `mcpb/manifest.json` with the Windows
  client release, 7.72.0 (nexus-f9bgu.43), as planned above.
- **Desktop install path (nexus-ijue9.21, closed).** `docs/windows-install.md`
  (`753079a3b`). The route changed from the plan: on Claude Desktop
  1.24012.9.0 (MSIX), measured 2026-10-11, a double-click on `conexus.mcpb` does
  nothing, because the package registers no `.mcpb` file type, and the Plugins
  upload dialog hides `.mcpb` files. Chat and Cowork get conexus from the
  claude.ai account marketplace, and the nexus server connected that way with 44
  tools, in cloud mode (T2 `nexus/desktop-install-friction-qwent-2026-10-11`;
  `bf3f12180`). In the same run `plugin:sn:serena` failed to connect
  ("Connection closed"), and the cause was not found. So
  the Windows bundle code is correct and tested, and no Windows user reaches it
  through Desktop today. A Windows run of the shipped bundle form is not
  recorded; the headless walk used the earlier `uv run` manifest (T2
  `224-windows-install-guide-walk`).

The code review (nexus-f9bgu.36; T2 `224-review-phase4-5-beads`) raised seven
findings:

| Finding | Disposition |
| --- | --- |
| A: a bare program name is resolved from the working directory on Windows | Fixed in the plugin scripts in 7.74.0 (`_exec_path.which_off_cwd`, lint `tests/test_hooks_bare_exec_lint.py`), and on 2026-10-12 in all of `src/nexus` (`7581cc834`, `7f77f6436`, `5508b2f71`, `464e848af`, `bf1293bd7`; lint `tests/test_nx_bare_exec_lint.py`). The Windows search order is simulated in those tests, not run on Windows |
| B: uv exits 2 when no interpreter is found, which blocks five hook entries | Kept as a loud block (Sam, on nexus-f9bgu.36). `conexus/README.md` names the remedy, `uv python install 3.12` |
| C: Phase 4 code ran in no Windows job, and the launcher read a planted `.venv` | Fixed: the Windows rehearsal job runs `tests/test_mcpb_bootstrap.py` and `tests/hooks/test_hook_launcher_uv.py` (`e741a577a`); the launcher form above |
| D: documents and this record claimed more than was measured | Partly fixed. The hooks assumption is marked Verified and `docs/windows-install.md` exists. Open: the Desktop wording in `web/index.html` and `docs/desktop-deployment.md` |
| E: the aspect-worker daemon died with the bootstrap's job | Fixed in code (`e741a577a`: breakaway allowed on the job, a breakaway-first spawn with a fallback). Tested with a fake `Popen` when this table was first written. Corrected 2026-10-12, later the same day: `06ee0cab2` added three real-Windows tests (`tests.test_mcpb_bootstrap.TestRealWindows`) that close a real Job Object and see the member killed and a breakaway child spared, and that see a refused breakaway retried inside the job. They passed natively (T2 `224-win-run-06ee0cab2`) and in the rehearsal job. Not measured: the breakaway under Claude Desktop's own job |
| F1: `uv sync` inherited the host's stdin and decoded with the locale codec | Fixed: stdin is `DEVNULL`, output is decoded as UTF-8 with replacement |
| G: the stdio handoff test could hang | Fixed: the exchange has a deadline |

Not measured, as of 2026-10-12: whether Claude Code and Claude Desktop resolve
the bare `uv`, `nx-hook` and `nx-session-end-launcher` names in `hooks.json` and
the manifest from the project directory on Windows; that closing the Job Object
kills a real child, and that a child spawned with breakaway survives it (a hand
run on qwentescence at `4aa3eb754` covered the kill once, and CI asserts only
that the job is created); the time a first hook run takes on a box with no
Python, against the 5 s hook timeout (nexus-f9bgu.37 carries these). The first
of these was measured on 2026-10-13: Claude Code 2.1.296 ran the installed
programs, not copies planted in the working directory (Revision History,
2026-10-13 evening).

Correction, 2026-10-12, later the same day: the job kill and the breakaway are
measured. `06ee0cab2` added three real-Windows tests to
`tests.test_mcpb_bootstrap.TestRealWindows`: a real Job Object is created,
closing it kills its member and spares a child spawned with breakaway, and a
job that forbids breakaway refuses it and the retry keeps the child in the job.
All three passed on qwentescence (T2 `224-win-run-06ee0cab2`: 1591 passed, 55
skipped, 2 xfailed, 0 failed) and in the rehearsal job, which requires them by
name. Finding E's fix is
therefore tested against a real job, not only a fake `Popen`. Still not
measured: the resolution of bare `uv` and `nx-hook` from the project directory
by the hosts; the time of a first hook run with no Python on the box; and the
breakaway under Claude Desktop's own job object, which may forbid it (the retry
path is tested against a job that does).

### Phase 5: Gate (Gap 7)

A hand-run, release-time gate on a real Windows box, outside CI, modelled on
`tests/e2e/mac-signed-binary-gate.sh` and on the journey of
`tests/e2e/fresh-install-mvv.sh` (install, init, store, search; T2
`224-research-26`). The script is `tests/e2e/windows-phase5-gate.ps1`, driven
unattended from the Mac by `tests/e2e/windows-guest/run-gate.sh`. ijue9.16 is
rewritten for it; the script `post-publish-dispatch-check.sh` is gone. Every run
was on a Hyper-V guest on qwentescence (Enterprise Evaluation, an unactivated
consumer Pro, consumer Home), the clean-machine host Phase 0 Step 0.5 chose, so
host sleep, physical hardware and Microsoft and domain accounts are unmeasured.
Sam decided at close, on 2026-10-15, that a clean Hyper-V guest satisfies the
clean-machine gate, with physical hardware and sleep as accepted residuals (T2
`nexus_rdr/224-close-decisions-2026-10-15`, item 3). It passed 5 of 5 on published conexus 7.72.1, 7.78.0, 7.79.0 and 7.80.0 (T2
`224-gate-run-7.72.1-2026-10-07`, `224-gate-run-7.78.0-2026-10-12`,
`224-published-7.79.0-gate-2026-10-14`, `224-published-7.80.0-gate-2026-10-15`).
In a native Windows Claude Code session it asserts:

1. `nx init --service` installs the published `windows-x64` assets and the
   supervisor starts the engine and PostgreSQL;
2. an MCP tool round trip from the session: a store, then a search, returns the
   stored text. This is the link ijue9.2 records as unproven (T2
   `224-research-27`);
3. the hook launcher chosen under nexus-efk2h runs when Claude Code invokes it.
   As built, the gate reads the session's hook records and counts the conexus
   hook runs by what they launch (an `nx-hook` verb, directly or through
   `launch/nx_launch.py`, the shim, a script under `hooks/scripts`,
   `nx-session-end-launcher`, or an MCP tool hook), not by how uv launches them
   (`$HOOK_OURS_RE` in the script). It passes when `nx-hook session-start` and
   the `nx_hook_shim.py` launcher each succeed with output and no conexus hook
   run failed;
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
| PostgreSQL after a host shutdown | N/A | In scope (`pg.log`) | N/A | Sign-out and restart measured clean (nexus-f9bgu.46, .51); sleep deferred (unmeasured, T2 `224-research-32`) | N/A |
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
- No psutil: Phase 3 used ctypes for Windows liveness (`pid_alive`,
  nexus-f9bgu.25).

## Test Plan

- **Scenario**: Windows release leg builds the engine — **Verify**: dependency
  check allows only VC++ runtime and system DLLs; embedded-resources check
  passes with one origin per native library. Status: both passed by hand on
  qwentescence at `-Ob`, and in the release leg at `-O2`, the pom default, for
  the `engine-service-v0.1.149` cut, which published the archive (T2
  `224-engine-cut-v0.1.149`). A hand `-O2` build was timed on 2026-10-05
  (Performance Expectations).
- **Scenario**: Windows PG bundle relocation — **Verify**: build prefix
  removed, `initdb`, `CREATE EXTENSION vector` and `pg_trgm`, HNSW query rows.
  Status: passed by hand from the packaged archive.
- **Scenario**: a stop, sent as `CTRL_BREAK`, in each engine phase on Windows.
  Measured outcome per phase, first with the 09-29 engine and a patched copy
  (T2 `224-research-18`, `-19`), then with the release-shaped native exe built at
  `-Ob` (T2 `224-p1.1-stop-probe`, `224-p1.1-ort-init-timeout-probe`). For the
  `-O2` release exe, only the serving stop is measured (the release-leg smoke and
  gate assertion 4); the migration and initialisation phases are not:
  - migration, before the changelog lock is taken: the engine exits at once
    (149 in 0.011 s), the changelog has 0 rows, and the next boot applies every
    changeset in the changelog (508 at 2026-10-05);
  - migration, during a changeset: the engine exits at once and leaves
    `databasechangeloglock` locked. This is not a clean stop: the next boot
    waited about 300 s and failed (T2 `224-research-19`; Failure Modes). The test
    records that outcome and does not assert a clean one. The held lock is
    released at the next boot since nexus-8sph2 (`engine-service-v0.1.156`),
    pinned by `SchemaMigratorStaleChangelogLockIntegrationTest`; a re-run of this
    Windows probe against the fix is not recorded;
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
  Visual Studio is a proxy and does not prove the redistributable is absent.
  Status: passed on the published archives in every Phase 5 gate run since
  2026-10-07 (precondition P0, assertion 1).
- **Scenario**: supervisor conformance suite on Windows — **Verify**: the same
  lifecycle assertions pass as on Linux and macOS, plus stop returning only once
  the supervisor and the engine have exited, and the lease file replaced under
  concurrent readers. It runs in the rehearsal workflow's conformance job (Phase 0
  Step 0.5), with a junit floor. Status: 74 passed, 2 skipped, 2 xfailed on real
  Windows, repeated.
- **Scenario**: Windows smoke in the release leg — **Verify**: the exe boots
  against the Windows PG bundle built in the same workflow run and handed over as
  an artifact. Status: passed by hand, then in the release workflow on
  `win-release` for the `engine-service-v0.1.149` cut (T2
  `224-engine-cut-v0.1.149`). One later run failed with a port collision, fixed
  in `b16a58f1c` (Revision History, 2026-10-15).
- **Scenario**: Phase 4, the hook launcher and the desktop bundle's launch on
  Windows (row added 2026-10-12) — **Verify**: in the rehearsal workflow's
  conformance job, `tests/hooks/test_hook_launcher_uv.py` passes (every uv hook
  entry launches its script, and the credential guard still denies through the
  launcher) and `tests/test_mcpb_bootstrap.py` passes (stdio and the exit code
  survive the handoff on real handles; `TestRealWindows` creates the Job Object).
  The junit floor requires `tests.test_mcpb_bootstrap.TestRealWindows` and
  `test_the_credential_guard_still_denies_through_the_launcher` to have passed.
  Status: green on real Windows, run 38194840901 on `2ea176075` (2026-10-12),
  1560 passed. Not in that job as first recorded: `tests/hooks/test_hook_launcher_cwd.py`
  (the planted `.venv` cases; its module skips on Windows), a real job kill and a real
  breakaway. Corrected 2026-10-12, later the same day: `06ee0cab2` put the job kill, the
  breakaway and a refused-breakaway retry into the job as three tests in
  `tests.test_mcpb_bootstrap.TestRealWindows`, green on real Windows (T2
  `224-win-run-06ee0cab2`, 1591 passed) and required by name in the floor. Still not
  in the job: the planted `.venv` cases.
- **Scenario**: the Phase 5 gate on a real Windows box — **Verify**: its five
  assertions pass and its executed-assertion count equals the declared count.
  Status: passed 5 of 5 on published conexus 7.72.1, 7.78.0, 7.79.0 and 7.80.0,
  on Hyper-V guests (Phase 5).
- **Scenario**: Windows 11 machine with Smart App Control enforcing, fresh
  `nx init --service`. **Verify** (since nexus-f9bgu.57): `nx init` refuses
  with the install guide's message before it downloads the PostgreSQL bundle,
  and `nx doctor`'s Smart App Control row fails in local mode. Status: not
  reached. The detection is on develop and ships in the next client release
  after 7.80.0, not in 7.80.0. On 2026-10-15, with a candidate wheel on the Home
  guest's Smart App Control checkpoint, Windows blocked `nx.exe` at install, so
  neither the refusal nor the row could report there (T2
  `224-sac-detection-guest-runs-2026-10-15`); on 2026-10-14 `nx.exe` still ran
  and the block came inside `nx init`, and why the two runs differ is not known.
  With Smart App Control in evaluation the candidate passed the gate 5 of 5, so
  the guard does not refuse there. In a second pair of runs the same day, the reader, run through the tool environment's Python, returned `on` on the Smart App Control checkpoint and `evaluation` on the other. On the Smart App Control checkpoint `nx.exe` ran that time, and `nx init` refused with the new message within a second, with no download and no `WinError 4551`. So the refusal and the reader work on real Windows, and whether Windows blocks `nx.exe` itself varied between two runs on the same checkpoint. The scenario first verified that the engine,
  PostgreSQL and the extracted DLLs all load with no CodeIntegrity block events.
  That FAILED on 2026-10-14 on the Home guest: `nx init --service` failed with
  `WinError 4551`, and CodeIntegrity blocked the unsigned PostgreSQL bundle, uv's
  unsigned CPython `.pyd` files and PortableGit DLLs (T2 `224-research-35`). A
  machine with Smart App Control on is an unsupported configuration. The blocked
  `.pyd` files (and on 2026-10-15 the blocked `nx.exe` launcher) show that
  signing nexus's binaries would not have made those runs pass, so it is not a
  test for signing (Critical Assumptions; Phase 0 Step 0.2
  amendment).
- **Scenario**: when signing is adopted (deferred, Phase 0 Step 0.2), a release
  leg with one shipped DLL left unsigned — **Verify**: the signature check fails
  the leg.

## Validation

### Testing Strategy

The Minimum Viable Validation is the acceptance proof. The Windows release leg
and the Phase 5 gate are the recurring proofs.

### Performance Expectations

Measured on qwentescence. The 09-29 spike: native build about 80 s, exe 143 MB,
health in about 3 s after start. The later builds at `-Ob`: build 1 min 24 s, exe
127 MB (127,148,032 bytes), engine archive 33,687,976 bytes (32.1 MiB), PG bundle
archive 7,407,044 bytes. Boot to ready 0.96 to 1.06 s warm and 3.1 s on the first
start after a fresh model path with a migration; 508 changesets in about 1.5 s; a
serving stop in 0.07 to 0.08 s. The release leg builds at `-O2`, the pom default.
A hand `-O2` build with the workflow's command, timed on 2026-10-05 (T2
`224-windows-o2-build-timing`): 1 min 37 s to 1 min 45 s wall (native-image 1 min
11 s to 1 min 17 s), peak RSS 9.44 to 10.14 GB, and 1 min 54 s with peak RSS
7.73 GB under `--parallelism=8`; exe 151,449,600 bytes. The published `-O2`
engine archive of `engine-service-v0.1.149` is 39.6 MiB, under the 55 MiB
ceiling `check_engine_cut_riders.py` holds it to (T2 `224-engine-cut-v0.1.149`).
No further targets.

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
(Amendment, 2026-10-05: the stop on the real supervisor and engine and the stop
during ONNX Runtime initialisation are now verified. Amendment, 2026-10-07: the
hooks, the clean-machine run and the release-workflow run are verified by the
7.72.1 gate and the `engine-service-v0.1.149` cut. Amendment, 2026-10-15: the
signed-binaries assumption is falsified for Smart App Control, because Smart App
Control blocked uv's unsigned CPython as well as nexus's files, T2
`224-research-35`. The gate text above records what the gate saw.)

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| native-image build on Windows | GraalVM 25.0.3 | Spike |
| `CREATE EXTENSION vector` on the Windows bundle | pgvector 0.8.2 | Spike |
| `OpenProcess` / `WaitForSingleObject` liveness | Win32 API | Verified on the real kernel: `tests.daemon.test_pid_alive_windows.TestRealWindowsKernel` (own pid alive, an exited child dead, a real handle as the non-vacuity check) is required by name in the Windows rehearsal job's junit floor (nexus-f9bgu.25) |
| Task Scheduler per-user logon task | Windows | Verified by hand at implementation (`schtasks`, a non-administrator token); the logon trigger firing was measured after sign-out and restart on the clean guest (nexus-f9bgu.46) |

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
- **Incremental adoption**: phases landed on develop without declaring support
  until Phase 5's gate passed. Sam declared Windows 11 x64 supported at close on
  2026-10-15, in the same conversation as the close decisions (T2
  `224-close-decisions-2026-10-15`); the residuals in § Delivered versus the
  Problem Statement are its known limits.
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

## Delivered versus the Problem Statement

Added 2026-10-15 at close. The residuals named here are accepted as known limits
of the first Windows release, by Sam's decision at close on 2026-10-15 (T2
`nexus_rdr/224-close-decisions-2026-10-15`; the 2026-10-15 comment on
nexus-f9bgu.53). "Accepted, no bead" means no open bead carries the item.

Closing this RDR as Implemented declares Windows 11 x64 supported: Sam decided
this at close on 2026-10-15, in the same conversation as the close decisions
(T2 `224-close-decisions-2026-10-15`). The residuals in the table below are the
known limits of that support. Windows 10 is not measured and not supported:
every measured guest is Windows 11 x64 (builds 26100 to 26300).

The Problem Statement was accepted at commit `4de7d020c`. Its gap texts were
edited in place since then with measured results; read that commit for the gaps
as accepted.

| Gap | Outcome | Residuals | Evidence |
| --- | --- | --- | --- |
| 1: No Windows engine binary | Met, with a named residual | The engine is not Authenticode-signed (Gap 6). | `engine-service-v0.1.149` published `nexus-service-windows-x64.txz` (T2 `224-engine-cut-v0.1.149`); every gate run installs it |
| 2: No Windows PostgreSQL bundle | Met, with a named residual | The bundle is not Authenticode-signed (Gap 6). | `nexus-pg-windows-x64.txz` in the same release; gate precondition P0 and assertion 1 on a guest with no VC++ runtime |
| 3: Supervisor POSIX-only | Met, with named residuals | Host sleep unmeasured (no sleep state in a Hyper-V guest); accepted, no bead. `schtasks` output on a Windows whose language is not English unmeasured; accepted, no bead. The aspect-worker daemon is outside the gate (observed, not asserted); accepted, no bead. A PostgreSQL bundle swap to a different `postgres.exe` is unmeasured (the measured swap went between two tags that carry the same binary); accepted, no bead. | Conformance on real Windows (T2 `224-f9bgu19-conformance`); sign-out and restart (nexus-f9bgu.46, .51); `install-binary` over a running stack after the nexus-f9bgu.56 fix (T2 `224-dll-rules-and-install-binary-proof-2026-10-14`); gate assertions 1 and 4 |
| 4: No graceful engine stop | Met, with named residuals | The initialisation-phase stop of the `-O2` release exe is unmeasured (the `-Ob` probes cover it); accepted, no bead. A real initialisation longer than the 3 s `OrtInitGate` bound, and the supervisor's 5 s grace against that bound, are unmeasured; accepted, no bead. The Windows changeset-stop probe was not re-run against the nexus-8sph2 fix (the fix is pinned by `SchemaMigratorStaleChangelogLockIntegrationTest`); accepted, no bead. Open shapes of the same-session stop (Critical Assumptions): a real engine that ignores the break (stand-in only), a stop aimed at another user's service, and a launcher layer other than the venv trampoline (the gate's stop, assertion 4, passed with the generation launcher, nexus-f9bgu.47); accepted, no bead. | T2 `224-p1.1-stop-probe`, `224-p1.1-ort-init-timeout-probe`, `224-f9bgu17round2`, `224-review-p3-fixes-a`; the release-leg smoke; gate assertion 4 |
| 5: Plugin and desktop surface | Met for Claude Code, with named residuals | The Desktop `.mcpb` bundle cannot be installed on the Claude Desktop build measured on 2026-10-11 (chat and Cowork get conexus from the account marketplace); accepted, no bead. A Windows Terminal window with several tabs, classic conhost, and sign-out with a session open are unmeasured; accepted, no bead. The time of a first hook run on a box with no Python, against the 5 s hook timeout, is unmeasured (no guest could show it); accepted, no bead. When uv finds no interpreter it exits 2 and five hook entries are blocked; kept as a loud block by Sam's decision on nexus-f9bgu.36, remedy `uv python install 3.12` in `conexus/README.md`; accepted, no bead. | Gap 5 amendment and "How Phase 4 ended"; gate assertion 3; T2 `224-wdac-and-window-close-2026-10-15`, `224-guest-batch-2026-10-12` (M4) |
| 6: Unsigned binaries | Not met (deferred by decision); with Smart App Control on, the outcome varied between runs | Binaries unsigned (nexus-dj01b, deferred; Sam, 2026-10-05 and 2026-10-14). A machine with Smart App Control on is unsupported; its blocking behaviour varied between runs (September no block, 2026-10-14 a block inside `nx init`, 2026-10-15 `nx.exe` blocked at install), cause not known, and in the runs that blocked, signing nexus's binaries alone would not have helped. Detection (nexus-f9bgu.57): `nx init` refuses before the bundle download and `nx doctor` reports the state, with no row where the registry value is absent (Windows 10, older Windows 11). It is on develop and ships in the next client release after 7.80.0, not in 7.80.0. On the 2026-10-15 Smart App Control checkpoint Windows blocked `nx.exe` at install, so the detection could not report there; in evaluation the candidate passed the gate 5 of 5 (T2 `224-sac-detection-guest-runs-2026-10-15`). In a second pair of runs the same day, the reader, run through the tool environment's Python, returned `on` on the Smart App Control checkpoint and `evaluation` on the other. On the Smart App Control checkpoint `nx.exe` ran that time, and `nx init` refused with the new message within a second, with no download and no `WinError 4551`. So the refusal and the reader work on real Windows, and whether Windows blocks `nx.exe` itself varied between two runs on the same checkpoint. Windows switching Smart App Control from evaluation to on after install is expected, not observed; `nx doctor` warns in evaluation, which can only speak before the switch; bead nexus-f9bgu.58 measures it. AppLocker and WDAC need an administrator's path rules. A gate run with script enforcement on is unmeasured under both (under AppLocker, enforced script rules are known to stop the uv installer script), and install, upgrade and removal under WDAC are unmeasured; accepted, no bead. | Phase 0 Step 0.2 and its amendment; T2 `224-research-34`, `224-research-35`; nexus-f9bgu.57 |
| 7: No gate on real Windows | Met by Sam's decision (Hyper-V guest, Step 0.5), 2026-10-15 | Every run was on Hyper-V guests on one host. Physical hardware and host sleep are accepted residuals by the same decision. Microsoft and domain accounts are unmeasured (all runs used local accounts); accepted, no bead. Every guest was Windows 11 x64 (builds 26100 to 26300); Windows 10 is unmeasured and not supported; accepted, no bead. | `tests/e2e/windows-phase5-gate.ps1` passed 5 of 5 on published 7.72.1, 7.78.0, 7.79.0 and 7.80.0 (Phase 5); T2 `nexus_rdr/224-close-decisions-2026-10-15`, item 3 |

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
  of a new unsigned program also ran: T2 `224-research-16`. Superseded on
  2026-10-14, when Smart App Control on blocked the install (T2
  `224-research-35`; the 2026-10-15 closure text pass).
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
  `check_engine_release_floor.py --require-windows`). Still open then: S3
  (clean-guest candidate run, bead nexus-f9bgu.45) and S6 (the release workflow's
  Windows jobs on `win-release`). Both closed by 2026-10-07: S6 by the
  `engine-service-v0.1.149` cut and S3 by the candidate run and the 7.72.1 gate
  (T2 `224-engine-cut-v0.1.149`, `224-f9bgu45-clean-guest-candidate-run`,
  `224-gate-run-7.72.1-2026-10-07`).
- 2026-10-05: Phase 3 review fix round B (nexus-f9bgu.33, .34). The same-session
  stop assumption is now marked partially verified, with its five uncovered shapes
  listed; fix round A then measured (a), (d) and (e) with a stand-in supervisor
  and wrote the results into the assumption, leaving (b), (c) and another user's
  service open; the getuid count is 22
  sites plus six exempt; the orphan-tracker fix is noted as unreachable from the
  production sweep on Windows; the autostart text says `Hidden` is false and what
  hides the window; the upgrade bullet says the real bundle swap with a live
  `postgres.exe` was not run; the refusal text is noted to cover a same-session
  denial. Added: the aspect-worker daemon port, with its decision that `service
  stop` leaves it running as on POSIX, and the stale `.nx_old_*` sweep. The
  rehearsal's Windows job now runs the 1076-test native set under a junit floor,
  and a real-stack stop driver is committed (`scripts/windows_service_stop_driver.py`).
- 2026-10-07: evidence update after the Phase 5 gate passed 5/5 on published
  conexus 7.72.1 (paired with `engine-service-v0.1.149`) on the clean guest,
  unattended, in its first cold session (T2 `224-gate-run-7.72.1-2026-10-07`).
  Checked: hooks under native Claude Code, the packaged archives on a clean
  machine, the Windows release-workflow jobs, and the prerequisite naming them.
  The host-shutdown assumption gains the nexus-f9bgu.46/.51 measurement and stays
  open for sleep. 7.72.1 carries the Windows fixes the gate needed: the MCP stdio
  protocol off fd 0 (nexus-jg99b), the preflight budget (nexus-3cydr) and
  `tuf>=7.0.1` (nexus-zw44w). Text and evidence only; no status change.
- 2026-10-12: Phase 4 record (source: the Phase 4 critique, S3, T2
  `224-review-p4-critique`). Added "How Phase 4 ended" under Phase 4: the
  shipped launcher form, the bundle's launch, the Desktop route that changed to
  the account marketplace, the dispositions of code-review findings A to G, and
  what is not measured. Added a dated note to Gap 5, the shipped launcher and
  its evidence to the hooks assumption, and a Phase 4 Test Plan row. Text and
  evidence only; no status change.
- 2026-10-12: corrections and Phase 5 results, text and evidence only; no status
  change. (a) Phase 4: the Test Plan row, the "Not measured" paragraph and the
  finding E row said the job kill and the breakaway were unmeasured and finding E
  was tested with a fake `Popen` only. `06ee0cab2` added them as real-Windows
  tests in `tests.test_mcpb_bootstrap.TestRealWindows`, which passed natively (T2
  `224-win-run-06ee0cab2`) and in the rehearsal job; each passage now carries a
  dated note. Still unmeasured: host resolution of bare `uv` and `nx-hook`, the
  first hook run with no Python, and the breakaway under Claude Desktop's own
  job. (b) Phase 5: the gate FAILED assertion 4 on published conexus 7.77.1
  (executed 5 of 5, A4 not passing: a plain `nx daemon service stop` also stopped
  a PostgreSQL the supervisor had started; T2 `224-gate-run-7.77.1-2026-10-12`;
  nexus-f9bgu.54). The fix, a PostgreSQL start on a console of its own, shipped in
  7.78.0, and the gate PASSED 5 of 5 on published conexus 7.78.0 with
  `engine-service-v0.1.160` on the clean guest, unattended (measured stop times
  2.1 s with `--with-pg` and 1.9 s plain, against the 15 s bound; T2
  `224-gate-run-7.78.0-2026-10-12`). The aspect-worker daemon, host sleep and the
  Desktop bundle stay outside what a green gate covers.
- 2026-10-13: measurements on the clean guest, text and evidence only; no status
  change. (a) The Phase 5 gate PASSED 5 of 5 on published conexus 7.78.0 run as a
  standard user: a local account in Users only, created after the restore by
  `tests/e2e/windows-guest/run-gate.sh --standard-user`, which then runs the
  install, the setup phase (with `nx init` and the `NexusStorageService` task),
  the Claude Code session and the verify phase as that account. The gate's
  precondition recorded "Administrators not a member, integrity Medium" (T2
  `224-gate-run-standard-user-7.78.0-2026-10-13`). This closes the Phase 3 "not
  run" item for a standard account as far as the gate reaches. The administrator
  still installed Git for all users, since winget's package needs elevation, so a
  standard user with no Git is not covered; neither is a Home or Pro edition, a
  Microsoft or domain account, or a managed machine's application policy. (b)
  `nx daemon service install-binary` with the published `windows-x64` assets: exit
  0 in 12.8 s with the PostgreSQL bundle and 6.7 s with `--no-pg-bundle`, a
  same-version reinstall with the service stopped (T2
  `224-guest-batch-2026-10-12`). (c) Host resolution of bare `uv` and `nx-hook`:
  with `uv.exe` and `nx-hook.exe` planted in the session's working directory, all
  four recorded SessionStart hooks ran the installed programs. On the guest
  `claude.exe` shares a directory with the installed ones, so this does not
  separate "the host never searches the working directory" from "the
  application's directory wins first". (d) Not measurable on this guest: sleep (the
  VM's firmware offers no sleep state) and a first hook run with no Python (the
  only Python is the one `uv tool install` brings; with it present the cold first
  session's hooks took 3.4 s against the 5 s timeout). Both stay open. (e) Three
  real-Windows tests joined the rehearsal's required floor: the PostgreSQL start
  flags under a console-wide CTRL_BREAK, with an unflagged control that dies; the
  off-cwd resolver against real planted files; and a planted `.venv` against the
  eleven uv hook entries (T2 `224-win-run-ffea86080`). The hand runs were in a
  desktop session; the rehearsal job on the runner's service account is the console
  test's first run in session 0.
- 2026-10-13, later: two more results from the clean guest, text and evidence
  only. (a) The gate PASSED 5 of 5 as a standard user on a guest with no Git
  (`run-gate.sh --standard-user --no-admin-git`): the account ran the PortableGit
  block of `docs/windows-install.md` as written, with no elevation, in 29 s, and
  the plugin install and Claude Code's Git Bash both worked from the user PATH
  entry alone (T2 `224-gate-run-standard-user-no-git-7.78.0-2026-10-13`). The
  administrator then does nothing a user's install needs. Still not covered: a
  Home or Pro edition, a Microsoft or domain account, and application-control
  policy, which is the kind of thing that blocks a self-extractor in a user
  folder. (b) The 16.5 s `mcp-connect-wait` seen in a `claude -p` child during the
  guest batch is not a Windows matter. The aspect extractor's child loaded the
  user's settings and so ran every SessionStart hook, and the plugin's connection
  wait timed out at its bound for an `nx-mcp` that child never starts. The child
  now passes `--setting-sources ""` (nexus-n6eaj).
- 2026-10-13, evening: two more results from the clean guest, text and evidence
  only. (a) Host resolution of bare `uv` and `nx-hook` with the installed
  programs NOT beside `claude.exe`: they were moved to a directory that is only
  on the user PATH, and copies of `whoami.exe` were planted under their names in
  the session's working directory. A plain `CreateProcess` and `cmd /c` both ran
  the plant; Claude Code 2.1.296 ran the installed programs for all four
  recorded SessionStart hooks and for the `uv`-launched MCP server (T2
  `224-hook-resolution-off-app-dir-2026-10-13`). This closes the limit recorded
  in the earlier 2026-10-13 entry, for one host version; `nx-session-end-launcher`
  stays unobserved because SessionEnd did not fire. (b) Application control:
  with AppLocker's default rules enforced by the administrator
  (`run-gate.sh --standard-user --no-admin-git --applocker`), the standard
  account's install failed at its first step and nothing from
  `docs/windows-install.md` worked. PowerShell ran in ConstrainedLanguage, so the
  uv installer script failed; the Claude Code installer's executable and the
  PortableGit archive were refused by policy (T2
  `224-gate-run-applocker-7.78.0-2026-10-13`). A first run showed that the policy
  reads as effective about 75 s before anything is blocked, and in that window
  the account installed uv and conexus; once enforcement began, `nx.exe`,
  `uv.exe` and the tool environment's `python.exe` were refused by path. The
  driver now waits for a start in the account's profile to be recorded as
  blocked before the account does anything. Not measured that evening, and
  measured since: the allow rules (the next entry), Smart App Control (2026-10-14,
  T2 `224-research-35`) and WDAC (2026-10-15, T2 `224-research-34`). The install guide
  now states the standard-account passes and the AppLocker result.
- 2026-10-13, night: the allow rules an AppLocker machine needs, text and
  evidence only. An audit-only run (`--applocker-audit`) passed the gate 5 of 5
  and its log named every program the enforced rules would refuse: all sit in
  `.local\bin`, `AppData\Roaming\uv`, `AppData\Local\uv\cache`,
  `AppData\Local\Programs\Git` and `.config\nexus` under the profile. With the
  executable rules enforced and seven path rules for those folders and the two
  installer locations (`--applocker-allow`), the standard account installed from
  the guide and the gate PASSED 5 of 5 (T2
  `224-gate-run-applocker-allow-rules-7.78.0-2026-10-13`). The script rules were
  audit only in that run: enforced, they put PowerShell in ConstrainedLanguage,
  where the uv installer script fails and no path rule helps. Publisher rules
  cannot replace the path rules while the engine and the PostgreSQL bundle are
  unsigned (P0.2). Not measured that night: DLL rules, WDAC, Smart App Control,
  update and removal under the policy. All four were measured on 2026-10-14 and
  2026-10-15 (nexus-f9bgu.53 item 9; T2 `224-research-34`, `224-research-35`).
  `docs/windows-install.md` lists the rules.
- 2026-10-13, late: the hook helper and the Desktop bundle launcher no longer
  return a `.cmd` or `.bat` hit from their `PATH` lookups (Sam's decision, the
  question the Phase 4 test review left open). A batch file runs through
  `cmd.exe` with quoting CPython does not escape for it; `nexus.util.nx_argv`
  and `generation_core` already refused one, so the four copies now agree and
  the parity test names no exception. A `uv` or `nx-hook` that exists only as a
  batch file counts as not found. The hook shim's Windows tests, which had
  installed a fake `nx-hook.cmd`, now place a real `nx-hook.exe` (setuptools'
  console launcher). Native Windows run at `091c9453e`: the rehearsal set 1637
  passed, 0 failed, floor met; the parity test 26 passed (T2
  `224-win-run-091c9453e`). Also corrected: the Phase 4 "Not measured" paragraph
  now points at the 2026-10-13 host-resolution measurement.
- 2026-10-14 to 2026-10-15: text encoding. Python on Windows opens text files
  and decodes child output in the locale code page (cp1252 on the guests), so a
  UTF-8 file or child output with a non-ASCII character, such as the em dash in
  the service unit templates, failed or was garbled. Every text-mode `open`,
  `read_text` and `write_text` in `src/nexus`, `conexus` and `sn` now names
  `encoding="utf-8"`, and child processes that return text decode as UTF-8,
  with `run_bounded` replacing a byte that is not UTF-8 instead of raising;
  `schtasks` output is read in the locale code page, because that is what it
  writes. Two lint tests allow no new unmarked call (commits `e7deb63cf`,
  `a08876f28`, `5d606a19b`, `91181e7f7`, `c98395c30`; nexus-ox0c0,
  nexus-ikoqj). Native Windows run of the Windows test set: 2396 passed, 0
  failed. Not measured: `schtasks` output on a Windows whose language is not
  English, where the ANSI and OEM code pages differ.
- 2026-10-15: WDAC (App Control for Business), text and evidence only, on the
  Enterprise guest with published conexus 7.79.0 and engine 0.1.160, standard
  account, `run-gate.sh --standard-user --no-admin-git` with `--wdac-audit` or
  `--wdac-allow` (commits `9180b12dd`, `9493e351e`). The policy is Microsoft's
  `DefaultWindows_Enforced` example, deployed with CiTool; an unsigned program
  built in the guest is the control that proves enforcement is on. Audit: the
  gate PASSED 5 of 5 and 271 events named the programs the policy would block,
  all under the account's profile (PortableGit, the uv Pythons and tool
  environment, the PostgreSQL bundle, the engine, the DJL and ONNX Runtime
  libraries, `.local\bin`, the Claude Code installer). Enforced with ten path
  rules for those folders, runtime file-path protection off (option 18) and
  script enforcement off (option 11): the gate PASSED 5 of 5 and the only
  program blocked was the control. A first enforced run failed because the
  driver built the rules in a form `Merge-CIPolicy` refused, so the policy went
  out with no rules; the driver now refuses to deploy without all ten. As with
  AppLocker, publisher rules cannot replace the path rules while the engine and
  the PostgreSQL bundle are unsigned (P0.2). T2
  `224-wdac-and-window-close-2026-10-15`; `docs/windows-install.md` lists the
  rules. Not measured: script enforcement on, `nx self install`, upgrade and
  removal under the policy, the Pro edition.
- 2026-10-15: closing Claude Code's window, on the Pro guest with the storage
  task running. The window was found by attaching to `claude.exe`'s console:
  a pseudo-console owned by a one-tab Windows Terminal window, which was sent
  `WM_CLOSE` (matching on the launch title failed, because Claude Code retitles
  its tab). `claude.exe` exited at once; none of its 21 descendants (uv, the MCP
  servers, the aspect worker, console hosts) were alive 30 s later; SessionEnd
  ran, and `hook.log` records the session's scratch expiry and `session_end`;
  the supervisor, engine and PostgreSQL kept running. The 2026-10-13 evening
  entry had recorded no SessionEnd. T2
  `224-wdac-and-window-close-2026-10-15`. Not measured: a Terminal window with
  several tabs, which asks before closing; classic conhost as the default
  terminal; signing out with a session open.
- 2026-10-15: the Windows engine smoke on the release runner failed once with
  `BindException: Address already in use`: it chose the engine's port before
  PostgreSQL bound its own, and both drew the same free port. The smoke now
  chooses the engine's port after PostgreSQL is up and logs it (commit
  `b16a58f1c`); a test fails against the old order. The rerun of that job was
  cancelled by a Windows Update restart of qwentescence (update service, 05:29
  to 05:31 UTC) and passed when run again.
- 2026-10-15: Gap 6 and Key Discoveries brought in line with the AppLocker
  and WDAC measurements: the research-round-2 claim that these policies need a
  signature or an allowlist is now Verified for the allowlist (path rules
  suffice, nothing signed; T2 `224-research-34`), and only the Defender
  machine-learning question stays Assumed (T2 `224-research-12`).
- 2026-10-15: closure text pass. (a) The Smart App Control result replaces the
  "not reproduced" premise in Gap 6, Key Discoveries, the signing Critical
  Assumption (now falsified for Smart App Control), the signing design, Risks,
  Phase 0 Step 0.2 and the Test Plan: with it on, `nx init --service` fails
  because the PostgreSQL bundle, uv's CPython and PortableGit DLLs are blocked,
  so such a machine is unsupported and signing nexus's binaries alone would not
  fix it (T2 `224-research-35`, `224-smart-app-control-on-2026-10-14`). The
  September runs that saw no block are kept beside it with their differences;
  the cause of the difference is not known. Step 0.2 gains Sam's 2026-10-14
  amendment (nexus-dj01b comment). (b) Stale sentences replaced: the gate on the
  shipped hook form, the Windows release workflow and published assets
  (`engine-service-v0.1.149`), the packaged archives on a clean machine, the gate
  script and what assertion 3 matches, nexus-8sph2 (closed, live since
  `engine-service-v0.1.156`), the `-O2` build (T2 `224-windows-o2-build-timing`)
  and archive size, sign-out, restart and the logon trigger (nexus-f9bgu.46,
  .51), and `OpenProcess` liveness on the real kernel (nexus-f9bgu.25). (c) Gap 7
  and Phase 5 state that every gate run was on Hyper-V guests on one host and
  what that leaves unmeasured. (d) Added "Delivered versus the Problem
  Statement". Sources: T2 `224-gate-run-7.72.1-2026-10-07`,
  `224-gate-run-7.78.0-2026-10-12`, `224-published-7.79.0-gate-2026-10-14`,
  `224-published-7.80.0-gate-2026-10-15`, `224-pro-guest-gate-2026-10-13`,
  `224-home-guest-2026-10-14`, `224-engine-cut-v0.1.149`, `224-research-34`;
  nexus-f9bgu.53. Text and evidence only; no status change.
- 2026-10-15: closure text pass, round 2, from the second closure critique.
  (a) Gap 7, Phase 5 and the Delivered row record Sam's close decision that a
  clean Hyper-V guest satisfies the clean-machine gate (Step 0.5), with physical
  hardware and sleep as accepted residuals (T2
  `nexus_rdr/224-close-decisions-2026-10-15`, item 3). (b) Smart App Control is
  now detected: Gap 6, Risks, the Test Plan row and Delivered row 6 state the
  refusal before the bundle download and the `nx doctor` row (nexus-f9bgu.57;
  the guest results are in the round 3 entry below). (c) The evaluation-mode basis now says what was
  measured: the Home guest's normal checkpoint (state 2) passed the gate and
  logged no blocks (T2 `224-home-guest-2026-10-14`,
  `224-published-7.79.0-gate-2026-10-14`, `224-published-7.80.0-gate-2026-10-15`);
  the September runs being in evaluation mode is inferred. Key Discoveries and
  the Step 0.2 amendment changed; a Failure Modes entry covers Windows switching
  evaluation to on after install. (d) The Delivered table gains the residuals
  the body records (same-session stop shapes, the ORT bound and grace, the
  changeset-stop re-run, a first hook run with no Python, uv exit 2, a bundle
  swap to a different binary), and its intro cites the close decisions and the
  accepted Problem Statement (`4de7d020c`). The three open Critical Assumption
  boxes are marked deferred (signing, nexus-dj01b) or waived at close. (e) Gap
  3 and Phase 3 now record the measured PostgreSQL bundle swap over a running
  stack and the nexus-f9bgu.56 extraction bug and fix (T2
  `224-dll-rules-and-install-binary-proof-2026-10-14`, `224-guest-batch-2026-10-12`).
  (f) The Phase 4 amendment adds that the Desktop run was in cloud mode and that
  `plugin:sn:serena` failed to connect (T2
  `nexus/desktop-install-friction-qwent-2026-10-11`). Text and evidence only; no
  status change.
- 2026-10-15: closure text pass, round 3, and the support declaration. (a) The
  guest results for the Smart App Control detection (T2
  `224-sac-detection-guest-runs-2026-10-15`): on the Home guest's Smart App
  Control checkpoint Windows blocked `nx.exe` at install, while uv, its Python
  3.12.15, Claude Code and Git ran, so the refusal and the row could not
  report; on the evaluation checkpoint the candidate passed the gate 5 of 5. Key
  Discoveries adds this third run; three runs with Smart App Control on had
  three different outcomes, cause not known, and Gap 6, the signing Critical
  Assumption, the signing design, Risks, the Step 0.2 amendment, the Test Plan
  and Delivered row 6 now say the blocking behaviour varied between runs. (b)
  The absent state: where the registry value does not exist (Windows 10, older
  Windows 11), `nx doctor` shows no Smart App Control row. (c) The detection's
  release status: on develop, shipping in the next client release after 7.80.0,
  not in 7.80.0. (d) The reader probe and the refusal on real Windows: In a second run that day `nx.exe` ran, the reader (through the tool environment's Python) returned `on`, and `nx init` refused with the new message within a second, with no download; on the evaluation checkpoint the reader returned `evaluation`. (e) Windows switching Smart App Control from evaluation to on after
  install is expected, not observed, and the doctor warning can only speak
  before the switch; bead nexus-f9bgu.58 measures it and replaces "accepted, no
  bead" for that item. (f) Windows 10: every measured guest is Windows 11 x64
  (builds 26100 to 26300); Windows 10 is unmeasured and not supported
  (Technical Environment, Delivered row 7). (g) The declaration: Sam declared
  Windows 11 x64 supported at close on 2026-10-15, in the same conversation as
  the close decisions (T2 `224-close-decisions-2026-10-15`); Incremental
  adoption and the Delivered intro record it, with the Delivered residuals as
  its known limits. Text and evidence only; no status change.
