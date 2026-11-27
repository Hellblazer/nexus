# RDR-259 post-mortem: conexus project and user settings

## What the RDR set out to do

The conexus plugin decided what to inject and what to enforce by asking who the
user was: a maintainer flag file in the account, or a marker in the repository.
Every user got nexus's own development process unless a person-keyed check said
otherwise, and nexus's names, hosts and ids leaked into plugin text. The RDR
replaced that with settings: one table in `_audience.py`, a tracked
`.nexus/project.json` that can only tighten, a user file
`~/.config/nexus/conexus.json`, strict-on-failure reading, the committed copy at
`HEAD` read where it can change a deny, telemetry opt-in, `nx settings show`
and `check`, and a first-prompt notice when a settings file has a problem.

## Implementation status

Implemented. Stages 1 to 4 shipped in conexus 8.5.9 with engine-service
v0.1.204; the auto-approve half shipped in 8.5.8. The review gate ran two waves
of code-review-expert and substantive-critic. The full affected closure on the
pushed tree passed (63,043). The real-corpus golden replay was green after
develop carried nexus's own project file. The live Minimum Viable Validation
ran on the laptop as cc-validation scenario 36 (42 of 42). On native Windows the
git-guard tests stand in for the live run, by Sam's ruling at close.

## What diverged

**The `HEAD` read was built but never called.** The reader had a `head=True`
path and a cost bench for it, and no hook passed it. A repository could commit
`subagent_git: strict`, loosen or delete the working copy, and a subagent's
`git commit` went through. Both reviewers found it in wave 1. The fix reads
`HEAD` only at the point where the answer can turn an allow into a deny, with
hook-level tests on real repositories. The RDR specified the reader and its
cost, not the call sites, and the bench measured a path no hook ran.

*Drift: under-specified architecture.*

**The settings reader started `git` and `nx` by bare name.** On Windows a hook
runs with the project as its working directory, and `CreateProcess` searches
that directory first, so a `git.exe` committed to a cloned repository would have
run at the first prompt. The plugin already had an off-cwd resolver; the new
module did not use it, and its byte copy under `src/` could not import it. The
fix resolves both tools off the cwd in both copies, with a test that fails if a
spawn bypasses the resolver.

*Drift: missing cross-cutting concern.*

**The two-process `HEAD` read missed its bound on Windows.** `ls-tree` then
`cat-file` measured 67 to 76 ms on Windows against a 50 ms bound. One
`cat-file --batch` process measures 22 to 23 ms. The RDR assumed a git spawn
was cheap everywhere.

*Drift: unvalidated assumption.*

**The SessionStart notice was invisible in Claude Desktop.** Desktop hides a
SessionStart `systemMessage` and shows a UserPromptSubmit one, so the notice
moved to the first prompt of a session, with a once-per-session mark.

*Drift: unvalidated assumption.*

**The compatibility window was dropped.** Decision 7 kept the legacy markers
readable for two client releases. Sam pulled Stage 4 into 8.5.9, so no window
shipped. This was a ruling, not drift.

**The release found what the narrower runs missed.** On the final tree the full
affected run failed 56 tests that passed on develop. Some tests still asserted
bead ids that the wording cleanup had removed. The ec37o decoy suites fed the
telemetry sender an environment it now ignores, and had been reading the lab
account's real config. The new POSIX git shim pushed the Windows set over its
skip ceiling. After the tag, a parity test that compares the newest release's
reader broke, because the newest release became the one with the new API. Each
was a test-side fix, found only by running the whole suite on the exact tree, or
by the tag itself.

*Drift: missing Day 2 operation.*

## What held

The settings table, the tighten-only project file and the strict failure table
held through both review waves with no change in intent. The guard's deny
decisions matched the old guard on the recorded corpus (0 commands tightened in
any posture). The behaviour leg ran with an empty allowlist.

## For the next RDR of this kind

- Name the call sites, not only the reader. A capability no caller uses passes
  every unit test and its own bench.
- A new module that starts a process inherits every rule the plugin already has
  for starting processes. Say so in the design.
- Measure each per-call cost on native Windows before fixing the bound.
- Run the full affected suite on the exact tree to be pushed, and expect the tag
  itself to change what "the previous release" means in tests that read it.
