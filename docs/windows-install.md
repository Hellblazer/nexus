# Installing nexus on Windows

Native Windows 11 on x64 runs the full local stack: the `nx` client, the
nexus engine, PostgreSQL with pgvector, and the conexus plugin in Claude Code.
Windows is **tested but not yet declared supported**. The steps below are the
ones the RDR-224 Phase 5 gate ran on a clean Windows 11 machine with published
conexus 7.72.1 (T2 `nexus_rdr/224-gate-run-7.72.1-2026-10-07`); where a step
was not measured, it says so.

## Before you start

- **Windows 11 on x64.** Windows on ARM is not supported: there is no engine
  build for it.
- **No administrator rights are needed.** Every step installs per user. The
  gate ran as a member of Administrators without elevation (a filtered token);
  a standard, non-administrator account has not been measured.
- **No Visual C++ redistributable install is needed.** The engine and the
  PostgreSQL bundle carry the runtime DLLs beside their executables, and nx
  points its own Python modules (PDF extraction, local embedding) at those
  copies when the system has none (nexus-lqjll). A cloud-mode install, which
  never downloads the local engine, still needs the redistributable for PDF
  extraction.
- **The binaries are unsigned** (signing is deferred, nexus-dj01b). See
  [Signing and SmartScreen](#signing-and-smartscreen).

## 1. Install uv

In a normal (not elevated) PowerShell window:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

`winget install --id astral-sh.uv --scope user` works too; it is the hint the
plugin's preflight prints when `nx` is missing. Open a new PowerShell window
afterwards so `uv` is on your PATH.

**If `uv tool install` fails with "untrusted mount point (os error 448)",** a
stale `python3.exe` trampoline in `%USERPROFILE%\.local\bin` is shadowing uv's
managed Python (measured during RDR-218). Add
`--python-preference only-managed` to the install command below.

## 2. Install conexus and start the local stack

```powershell
uv tool install --python 3.13 conexus
```

Open a new window, then:

```powershell
nx init --yes
```

`nx init` downloads the pinned engine and PostgreSQL bundle (signature-checked),
creates the local database, fetches the bge-768 embedding model, and registers a
Task Scheduler logon task, `NexusStorageService`, that starts the stack when you
sign in. The first run downloads about 1.5 GB. Without `--yes` it asks before
registering the task. Check the result with:

```powershell
nx doctor
nx daemon service status
```

`status` should show `health: ok` and `pg: up`.

## 3. Claude Code and the plugin

Install Claude Code, then Git for Windows (the plugin marketplace is fetched with
git):

```powershell
irm https://claude.ai/install.ps1 | iex
winget install --id Git.Git -e --source winget
```

Open a new window, start `claude`, and add the plugin:

```
/plugin marketplace add Hellblazer/nexus
/plugin install conexus@nexus-plugins
```

Restart Claude Code. The conexus hooks and MCP tools then run against the local
stack. This path is the one the Phase 5 gate verified end to end: MCP store and
search from a live session, hooks firing, and `claude -p`.

## 4. Claude Desktop (the Desktop Extension)

Download `conexus.mcpb` from the
[latest GitHub release](https://github.com/Hellblazer/nexus/releases/latest) and
double-click it; Claude Desktop registers it under Settings → Connectors →
Desktop as "Conexus" ([Desktop deployment](desktop-deployment.md)). The extension talks to the stack step 2 started; it does not start
the service itself, so do step 2 first.

What was measured: the extension's launch command, run headless on the clean
machine exactly as Claude Desktop runs it, built its environment, served 44
tools, stored and searched, and on quit left no process of its own while the
stack kept running (nexus-ijue9.21). The Claude Desktop install dialog itself
has not been walked on Windows.

## Stopping, starting and upgrading

- `nx daemon service stop` stops the engine and leaves PostgreSQL running.
- `nx daemon service stop --with-pg` stops PostgreSQL too.
- Signing out, or restarting Windows, stops the stack cleanly, and the logon task
  starts it at the next sign-in (measured, nexus-f9bgu.46 and .51). Sleep is
  unmeasured.
- Upgrade with `nx self install`. It builds a new version beside the running one
  and never replaces files a running process uses. Do not use
  `uv tool install --force conexus` while the stack runs: it replaces files in use.
- Remove the logon task with `nx daemon service uninstall --autostart`.

## Signing and SmartScreen

The Windows binaries are not Authenticode-signed. What that means in practice:

- Files `nx init` downloads carry no Mark of the Web, so SmartScreen does not
  screen them.
- A file you download with a browser (such as `conexus.mcpb`) does carry it. The
  extension is opened by Claude Desktop rather than run by Windows; what
  SmartScreen shows on that path has not been observed.
- Smart App Control in enforcing mode blocks unsigned code, and that
  configuration has not been tested. An enterprise application-control policy
  (WDAC, AppLocker) may also block the engine.

## What is not supported or not measured

- Windows on ARM: not supported.
- A standard (non-administrator) account, Smart App Control in enforcing mode,
  host sleep, and the Claude Desktop install dialog: not measured.
