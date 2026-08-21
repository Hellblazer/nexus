---
title: "Display Projection via the MessageDisplay Hook: One-Way Output Mirroring and Routing-Marker Hiding"
id: RDR-136
type: Architecture
status: draft
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-05-27
accepted_date:
related_issues: []
related_rdrs: [RDR-111, RDR-121, RDR-125, RDR-127]
related_tests: []
implementation_notes: ""
---

# RDR-136: Display Projection via the MessageDisplay Hook

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

> **STUB** captured 2026-05-27. Claude Code 2.1.152 shipped a new
> `MessageDisplay` hook event that, on inspection of the installed
> binary at 2.1.153, is a verified display-only projection point at the
> rendering boundary. This RDR scopes how nexus uses it as a substrate
> for hiding routing markers from the user while preserving them in the
> transcript, and for mirroring assistant output to an external bus.
> Pinned-spec investigation done 2026-05-27 against
> `code.claude.com/docs/en/hooks.md` § MessageDisplay. Problem Statement
> and Approach are sketched; deeper gate sections await
> `/conexus:rdr-research`.

## Problem Statement

nexus has repeatedly approached the same shape: an observable bus or
routing-control plane where agent output carries structured markers that
the system reads but the user should not see. RDR-111 (ORB, scrapped)
named this; RDR-121 and RDR-125 ship the routing-hook discipline; RDR-127
positions palinex as a downstream renderer for surfaces. None of them
had a clean, in-process hook point at the **display boundary** between
"what the model wrote" and "what the terminal shows the user." Until
2.1.152 there was no such hook; transcript-side rewriting or wrapper
processes were the only options, and both bleed into the model's own
view of the conversation.

Claude Code 2.1.152 adds `MessageDisplay`, a per-chunk hook that fires
while an assistant message streams to the terminal. Its `displayContent`
output replaces the rendered text on screen without touching the
transcript, the model's next-turn input, verbose mode, or subagent
context. That is exactly the one-way display fork the prior work needed
and never had.

### Enumerated gaps to close

#### Gap 1: no in-process display-projection point

Today, anything that wants to observe or transform assistant output
between "model wrote it" and "user sees it" has to wrap Claude Code
externally or post-process the transcript. Both approaches couple to
internals nexus does not own and risk drift across Claude Code releases.
`MessageDisplay` provides the substrate; nexus has nothing wired to it.

#### Gap 2: routing-control markers leak to the user's display

Routing-aware agent prose (the kind RDR-121/125 are about, and what the
ORB lineage of RDR-111 imagined) needs to carry structured markers the
system reads. Those markers belong in the transcript so the model can
reason about its own routing, but they should not clutter the terminal.
Without a display hook, the only options are to keep the markers visible
or strip them from the model's view (which loses routing affordance).

#### Gap 3: no shipped path to mirror assistant output to an external bus

Cockpit and observability use cases want a stream of `(message_id,
index, delta)` tuples for assistant output. Nothing in nexus ships that
stream. `MessageDisplay` is a natural producer.

## Context

### Background

Pinned spec, verified 2026-05-27 against `code.claude.com/docs/en/hooks.md`
on Claude Code 2.1.153 (entries below cite that source unless noted).

- **Hook input (per chunk):** `{session_id, transcript_path, cwd,
  hook_event_name: "MessageDisplay", turn_id, message_id, index, final,
  delta}`. `delta` carries newly completed lines (whole lines except the
  final chunk, which may end mid-line and may be empty). `message_id`
  and `turn_id` are stable across chunks.
- **Hook output:** `{"hookSpecificOutput": {"hookEventName":
  "MessageDisplay", "displayContent": "<text>"}}`. `displayContent` is
  the only decision field; empty string or omitting the field both hide
  the chunk's `delta` from display entirely.
- **Display-only guarantee:** transcript file (JSONL), model's next-turn
  input, verbose mode, and subagent context all keep the original
  `delta`. The transformation is exclusive to the terminal display
  stream.
- **Cannot block, but adds latency.** The original `delta` is always
  displayed in the end (transformed if `displayContent` is returned, raw
  if the hook errors or times out). Hook execution adds latency to
  display until the hook returns or its timeout fires (default 600s for
  `command`/`http`/`mcp_tool`; configurable per-hook).
- **No matcher selector.** The hook fires on every chunk of every
  assistant message. Per-message overhead is fixed; the hook itself is
  responsible for cheap fast-paths.
- **Streaming vs non-streaming.** Interactive mode fires per chunk
  (`index` 0..N, exactly one `final: true`); non-interactive (`claude -p`,
  Agent SDK) fires once per complete message. Total observed text is
  identical across modes.

### Technical Environment

- Claude Code 2.1.153+ required (`MessageDisplay` shipped 2.1.152).
- Hook ownership: the conexus plugin is the natural shipper, per RDR-125
  (routing-hook plugin ownership) and the existing routing-hook
  discipline.
- The hook is `type: command` with stdin JSON in, stdout JSON out, and a
  tight `timeout` to protect interactive streaming smoothness.

## Research Findings

### Investigation

[To be completed during `/conexus:rdr-research`: choose the canonical
marker syntax for routing/projection (avoid collision with palinex
surface descriptors per RDR-127); measure per-chunk hook latency
overhead on a representative session; design the chunk-boundary
robustness of the marker grammar (single-line markers are safe per the
pinned spec; multi-line markers need a different design).]

### Key Discoveries

- **Verified:** `MessageDisplay` is display-only; transcript and model
  context untouched. Source: `code.claude.com/docs/en/hooks.md`
  § MessageDisplay, 2026-05-27.
- **Verified:** per-chunk firing in interactive mode means hooks must be
  idempotent across chunks and key on `message_id` if they aggregate.
- **Verified:** no matcher selector. Filtering is the hook's job.
- **Assumed:** a sub-50ms hook is invisible to streaming UX. Needs
  measurement.
- **Assumed:** markers that fit on a single line are sufficient for the
  intended routing/cockpit use cases. Needs validation against actual
  marker workloads.

### Critical Assumptions

- [ ] A canonical, single-line marker grammar covers the routing and
  projection use cases without colliding with palinex surface
  descriptors (RDR-127) or normal agent prose markdown — **Status**:
  Unverified — **Method**: Spike
- [ ] Hook latency stays below the threshold where users perceive
  streaming stutter (target: sub-50ms p99 per chunk) — **Status**:
  Unverified — **Method**: Spike
- [ ] Markers re-entering the model's context next turn (display-only
  scope) is a feature, not a bug, for routing — **Status**: Unverified
  — **Method**: Source Search (RDR-121/125 design intent)

## Proposed Solution

### Approach

Ship a single `MessageDisplay` hook in the conexus plugin that performs
two responsibilities behind one fast filter:

1. **Hide routing/control markers from display.** A canonical
   single-line marker pattern (TBD; candidate `[NX:<kind>:<payload>]`)
   is stripped from `displayContent` before render. The marker remains
   in the transcript, so the model continues to see and reason about
   its own routing tokens next turn.
2. **Mirror to a local append-only log.** Each non-empty `delta` is
   appended to a local file as `{ts_ns, message_id, index, final,
   delta}` JSONL. A downstream consumer (out of scope for this RDR;
   palinex or a cockpit reader per RDR-127) ships from that log.

The hook is `type: command`, `timeout` set tight (target 2s, sized to
absorb a stuck local write but well under the 600s default). Bus output
is a local append (cheap, no network), not an HTTP POST; any network
shipping is downstream.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| MessageDisplay hook script | `conexus/hooks/` | Add new |
| Marker grammar | (none) | New, canonical for conexus |
| Local append log path | `~/.config/nexus/projection.log` (proposed) | New, configurable |
| Downstream bus consumer | RDR-127 palinex / cockpit | Out of scope |

### Decision Rationale

The hook is the substrate; consumers (cockpits, palinex, the
ever-conceptual ORB) are downstream. Keeping the hook itself dumb (strip
+ append) preserves the per-chunk latency budget and isolates the
display-side fork from the question of where output ultimately goes.

[Alternatives (transcript post-processing, wrapper process,
PreToolUse-only routing without display projection), trade-offs, test
plan, finalization gate: to be completed during research.]

## References

- `code.claude.com/docs/en/hooks.md` § MessageDisplay (verified
  2026-05-27, Claude Code 2.1.153).
- Claude Code CHANGELOG 2.1.152 (MessageDisplay shipped).
- RDR-111 (ORB, scrapped — conceptual ancestor of the projection
  substrate).
- RDR-121 (Hook-Enforced Tool Routing), RDR-125 (Routing-Hook Plugin
  Ownership) — the routing-hook discipline this consumes.
- RDR-127 (Substrate-Decoupled Surface Rendering — palinex as
  downstream) — the natural consumer of the mirrored stream.
