---
title: "Claude Desktop Deployment: Unified Chat and Cowork Surface"
id: RDR-126
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-23
related_issues:
  - nexus-bsjro
  - nexus-lbie5
related_rdrs:
  - RDR-120
  - RDR-105
---

# RDR-126: Claude Desktop Deployment: Unified Chat and Cowork Surface

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Nexus is consumed today through three MCP surfaces, all of which terminate at the same `nx-mcp` server and the same host T2 / T3 daemons (the substrate landed in RDR-120). The three surfaces are:

1. **Claude Code (terminal)**, installed via `/plugin marketplace add Hellblazer/nexus && /plugin install nx@nexus-plugins`. Shipping.
2. **Claude Cowork (cloud agents in Anthropic's VM)**, reached via the Anthropic SDK transport. Documented as working in `docs/container-integration.md` § Cowork, but no end-to-end verification artifact exists.
3. **Claude Desktop chat (the desktop app)**, no install path. Users with no Claude Code install have no way to use Nexus from Claude Desktop chat.

The three surfaces have inconsistent install / first-run / uninstall stories despite being architecturally similar. Claude Code uses the marketplace and a `SessionStart` hook that calls `ensure-running` (but does not install the daemon unit, so the daemon doesn't survive reboots after a fresh plugin install). Cowork inherits whatever the host is doing. Claude Desktop chat has no story. There is no in-chat uninstall path on any surface; users wanting to remove the daemon must shell out.

### Enumerated gaps to close

#### Gap 1: No `.mcpb` Desktop Extension for Claude Desktop chat

A user who has Claude Desktop installed and wants to use Nexus has no install path. The only documented MCP-server install model for Claude Desktop chat (outside of Claude Code) is manual `claude_desktop_config.json` editing plus `uv tool install conexus` plus `nx daemon t2 install --autostart`, which is a five-step manual setup and not viable for non-CLI users. MCPB v2.1.2's `server.type: "uv"` (manifest schema v0.4+) is the sanctioned one-click install path for Python MCP servers with compiled C-extension dependencies and is the right packaging for Nexus.

#### Gap 2: Cowork integration is undocumented at the level of end-to-end verification

`docs/container-integration.md` § Cowork claims the SDK-transport path works and shares state bidirectionally with the host CLI Claude. There is no test case, no recorded live run, and no regression artifact. A user reading the docs has to trust the claim. A future regression in the SDK bridge or daemon discovery path would not be caught by CI.

#### Gap 3: Claude Code plugin SessionStart hook installs no persistent daemon

The current plugin hook calls `nx daemon t2 ensure-running --quiet`, which spawns a daemon process for the current session but does not install the LaunchAgent (macOS) or systemd user unit (Linux). After a reboot, the daemon is gone until the next Claude Code session starts. The user who runs `/plugin install nx@nexus-plugins` and never reads the README never gets a persistent daemon. The first-run install logic developed for `.mcpb` should activate here too as a side-effect benefit.

#### Gap 4: No in-chat uninstall path

The only way to remove the daemon is `nx daemon t2 uninstall --autostart` from a terminal. A Claude Desktop chat user with no terminal habits cannot remove the daemon. The `.mcpb` uninstall flow in Claude Desktop removes the bundle but does NOT cascade to the LaunchAgent, leaving orphaned daemons after an apparent clean uninstall.

#### Gap 5: First-run UX is opaque

The Claude Code plugin's SessionStart hook runs silently. The `.mcpb` install path is not yet built, so there is no first-run UX at all there. A new user has no visibility into what was installed, where it lives, or how to remove it. The cross-surface design should standardize a one-time banner that explains the install side effects in clear terms.

## Context

### Background

Strategic synthesis on 2026-05-23 (persisted to T2 as `nexus-plugin-connector-strategy-2026-05-23`) ranked R4 "Build a .mcpb Desktop Extension (uv type)" as an M-effort recommendation and R5 "Reduce install to two steps with a shell installer" as an S-M alternative. The same synthesis surfaced that Cowork integration was claimed working but never verified, and identified daemon lifecycle inconsistency across surfaces as a cross-cutting concern.

The MCPB v2.1.2 spec (released December 2025) added `server.type: "uv"` for Python servers with compiled-extension dependencies. This is what unblocks Nexus packaging: `chromadb`, `tree-sitter`, and `pydantic-core` all have C / Rust extensions that the traditional `"type": "python"` MCPB cannot portably bundle. Azure MCP Server ships using the `"uv"` path in production (Microsoft Azure SDK blog, April 2026), establishing the precedent.

The RDR-120 substrate split (conexus 4.34.0+, shipped 2026-05-22) is what makes one daemon serve all surfaces. Without it, each surface would have its own SQLite file and silently diverge.

### Technical Environment

- **conexus** ≥ 4.34.6 (PyPI), CLI entry point `nx`, MCP entry points `nx-mcp` (= `nexus.mcp.core:main`) and `nx-mcp-catalog` (= `nexus.mcp.catalog:main`).
- **MCPB** v2.1.2, manifest schema v0.4. Toolchain: `npm install -g @anthropic-ai/mcpb`. Spec: `github.com/modelcontextprotocol/mcpb`.
- **T2 daemon** from RDR-120. LaunchAgent at `~/Library/LaunchAgents/com.nexus.t2.plist` (macOS) or systemd user unit at `~/.config/systemd/user/nexus-t2.service` (Linux). Discovery file at `~/.config/nexus/t2_addr.<uid>`. Install logic in `src/nexus/commands/daemon.py`.
- **Cowork** uses `--mcp-config` with `"type": "sdk"` to bridge host MCP servers into the Cowork VM. Documented in `docs/container-integration.md` § Cowork.

## Research Findings

### Investigation

- **MCPB manifest spec (v0.4)** read at `github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md`. Server types `node`, `python`, `binary`, `uv`. The `uv` type ships a `pyproject.toml` instead of bundled deps; host provides the uv runtime and installs deps on first launch.
- **build-mcpb skill** (`mcp-server-dev:build-mcpb`) confirms: "Handles compiled dependencies (pydantic, numpy, chromadb, etc.). Eliminates manual pre-compilation burden." Reference example: `examples/hello-world-uv` in the mcpb repo.
- **Container integration document** (`docs/container-integration.md` lines 182-220) describes the Cowork SDK transport: Claude Desktop passes configured MCP servers into the VM via `--mcp-config` with `"type": "sdk"`; the MCP server stays on the host; the VM's tool calls are bridged through the Anthropic SDK channel. State is shared with host CLI Claude through the same T2 / T3 daemons.
- **MCP spec** for user-visible communication: `notifications/message` for log-level messages and tool-response `content` for in-thread text. Client behavior varies; safest pattern is to use both.
- **Current SessionStart hook** at `nx/hooks/scripts/session_start.py` runs `nx daemon t2 ensure-running --quiet` only; never calls `install --autostart`.
- **Strategic synthesis 2026-05-23 (T2)**: cited 16 sources; identified competitive landscape (Context7 49k stars, cognee 14k, mem0 634); confirmed `nx` plugin name collides with nrwl Nx Q1 2026 Claude Code plugin (separate concern, bead `nexus-mkj6u`).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `@anthropic-ai/mcpb` | Yes (spec repo) | Manifest schema v0.4 with `server.type: "uv"` supports Python servers carrying `pyproject.toml`; no pre-bundled deps; host provides uv runtime |
| MCP protocol (notifications) | Yes (modelcontextprotocol/specification) | `notifications/message` is the spec mechanism for server → client log-level messages; clients may render them |
| `src/nexus/commands/daemon.py` | Yes | Install logic for LaunchAgent / systemd unit is already implemented; needs lifting to a callable function so MCP startup can use it without shelling out |
| `docs/container-integration.md` § Cowork | Yes | Bidirectional state-sharing via host daemons documented; no test artifact exists |

### Key Discoveries

- **Verified** — RDR-120's daemon substrate is the load-bearing piece that makes this RDR's cross-surface story possible. Without one arbitrated writer, each MCP consumer would have its own SQLite file.
- **Verified** — Cowork uses SDK transport, not network. The Cowork VM has a strict allowlist (`api.anthropic.com`, `pypi.org`, `registry.npmjs.org`); TCP loopback and UDS mount paths do not apply. State sharing happens through the SDK bridge.
- **Verified (spike 2026-05-23)** — MCPB v0.4 `"uv"` server type resolves Nexus's full compiled-extension dep stack (chromadb Rust, pydantic-core Rust, tree-sitter C, numpy C, torch native, onnxruntime native, lxml C, pymupdf C, grpcio C, mineru + 227 others). Cold-run 19s, warm-run 4.5s on M-series Mac. `nx-mcp` boots cleanly via stdio and all 31 tools register in Claude Desktop's Connectors panel. The Azure MCP Server precedent is real and extends to Nexus's harder dep graph. Full report: `mcpb/SPIKE.md`.
- **Verified (spike 2026-05-23)** — Claude Desktop does NOT bundle `uv`; the manifest's `mcp_config.command` literally invokes `uv run`. Host must have `uv` on PATH or the spawn fails. README install instructions must list `uv` as a hard pre-requisite (e.g. `brew install uv` / `pipx install uv`).
- **Verified (spike 2026-05-23)** — When a user already has the Claude Code plugin (`nx@nexus-plugins`) installed, the Claude Desktop chat surface (local-agent mode) already exposes Nexus tools via the `plugin:nx:nexus` namespace. The `.mcpb` installs as a SECOND copy under the bare `nexus` namespace. They coexist on disk and in the UI; tool-name strings are distinct (`mcp__plugin_nx_nexus__memory_get` vs `mcp__nexus__memory_get`) so there is no hard collision, but they ARE functionally duplicate. This reframes the `.mcpb`'s strategic audience: not "the install path for Claude Desktop chat" but "the install path for Claude Desktop users who don't have Claude Code".
- **Verified (spike 2026-05-23 17:00 UTC)** — Cowork SDK bridge round-trips bidirectionally via the host T2 daemon. Host wrote `sentinel-host-to-cowork`; Cowork VM read it, then wrote `sentinel-cowork-to-host`; host re-read Cowork's write. The Cowork→host write was visible from the host within ~4 seconds. Confirms the docs/container-integration.md § Cowork claim with a documented live trace.
- **Documented** — Claude Desktop has three MCP integration models: local MCP (manual config), Desktop Extensions (`.mcpb`), Custom Connectors (remote MCP via OAuth). Only `.mcpb` matches Nexus's local-first architecture.
- **Documented** — MCPB has no signing-trust signal in either Anthropic marketplace as of May 2026; distribution carries no signing guarantee.
- **Assumed** — `notifications/message` renders in Claude Desktop chat in a user-visible way. Spike scope-narrowed; verification deferred to Phase 2 where the banner-emission code lives.

### Critical Assumptions

- [x] **A1**: Claude Desktop's MCP-spawn context provides a `uv` runtime such that a `"type": "uv"` MCPB resolves `conexus` and its compiled C-extension dependencies on first launch — **Status**: Verified 2026-05-23 — **Method**: Spike (`mcpb/SPIKE.md`). Refinement: uv is NOT bundled by Claude Desktop; uv must be on host PATH. Documented as a hard pre-requisite.
- [ ] **A2**: `notifications/message` (severity `info`) emitted by the MCP server renders in Claude Desktop chat in a way the user notices — **Status**: Unverified — **Method**: Spike deferred to Phase 2 (the banner-emission code does not exist yet; spike honestly narrowed scope to packaging feasibility)
- [ ] **A3**: LaunchAgent install from inside MCP startup succeeds without elevated privileges (writes to `~/Library/LaunchAgents/` which is user-owned) — **Status**: Verified — **Method**: Source Search (`src/nexus/commands/daemon.py` already does this)
- [ ] **A4**: MCPB uninstall via Claude Desktop removes the bundle but does NOT cascade to LaunchAgent removal — **Status**: Verified — **Method**: Docs Only (no manifest hook in MCPB spec for uninstall-time actions)
- [x] **A5**: Cowork SDK transport end-to-end bidirectional state-sharing works as documented (sentinel test) — **Status**: Verified 2026-05-23 17:00 UTC — **Method**: Spike (bidirectional sentinel exercised via T2 memory_put / memory_get). Host wrote `sentinel-host-to-cowork` at 15:00:46Z. Cowork VM read it successfully, then wrote `sentinel-cowork-to-host` at 17:00:44Z. Host re-read the Cowork write at 17:00:48Z (id 1465). Round-trip latency through the SDK bridge: ~4 seconds for the write side. State is genuinely shared via host T2 daemon; the docs/container-integration.md § Cowork claim is now backed by a documented live trace.
- [ ] **A6**: First-run marker (`~/.config/nexus/.mcp_first_run_complete`) is the right granularity to distinguish "banner shown" from "OS unit installed"; OS unit is the source of truth for install state — **Status**: Verified — **Method**: Source Search (no current code conflates these)

**Method definitions**:

- **Source Search**: API verified against dependency source code
- **Spike**: Behavior verified by running code against a live service
- **Docs Only**: Based on documentation reading alone (insufficient for load-bearing assumptions)

## Proposed Solution

### Approach

The work decomposes into nine specification items, numbered for phase-review-gate cross-walk:

1. **Distribution architecture (three-surface, with audience reframing from spike).** Claude Code: marketplace path unchanged — serves the existing Claude Code user base. Claude Cowork: SDK transport unchanged (no new install artifact; verification artifact added). Claude Desktop chat: new `.mcpb` Desktop Extension via `"type": "uv"`, deps declared in a bundled `pyproject.toml` pinning `conexus>=<version>`. **Spike-driven audience refinement (2026-05-23):** the `.mcpb` is NOT a duplicate path for Claude Code users (who already get Nexus in Claude Desktop chat via the plugin's `plugin:nx:nexus` namespace); it's the install path for Claude Desktop users WITHOUT Claude Code. The three surfaces are documented as one unified product surface in `docs/desktop-deployment.md` (new doc), with the audience refinement made explicit.

2. **Daemon first-run on MCP startup.** All MCP entry points (`nx-mcp`, `nx-mcp-catalog`) call a shared `_first_run.ensure_installed()` function before serving tools. The function: (a) checks if the OS unit exists (LaunchAgent / systemd file) — if yes, skip install; (b) if no, invokes the existing install logic from `nexus.daemon.installer` (lifted from `src/nexus/commands/daemon.py`); (c) always calls `daemon t2 ensure-running` so the current session works. Idempotent across clean state, pre-installed state, and marker-only state. The OS unit is the source of truth.

3. **First-run banner.** On first MCP startup after `ensure_installed()` runs (whether it installed or skipped), `_first_run.maybe_banner()` checks the marker `~/.config/nexus/.mcp_first_run_complete`. If absent: queue banner content for the first tool response (prepend to `content[0].text`) AND emit `notifications/message` with severity `info`. Mark shown after either channel succeeds. Two text variants: "Daemon installed at <path>" (fresh install) and "Daemon already configured at <path>" (pre-installed). Both variants include the in-chat uninstall instruction.

4. **`daemon_uninstall` MCP tool.** Exposed by `nx-mcp`. Parameters: `confirm: bool = false`, `remove_data: bool = false`. Default `confirm=false` returns a description of what would be removed and asks the model to call again with `confirm=true`. With `confirm=true`: removes the LaunchAgent / systemd unit, stops the daemon, removes the first-run marker. With `remove_data=true`: also wipes `~/.config/nexus/`. The tool is exposed in `nx-mcp` only (not `nx-mcp-catalog`) to avoid duplication.

5. **Cowork verification.** Add an integration test (`tests/test_cowork_sdk_bridge.py`) that records the expected bidirectional sentinel pattern documented in this RDR's §5 below, with a fixture that exercises the daemon-side state. The actual cross-process Cowork bridge can only be exercised by hand (Claude Desktop UI action); the integration test verifies the host-side surface (T2 client through `nx-mcp` round-trips a write/read) so any regression in the substrate is caught even though the bridge itself is not unit-testable. Live verification recipe is committed to `docs/desktop-deployment.md` § Cowork verification.

6. **Cross-surface consistency.** The same `_first_run.ensure_installed()` runs in all MCP startup paths. This means installing the Claude Code plugin and starting a session for the first time also runs the install (currently only `ensure-running`). Side effect: patches Gap 3 (plugin installed but daemon never persistently installed). The SessionStart hook continues to call `ensure-running` for the rare case where the MCP server has not been started yet in the session, but the install-on-first-MCP-spawn path dominates in practice.

7. **`.mcpb` manifest schema v0.4 with `server.type: "uv"`.** Repository layout: new `mcpb/` directory at repo root containing `manifest.json` and a bundled `pyproject.toml` pinned to `conexus>=<version>`. Manifest fields: `manifest_version: "0.4"`, `name: "nexus"`, `version: <from pyproject.toml>`, `description`, `author`, `server.type: "uv"`, `server.entry_point: "src/server.py"` (or equivalent), `compatibility.claude_desktop: ">=1.0.0"`, `compatibility.platforms: ["darwin", "linux"]`. `user_config` optional for nexus data directory.

8. **Out of scope.** Signing trust signals (no spec support in MCPB v2.1.2). Windows Claude Desktop (daemon installer has no Windows path; defer to a follow-up RDR). MCP Apps interactive UI for `nx_answer` (LB2 in the strategic synthesis; separate large bet). Marketplace submission of the Claude Code plugin and Smithery listing (QW2/QW3 in the strategic synthesis; not technical, do separately).

9. **Risks.** See § Risks and Mitigations below.

### Technical Design

**New module: `src/nexus/mcp/_first_run.py`**

```text
# Illustrative — verify API signatures during implementation

def ensure_installed() -> InstallStatus:
    """Idempotent. OS unit is source of truth.

    Returns:
      InstallStatus.ALREADY_PRESENT — OS unit exists, no action taken
      InstallStatus.NEWLY_INSTALLED — installed unit, started daemon
      InstallStatus.FAILED — install failed; daemon ensured-running for session only
    """

def maybe_banner(status: InstallStatus) -> BannerSpec | None:
    """One-shot per marker. Returns None if marker file exists."""

def mark_shown() -> None:
    """Write the marker after banner delivery."""

def uninstall(remove_data: bool = False) -> UninstallReport:
    """Remove OS unit, stop daemon, remove marker. Optional data wipe."""
```

**Lift from `src/nexus/commands/daemon.py`:**

The existing CLI command logic for `install --autostart` becomes a callable function `nexus.daemon.installer.install_autostart()` (verified API on click vs library code separation). The CLI command becomes a thin wrapper over the library function.

**Banner delivery:**

The MCP server holds a queued banner spec until the first tool call after install. The tool-dispatch path checks the queue, prepends the banner text to the response's `content[0].text`, marks the queue empty. Also emits a `notifications/message` of severity `info` with the same content.

**Verification of A1 (uv availability):**

The spike packs a hello-world MCPB declaring `conexus` as a dep, installs it on a clean Claude Desktop, observes whether the MCP server reaches `serve()` (deps resolved) or fails (deps did not). The spike report records the empirical result.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `_first_run.ensure_installed()` | `src/nexus/commands/daemon.py` (CLI install logic) | Extract: lift to `src/nexus/daemon/installer.py`; CLI command becomes thin wrapper |
| `_first_run.maybe_banner()` | none | New |
| `daemon_uninstall` MCP tool | `src/nexus/commands/daemon.py` uninstall logic + `src/nexus/mcp/core.py` tool registration | Extract uninstall logic to library + register new tool wrapping it |
| `mcpb/manifest.json` | none | New |
| `mcpb/pyproject.toml` | repo-root `pyproject.toml` declares `conexus` (this is itself the package) | New (separate file that pins the published version of `conexus`; not the dev pyproject) |
| `docs/desktop-deployment.md` | `docs/container-integration.md` § Cowork (Cowork-only) | New umbrella doc that links to container-integration for Cowork, adds `.mcpb` and Claude Code chapters |
| Cowork SDK-bridge integration test | none | New: `tests/test_cowork_sdk_bridge.py` |

### Decision Rationale

The unified scope (chat + Cowork + Claude Code first-run) is correct because all three surfaces share the same substrate (RDR-120 daemon) and the same MCP entry points. Designing the `.mcpb` first-run logic in isolation would force a second design pass when the same logic obviously applies to the Claude Code plugin SessionStart path. Designing the daemon-install lifecycle without a Cowork verification artifact would leave Cowork at "documented, untested" forever.

Treating MCPB `server.type: "uv"` as the only viable Python path was driven by the compiled-C-extension constraint. `"type": "python"` cannot portably bundle `chromadb` / `tree-sitter` / `pydantic-core`; the Azure MCP Server precedent and the explicit MCPB v0.4 release notes both point at `"uv"` as the intended solution.

Idempotency rules treat the OS unit as the source of truth, not the marker file, because the OS unit is what determines whether the daemon survives reboots. The marker only records UX state ("have we surfaced the banner once").

The `daemon_uninstall` tool with default `confirm=false` matches MCP destructive-operation conventions: the model doesn't accidentally remove user state on a single utterance; the user has to explicitly approve.

## Alternatives Considered

### Alternative 1: Two separate RDRs (Chat + Cowork verification)

**Description**: Split the work into RDR-126 (.mcpb Desktop Extension only) and RDR-127 (Cowork verification + Claude Code first-run patch).

**Pros**:

- Smaller per-RDR scope
- Faster initial RDR-accept

**Cons**:

- The first-run / banner / uninstall code is identical across surfaces; splitting forces either duplication or one RDR to forward-reference the other
- Cowork verification gets deferred indefinitely (same pattern as today's "documented but not tested")
- Cross-surface consistency story gets lost between the two RDRs

**Reason for rejection**: The user's framing ("the chat stuff is intimately connected") is correct. Same substrate, same MCP server, same lifecycle. Designing them together is cheaper than two passes.

### Alternative 2: Remote MCP via OAuth (Custom Connector)

**Description**: Host a public Nexus endpoint and expose it as a Claude Desktop Custom Connector via OAuth.

**Pros**:

- One-click connect from any Claude surface
- No bundle to ship, no host installer
- Mobile / web surfaces work too

**Cons**:

- Requires a hosted endpoint, breaking the local-first ethos that is one of Nexus's core value props
- Multi-tenancy adds significant operational complexity
- User data leaves the user's machine, conflicting with the project's privacy model

**Reason for rejection**: Out of scope for the current architecture. May be revisited as a separate offering, not as a replacement.

### Briefly Rejected

- **MCPB `"type": "python"` (traditional bundle)**: Cannot portably bundle compiled C extensions, eliminating chromadb / tree-sitter / pydantic.
- **MCPB `"type": "binary"`**: Would require pre-compiled binaries per platform; loses uv's cross-platform dependency resolution.
- **Shell installer alone (R5 in strategic synthesis)**: Helps CLI users but doesn't reach Claude Desktop chat users with no terminal habits.

## Trade-offs

### Consequences

- **Positive**: Claude Desktop chat becomes a first-class Nexus surface for the first time
- **Positive**: Cowork integration moves from "documented" to "documented and tested"
- **Positive**: Claude Code plugin first-run UX gets the daemon-install side benefit (Gap 3 patched)
- **Positive**: Users can uninstall from chat (no terminal required)
- **Negative**: MCPB uninstall has a documented orphan-LaunchAgent caveat — Claude Desktop's uninstall does not cascade
- **Negative**: One more distribution channel to maintain (release workflow gains an `mcpb pack` step + asset upload)
- **Negative**: First-run banner adds latency to the first tool call on a fresh install (single LaunchAgent file write; sub-second)

### Risks and Mitigations

- **Risk**: Claude Desktop does not provide a `uv` runtime, breaking the `"type": "uv"` install entirely
  **Mitigation**: P1 spike empirically verifies before any further work commits. If `uv` is absent, fall back to documenting an install pre-requisite ("install uv before installing this .mcpb") and shipping with that constraint.

- **Risk**: `notifications/message` is silently dropped by Claude Desktop, banner never reaches user
  **Mitigation**: Dual-channel delivery (banner also prepended to tool-response content). Tool content is the primary channel; notification is best-effort.

- **Risk**: `.mcpb` uninstall leaves a daemon that the user thought they removed
  **Mitigation**: Document prominently in `docs/desktop-deployment.md` and in the `daemon_uninstall` tool description; emit a final-run banner before bundle uninstall is not possible per MCPB spec, so documentation + tool discoverability is the mitigation.

- **Risk**: Cowork SDK bridge changes upstream and our verification rots
  **Mitigation**: `tests/test_cowork_sdk_bridge.py` exercises the host-side surface (T2 round-trip via `nx-mcp`); the bridge itself can only be checked by hand. Hand verification is committed as a recipe in `docs/desktop-deployment.md` for periodic re-check.

- **Risk**: Existing CLI users see unexpected behavior when MCP startup now installs the daemon unit
  **Mitigation**: Idempotent install (OS unit detection); pre-existing units are detected and skipped. Banner text variant handles this case.

### Failure Modes

- **MCP startup install fails (write permission, unsupported platform)**: `ensure_installed()` returns `FAILED`; daemon still spawned via `ensure-running` so the current session works; banner shows the failure with manual-install instructions. Visible to developer through MCP server stderr.
- **Banner content malformed**: Tool response is malformed; first tool call appears to fail. Mitigated by treating banner as best-effort: try/except around prepend; on exception, log to stderr and serve the tool response unchanged. Marker still gets written (one-shot semantics preserved).
- **uv runtime absent on Claude Desktop chat host**: MCPB install fails with "uv not found"; no daemon installed; user sees a Claude Desktop install error. Mitigated by README documentation pointing at `brew install uv` (macOS) or `pip install uv` / `pipx install uv` (Linux).
- **Cowork SDK bridge dropped a tool call**: Test sentinel write never appears on host side. Diagnostic recipe: check `nx daemon t2 status` then `nx memory list -p _cowork_test`.

## Implementation Plan

### Prerequisites

- [x] **A1** verified via 2026-05-23 spike (`mcpb/SPIKE.md`).
- [ ] **A2** verified during Phase 2 (banner-emission code lives there; spike honestly narrowed scope).
- [x] **A5** verified 2026-05-23 17:00 UTC (bidirectional Cowork sentinel test — host wrote, Cowork read+wrote, host re-read).
- [x] **GUI-spawn credential bug** (`nexus-m7evs`) merged to develop (PR #935 squash-merged 2026-05-23 as `4f54f77e`).
- [ ] RDR-126 accepted via `/nx:rdr-gate` + `/nx:rdr-accept`.
- [ ] Strategic-synthesis rename decision (`nexus-mkj6u`) made and shipped, OR explicitly deferred to a later RDR (decoupled scope).

### Minimum Viable Validation

A user with no prior Nexus install on a fresh macOS user account can: (1) double-click a downloaded `nexus.mcpb`, (2) see the install banner in their first Claude Desktop chat turn, (3) successfully execute `mcp__plugin_nx_nexus__memory_put` and `memory_get`, (4) ask Claude to uninstall via the `daemon_uninstall` tool, (5) observe that the LaunchAgent is gone after restart.

### Phase 1: Spike (RDR-126 §1, §2, §3, §7)

#### Step 1: Hello-world MCPB

Author a minimal `mcpb/manifest.json` and `mcpb/pyproject.toml` declaring `conexus>=<current>`. Pack with `mcpb pack`. Install in fresh Claude Desktop on macOS.

#### Step 2: A1 verification

Observe whether `uv` resolves `conexus` and its compiled-C-extension deps in Claude Desktop's MCP-spawn context. Record verdict.

#### Step 3: A2 verification

Emit a `notifications/message` of severity `info` from the MCP server startup. Observe whether it appears anywhere user-visible in Claude Desktop chat. Record verdict.

#### Step 4: Idempotency test matrix

Manually toggle states: clean / OS unit pre-installed / marker pre-existing. Verify each combination behaves per § Approach §2 / §3.

#### Step 5: Spike report

Write `mcpb/SPIKE.md` documenting A1, A2, gotchas, and concrete decisions for Phase 2.

### Phase 2: Library lift + daemon install/uninstall (RDR-126 §2, §4, §6)

#### Step 1: Lift install logic from CLI

Move install / uninstall logic from `src/nexus/commands/daemon.py` to `src/nexus/daemon/installer.py`. CLI command becomes a thin wrapper.

#### Step 2: `_first_run.py`

Implement `ensure_installed()`, `maybe_banner()`, `mark_shown()`, `uninstall()`.

#### Step 3: Wire into MCP entry points

`src/nexus/mcp/core.py` and `src/nexus/mcp/catalog.py` call `ensure_installed()` on startup; `core.py` registers `daemon_uninstall` tool; both prepend banner to first tool response.

#### Step 4: Tests

Unit tests for `_first_run` covering all idempotency states. Integration test verifying MCP entry point honors first-run path.

### Phase 3: Manifest authoring (RDR-126 §7)

#### Step 1: Production manifest

Author `mcpb/manifest.json` with full metadata: name, version, description, author, compatibility constraints, optional user_config.

#### Step 2: pyproject.toml pinning

`mcpb/pyproject.toml` pins `conexus>=<release version>`. Sync with the release workflow to update on tag push.

### Phase 4: Release integration

#### Step 1: Workflow change

Add `mcpb pack` step to `.github/workflows/release.yml`. Upload `nexus.mcpb` as a release asset on tag push (alongside the wheel and sdist).

#### Step 2: README + docs/desktop-deployment.md

Update `README.md` install section with the `.mcpb` path. Author `docs/desktop-deployment.md` covering all three surfaces and the uninstall caveat.

### Phase 5: Cowork verification (RDR-126 §5)

#### Step 1: Live sentinel test

Execute the bidirectional sentinel recipe documented in this RDR § Test Plan; record results in `docs/desktop-deployment.md`.

#### Step 2: Host-side integration test

`tests/test_cowork_sdk_bridge.py` exercises T2 round-trip through `nx-mcp` (the host-observable surface of the Cowork bridge).

### Phase 6: Clean-machine verification

Test full install + first-run + uninstall on a fresh macOS user account and a Linux VM. Update `docs/desktop-deployment.md` with any gotchas.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| LaunchAgent / systemd unit | `nx daemon t2 status` | `nx daemon t2 status` | `nx daemon t2 uninstall --autostart` OR `daemon_uninstall` MCP tool | OS unit absent after delete | OS unit recreatable from CLI |
| First-run marker | `ls ~/.config/nexus/.mcp_first_run_complete` | `cat` for timestamp | Removed by `daemon_uninstall` | Banner re-shows after deletion | Stateless; no backup needed |
| `.mcpb` bundle in Claude Desktop | Claude Desktop Settings → Extensions | Claude Desktop UI | Claude Desktop Uninstall (LaunchAgent orphans — documented) | UI no longer lists | Re-download from GitHub release |

### New Dependencies

- **`@anthropic-ai/mcpb`** (dev dep, runtime in CI only): Node CLI for packing `.mcpb`. License: open source per `modelcontextprotocol/mcpb`. No legal review required.

## Test Plan

- **Scenario**: Hello-world MCPB packs and installs on fresh Claude Desktop — **Verify**: First MCP tool call succeeds, deps resolved through `uv`. (A1 verification.)
- **Scenario**: `notifications/message` emitted from MCP server startup — **Verify**: Message reaches user-visible surface in Claude Desktop chat. (A2 verification.)
- **Scenario**: MCP startup with no pre-existing LaunchAgent — **Verify**: LaunchAgent installed at `~/Library/LaunchAgents/com.nexus.t2.plist`, marker written, banner queued.
- **Scenario**: MCP startup with pre-existing LaunchAgent from CLI install, no marker — **Verify**: LaunchAgent untouched, marker written, banner queued with "already configured" variant.
- **Scenario**: MCP startup with both LaunchAgent and marker — **Verify**: No filesystem changes, no banner, daemon ensured-running.
- **Scenario**: Cowork session writes T2 sentinel, host session reads — **Verify**: Round-trip succeeds, host sees Cowork's write.
- **Scenario**: Host session writes T2 sentinel, Cowork reads — **Verify**: Cowork sees host's write.
- **Scenario**: `daemon_uninstall(confirm=false)` — **Verify**: Returns description of what would be removed; no filesystem changes.
- **Scenario**: `daemon_uninstall(confirm=true)` — **Verify**: LaunchAgent removed, daemon stopped, marker removed.
- **Scenario**: `daemon_uninstall(confirm=true, remove_data=true)` — **Verify**: All of above + `~/.config/nexus/` removed.
- **Scenario**: `.mcpb` uninstall via Claude Desktop UI — **Verify**: Bundle removed, LaunchAgent persists (documented orphan condition).

## Validation

### Testing Strategy

1. **Scenario**: A1 spike (uv availability)
   **Expected**: Empirical pass / fail recorded in `mcpb/SPIKE.md`; if fail, drives manifest constraint update.

2. **Scenario**: A2 spike (notifications visibility)
   **Expected**: Empirical pass / fail recorded; if fail, banner relies solely on tool-content prepend.

3. **Scenario**: A5 spike (Cowork live sentinel)
   **Expected**: Recipe documented in `docs/desktop-deployment.md`; pass = bidirectional round-trip succeeds (executing during RDR draft phase in user's current session).

4. **Scenario**: Idempotency matrix (4 states × 2 platforms)
   **Expected**: All 8 combinations behave per spec; no piled-up plists, no double banners.

### Performance Expectations

First-run install adds one LaunchAgent file write to MCP startup. Sub-second. Subsequent runs (OS unit detected) add only a `stat()` call to startup. No measurable impact.

## Finalization Gate

### Contradiction Check

[To be filled at gate time.]

### Assumption Verification

[To be filled at gate time. A1, A2, A5 require spike completion before gate-pass.]

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `mcpb pack` / `mcpb init` | `@anthropic-ai/mcpb` | Docs Only (spec read) |
| `notifications/message` | MCP protocol | Docs Only (spec read) |
| LaunchAgent install | `nexus.daemon.installer` (lifted) | Source Search |
| `daemon t2 ensure-running` | `nexus.daemon.installer` | Source Search |

### Scope Verification

[To be filled at gate time. Minimum Viable Validation is in-scope (Phase 1 spike result drives whether it remains feasible).]

### Cross-Cutting Concerns

- **Versioning**: `.mcpb` bundle version is read from `pyproject.toml`; release workflow keeps it in sync with conexus version.
- **Build tool compatibility**: `mcpb pack` is a Node tool; release workflow needs Node available. CI already has Node for `sn` plugin's `npx context7-mcp` dep checks.
- **Licensing**: `@anthropic-ai/mcpb` is open source per the spec repo; no legal review required.
- **Deployment model**: Three surfaces — Claude Code (marketplace), Cowork (SDK), Chat (`.mcpb`). All point at the same `nx-mcp` entry point.
- **IDE compatibility**: N/A — this is a Claude Desktop concern, not IDE.
- **Incremental adoption**: Yes — Claude Code plugin first-run install logic activates whether `.mcpb` is shipped or not. User can adopt the daemon-install side benefit before the `.mcpb` is published.
- **Secret/credential lifecycle**: N/A — Nexus is local-first; no secrets in the MCP server (Voyage API key is per-user CLI config).
- **Memory management**: N/A — install is filesystem-only; daemon memory profile unchanged from RDR-120.

### Proportionality

The RDR is sized for a cross-surface lifecycle change. Sections covering individual phases are slim; sections covering the substrate (Approach §2, §3, §6) are denser because that is where the load-bearing decisions live.

## References

- `docs/container-integration.md` § Cowork (existing Cowork integration model)
- `docs/architecture.md` (T2 daemon substrate from RDR-120)
- `docs/rdr/rdr-120-storage-substrate-split.md` (daemon substrate)
- `docs/rdr/rdr-105-t1-sub-agent-contract.md` (T1 process-local discipline; informs Cowork T1 isolation)
- MCPB v0.4 manifest spec: `github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md`
- MCPB repo: `github.com/modelcontextprotocol/mcpb`
- MCP protocol notifications: `modelcontextprotocol.io` spec
- T2 memory `nexus-plugin-connector-strategy-2026-05-23` (strategic synthesis with 16 cited sources)
- Beads: `nexus-bsjro` (epic, will close on RDR-accept and re-spawn from §Approach), `nexus-lbie5` (spike, will close and re-spawn as Phase 1)

## Revision History

- 2026-05-23: Initial draft.
- 2026-05-23 (later): Folded Phase 1 spike results. A1 verified. A2 method
  refined to "Spike deferred to Phase 2" (banner-emission code does not
  exist yet; spike honestly narrowed scope to packaging feasibility). New
  Key Discovery: spike `.mcpb` install + 31 tools register; uv is NOT
  bundled by Claude Desktop (hard pre-requisite). New Key Discovery on
  coexistence with Claude Code plugin: tools live in distinct namespaces
  (`plugin:nx:nexus` vs `nexus`); they are functionally duplicate but
  technically separable. Strategic audience refinement of `.mcpb`: serves
  Claude Desktop users WITHOUT Claude Code, not existing Claude Code users.
  GUI-spawn credential bug (`nexus-m7evs`) discovered during spike
  verification; fixed and shipped in PR #935 on the same day.
- 2026-05-23 17:00 UTC: A5 verified. Bidirectional Cowork sentinel test
  passed: host wrote `sentinel-host-to-cowork` at 15:00:46Z, Cowork VM
  read it then wrote `sentinel-cowork-to-host` at 17:00:44Z, host re-read
  Cowork's write at 17:00:48Z. SDK bridge round-trips both directions
  through the host T2 daemon. RDR-126 §5 claim is now backed by a
  documented live trace. Only A2 remains outstanding (deferred to
  Phase 2 by design).
