# RDR-249 post-mortem: declarative auras

## What the RDR set out to do

A board post could already wake a session, but what the session did about it
was improvised each time, and a new watch cost code and a release. The RDR
added the aura: a small TOML file beside a read-only Lua procedure (RDR-242)
that names a board subspace to watch, the posts that trigger it, the
procedure's arguments, a condition on its result, and a display. The
session's MCP server evaluates each matching post, writes one line to the
session's threads entry, optionally sends a fixed-template channel notice,
and removes the line when a clearing post arrives. Every aura ships off. It
is stage 2 of the scriptable tuple space (nexus-mdbds); the first consumer is
`ci_develop_red`, which flags a red develop sha.

## Implementation status

Implemented. Phases 1 to 3 closed as nexus-mdbds.10, .11 and .12 with every
child closed; phase reviews in T2 `nexus/phase-review-rdr-249-phase-{1,2,3}-*`.
Everything shipped in conexus 8.3.0. The Minimum Viable Validation passed on
2026-11-16: a real develop red (d0d743c71, posted 12:40:42Z) produced the
threads block and the fixed-template notice within 4 s, and the next green
(d76add41d, 13:01:27Z) removed the block within 19 s. One fire costs 66 ms
the first time and 49 ms after (median of five).

## What diverged

**The MVV asked for one opted-in session, and the design has no per-session
switch.** The MVV says "enabled in one session", but Technical Design 3 offers
three levels only: the global config, a repo's `.nexus.yml`, and
`NX_AURAS_OFF`. A `.nexus.yml` is gitignored, so the repo level means a local
file in the primary checkout and reaches every session started there. Only
the global level was usable, and it reaches every session on the machine. Sam
chose global for the duration of the check; only one session loaded the aura,
because the others had started before the opt-in.

*Drift: internal contradiction* (the validation assumed a scope the design
never offered).

**Turning an aura on needs a server restart.** The config is read once, when
the session's MCP server starts (Technical Design 3 says so). In practice that
meant the opt-in did nothing until Sam ran `/mcp` and reconnected the server,
and turning the aura off again does not unload it from a session that already
loaded it. Nothing reloads the setting, and nothing tells the user it is
pending. The aura guide (`docs/auras.md`) now says this plainly.

*Drift: missing Day 2 operation.*

**The MVV waited a release.** The installed client (8.2.3) carried no aura
code, so the live check could not run from the branch; Sam chose to wait for
8.3.0 instead of a dev install.

*Drift: missing cross-cutting concern* (the MVV's dependency on a release was
not in the plan).

**`NX_AURAS_OFF` was checked at the resolver, not in a live session.**
`nx aura list` under `NX_AURAS_OFF=ci_develop_red` shows the aura off at the
`env` level, and the loader calls the same resolver, but no session was
restarted under the variable to watch it stay quiet. Likewise the engine's
announce-once on the aura path, disabled-after-three, the no-entry rule, the
`claude -p` rule and the Stop-hook rule are tested against an in-memory
model of the engine, not live; the RDR's Test Plan says so.

*Drift: none* (a narrower verification, recorded so nobody reads it as live).

**The clear matches any green, not the red sha's successor.** Sam ruled "Keep
it as is" on that question (ask aura-clear-sha, 2026-11-15): the next green
develop run clears the block whichever sha it is for. The design had been
silent on it; the ask raised it during Phase 2, and the answer is recorded as
Resolved Question 8.

## What to carry forward

- An MVV that names a scope ("one session", "one repo") is checked against the
  design's switches before the gate; if no switch gives that scope, either add
  one or reword the MVV.
- A setting read at server start needs a stated way to apply it (reload,
  restart, reconnect) in the user-facing docs, written with the feature.
- When a live validation depends on a released client, the plan names the
  release, so the wait is planned rather than discovered.
