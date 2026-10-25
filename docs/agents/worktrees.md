# Worktrees: one session, one worktree, full text

Moved from AGENTS.md on 2026-10-14 (nexus-2ziaz); AGENTS.md keeps only the per-turn rules and links here. A `§ <heading>` reference below names a section of AGENTS.md or of a sibling file; [README.md](README.md) maps each heading to its file.

## Worktrees: one session, one worktree (2026-09-19)

**Every session works in its own worktree on its own branch. The shared
primary checkout is not edited.** Agreed by the three sessions sharing this
box after a day in which every coordination failure traced to one cause: a
commit that nearly carried a peer's uncommitted draft, `PUSH_REFUSED_FOREIGN`
in both directions, a deadlock where a fix could not reach origin because a
peer's unpushed commit was its ancestor, subagent briefs restricted to named
files purely because the tree was shared, and a branch pointer nobody could
reconcile while anyone's tree was dirty. In separate worktrees these are not
things to avoid carefully; they are impossible.

1. **Site worktrees as siblings**: `../nexus-wt/<session>`, branch
   `feature/<bead-id>-<slug>`.

2. **The primary KEEPS `develop` checked out** and is the reference
   checkout. Not detached: git refuses to check out a branch that is
   already checked out elsewhere, so the primary holding `develop` makes
   rule 1 self-enforcing at zero discipline.

3. **In the primary: no edits, no commits, no staging, no branch
   switches.** Build and test output (`.venv/`, `service/target/`,
   `__pycache__`, coverage) is expected and fine. "Read-only" would be
   false the first time someone runs a suite there, and a rule that is
   false on first contact becomes advisory.

4. **The release battery runs IN THE RELEASE WORKTREE, never the primary**
   (nexus-57cvk; this said "in the primary" until 2026-09-22). The battery
   is three legs behind one preflight — the fresh-install MVV, the
   package-upgrade MVV and the local-service gate — and each builds and
   gates the checkout it runs in, so the tree must not move underneath an
   hour-long run — and the primary moves by construction, because rule 9
   fast-forwards it on every push to `develop`. On 2026-09-22 that ended
   7.57.0's battery on its twelfth leg with a tree-identity mismatch
   naming two hashes (the battery then keyed prebuilt artifacts on a tree
   identity; that mechanism was deleted in cleanup step 11, nexus-0r1uz):
   eleven legs green, nothing wrong with the code, and neither session
   having done anything the rules did not tell it to. A release worktree holds the release
   branch, which a `develop` push cannot move at all, so the collision
   stops existing rather than being avoided carefully.

   This is ENFORCED, not advised (nexus-57cvk's own closing question, which
   noted that "advice decays"): `release-battery.sh` refuses when this
   checkout holds `develop` and it is not the only worktree on the box —
   exactly the condition under which rule 9 lets a peer move the tree. The
   branch test is rule 2's own, because git will not check out a branch
   twice, so "holds develop" IS "is the primary"; a path heuristic would
   false-positive on a renamed directory, and a false positive here blocks
   a release. A lone checkout with no peers is allowed, since nothing there
   can move anything. `NX_BATTERY_ALLOW_DEVELOP=1` opts a deliberate
   non-release sweep back in.

   Rule 4 is the one that gave way, because the two are not the same kind
   of rule. Rule 9's reason is correctness: a stale primary answers
   questions wrongly and looks complete doing it. Rule 4's reason was
   cost: the primary was where the artifacts already were, so rotating
   worktrees rebuilt the wheel and the engine jar every time. Cost
   yields to correctness.

   The rebuild is worth paying on its own merits anyway. The version bump
   lands in the release branch, so a battery run in the primary gates a
   tree that is NOT the tree that ships. One rebuild per release
   buys "we gated what we shipped", which is the whole point of a battery.

   The engine build needs neither tree: the lease lives in the git common
   dir and the jar cache is keyed on the engine's build inputs, so any
   worktree links the cached jar (`scripts/build-gate-jar.sh`).

5. **ONE FULL SUITE PER BOX AT A TIME, announced on the bus.** Scoped runs
   are unaffected. This rule exists because worktrees DESTROY an accidental
   serialisation: three sessions in one tree cannot comfortably run three
   suites at once, so they take turns without agreeing to. Three worktrees
   make three concurrent `pytest -n auto` runs easy, and that exhausts the
   machine-wide SysV shared-memory budget — each xdist worker boots its own
   Postgres substrate holding a segment against `kern.sysv.shmmni`
   (nexus-6qp25). The build lease does not cover this; it is a separate
   budget.

6. **A NEW WORKTREE RUNS `scripts/build-gate-jar.sh` BEFORE ITS FIRST
   SUBSTRATE-BACKED TEST.** `service/target/` is untracked build output, so
   a fresh worktree has no service jar. The fix is seconds rather than a
   nine-minute rebuild because the cache key is on `service/` CONTENT and
   lives in the git common dir, so every worktree on the box shares one
   build.

   A missing or stale jar used to produce the SAME SYMPTOM as rule 5's
   exhaustion: thousands of setup errors (20673 on 2026-09-19, in a fresh
   worktree with zero shared-memory segments). It no longer does. The
   session-start substrate preflight (`tests/conftest.py`
   `_preflight_engine_substrate`, `tests/_substrate_preflight.py`) exits 75
   with one line naming `scripts/build-gate-jar.sh`, and does the same when
   the pinned engine tag's PG bundle is not downloadable. The setup-error
   wall now means shared-memory exhaustion (rule 5) or a jar rewritten
   after the session started; check `ipcs -m` first.

7. **Before pushing, ask whether a run someone is waiting on is in flight.**
   Worktrees split the tree; CI remains one shared resource with one queue,
   and a push cancels the run in progress. Read one commit's checks with a
   single call: `gh api repos/Hellblazer/nexus/commits/<sha>/check-runs`. Never
   `gh run watch` and never a polling loop: several concurrent watch loops on
   one token tripped GitHub's secondary rate limit on 2026-09-26 and every
   Actions call 403'd (T2 `nexus/github-api-usage-research-2026-09-26`). The
   conexus-hosted adapter still posts one tuple per run and job state change
   to `board/ci/nexus-develop`; subscribe once per session with
   `mcp__plugin_conexus_nexus__tuple_subscribe("board/ci/nexus-develop")` if
   you want pings instead of polling. Nothing in this repo folds those posts
   into a verdict any more, so read the `conclusion` of the check runs, not
   the board.

8. **Push unchanged**: `NX_PUSH_SOURCE=HEAD scripts/git-push-develop.sh <sha>...`
   from INSIDE the worktree. It reads HEAD from the shell's cwd, so `cd`
   in; `git -C` does not cover it. Direct to `develop` per the project
   rule; the feature branch is a local name that never reaches origin.
   The script itself now takes the `lock/ci-develop-push` tuple-space
   lock immediately before the push and releases it right after
   (nexus-agctp) — a `PUSH_REFUSED_LOCK_HELD` names the holder and lease
   expiry; `NX_PUSH_SKIP_LOCK='<reason>'` is the named escape when the
   tuple space is unreachable.

   **Batch develop pushes** (Sam, 2026-10-21, nexus-cvtxu): develop CI runs
   one run at a time, so each push waits for the one before it. Hold green
   commits and push them together, alone only for a develop red or a peer
   who is waiting. A push within 30 minutes of the worktree's last one
   prints `PUSH_BATCH_HINT` on stderr.

9. **Whoever pushes to `develop` fast-forwards the primary in the same
   breath.** `cd` to the primary and `git merge --ff-only origin/develop`.
   Rule 2 makes the primary the reference checkout — the one place to read
   what is on `develop` without a fetch dance — and nothing kept it
   current, so it silently stopped doing that job. Found 2026-09-19 twelve
   commits behind: `nx rdr preamble` run there omitted an RDR that had
   been on `develop` for hours, because the tool reads the RDR directory
   relative to cwd and returned a confident, complete-looking, stale list.

   Attach it to the push rather than to a schedule or a habit, because the
   push is the event that creates the staleness and is already a thing
   someone does deliberately. It is unconditional: rule 4 moved the
   release battery out of the primary precisely so that nothing you have
   to check for can be running in there. If the primary is dirty, do NOT force it:
   a dirty primary is a rule-3 violation someone is mid-way through, and
   clobbering it is worse than a stale read. Say so on the bus instead.

   Same failure shape, for the same reason, one layer up: a stale checkout
   and a glob that matches nothing both answer confidently with a wrong
   maximum. `conexus/skills/rdr-create/SKILL.md` Step 2 told sessions to
   hand-scan for `[0-9][0-9][0-9]-*.md`, which matches none of this repo's
   `rdr-NNN-*.md` files, so a session following it literally found no
   maximum, fell through to the step's "start at 001" clause, and would
   have collided with RDR-001. Fetching fixes neither; each needs its own
   fix.

10. **Never hand-run `nx index repo` from a worktree.** The post-commit hook
   already refuses worktree indexing by construction (nexus-ws67k, comparing
   `--git-dir` to `--git-common-dir`), but the guard lives in the HOOK and
   not in the command, so a hand-run bypasses it entirely.

11. **Serena differs between a session STARTED in a worktree and one that
    RELOCATED into it.** A session started there gets its own server rooted
    at the worktree via `--project-from-cwd` and keeps symbol editing — the
    sn PreToolUse guard allows its writes (round 2, nexus-ebx0s: it
    RECORDS each session's own startup cwd and compares later write calls
    against that record; a match allows even though the call's cwd is
    itself a linked worktree, which an earlier version of the guard denied
    unconditionally, review-caught before it shipped). A session
    that relocates mid-flight — reaches the worktree by absolute path
    without its own cwd ever moving there — keeps the server rooted at the
    primary, so a Serena WRITE tool there is not merely restricted; it can
    SUCCEED, silently, against the primary instead of the tree you meant
    (nexus-ebx0s, 2026-09-22: `replace_in_files` reported "DRY RUN - no
    changes were applied" and had in fact written both occurrences to the
    primary — the report and the effect disagreed, so the dry run cannot
    be trusted as a safety check either). Because the session's cwd never
    actually differs from the primary in that shape, no cwd-only check can
    catch it by construction; the sn PreToolUse guard closes the general
    form of this (a recorded-root mismatch denies regardless of which side
    is the linked worktree) but the exact reported shape — cwd stays at
    the primary and so does Serena's root — remains undetectable from cwd
    alone. Use Edit/Write with absolute worktree paths, never a Serena
    write tool, whenever you are not certain your session started inside
    the worktree it is editing.

12. **The default test host is `chas`; `hellmini` is the second.** chas
    (`ssh chas-test`, user `nxtest`, no sudo) holds its own clone at
    `~/src/nexus` (primary on `develop`, never edited) with worktrees in
    `~/src/nexus-wt/`. Its leases live in that clone's git common dir. Push
    with `git push chas HEAD:refs/heads/<unique-ref>` (laptop remote `chas`)
    and add a worktree at that ref. Unwrapped, `-n auto` there
    meant 32 workers (`PYTEST_XDIST_AUTO_NUM_WORKERS`); 32 workers peak at
    about 95 of 123 GB, so do not raise it. Measured 2026-10-08 on develop
    14fb28184: the Python suite in about 11 min, `scripts/mvnw-leased.sh
    test` in 9 min. Since the lab-run grants (2026-10-17) a full suite runs
    inside a grant and `-n auto` follows it, clamped to the grant's memory
    (`tests/_xdist_cap.py`): lab-l runs 15 workers, 28,085 tests in 1317 s
    on 7.82.0. Detach a long
    run with `setsid -f`, not `nohup ... &`: a backgrounded process inherits
    SIGINT ignored, and one test reads that (nexus-jo8za). Details: T2
    `nexus/chas-test-host-howto`.

    hellmini, the second host, takes macOS runs and overflow. A Mac
    mini on the tailnet (`ssh hellmini`) holds its own clone at
    `/Volumes/Bulk/src/nexus` (primary on `develop`, never edited) with
    worktrees in `/Volumes/Bulk/src/nexus-wt/`. Its suite and build leases
    live in that clone's git common dir, so it runs one full suite at a time
    independently of the laptop. Move an unpushed branch there with
    `git push hellmini HEAD:refs/heads/<branch>` from the laptop (remote
    `hellmini`), or `git fetch laptop <branch>` on the mini (a fetch-only
    remote). Pushes to origin go from the laptop through
    `scripts/git-push-develop.sh` only. Run `tests/e2e/local-service-gate.sh`
    there with `</dev/null`. The same box is also the self-hosted `hellmini`
    runner for release jobs; see § Self-hosted runners and fork PRs. How-to and
    host details: T2 `nexus/hellmini-second-test-host-howto`.

**Moving an in-flight session.** Cherry-pick or apply into the new worktree
FIRST and verify there, and only then revert the primary — never the
reverse. Two methods, and the right one depends on the starting state:
uncommitted work moves with `git diff origin/develop -- <paths>` then
`git apply` in the worktree (checking that diff-vs-origin and diff-vs-HEAD
are identical first, which is what proves the paths rebase cleanly BEFORE
anything moves); work that is already committed is cherry-picked, because a
commit is recoverable where an applied-but-unverified diff is not.

**A note on reading long runs.** Preconditions are warned at the TOP of a
run (the shared-memory cap note above is one). Whether such a warning reaches you
depends on how much output follows it, which inverts against its value: the
longer and more expensive the run, the further the warning sits from the
tail. `head` as well as `tail`, or grep the warning shape.

**A remote pytest hand run goes through `scripts/watched_pytest.py`** (nexus-hlvg1),
on chas, qwentescence, qwent-test and hellmini alike:
`python scripts/watched_pytest.py --stall 180 --status <file> -- uv run pytest ...`.
It adds `-v` and `faulthandler_timeout`, kills the process tree after
`--stall` seconds of silence and names the hung test (exit 124), names a run
killed by a console Ctrl event (exit 125), and keeps a JSON status file current.
Read that file to check on a run; never sleep and guess. Measured 2026-10-07: a
native-Windows run was killed at 23% by a test's console-wide Ctrl+Break, and
behind `-q` and a blind wait nobody knew for 30 minutes.
