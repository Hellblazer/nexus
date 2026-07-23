# Post-Mortem: RDR-184 — Multi-Agent Orchestration Protocol Hardening

Closed: 2026-07-22. Accepted: 2026-07-15. Epic nexus-ccs9v, 20/20 children closed; five post-epic hardening beads closed at the boundary (hybv1, 0s0o1, 3ra9h, s88vq, 3xg21).

## What the RDR set out to do

Mechanize four dispatch-protocol failure classes from the 2026-07-15 marathon: idle-without-report (Gap 1), crossed-message scope losses (Gap 2), singleton-resource collisions (Gap 3), shared-git-index sweeps (Gap 4).

## Outcome vs Success Criteria (measured 2026-07-22, ccs9v.19)

All five SCs PASS, measured with the scripted census (`expectations_census`) over the full population — all 17 session ledgers plus transcript forensics:

- **SC1 zero idle-without-report**: every one of the 18 BLOCKED events ever recorded was legitimate (agent stopped without reporting) and resolved with a real SendMessage 15–30s after the guard's nudge. False-block rate 0/18. Zero unresolved idles.
- **SC2 zero lost scope updates**: no lost-directive incident on record since P0; measured via retro records (inherits the Obs-2 opt-in residual).
- **SC3 concurrent guarded-harness fails loud <1s**: harness_lock suite 65/65 in 0.71s wall.
- **SC4 zero foreign-file commits**: none in any retro since the protocol; escalation trigger fired once pre-protocol (planner-186) and produced s88vq.
- **SC5 overhead**: one shell line + ~100 prompt tokens per dispatch; applied to 25 consecutive dispatches without friction (cac4bda5).

## What we got wrong along the way (and how it was caught)

1. **The ledger had no verb for "blocked, then delivered."** Both once-guard exits recorded nothing post-block, so a guard SUCCESS was ledger-identical to a dead agent — the .11-adjacent censuses over-read resolved blocks as failures, and Hal's 07-21 complement note hypothesized channel-sensitivity. Transcript forensics refuted that hypothesis and found the real cause; the hook now stamps post-block REPORTED resolutions with causal strength (immediate/later). Lesson: **a measurement instrument that cannot represent the success case will report success as failure**.
2. **Hand-counting drifted from the file twice in one week** (a real BLOCKED counted as 0 on bfbfa2fe; resolved blocks read as failures on b819e8f3). The census is now a script, and the retro checklist points at it. Lesson: any count a process depends on gets a script the first time, not after the second drift.
3. **The census script itself shipped with two blind spots** (sticky-REPORTED masking a later unresolved block; terminal rows without START vanishing) — caught pre-commit by the stacked reviewers, both reproducing the exact under-count class the script existed to kill. Lesson: measurement code deserves the same adversarial review as product code.
4. **Declaration discipline is the weakest link**: 11/17 sessions followed the EXPECT protocol; 6/17 have bare/undeclared STARTs, root-caused to dispatch-site discoverability (3ra9h — the surface was a shell lib named only in a retro checklist), plus a harness variant where the Agent tool exposes no name parameter (unnamed morphology is fail-open by design). The orchestration skill now documents the ledger at the dispatch site.

## Enforcement posture at close

- SubagentStop guard default-ON (live-block), fail-open on every uncertain path; post-block resolution recording; scripted census.
- Gap-4 hardened beyond the RDR's scope at close: subagent `git commit`/`git add` in the shared tree is now a PreToolUse hard block (agent_id marker; linked worktrees exempt), not just prompt text.
- `nx doctor` warns when the installed plugin predates the hook registrations (a pre-floor plugin cannot warn about itself).

## Residuals (tracked, not silent)

- Round-2+ idle of a once-blocked agent remains unmeasurable with the whole-transcript scan (documented in the hook header; the once-guard never re-blocks by design).
- Retro coverage is opt-in (Obs-2): sessions ending without /conexus:continuation are invisible to the tripwires.
- RDR-183 (supervisor ownership topology) remains a separate draft.
