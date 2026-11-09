---
name: pre-push-review
description: Use when about to push nexus work and deciding what to run first, reading a green or red test result before a push, fixing a lint or ruff finding, rebasing before a push, or briefing concurrent runs and reviewers that share a tree.
effort: low
---

# Pre-push review (nexus)

The fixed checks for a diff come from preflight: `nx run preflight --args-file -` fed `{"base": "origin/develop"}`, or `mcp__plugin_conexus_nexus__procedure_run(name="preflight", args={"base": "origin/develop"})`. It reads the diff and returns the commands to run, in order, each marked heavy or not; it runs none of them. This skill holds what a path list cannot decide: how to read a result, what to do about a finding, what a diff's removed lines and symbols reach.

Source: `feedback_test_runs_before_push.md` in Sam's nexus project memory (`~/.claude/projects/<nexus project>/memory/`). The R-numbers below are the rule numbers of the census in T2 `nexus/qo7ie.36-census-feedback_test_runs_before_push`. Where the source and the repo's own docs disagree, the docs win: `docs/agents/test-running.md` § Hand runs and develop CI and `AGENTS.md` rules 5 and 13.

## Lint remedies

- **BLE001 (blind `except Exception`).** Narrow the catch to the sibling code's own error class. Adding `# noqa: BLE001` is the wrong remedy: it makes a defect arriving through the same path indistinguishable from the refusal you meant to handle. A new `_install` core went red on a single one (2026-09-20) (R2).
- **PLC0415 (function-level import).** Keep it only with the repo's form, `# noqa: PLC0415 -- <reason>`; ruff enforces the noqa, not the reason (R3).
- **A line-pin or ceiling ratchet fails.** Regenerate the pins from the detector; never widen a ceiling to make it pass. One exception is sanctioned: the tenant ceiling moves in the same push as the change that adds tenant-minting tests, with the reason in `tests/storage_touch_ceiling.toml` (`uv run python scripts/tenant_delta.py` says by how much) (R36, R71).
- **New Java test code** uses `TestHttp.client()` and `TestHttp.request()`, never the bare JDK builders; a lint enforces it (R38).
- **Run the whole lint bucket, not a subset.** The lint markers are deselected from default addopts, so naming one lint file with plain `pytest` runs nothing and prints a green-looking "deselected". A directory subset or a `-k` is a subset too: five recurrences (R32, R34). Preflight emits the whole bucket after a precondition that no new file is untracked (R33).

## Reading a green

A green is evidence only about what its selection could fail on, and about the tree it ran against.

- **Name what the run excluded, then say whether the change touches it.** The question is not "what did I change, what should I run" but "what can this run fail on, and is what I changed inside that set". A large passed count from a narrow selection is a reason for suspicion, not comfort (R16).
- **Read the summary line, never the exit code.** A run piped into `tail` or `head` reports the pipe's exit. Write the run to a file with `-rf` and read pytest's own summary line with a pattern that fails on any failure (`^[0-9]+ passed` with nothing else on the line); `grep -q passed` matches "1 failed, 964 passed", and an empty filter output is a failed run, not a pass. Preflight's pytest commands already carry this contract (R44, R55).
- **Never join a test run and a push in one command**, with `;`, a newline or `&&` after a pipe. A red must stop the push in a call of its own: run the tests in one call, read the result, push in the next. The push script's own output is never filtered: grep it for `PUSH_TENANT_HINT` before reading `PUSH_OK` (R43, R45, R72).
- **A run under `NX_TEST_T2_SUBSTRATE=none` reports substrate tests as failed or errored.** Build the gate jar and run with the substrate, or pick substrate-free tests. Substrate `none` is right for lint only: it hides the storage-touch guard, so tests that drive a CLI command end to end run once without it and the summary's `touched_unprovisioned=` is read (R46, R60).
- **A removed or changed printed string, or an edited doc line, has the blast radius of an API change.** The covering set is `grep -rn "<old string>" tests/`, not the tests that share the subject. Run the hits (R14).
- **A change to a method the tests fake** (`HttpVectorClient.upsert_chunks_with_embeddings` and its kin) needs `grep -rn '"<method>"' tests` and the local-service gate before it closes: the unit suite cannot reach those fakes (R40).
- **A change that stops writing, renames or retires a metadata key**: `grep -rln '"<key>"' tests/` and run every file it names (read each hit: `session_id` is also the T1 session id). A required-set pin names the key far from the writer (R61).
- **A behaviour change**: grep tests for the behaviour's words (here `retired`, `superseded`), run the hits with `-m ""` so integration-marked ones are included, and run `-m lint` over the new test files (R41).
- **User-facing remedy text**: run `tests/upgrade/test_verb_demotion.py` (R9).
- **Tests that skip on macOS (Linux-only, skip-guarded)** are not verified by a laptop or hellmini run. Run them on Linux before landing: chas through `lab-remote`, or in `python:3.12-slim` as a non-root user literally named `runner`, which is CI's shape. When editing a doc line, grep tests for the old wording first (R56).
- **A green belongs to a tree.** Name the tree it ran against and whether anything moved under it since: a rebase, a peer's landed commits, your own fixes. Rerunning costs minutes; the reasoning that the interaction cannot matter has failed three times in one day (R42).
- **Two narrow selections have already cleared something that turned out red.** Stop choosing selections for that change. Run what its change class requires, which preflight names, and announce it as `docs/agents/test-running.md` § Hand runs says (the change class, the tree sha, "no green full run of this tree exists"). If a green full run of this exact tree already exists, read it instead; no tree gets two. "Pre-push" alone is not a reason for a hand full suite (R17).
- **When two sessions stack commits, the last to push owns one full run on the combined tree** (the same section of test-running.md).

## After a rebase

A rebase rewrites the shas a ledger or doc cites (642e8f42f became 83f6b9197) and can pull a peer's commits under a newly added lint that scans the directories they changed. After one, recheck every ledger citation by sha, and rerun the checks against the rebased tree: a green earlier than the rebase is a green of another tree (R42, R47).

## Concurrent runs and reviewers

- **The concurrency test: do the jobs share a mutable resource?** A tree, a build artifact, ports, a config dir, an install. A running suite is a write-lock on the tree. A Java agent and a Python agent are producer and consumer through the gate jar, never peers. Sandbox-isolated E2E gates may run in parallel among themselves, but the unit suite stays serial to any gate whose teardown stops services or sweeps clusters (R22).
- **A critic briefed to mutate and restore is a writer, not a reviewer.** It gets its own worktree (R23).
- **A block-shaped mass failure under overlap is contention.** The quiet rerun of the reds alone is mandatory evidence, whichever way it comes out (R24).
- **One full Python suite per box, and a queued run waits** (`AGENTS.md` rule 5, test-running.md). Reap dead clusters afterwards with `scripts/sweep-test-substrates.sh`.
