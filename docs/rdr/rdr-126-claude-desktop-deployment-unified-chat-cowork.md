---
title: "Claude Desktop Deployment: Unified Chat and Cowork Surface"
id: RDR-126
type: Architecture
status: closed
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-23
accepted_date: 2026-06-02
closed_date: 2026-06-02
related_issues:
  - nexus-bsjro
  - nexus-lbie5
related_rdrs:
  - RDR-120
  - RDR-105
  - RDR-143
---

# RDR-126: Claude Desktop Deployment: Unified Chat and Cowork Surface

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Nexus is consumed today through three MCP surfaces, all of which terminate at the same `nx-mcp` server and the same host T2 / T3 daemons (the substrate landed in RDR-120). The three surfaces are:

1. **Claude Code (terminal)**, installed via `/plugin marketplace add Hellblazer/nexus && /plugin install conexus@nexus-plugins`. Shipping.
2. **Claude Cowork (cloud agents in Anthropic's VM)**, reached via the Anthropic SDK transport. Documented as working in `docs/container-integration.md` § Cowork, but no end-to-end verification artifact exists.
3. **Claude Desktop chat (the desktop app)**, no install path. Users with no Claude Code install have no way to use Nexus from Claude Desktop chat.

The three surfaces have inconsistent install / first-run / uninstall stories despite being architecturally similar. Claude Code uses the marketplace and a `SessionStart` hook that calls `ensure-running` (but does not install the daemon unit, so the daemon doesn't survive reboots after a fresh plugin install). Cowork inherits whatever the host is doing. Claude Desktop chat has no story. There is no in-chat uninstall path on any surface; users wanting to remove the daemon must shell out.

### Enumerated gaps to close

#### Gap 1: No `.mcpb` Desktop Extension for Claude Desktop chat

A user who has Claude Desktop installed and wants to use Nexus has no install path. The only documented MCP-server install model for Claude Desktop chat (outside of Claude Code) is manual `claude_desktop_config.json` editing plus `uv tool install conexus` plus `nx daemon t2 install --autostart`, which is a five-step manual setup and not viable for non-CLI users. MCPB v2.1.2's `server.type: "uv"` (manifest schema v0.4+) is the sanctioned one-click install path for Python MCP servers with compiled C-extension dependencies and is the right packaging for Nexus.

#### Gap 2: Cowork integration is undocumented at the level of end-to-end verification

`docs/container-integration.md` § Cowork claims the SDK-transport path works and shares state bidirectionally with the host CLI Claude. There is no test case, no recorded live run, and no regression artifact. A user reading the docs has to trust the claim. A future regression in the SDK bridge or daemon discovery path would not be caught by CI.

#### Gap 3: Claude Code plugin SessionStart hook installs no persistent daemon

The current plugin hook calls `nx daemon t2 ensure-running --quiet`, which spawns a daemon process for the current session but does not install the LaunchAgent (macOS) or systemd user unit (Linux). After a reboot, the daemon is gone until the next Claude Code session starts. The user who runs `/plugin install conexus@nexus-plugins` and never reads the README never gets a persistent daemon. The first-run install logic developed for `.mcpb` should activate here too as a side-effect benefit.

#### Gap 4: No in-chat uninstall path

The only way to remove the daemon is `nx daemon t2 uninstall --autostart` from a terminal. A Claude Desktop chat user with no terminal habits cannot remove the daemon. The `.mcpb` uninstall flow in Claude Desktop removes the bundle but does NOT cascade to the LaunchAgent, leaving orphaned daemons after an apparent clean uninstall.

#### Gap 5: First-run UX is opaque

The Claude Code plugin's SessionStart hook runs silently. The `.mcpb` install path is not yet built, so there is no first-run UX at all there. A new user has no visibility into what was installed, where it lives, or how to remove it. The cross-surface design should standardize a one-time banner that explains the install side effects in clear terms.

## Context

### Background

Strategic synthesis on 2026-05-23 (persisted to T2 as `nexus-plugin-connector-strategy-2026-05-23`) ranked R4 "Build a .mcpb Desktop Extension (uv type)" as an M-effort recommendation and R5 "Reduce install to two steps with a shell installer" as an S-M alternative. The same synthesis surfaced that Cowork integration was claimed working but never verified, and identified daemon lifecycle inconsistency across surfaces as a cross-cutting concern.

The MCPB v2.1.2 spec (released December 2025) added `server.type: "uv"` for Python servers with compiled-extension dependencies. This is what unblocks Nexus packaging: `chromadb`, `tree-sitter`, and `pydantic-core` all have C / Rust extensions that the traditional `"type": "python"` MCPB cannot portably bundle. No confirmed production `server.type: "uv"` MCPB exists in the ecosystem as of May 2026 (the Azure MCP Server, sometimes cited as a precedent, actually uses `server.type: "binary"` per its blog announcement; see "Assumption Research / A1" below). Whether the host provides `uv` at MCP-spawn time is the load-bearing assumption (A1).

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
- **Current SessionStart hook** at `conexus/hooks/scripts/session_start.py` runs `nx daemon t2 ensure-running --quiet` only; never calls `install --autostart`.
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
- **Documented** — MCPB v0.4 `"uv"` server type is specified to handle compiled dependencies via host-provided uv. No confirmed production `"uv"`-type MCPB exists as of May 2026 (Azure MCP Server uses `"binary"`, not `"uv"`); see Assumption Research / A1.
- **Documented** — Claude Desktop has three MCP integration models: local MCP (manual config), Desktop Extensions (`.mcpb`), Custom Connectors (remote MCP via OAuth). Only `.mcpb` matches Nexus's local-first architecture.
- **Documented** — MCPB has no signing-trust signal in either Anthropic marketplace as of May 2026; distribution carries no signing guarantee.
- **Verified (live spike, 2026-06-02)** — Claude Desktop's MCP-spawn context resolves `uv` (by absolute path, via an injected login-shell PATH) and launches the uv-type extension; the installed conexus `.mcpb` extension's `.venv` resolves `nexus` + compiled deps and `nx-mcp` serves. See §A1 below and Revision History.
- **Assumed** — `notifications/message` renders in Claude Desktop chat in a user-visible way. Likely-false (see §A2); the design uses tool-response content prepend as the primary channel, so this is not gate-blocking.

### Assumption Research

Resolution of the three remaining unverified assumptions (A1, A2, A5) as of 2026-05-28. Sources cited inline; full investigation persisted to T3 (`research-rdr126-claude-desktop-deployment-2026-05-28`).

#### A1 Research — uv runtime availability in Claude Desktop MCP-spawn context

**Sources**: MCPB MANIFEST.md v0.4 (`github.com/anthropics/mcpb/blob/main/MANIFEST.md`); MCPB schema v0.4 (`schemas/mcpb-manifest-v0.4.schema.json`); Anthropic Desktop Extensions engineering post (`anthropic.com/engineering/desktop-extensions`); MCPB issue #84 (`github.com/modelcontextprotocol/mcpb/issues/84`, closed as not planned, July 2025); MCPB issue #89 (`github.com/modelcontextprotocol/mcpb/issues/89`, open).

MCPB MANIFEST.md states "host application manages Python and dependencies automatically" for `server.type: "uv"` and "no user Python installation required." However, the Anthropic engineering post confirms only Node.js is bundled with Claude Desktop. MCPB issue #84 reports Claude Desktop marks uv-based extensions as incompatible when system Python is absent, even with uv installed. Issue #89 explicitly states the documentation gap: Python is not bundled with Claude Desktop.

The nexus `mcpb/manifest.json` includes `mcp_config.command: "uv"`. If Claude Desktop uses this field (as the schema requires), uv must be on the PATH that Claude Desktop inherits at GUI process spawn time, which on macOS typically does not include `~/.local/bin` or homebrew paths. The "Azure MCP Server = uv precedent" claim previously made in Background and Key Discoveries is incorrect; Azure uses `server.type: "binary"`.

The key ambiguity: MANIFEST.md calls `mcp_config` optional for the uv type (host manages uv internally), but the v0.4 JSON schema lists it as required. Whether Claude Desktop uses the `entry_point` field alone (host-managed uv path) or `mcp_config.command` (user-PATH-dependent uv path) is only resolvable by running the A1 spike.

**Verdict**: **VERIFIED by live spike (2026-06-02)** — the docs-only "likely-false" prediction below was OVERTURNED. The pre-spike reasoning (uv absent from the bare launchd GUI PATH) did not model Claude Desktop's actual behavior: Desktop injects a login-shell-like PATH at extension spawn and resolves `uv` by absolute path (`/Users/.../.local/bin/uv`). Evidence: the installed conexus uv-type `.mcpb` extension has a built `.venv` importing `nexus` + `chromadb` + `pydantic` (compiled C-extensions); `~/Library/Logs/Claude/mcp-server-Conexus.log` shows `Using MCP server command: .../.local/bin/uv` → `Server started and connected successfully` → `nx-mcp` over stdio → full `tools/list`; a prior `Nexus (Spike)` extension launched cleanly 2026-05-23. The `brew install uv` fallback is therefore adequate-to-unnecessary; an astral-installer `uv` at `~/.local/bin` suffices. Install surface: Settings → Extensions (Desktop Extensions), NOT the Claude Code `/plugin` installer (which rejects `.mcpb`). Full evidence: T2 `nexus_rdr/126-research-A1-A2-A5`.

_Historical (pre-spike) reasoning, retained for the record: docs suggested likely-false because MANIFEST.md claims the host manages uv while only Node.js is bundled (issues #84/#89), and the bare launchd GUI PATH excludes `~/.local/bin`/homebrew. The live spike showed Desktop augments PATH, so this prediction did not hold._

#### A2 Research — notifications/message rendering in Claude Desktop

**Sources**: MCP spec JSON schema 2024-11-05 (`github.com/modelcontextprotocol/specification`); Claude Code issue #3174 (`github.com/anthropics/claude-code/issues/3174`, closed as not planned, July 2025).

MCP spec defines `notifications/message` with severity levels matching RFC 5424 syslog: `debug`, `info`, `notice`, `warning`, `error`, `critical`, `alert`, `emergency`. Client obligation is **MAY** present log messages in the UI (not MUST). Claude Code issue #3174 confirms Claude Code receives these notifications at the MCP layer but does not surface them in the chat interface; Anthropic closed this as "not planned." No evidence Claude Desktop chat differs from Claude Code in this regard.

The RDR's dual-channel design (tool-response content prepend as primary, notification as best-effort) is confirmed correct. A2's likely failure does not invalidate the design; tool-content prepend is the reliable channel and is already the dominant delivery mechanism.

**Verdict**: Notification channel likely-false; design unaffected. Spike confirms behaviour but is not gate-blocking.

#### A5 Research — Cowork SDK transport state-sharing

**Sources**: `docs/container-integration.md` § Cowork (lines 182-220); T2 memory (project `nexus`) search for cowork/sentinel/sdk transport spike (no result); T3 knowledge corpus search (no result).

The documented transport model is architecturally verified: Claude Desktop passes configured MCP servers into the Cowork VM via `--mcp-config` with `"type": "sdk"`; tool calls bridge through the Anthropic SDK channel to host `nx-mcp` and host T2/T3 daemons. Shell-side `nx memory put` inside the VM does NOT work (no network path). T1 is process-local (RDR-105); cross-VM coordination requires T2.

No end-to-end sentinel run has been recorded. The "live test executing in user's session at draft time" note in A5 was aspirational, not a completed result.

**Verdict**: Architecture verified by docs and code. Bidirectional behavioural claim still requires the live sentinel test (Cowork agent writes T2 sentinel, host reads it; host writes T2 sentinel, Cowork agent reads it).

### Critical Assumptions

- [x] **A1**: Claude Desktop's MCP-spawn context provides a `uv` runtime such that a `"type": "uv"` MCPB resolves `conexus` and its compiled C-extension dependencies on first launch — **Status**: VERIFIED (live spike, 2026-06-02; the docs-only "likely-false" is OVERTURNED) — **Method**: Spike. Evidence on Claude Desktop 1.9255.2: conexus is installed as a uv-type `.mcpb` extension with a fully-built `.venv` (imports `nexus` + `chromadb` + `pydantic` compiled C-extensions); `~/Library/Logs/Claude/mcp-server-Conexus.log` shows `Using MCP server command: /Users/.../.local/bin/uv` → `Server started and connected successfully` → `nx-mcp` over stdio → full `tools/list`. A prior `Nexus (Spike)` extension also launched cleanly 2026-05-23. Claude Desktop injects a login-shell-like PATH at extension spawn and resolves `uv` by absolute path (NOT the bare launchd default), so the earlier "uv not on GUI PATH" concern is moot; `uv` at `~/.local/bin` (astral installer) suffices.
- [ ] **A2**: `notifications/message` (severity `info`) emitted by the MCP server renders in Claude Desktop chat in a way the user notices — **Status**: Unverified (docs suggest likely-false; design's primary channel is tool-response content prepend, which is reliable; see Assumption Research / A2) — **Method**: Spike (not gate-blocking)
- [x] **A3**: LaunchAgent install from inside MCP startup succeeds without elevated privileges (writes to `~/Library/LaunchAgents/` which is user-owned) — **Status**: Verified — **Method**: Source Search (`src/nexus/commands/daemon.py` already does this)
- [x] **A4**: MCPB uninstall via Claude Desktop removes the bundle but does NOT cascade to LaunchAgent removal — **Status**: Verified — **Method**: Docs Only (no manifest hook in MCPB spec for uninstall-time actions)
- [x] **A5**: Cowork SDK transport end-to-end bidirectional state-sharing works as documented (sentinel test) — **Status**: VERIFIED (live, 2026-06-02, user-attested) — **Method**: Spike. Architecture was already verified by docs + code; the live cross-surface interaction (Cowork VM ↔ host T2/T3 daemons, shared with CLI/plugin) was confirmed in a Cowork session. All surfaces interact correctly.
- [x] **A6**: First-run marker (`~/.config/nexus/.mcp_first_run_complete`) is the right granularity to distinguish "banner shown" from "OS unit installed"; OS unit is the source of truth for install state — **Status**: Verified — **Method**: Source Search (no current code conflates these)

**Method definitions**:

- **Source Search**: API verified against dependency source code
- **Spike**: Behavior verified by running code against a live service
- **Docs Only**: Based on documentation reading alone (insufficient for load-bearing assumptions)

## Proposed Solution

### Approach

The work decomposes into nine specification items, numbered for phase-review-gate cross-walk:

1. **Distribution architecture (three-surface).** Claude Code: marketplace path unchanged. Claude Cowork: SDK transport unchanged (no new install artifact; verification artifact added). Claude Desktop chat: new `.mcpb` Desktop Extension via `"type": "uv"`, deps declared in a bundled `pyproject.toml` pinning `conexus>=<version>`, **installed via Claude Desktop Settings → Extensions** (the Desktop Extensions surface — NOT the Claude Code `/plugin` installer, which rejects `.mcpb` with "plugin validation failed"). The three surfaces are documented as one unified product surface in `docs/desktop-deployment.md` (new doc).

2. **Daemon first-run on MCP startup.** All MCP entry points (`nx-mcp`, `nx-mcp-catalog`) call a shared `_first_run.ensure_installed()` function before serving tools. The function: (a) checks if the OS unit exists (LaunchAgent / systemd file) — if yes, skip install; (b) if no, invokes the existing install logic from `nexus.daemon.installer` (lifted from `src/nexus/commands/daemon.py`); (c) always calls `daemon t2 ensure-running` so the current session works. Idempotent across clean state, pre-installed state, and marker-only state. The OS unit is the source of truth.

3. **First-run banner.** On first MCP startup after `ensure_installed()` runs (whether it installed or skipped), `_first_run.maybe_banner()` checks the marker `~/.config/nexus/.mcp_first_run_complete`. If absent: queue banner content for the first tool response (prepend to `content[0].text`) AND emit `notifications/message` with severity `info`. Mark shown after either channel succeeds. Two text variants: "Daemon installed at <path>" (fresh install) and "Daemon already configured at <path>" (pre-installed). Both variants include the in-chat uninstall instruction.

> **Implementation note (deferral, 2026-06-02).** The shipped implementation delivers the banner via the tool-response content prepend only; the `notifications/message` channel is **deliberately deferred**. Per Assumption A2 (likely-false: Claude Desktop does not render `notifications/message`), the second channel carries no verified user-visible value today, and emitting it from the low-level `CallToolRequest` wrapper has no clean session handle. The content-prepend is the primary, load-bearing channel (delivered + marker-gated as specified). If a future spike confirms A2 (a Desktop version that renders notifications), adding the secondary emit is a localized follow-up; tracked under the staleness/drift follow-up bead family, not a silent scope reduction.
>
> **Amendment (2026-06-02, nexus-vlo2b — channel reversal after P6-B).** The P6-B live Desktop run (conexus 5.9.0 `.mcpb`) found the content-prepend banner is *delivered* (marker written) but the Claude Desktop model paraphrases the tool result and **drops it — the user never sees it.** A spike (`scripts/spikes/spike_rdr126_instructions_banner.py`) confirmed the banner survives into `InitializeResult.instructions` (the `initialize` handshake, the same channel `apply_embedder_notice` ships on, RDR-144 P5b). The banner's **primary channel is now the server `instructions` field**, framed as an explicit relay instruction; the content-prepend is retained only as an **injection-failure recovery path** (it fires solely when the instructions injection raises — e.g. a FastMCP-internals change — since both Desktop and Claude Code run the same FastMCP binary and the injection normally succeeds on both). `apply_first_run_banner_instructions` runs at startup in `nexus.mcp.core:main` before the dispatch hook; on success it marks the one-shot and clears the pending queue so the two channels never double-fire. The instructions channel marks at startup (delivered unconditionally at `initialize`) rather than deliver-then-mark — there is no in-session retry. **This reroute is a mechanism fix, not a verified user-visibility fix:** `instructions` is still model-mediated context, so true Desktop user-visibility (specifically that the model relays the `daemon_uninstall` instruction in its first reply) is **a gate, not a follow-up** — the nexus-vlo2b bead stays open until a live Desktop re-test on the release carrying this change confirms it.
>
> **Final verdict (2026-06-02, nexus-vlo2b — conclusive, docs-validated). The proactive first-run banner is NOT user-visible on Claude Desktop chat, and this is an externally-imposed client limitation, not a Nexus defect.** The 5.9.1 live re-test (conexus `5.9.1` `.mcpb` on an isolated Desktop profile) **proved** the banner is delivered into the `initialize` handshake — `~/Library/Logs/Claude/mcp-server-Conexus.log` captured the full banner verbatim in the `instructions` field — yet the model still did not relay it to the user. The MCP specification and Anthropic's own issue tracker explain why every server-emittable channel is unavailable for a proactive user-facing message:
>
> - **`instructions` is tool-usage guidance, not a user-message channel.** The MCP spec (2025-11-25, lifecycle/schema) defines it as *"guidance on how to use the server and its features … to help clients improve the language model's understanding of available tools and resources, potentially by being added to the system prompt"*; the Claude Code MCP docs describe server instructions as loaded at session start to help the model find tools, *"similar to how skills work."* The model absorbs it as background tool-guidance and correctly does not echo it verbatim — no relay framing overrides how the client categorises the field.
> - **`notifications/message` is received but not displayed.** `anthropics/claude-code#3174` ("Claude Code Receives But Doesn't Display Messages") was **closed as "not planned,"** and explicitly lists *"server introduction/welcome messages"* as a missing capability. The spec's "Clients MAY present log messages in the UI" is not exercised by Claude.
> - **MCP Apps inline UI does not render in Claude Desktop** (`anthropics/claude-ai-mcp#165`), so the richest channel (RDR-126 §8 out-of-scope LB2) is also unavailable.
>
> **Resolution (accept).** The 5.9.1 `instructions`-channel implementation is retained — it is correct, harmless, and the right primary channel for any client that surfaces instructions. No further channel work is warranted (two empirical negatives plus the spec plus Anthropic's "not planned" close). The banner's actionable purpose — *how to remove the daemon* — is served without a banner: `daemon_uninstall` is a discoverable, auto-approved `nx-mcp` tool and `docs/desktop-deployment.md` carries the uninstall recipe. Only the proactive "daemon installed" announcement is unachievable on the Desktop chat surface, and only there. `nexus-vlo2b` is closed with this verdict (docs-only; no 5.9.2). Full evidence + sources: T2 `nexus/rdr-126-banner-desktop-verdict`.

4. **`daemon_uninstall` MCP tool.** Exposed by `nx-mcp`. Parameters: `confirm: bool = false`, `remove_data: bool = false`. Default `confirm=false` returns a description of what would be removed and asks the model to call again with `confirm=true`. With `confirm=true`: removes the LaunchAgent / systemd unit, stops the daemon, removes the first-run marker. With `remove_data=true`: also wipes `~/.config/nexus/`. The tool is exposed in `nx-mcp` only (not `nx-mcp-catalog`) to avoid duplication.

5. **Cowork verification.** Add an integration test (`tests/test_cowork_sdk_bridge.py`) that records the expected bidirectional sentinel pattern documented in this RDR's §5 below, with a fixture that exercises the daemon-side state. The actual cross-process Cowork bridge can only be exercised by hand (Claude Desktop UI action); the integration test verifies the host-side surface (T2 client through `nx-mcp` round-trips a write/read) so any regression in the substrate is caught even though the bridge itself is not unit-testable. Live verification recipe is committed to `docs/desktop-deployment.md` § Cowork verification.

6. **Cross-surface consistency.** The same `_first_run.ensure_installed()` runs in all MCP startup paths. This means installing the Claude Code plugin and starting a session for the first time also runs the install (currently only `ensure-running`). Side effect: patches Gap 3 (plugin installed but daemon never persistently installed). The SessionStart hook continues to call `ensure-running` for the rare case where the MCP server has not been started yet in the session, but the install-on-first-MCP-spawn path dominates in practice.

7. **`.mcpb` manifest schema v0.4 with `server.type: "uv"`.** Repository layout: new `mcpb/` directory at repo root containing `manifest.json` and a bundled `pyproject.toml` pinned to `conexus>=<version>`. Manifest fields: `manifest_version: "0.4"`, `name: "conexus"` (matches the installed extension identifier `local.mcpb.<owner>.conexus` and the 5.0 package rename — NOT the legacy `nexus`), `version: <from pyproject.toml>`, `description`, `author`, `server.type: "uv"`, `server.entry_point: "src/server.py"` (or equivalent), `compatibility.claude_desktop: ">=1.0.0"`, `compatibility.platforms: ["darwin", "linux"]`. `user_config` optional for nexus data directory.

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

Treating MCPB `server.type: "uv"` as the only viable Python path was driven by the compiled-C-extension constraint. `"type": "python"` cannot portably bundle `chromadb` / `tree-sitter` / `pydantic-core`; the MCPB v0.4 release notes point at `"uv"` as the intended solution. (An earlier draft cited the Azure MCP Server as a `"uv"` precedent; that was incorrect — Azure actually uses `server.type: "binary"`. See Assumption Research / A1.)

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

- **Risk**: The installed `.mcpb` Desktop extension does not auto-update with PyPI/marketplace releases — users silently run the version bundled at install time (a THIRD version-drift surface beyond the plugin↔CLI lockstep RDR-143 addresses). Observed live: an installed conexus 5.1.0 extension while CLI/plugin were 5.8.0.
  **Mitigation**: The mcpb runtime already self-reports staleness at launch (`mcp-server-Conexus.log`: `[conexus-mcpb] installed conexus=X, latest on PyPI=Y … re-download the .mcpb to upgrade. NX_MCPB_SKIP_UPDATE_CHECK=1 to silence`) — this is the primary user signal. Day-2 recipe: download the current `nexus.mcpb` from the GitHub release and reinstall via Settings → Extensions (see Day 2 Operations). Whether to extend RDR-143-style nudging/automation to this surface is tracked as a follow-up bead (filed 2026-06-02); manual re-install is the v1 story.

### Failure Modes

- **MCP startup install fails (write permission, unsupported platform)**: `ensure_installed()` returns `FAILED`; daemon still spawned via `ensure-running` so the current session works; banner shows the failure with manual-install instructions. Visible to developer through MCP server stderr.
- **Banner content malformed**: Tool response is malformed; first tool call appears to fail. Mitigated by treating banner as best-effort: try/except around prepend; on exception, log to stderr and serve the tool response unchanged. **The marker is NOT written on a prepend exception** — it is written only after the banner is actually delivered on a channel (§Approach §3 "mark shown after either channel succeeds"), so a failed prepend retries on the next tool call rather than silently burning the one-shot banner. (This resolves the earlier ambiguity where the marker was written on exception, which would permanently lose the first-run banner — the exact Gap-5 contract — when prepend fails and notifications are not rendered.)
- **uv runtime absent on Claude Desktop chat host**: MCPB install fails with "uv not found"; no daemon installed; user sees a Claude Desktop install error. Mitigated by README documentation pointing at `brew install uv` (macOS) or `pip install uv` / `pipx install uv` (Linux).
- **Cowork SDK bridge dropped a tool call**: Test sentinel write never appears on host side. Diagnostic recipe: check `nx daemon t2 status` then `nx memory list -p _cowork_test`.

## Implementation Plan

### Prerequisites

- [x] All Critical Assumptions resolved: A1 + A5 VERIFIED by live spike (2026-06-02); A3/A4/A6 source-search verified; A2 likely-false but not gate-blocking (design routes around it via tool-response content prepend)
- [ ] RDR-126 accepted via `/conexus:rdr-gate` + `/conexus:rdr-accept`
- [ ] Strategic-synthesis rename decision (`nexus-mkj6u`) made and shipped, OR explicitly deferred to a later RDR (decoupled scope)

### Minimum Viable Validation

A user with no prior Nexus install on a fresh macOS user account can: (1) double-click a downloaded `nexus.mcpb`, (2) see the install banner in their first Claude Desktop chat turn, (3) successfully execute `mcp__plugin_conexus_nexus__memory_put` and `memory_get`, (4) ask Claude to uninstall via the `daemon_uninstall` tool, (5) observe that the LaunchAgent is gone after restart.

### Phase 1: Spike (RDR-126 §1, §2, §3, §7)

#### Step 1: Hello-world MCPB

Author a minimal `mcpb/manifest.json` and `mcpb/pyproject.toml` declaring `conexus>=<current>`. Pack with `mcpb pack`. Install in fresh Claude Desktop on macOS.

#### Step 2: A1 verification — DONE (2026-06-02)

Observe whether `uv` resolves `conexus` and its compiled-C-extension deps in Claude Desktop's MCP-spawn context. **Verdict: VERIFIED** (uv resolved by absolute path; `.venv` built; `nx-mcp` serves). See §A1 / Revision History.

#### Step 3: A2 verification — DONE (docs + prior evidence)

Emit a `notifications/message` of severity `info` from the MCP server startup. Observe whether it appears user-visible in Claude Desktop chat. **Verdict: likely-false** (MCP spec MAY-render; Claude Code #3174 receives-but-doesn't-display); design routes around it. Not gate-blocking. A confirmatory live observation may still be run in Phase 1.

#### Step 4: Idempotency test matrix

Manually toggle states: clean / OS unit pre-installed / marker pre-existing. Verify each combination behaves per § Approach §2 / §3.

#### Step 5: Spike report

The spike evidence lives in T2 (`nexus_rdr/126-research-A1-A2-A5`, `126-research-A5-update`) and the Desktop MCP logs; producing a dedicated `mcpb/SPIKE.md` is OPTIONAL (retire this step in favor of the T2 record + Revision History, or distill them into SPIKE.md at Phase 2 start for a single Phase-2 reference).

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
| `.mcpb` bundle in Claude Desktop | Claude Desktop Settings → Extensions | Claude Desktop UI (shows installed version) | Claude Desktop Uninstall (LaunchAgent orphans — documented) | UI no longer lists | Re-download from GitHub release |

**Update (`.mcpb` does not auto-update).** The installed extension stays on the version bundled at install; it does not track PyPI/marketplace releases. The mcpb runtime self-reports staleness at launch (`mcp-server-Conexus.log`: `installed conexus=X, latest=Y … re-download the .mcpb to upgrade`). Recipe: download the current `nexus.mcpb` from the latest GitHub release and reinstall via Settings → Extensions (re-install replaces the bundle and rebuilds the `.venv`). This is the same drift class as RDR-143 (plugin↔CLI) but for the Desktop surface; automating it is a tracked follow-up.

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

Checked 2026-06-02 at gate:
1. **RDR-143 consistency (version drift + SessionStart daemon).** RDR-143's Shape B SessionStart hook (plugin↔CLI lockstep) and this RDR's `_first_run.ensure_installed()` fire at different lifecycle points (session start vs MCP-server startup) and target different surfaces (CLI binary vs daemon OS unit). No conflict. This RDR adds a third drift surface (the `.mcpb` extension) which is now explicitly documented (Risks + Day 2 Update recipe) rather than left undefended.
2. **Install-surface distinction.** `.mcpb` installs via Settings → Extensions, NOT the Claude Code `/plugin` installer (which rejects `.mcpb`) — verified live. Captured in §A1, Day 2, and §Approach.
3. **A1 verdict propagation.** The pre-spike "likely-false" reasoning in §Research Findings is now marked historical; §Key Discoveries, §A1 verdict, and §Critical Assumptions all read VERIFIED consistently.
No remaining contradictions between Research Findings, the design, and the related RDRs.

### Assumption Verification

All critical assumptions resolved (2026-06-02): A1 VERIFIED (live spike — uv resolves, `.venv` builds, `nx-mcp` serves), A5 VERIFIED (live Cowork attestation), A3/A4/A6 source-search/docs verified, A2 likely-false but not gate-blocking (design's primary banner channel is tool-response content prepend; notification is best-effort). No unverified load-bearing assumption remains. Evidence: T2 `nexus_rdr/126-research-A1-A2-A5`, `126-research-A5-update`.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| `mcpb pack` / `mcpb init` | `@anthropic-ai/mcpb` | Docs Only (spec read) |
| `notifications/message` | MCP protocol | Docs Only (spec read) |
| LaunchAgent install | `nexus.daemon.installer` (lifted) | Source Search |
| `daemon t2 ensure-running` | `nexus.daemon.installer` | Source Search |

### Scope Verification

All 9 §Approach spec items have phase coverage (cross-walked at gate): §1/§7 → Phase 1 (spike) + Phase 3 (manifest); §2/§4/§6 → Phase 2 (library lift + install/uninstall); §3 → Phase 1/2 (first-run banner); §5 → Phase 5 (Cowork verification); release integration → Phase 4; clean-machine → Phase 6. No orphaned spec item. The Minimum Viable Validation (fresh-account install → banner → memory_put/get → uninstall) is in-scope and feasible (A1 verified, so the uv-resolve step is proven). Phase 1 Steps 1-3 are already complete (spikes done); the remaining phases are net-new implementation.

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

- 2026-06-02: **Gate run — PASSED** (0 Critical, 5 Significant, 4 Observations; all resolved in-place). substantive-critic Layer-3 review found no critical issues and judged the RDR structurally sound for acceptance. Significant fixes applied in-place: (1) propagated the A1 VERIFIED verdict through §Key Discoveries + §A1 (stale "likely-false"/spike-spec text marked historical); (2) checked A3/A4/A6 boxes + corrected the Prerequisites parenthetical; (3) resolved the banner marker-on-exception ambiguity (marker NOT written on prepend failure → retries, preserving the Gap-5 contract); (4) integrated the `.mcpb` staleness drift surface into §Risks + the Day 2 Update recipe (+ follow-up bead); (5) filled the Finalization Gate Contradiction/Assumption/Scope sections. Observations: marked Phase-1 spike steps done (SPIKE.md optional), named the install surface in §Approach §1, added RDR-143 to related_rdrs, corrected the manifest `name` to `conexus`.
- 2026-06-02: Spike results. **A1 VERIFIED** by live Claude Desktop evidence (installed uv-type `.mcpb` extension with built `.venv`; MCP logs show `uv` resolved by absolute path + `nx-mcp` serving + full `tools/list`; a prior `Nexus (Spike)` launched cleanly 2026-05-23) — the docs-only "likely-false" verdict is overturned; Desktop injects a login-shell PATH, so the GUI-PATH concern is moot. **A5 VERIFIED** (user-attested live Cowork session: all surfaces interact correctly). A2 remains likely-false but the design routes around it (not gate-blocking). With A1/A5 cleared, all critical assumptions are resolved. Side-findings: (1) the installed Desktop extension is stale (conexus 5.1.0 vs 5.8.0) — the `.mcpb` surface does not auto-update with the marketplace/PyPI, a third drift surface beyond plugin↔CLI (RDR-143) [follow-up bead]; (2) `.mcpb` installs via Settings → Extensions, NOT the Claude Code plugin installer (which rejects it); (3) an orphaned `local.mcpb.hal-hildebrand.nexus.json` settings file (rename residue) can be removed. Evidence: T2 `nexus_rdr/126-research-A1-A2-A5`.
- 2026-05-23: Initial draft.
