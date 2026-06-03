# RDR-126 Post-Mortem: Claude Desktop Deployment — Unified Chat and Cowork Surface

Epic: nexus-osktr (completion) · prior epic nexus-bsjro (substrate, shipped)
Closed: 2026-06-02 · Released: conexus 5.9.0 (feature) + 5.9.1 (banner reroute)

## Summary

RDR-126 made Nexus installable and removable from Claude Desktop chat without a
terminal. The `.mcpb` Desktop Extension substrate (`server.type: "uv"`) shipped
under the original epic; this completion arc closed the nine §Approach gaps:
installer lift (`nexus.daemon.installer`), first-run banner, `daemon_uninstall`
MCP tool, Cowork host-side verification + recipe, mcpb version-parity guard, and
the staleness decision. Eight of nine landed cleanly. The ninth — the first-run
banner, the "signature feature" — produced the most valuable result of the arc:
a conclusive, externally-validated negative.

## Headline finding: the proactive first-run banner is undeliverable to Claude Desktop users via MCP

Two releases tested two channels on a real, isolated Claude Desktop profile:

- **5.9.0 — tool-result content prepend.** Delivered (marker written) but the
  Desktop model paraphrased the tool result and dropped the banner.
- **5.9.1 — server `instructions` field.** PROVEN delivered into the
  `initialize` handshake (`mcp-server-Conexus.log` captured the banner
  verbatim) — yet the model still did not relay it.

The MCP spec + Anthropic's own issue tracker explain why this is by design, not
a Nexus defect:

- `instructions` is *"guidance on how to use the server … to improve the
  language model's understanding of available tools,"* absorbed like a skill —
  not a user-message channel the model echoes verbatim.
- `notifications/message` is received but not displayed —
  `anthropics/claude-code#3174` **closed "not planned,"** explicitly naming
  "server introduction/welcome messages" as missing.
- MCP Apps inline UI does not render in Desktop (`anthropics/claude-ai-mcp#165`).

**There is no MCP-server channel that pushes a proactive message to a Desktop
chat user.** Resolution: accept. The 5.9.1 instructions code is retained (correct;
right primary for clients that surface instructions). The banner's actionable
purpose — how to remove the daemon — is served without a banner: `daemon_uninstall`
is a discoverable, auto-approved tool and `docs/desktop-deployment.md` carries the
recipe. Only the proactive announcement is lost, and only on Desktop chat.
Evidence + sources: T2 `nexus/rdr-126-banner-desktop-verdict`. Bead: nexus-vlo2b.

## Process lessons

- **Live-surface testing is load-bearing.** Green unit tests AND an isolated
  raw-MCP probe both reported the banner "delivered." Only the real Claude
  Desktop run (P6-B) revealed it was never user-visible. The MVV that exercises
  the actual product surface caught what no in-repo test could. The automated
  P6-A runner (`scripts/p6-clean-run.sh`) proved the mechanism; P6-B proved the
  outcome — both were needed.
- **The brainstorming-gate paid for itself.** It caught a banner-marker
  data-loss bug pre-implementation (marker written on a failed delivery would
  have silently burned the one-shot) and refused to re-plan the already-shipped
  `.mcpb` substrate.
- **Stacked review caught a documentation-vs-code lie.** The substantive-critic
  flagged that the "Claude Code fallback" framing was wrong — both surfaces run
  the same FastMCP binary, so the content-prepend path is injection-failure
  recovery, not per-surface routing. The code reviewer separately caught
  untested marker-write-failure paths. Different classes, both real.
- **Honest scoping under pressure.** When the instructions reroute was first
  shipped, the critic correctly demanded the "fix" framing be downgraded to
  "mechanism reroute, user-visibility unverified" and made the Desktop re-test a
  gate, not a follow-up. That discipline is what turned a hopeful guess into a
  validated verdict.
- **Release hygiene gap surfaced.** The develop/main reconciliation was skipped
  after 5.9.0 (version bumps land on main via the release PR; develop lagged),
  forcing an inline main-merge during the 5.9.1 release. Reconcile develop with
  main after every release.
- **CI hermeticity debt became visible and got fixed.** The docling/HF model
  was fetched at test time with no cache, flaking the 5.9.0 and 5.9.1 tag-push
  runs. Fixed (nexus-cxsbs): cache `~/.cache/huggingface` + a retryable docling
  pre-fetch in both `ci.yml` and `release.yml`.

## Deferred / follow-on (surfaced at close, not gates)

- **nexus-qkb9v** — the `.mcpb` Desktop Extension is a third version-drift
  surface (no auto-update). Per the G7 staleness decision (T2
  `nexus/rdr-126-mcpb-staleness-decision`), v1 is manual re-install; whether to
  extend RDR-143-style lockstep automation to this surface is tracked here.

## What shipped (§Approach cross-walk, gate PASSED)

1 Distribution (three-surface) · 2 Installer lift · 3 First-run banner (verdict:
not user-visible on Desktop) · 4 `daemon_uninstall` tool · 5 Cowork host-side
test + recipe · 6 Cross-surface consistency · 7 mcpb v0.4 + parity guard ·
8 Out of scope · 9 Staleness decision. Phase-review-gate cross-walk: PASSED
(nexus-7v8ag).
