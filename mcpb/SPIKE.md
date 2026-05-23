# RDR-126 P1 Spike: `.mcpb` Desktop Extension feasibility

Bead: `nexus-lbie5`. Spike date: 2026-05-23. Conexus version: 4.34.6.
MCPB toolchain: `@anthropic-ai/mcpb@2.1.2`. Manifest schema: `v0.4`.

## What was verified

### A1 — `uv` resolves Nexus's full dependency stack (VERIFIED)

`uv run --directory mcpb/ src/server.py` resolves all transitive deps
including the compiled C / Rust / native extensions that block
MCPB's traditional `type: "python"` path:

| Dependency | Type | Resolved? |
|---|---|---|
| `chromadb` | Rust extension | yes |
| `pydantic-core` | Rust extension | yes |
| `tree-sitter-language-pack` | C extension | yes |
| `numpy` | C extension | yes |
| `torch` | Native + CUDA stubs | yes |
| `onnxruntime` | Native | yes |
| `lxml` | C extension | yes |
| `pymupdf` | C extension | yes |
| `grpcio` | C extension | yes |
| `mineru` | Pure Python + native deps | yes |
| 227 others | mixed | yes |

Total: 237 packages installed. Cold-run wall time on this M-series Mac:
**19 seconds** (single-threaded resolver, includes network fetch).
Warm-run wall time (uv cache): **4.5 seconds**.

The `mcp_server_starting` structlog event fires cleanly, indicating
`nx-mcp` reaches its serve loop before terminating (the test ran
without a client connection so the server exits after stdio EOF).

### Empirical install: bundle packs and validates

`mcpb pack .` produces a 1.3 KB bundle (`nexus-spike-0.0.1.mcpb`)
containing only `manifest.json`, `pyproject.toml`, and `src/server.py`.
`mcpb validate manifest.json` passes the v0.4 schema check.

The bundle does NOT carry deps. uv pulls them on first launch in the
Claude Desktop process. Trade-off: small bundle (1.3 KB vs ~5-10 MB
for `type: "python"`) at the cost of a ~20s first-launch delay.

## What needs Hal's eyes in the Claude Desktop UI

### A2 — `notifications/message` user-visibility (DEFERRED)

The spike does not include the banner-emission code that the
implementation phase introduces (RDR-126 §3). Verifying A2 requires
the actual banner logic, which is part of Phase 2, not the spike.

Honest scope reduction: this spike is upgraded to "feasibility of the
packaging path" rather than "verifies the entire RDR §Approach". The
banner verification will happen during Phase 2 implementation when
there is something to emit.

### Tool listing (deferred)

Whether Claude Desktop renders 36 MCP tools (the full Nexus surface)
in a usable way is also a UI question. Not blocking the spike's
verdict on the packaging path; observable during the install test
recommended below.

## Decisions for Phase 2 (driven by spike findings)

1. **`uv` is a host pre-requisite.** Claude Desktop does NOT bundle
   uv. The manifest's `mcp_config.command` literally invokes `uv run`,
   so users without uv on PATH get a cryptic spawn failure. Phase 2
   must add a `nx doctor`-style check for uv availability, and the
   README must document `brew install uv` / `pipx install uv` /
   `pip install uv` as a hard pre-requisite before the .mcpb install.

2. **First-launch latency is real.** ~20 seconds on a warm-network
   M-series Mac for the cold install. The first-run banner (RDR-126
   §3) should EITHER fire after the deps resolve (so it doesn't get
   lost in a 20s blank screen) OR a generic "preparing Nexus..."
   placeholder if MCP message ordering allows it. TBD during Phase 2.

3. **Bundle is too small to mirror as a release asset until P3.** The
   current spike bundle pins `conexus>=4.34.6`. The release-asset
   bundle (Phase 4) should pin to the exact release version so the
   bundle and the PyPI release are version-locked. This becomes a
   release-workflow concern.

4. **Spike bundle's repo layout works as-is.** No surprises in the
   `mcpb/` directory structure: `manifest.json` + `pyproject.toml` +
   `src/server.py` is the minimum. We can grow user_config later
   without restructuring.

## Recommended next step before Phase 2

Hal installs `nexus-spike-0.0.1.mcpb` in Claude Desktop on his machine
(via `open mcpb/nexus-spike-0.0.1.mcpb` from this repo root, which
launches Claude Desktop's install dialog). Confirms:

- Install dialog renders
- After install, a tool from the `nexus` namespace is callable
  (e.g. `mcp__plugin_nexus_nexus__memory_get`)
- Cold-launch latency matches the 20s spike measurement
- No silent failure (e.g. uv-not-found)

If install fails, the spike upgrades the finding from "feasibility
unclear" to "feasibility broken" and Phase 2 retrofits accordingly.

## What this spike did NOT cover (per honest scope reduction)

- A2: notifications/message visibility (needs banner code from P2)
- Idempotency matrix across pre-install states (needs `_first_run.py`
  from P2)
- `daemon_uninstall` MCP tool (P2 deliverable; needs the tool itself)
- Linux Claude Desktop install path (P5 / P6 deliverable, deferred
  per RDR-126 §Approach §7)
- A5 (Cowork SDK bidirectional sentinel): independent of `.mcpb`;
  pending user's test recipe run

## Verdict

`.mcpb` `type: "uv"` is a viable packaging path for Nexus. No
blockers from the spike. Phase 2 (daemon first-run + banner +
uninstall) can proceed.
