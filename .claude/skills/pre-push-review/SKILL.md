---
name: pre-push-review
description: Use when about to push nexus work and deciding what to run first, reading a green or red test result before a push, fixing a lint or ruff finding, rebasing before a push, or briefing an implementer, concurrent runs and reviewers that share a tree.
effort: low
---

# Pre-push review (nexus)

The fixed checks for a diff come from preflight: `nx run preflight --args-file -` fed `{"base": "origin/develop"}`, or `mcp__plugin_conexus_nexus__procedure_run(name="preflight", args={"base": "origin/develop"})`. It reads the diff and returns the commands to run, in order, each marked heavy or not; it runs none of them. This skill holds what a path list cannot decide: how to read a result, what to do about a finding, and what removed lines and symbols reach.

Source: T2 `nexus/qo7ie.36-census-feedback_test_runs_before_push`. Where it and the repo's own docs disagree, the docs win: `docs/agents/test-running.md` § Hand runs and develop CI, and `AGENTS.md` rules 5 and 13.

## Commit messages

Before a push, run `git fetch origin develop` and read `git log --oneline origin/develop..HEAD`. Each subject is 72 characters or fewer and says what changed, as `type(scope): what changed (bead-id)`, with the bead id left out only when no bead exists (a release bump such as `chore(release): conexus X.Y.Z`); the why, the evidence and the numbers go in the body after one blank line (`docs/contributing.md` § Git Workflow). To reword an unpushed subject, work in your own worktree, never the primary checkout. For the last commit, use `git commit --amend`. For an earlier one, rebuild the stack and keep its split: note the shas, `git reset --hard <base>`, then `git cherry-pick` each sha in order, running `git commit --amend` right after the one to reword. Then name the new shas to the push script.

## Lint remedies

- **BLE001 (blind `except Exception`).** Narrow the catch to the error class the neighbouring code catches. A `# noqa: BLE001` makes a defect that arrives through the same path look like the refusal you meant to handle.
- **PLC0415 (function-level import).** Keep it only as `# noqa: PLC0415 -- <reason>`. Ruff enforces the noqa, not the reason.
- **A line-pin or ceiling ratchet fails.** Regenerate the pins from the detector; do not widen a ceiling to pass.
- **The tenant ceiling moves only after consolidating, and a raise beyond Sam's 2026-10-25 ruling is his decision.** The ruling (RDR-232 Phase 1) covered about five irreducible tenant tests; a larger raise goes to him as an ask. An approved raise moves in the same push as the tests, with the reason as a comment above the new value in `tests/storage_touch_ceiling.toml`; `uv run python scripts/tenant_delta.py` says by how much.
- **New Java test code** uses `TestHttp.client()` and `TestHttp.request()`, not the bare JDK builders. A lint enforces it.
- **Run the whole lint bucket, not a subset.** The lint markers are deselected from default addopts, so naming one lint file in a plain `pytest` run collects nothing and prints a green-looking "deselected". A directory, a touched-file list or a `-k` is a subset too, and each cost a develop red (first miss, then four recurrences).
- **When preflight lists the lint bucket.** It lists it for every code push, which is the standing rule for a push through preflight. To decide by hand, `tests/AGENTS.md:49` says run it when touching `conexus/`, RDR frontmatter or a storage-boundary or hook-registration invariant, and `docs/agents/test-running.md` adds a new test file.
- **Stage new files before the bucket runs.** The repo-wide shell lints enumerate tracked files, so an untracked new file is never scanned. Preflight checks this first.

## Reading a green

A green is evidence only about what its selection could fail on, and about the tree it ran against.

- **Name what the run excluded, then say whether the change touches it.** Ask "what can this run fail on, and is what I changed inside that set", not "what did I change, what should I run".
- **Treat a large passed count from a narrow selection as a reason for suspicion.**
- **Read the summary line, never the exit code.** A run piped into `tail` or `head` reports the pipe's exit. Write the run to a file with `-rf` and match pytest's own summary line with a pattern that fails on any failure (`^[0-9]+ passed` and nothing worse on the line). `grep -q passed` matches "1 failed, 964 passed", and empty filter output is a failed run. Preflight's pytest commands already do this.
- **Run a test and a push as separate tool calls, never one command.** Read the result between them; a red must stop the push because you read it, not because of how the command was chained.
- **Read the push script's whole output, and search it for `PUSH_TENANT_HINT` before you read `PUSH_OK`.** The hint goes to stderr and never refuses the push; a `| tail` has already cut it once.
- **Under `NX_TEST_T2_SUBSTRATE=none`, substrate tests fail or error.** Build the gate jar and run with the substrate, or pick substrate-free tests.
- **Use substrate `none` for the lint bucket only.** It hides the storage-touch guard, so run tests that drive a CLI command end to end once without it, and read `touched_unprovisioned=` in the summary.
- **Put that run in every developer brief** whose change drives a CLI command end to end.
- **Check a hand-listed test set before you send it.** Build it from `git diff --name-only` plus the files `grep -rl` finds, never from memory. In the same command, before `lab-remote`, run `for f in ...; do test -e "$f" || echo "missing $f"; done` and stop on a miss. Name explicit paths: a zsh glob that matches nothing leaves pytest running the whole suite.
- **Keep a remote run's log in the copy and fetch it** (`lab-remote --fetch <path>`), instead of piping the run through `tail`.
- **After you change or remove a printed string, run `grep -rn "<old string>" tests/` and run the files it names.** A printed string has the blast radius of an API change; the covering set is not the tests that share the subject.
- **After you edit a doc line, grep the tests for the old wording** before you push.
- **After you change a method that tests replace with a fake** (`HttpVectorClient.upsert_chunks_with_embeddings`, or any other method a test stubs), run `grep -rn '"<method>"' tests` and the local-service gate. The unit suite cannot reach those fakes.
- **After you stop writing, rename or retire a metadata key**, run `grep -rln '"<key>"' tests/` and the files it names. A required-set pin names the key far from the writer. Read each hit: `session_id` is also the T1 session id.
- **After a behaviour change**, grep the tests for the words that describe the old behaviour (for a retired-name write that is now refused: `retired`, `superseded`). Run the hits with `-m ""` so integration-marked ones run, and run `-m lint` over any new test file.
- **After any `service/` change**, grep `tests/` for the routes it changed and run the hits with `-m ""` on chas after the gate jar. Preflight's integration selector picks nothing for a `service/` change, and its engine rows are Java only. Named classes are for the red-first loop, not the proof: for the RDR-236 Phase 3 engine batch, develop CI selected a journey (8o06q) that no named Python set had run, and it found a regression.
- **After you change user-facing remedy text**, run `tests/upgrade/test_verb_demotion.py`.
- **Run Linux-only and skip-guarded tests on Linux before landing**: on chas through `lab-remote`, or in `python:3.12-slim` as a non-root user named `runner` (CI's shape). A laptop or hellmini run skips them and reads green.
- **Name the tree a green ran against, and whether anything moved under it since.** A rebase, a peer's landed commits and your own fixes all change the tree. Rerun; the argument that the interaction cannot matter failed three times in one day.
- **When two narrow selections have already cleared something that turned out red**, stop choosing selections for that change. Run what its change class requires (preflight names it) and announce it as `docs/agents/test-running.md` § Hand runs says: the change class, the tree sha, and "no green full run of this tree exists". If a green full run of this exact tree already exists, read it instead; no tree gets two. "Pre-push" alone is not a reason for a hand full suite.
- **When two sessions stack commits**, the last to push owns one full run on the combined tree.

## After a rebase

A rebase rewrites the shas that docs cite (642e8f42f became 83f6b9197) and can pull a peer's commits under a newly added lint that scans the directories they changed.

- **Recheck the shas** that `docs/wire-contract-pending.md` and any other doc you edited cite.
- **Rerun the checks against the rebased tree.** A green from before the rebase belongs to another tree.

## Concurrent runs and reviewers

- **Ask whether the jobs share a mutable resource**: a tree, a build artifact, ports, a config dir, an install. A running suite is a write-lock on the tree.
- **Treat a Java agent and a Python agent as producer and consumer** through the gate jar, never as peers.
- **Run sandbox-isolated E2E gates in parallel with each other, but keep the unit suite serial to any gate whose teardown stops services or sweeps clusters.**
- **Give a critic briefed to mutate and restore its own worktree.** It is a writer, not a reviewer.
- **Read a block-shaped mass failure under overlap as contention**, and rerun the reds alone in quiet. That rerun is mandatory evidence, whichever way it comes out.
- **Run one full Python suite per box.** A second run queues (`AGENTS.md` rule 5, `docs/agents/test-running.md`); afterwards reap dead clusters with `scripts/sweep-test-substrates.sh`.

## Briefs

A brief carries what the agent's own run cannot tell it. These omissions each cost a develop red.

- **A brief for a `service/src` edit says to run the full engine suite, once per tree.** A `-Dtest` filter is the inner loop: it never selects the repo-wide `*GateTest`, `*WiringTest` and `*ConformanceTest` scanners. The run goes through `scripts/mvnw-leased.sh --lab-run <class>`, is announced with its change class, the sha and "no green full run of this tree exists" (`AGENTS.md` rule 13), and is handed on by sha and status file. A fix round that touches only a test or a doc file does not trigger another, with two exceptions. A change to a shared test changelog (`db/changelog-test/`, above all `db.changelog-test-objects.xml`) runs the whole suite plus the integration group, because every class that installs test objects reads it and no name filter finds them. An edit that adds SQL-looking literals or fixtures to a Java test runs `-Dtest='*GateTest,*WiringTest,*ConformanceTest,*ScanTest,<touched>'` or the whole suite.
- **A brief for a schema, seed-helper or SQL-function-signature change, or an edit to a `db/changelog-test/*.xml`, names the integration group as CI runs it.** A default engine run excludes it. Preflight's engine class lists the command (`-Dnexus.ddl.suite=integration -Dtest.excluded.groups= -Dgroups=integration` through `scripts/mvnw-leased.sh --lab-run`); put it in the brief and in any run script.
- **A brief whose scope includes a file under `conexus/` (hooks, skills, agents, scripts) names the `conexus/PENDING_RELEASE.md` line as a deliverable.** That holds for a doc-only `SKILL.md` edit too. The line follows the ledger's header (one path per bullet, on the bullet's first line; the bead is attributed by its commit message). The brief also says that `tests/test_plugin_release_drift_ledger.py` runs after the commit, because it diffs committed trees against the pinned tag. A brief without the line reds the drift ledger on develop (2026-10-31, a hook script edit).
- **A brief that adds substrate tests names `uv run python scripts/tenant_delta.py --base <develop sha>`**, run in the integration worktree before the push. Read the `PUSH_TENANT_HINT` line the push script prints for the count that range adds. Say in the brief that a `provisioned_tests` raise waits for Sam after the tests are consolidated.
