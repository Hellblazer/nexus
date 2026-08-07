#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-121 Phase 2 hook 3: deny ``git add`` wildcard forms.

Standing rule (``feedback_no_git_add_all.md``): wildcard adds pull in
unrelated untracked drafts. Stage by explicit path instead.

Denied forms:
- ``git add -A``        (and ``-Av``, ``-AV``, etc. -- as a flag group)
- ``git add .``
- ``git add --all``

Allowed:
- ``git add <path> [<path> ...]`` with explicit path arguments.
- Any ``git add`` invocation carrying a valid ``# routing-allow:``
  escape token.

SECOND RULE, CONSOLIDATED HERE (nexus-vduer, Hal decision 2026-07-25): deny
``git push`` whose EFFECTIVE target is ``main``.

Why it lives in this file rather than its own: RDR-121 § Performance
Expectations caps PreToolUse:Bash routing rules at FOUR (RDR-125 made the cap
cross-plugin), to honour a <300ms p95 cumulative budget. A fifth rule requires
consolidation or a budget revision in a successor RDR. Measured worst case for
five hooks was ~147ms against that 300ms budget, so the budget is not binding —
but the cap is on rule COUNT, and revising an RDR-owned constant on the strength
of a floor estimate is not this change's business. Consolidation is the path the
cap's own message names, so both checks share one script and therefore one
subprocess spawn per Bash call.

THE PUSH INCIDENT (2026-07-23, self-reported). The orchestrator pushed directly
to main. Session restarts had left the working tree on main and
verify-branch-before-commit was a MEMORY-ONLY control, so it failed the way
memory-only controls fail. Nobody typed "main" — the checkout was already on it,
so a bare ``git push`` inherited the target from the branch's upstream. A matcher
looking for the literal token would have missed the exact event it prevents,
which is why the EFFECTIVE target is resolved (explicit refspec, else upstream).

Tag pushes stay allowed: they are the release publish step, not a branch update.

THIRD RULE, CONSOLIDATED HERE TOO (nexus-4av2n, scope (b)): deny ``git push``
when the outgoing range carries a commit that touches a GATED path
(``src/``, ``service/src/main/``, ``conexus/``, ``tests/``) with no
review-completed coverage. Same cap reasoning as the push-to-main rule above --
a fourth logical check sharing this script's one registry entry and one
subprocess spawn, rather than a fifth cross-plugin routing rule the aggregate
cap (RDR-121/125, currently AT 4) would refuse.

THE MISS THIS CLOSES (bead nexus-4av2n, filed 2026-07-31): five commits
reached develop in one session with zero code-review-expert / substantive-
critic runs. (a) (the sibling fix in pre_close_verification_hook.sh) makes
``bd close`` block on a missing marker; but that miss's own postmortem notes
all five pushes happened BEFORE any bead close that day, so even a blocking
close-time gate would not have stopped a single one. This is the check that
would have (empirically confirmed against the 5 real shas in round-2 review).

COVERAGE-RULE DESIGN (the bead's open question, decided here; ROUND 2 revises
several points after code-review-expert T2 [21539] and substantive-critic
T2 [21540] both returned FIX-FIRST / not-justified on round 1):

  - Range: the outgoing commits are ``<before>..HEAD``, where <before>
    resolves via (in order) ``@{push}`` (git's own "what would this push
    update" ref), ``@{upstream}``, then a merge-base against
    ``origin/develop``/``origin/main`` (first-push-of-a-branch case, no
    push/upstream ref exists yet). If NONE resolve (fresh/offline repo with
    no remote history at all) the range degrades to just the tip commit
    rather than skipping the check outright. Capped at the 50 most recent
    commits (rev-list's default newest-first order keeps the cap meaningful).
    ROUND 2 fix (Significant-a, both reviewers): truncation is now LOUD --
    when the true range exceeds the cap, the decision is always at least
    "warn", naming the cap and how many commits went unscanned, never a
    silent "ok" just because the scanned subset happened to be clean.
  - Gated predicate: a commit is GATED iff at least one changed path starts
    with ``src/``, ``service/src/main/``, ``conexus/``, or ``tests/``. ROUND 2
    fix (Significant-c, both reviewers): ``tests/`` was originally exempt
    "by construction" alongside docs/; both reviewers pointed out that lets a
    standalone test-quality regression (a vacuated assertion, a leaked probe
    with no co-located src/ change -- the 19801b9e class, had it shipped
    alone) slip through uncaught, which is squarely code-review-expert's own
    mandate. ``docs/`` (and anything else outside the four prefixes) stays
    exempt -- pure documentation changes are not the risk class this bead
    exists for.
  - Merge commits: ROUND 2 fix (Critical-3b, reviewer). ``git diff-tree``
    without ``-m``/``-c``/``--cc`` returns NOTHING for a merge commit (git's
    documented default) -- every merge, including AGENTS.md's own mandated
    step-8b back-merge, was silently invisible to the gated-path scan
    regardless of actual content, a false-ALLOW gap. Fixed by diffing a
    merge commit against its FIRST PARENT only (what the merge actually
    integrates into the branch, verified live against a real 2-parent merge
    fixture) rather than ``-m``'s union-of-all-parents, which would also
    re-flag paths already covered by their own separate outgoing commit.
  - Coverage, DUAL-SOURCE (ROUND 2, Critical-1, substantive-critic,
    empirically reproduced live): a review-completed marker written via the
    MCP ``scratch`` tool is INVISIBLE to a T1-only check -- MCP-tool T1 is
    frozen to the session id at MCP-server spawn, while this hook's detached
    CLI ``nx scratch list`` resolves a structurally different scope (the
    pooled CLI-dedicated fallback, nexus-6a19f) whenever the CLI's own T1
    lease for that session isn't fresh -- a documented, recurring state
    (RDR-149 lease system). A gate that only ever asks T1 therefore denies
    genuinely-reviewed work whenever the CLI lease is stale, which erodes
    the gate into "always override" rather than an audited exception. FIX:
    coverage lookup is now T1 (fast path, ONE ``nx scratch list`` call) OR
    T2 memory (``nx memory search``, PG-backed, the cross-process shared bus
    BY DESIGN per AGENTS.md, visible to every process regardless of which
    T1 scope wrote the marker). T2 is queried LAZILY -- only for bead ids /
    the range tip-sha that T1 did not already cover. ROUND 3 (see LATENCY
    below): bounded by a wall-clock ``_Deadline`` rather than a call-COUNT
    budget, since the round-2 count cap did not actually bound wall-clock
    time and closure verification measured the worst case exceeding the
    hook's own 5s PreToolUse timeout. Running out of deadline before every
    gated commit resolves DENIES (folds into "missing"); ``nx`` itself being
    unreachable (a true capability gap, distinct from merely slow) still
    ALLOWS with a warning. WRITE-SIDE
    CONTRACT (documented here + PENDING_RELEASE.md): a marker is visible to
    this gate iff it exists in T1 scratch (``nx scratch put`` /
    ``mcp__...__scratch put`` when the CLI lease is fresh) OR T2 memory
    (``nx memory put`` / ``mcp__...__memory_put``) with content containing
    ``review-completed`` and the bead id (or ``range <tip-sha>`` for a
    per-range marker). When in doubt, write BOTH; T2-alone is the correct
    choice specifically when the CLI T1 lease is known-stale.
    Deny-on-absence STAYS (an allow-when-T1-empty fallback would re-vacuate
    the gate for the 2026-07-31 no-markers-anywhere case this bead exists to
    catch) -- the fix widens WHERE coverage can be found, not WHETHER absence
    still denies.
  - Tag pushes: ROUND 2 fix (Critical-3a, reviewer). The push-to-main check
    (rule 2, same file) already detects and exempts tag pushes with a
    documented incident history; this check had NO equivalent, so tagging
    (release ``vX.Y.Z`` or engine ``engine-service-vX.Y.Z``) from a checkout
    carrying local unpushed-but-gated-and-unmarked commits denied the TAG
    PUSH ITSELF -- the exact "a tag gates delivery, not work" failure class
    this project has been burned by twice (GH #1402, the 7.1.0/v0.1.62
    inversion). Fixed via a dedicated ``_is_pure_tag_push`` recognizing BOTH
    ``vX.Y.Z`` and ``engine-service-vX.Y.Z`` shapes (a superset of the
    push-to-main check's own inline matching, which only recognizes bare
    ``vX.Y.Z`` -- left that check untouched since it already produces the
    correct answer for engine tags via the branch-name-mismatch fallback
    path, and refactoring proven, heavily-tested code was not worth the risk
    here). Kept as an INDEPENDENT implementation rather than extracted from
    ``_targets_protected``, for the same reason.
  - Release branches: ROUND 2 fix (Critical-2, reviewer). AGENTS.md's own
    "Cutting a release" Step 7 does
    ``git checkout -b release/vX.Y.Z; <bump>; git commit -m "chore(release):
    ..."; git push -u origin release/vX.Y.Z`` -- a first-push-of-a-new-branch
    whose sole commit touches ``conexus/`` (a gated prefix) and carries NO
    bead-id token (verified against 10+ real release commits, 6.17.0 through
    7.2.0). Undocumented, that push would always deny. Fixed: a push whose
    EVERY non-tag destination branch matches ``release/*`` is exempt with a
    LOUD INFO message (never silent -- release branches are PR-gated to main
    plus a human releaser BY POLICY already, so this push itself is not the
    review boundary, but the exemption is auditable in the routing log all
    the same). A compound command mixing a release-branch segment with a
    normal-branch segment still runs the full check for the normal segment
    (all-or-nothing across segments, safe default: under-exempt rather than
    over-exempt).
  - T1/T2 UNREACHABLE (nx missing / nonzero exit / connection failure): ALLOW
    with a loud warning, never DENY -- a genuinely broken verification path
    must not brick every push, matching (a)'s capability-honest direction.
    ROUND 3 (Critical, substantive-critic closure verification): this is now
    DISTINCT from running out of ``_Deadline`` wall-clock budget while nx
    WAS reachable and working -- that case DENIES instead (see LATENCY),
    because "ran out of time" is operationally indistinguishable from "would
    have found nothing" and an open-ended harness-timing race is exactly the
    nondeterminism this bead exists to remove. Also distinct from "checked
    within budget, no marker anywhere reachable", which likewise DENIES (a
    real absence, not a capability gap).
  - Override: ``NX_REVIEW_GATE_OVERRIDE=1`` downgrades a coverage deny to an
    allow-with-loud-warning, auditable via the routing log. The existing
    ``# routing-allow: <reason>`` escape token (this file's shared body()
    plumbing) works too, same as the other two checks in this file.

LATENCY (Significant-b round 2, ELEVATED TO CRITICAL round 3, substantive-
critic, measured live both rounds): round 2 measured ~550-730ms for the
common case, but closure verification measured the WORST case directly
against the 2026-07-31 incident's own shape (5-8 uncovered gated commits,
no coverage anywhere) at 6.49s-6.91s -- EXCEEDING the hook's own 5s
PreToolUse timeout (hooks.json). Root cause: the round-2 ``_MAX_T2_LOOKUPS``
budget capped subprocess COUNT, not WALL-CLOCK time, and each ``nx`` spawn
measured ~400ms-1.7s depending on load. A harness-killed hook is a defect
either way it defaults: fail-open silently reproduces the exact 2026-07-31
vacuity via a NEW mechanism (timeout) on precisely the motivating case;
fail-closed bricks every multi-commit gated push. ROUND 3 FIX: the count
budget is REPLACED by an internal wall-clock ``_Deadline`` (``_DEADLINE_SECONDS
= 3.5``, comfortably under the 5s ceiling with margin for git subprocess
overhead) -- the hook now enforces its OWN deadline and returns a
DETERMINISTIC verdict itself rather than ever risking a harness kill.
Exceeding the deadline before every gated commit's coverage is established
DENIES (folds into the same bucket as a confirmed-missing marker) rather
than warning -- "ran out of time while nx was reachable and working" is
operationally indistinguishable from "would have found nothing", and
deny-on-indeterminate is the only direction consistent with this bead's
purpose (an open-ended harness-timing race is exactly the nondeterminism
it exists to remove). This is DISTINCT from "nx itself is unreachable"
(missing binary / nonzero exit / connection failure), which still ALLOWS
with a warning unchanged from round 2 -- a genuinely broken tool must not
brick every push, but a merely SLOW one that we chose not to wait longer
for is a different, self-inflicted risk this hook now owns rather than
externalizes to the harness. FAST PATH restored to 1 call (round-2
regression, also found in closure verification): the range-tip T2 lookup
used to run UNCONDITIONALLY before the per-commit loop, costing a second
``nx`` spawn even when T1 alone already covered everything. Reordered so
the full per-commit T1-only pass runs FIRST (zero extra ``nx`` calls), and
the range-tip lookup is attempted only if that pass leaves something
uncovered. Every ``nx`` subprocess call in the coverage phase also has its
OWN timeout clamped to the deadline's remaining budget (defense in depth
against a single hanging call consuming the whole allowance). See
registry.yaml for honest fast-path / typical / worst-case measurements
taken after this fix. CACHE (critic's closure-verification note, round 3):
a NEGATIVE-only or short-TTL cache for the ``nx`` calls was pointed out as
available and safe-directioned (a stale negative entry only re-denies,
never falsely allows) — correctly distinct from round 2's blanket rejection
of caching entirely. NOT implemented this round: the worst-case path that
actually risks the timeout is a COLD-cache scenario (many distinct,
never-before-queried bead ids in one push -- the real 2026-07-31 shape),
which a first-pass cache does not help regardless of polarity, so it would
add complexity without closing the gap the deadline fix already closes.
Left here as an available future optimization, not a rejected one.

REPO-SCOPE GUARD (nexus-vscgz, 2026-08-07): rules 2 (push-to-main,
nexus-vduer) and 3 (review-coverage gate, nexus-4av2n) are NEXUS-REPO
POLICY -- personal standing rules about THIS project's release-branch
discipline and review coverage. This hook, however, ships in a plugin
installed GLOBALLY, so its PreToolUse:Bash matcher runs in every repo a
session touches. EVIDENCE (2026-08-07): in ChiralBehaviors/inviscid (a
hobby repo -- `master` default branch, no `develop`, no marketplace
surface) rule 2 blocked a plain `git push origin master` with the
nexus-vduer deny message; the operator had to reach for the
`# routing-allow:` escape in a repo that has never heard of nexus-vduer.

``_repo_scope_is_nexus(cwd)`` gates rules 2+3 only -- rule 1 (the
wildcard-add check) is a personal, repo-agnostic standing rule
(`feedback_no_git_add_all.md`) and stays GLOBAL, unaffected by this guard.
Two signals, OR'd (nexus-w3apo: the first cut consulted Signal B only
when Signal A was fully undeterminable, so a nexus checkout with a
renamed/local origin basename resolved determinately-wrong and silently
lost the vduer guard inside nexus itself -- substantive-critic Critical,
reproduced end-to-end):

  - Signal A: ``git remote get-url origin``, normalized (lowercased, a
    trailing ``/`` stripped BEFORE the trailing ``.git`` -- order matters
    for ``.../nexus.git/`` -- then split on BOTH ``/`` and ``:`` so an
    scp-style remote (``git@github.com:Hellblazer/nexus.git``) and an
    https/local-path remote resolve their last path component the same
    way). Match iff that last component is ``nexus`` -- true for Hal's
    real origin, the https clone form, forks, and a local bare-repo path
    whose basename is ``nexus`` (what this file's own test fixtures use
    to opt in).
  - Signal B: does ``<toplevel>/conexus/.claude-plugin/plugin.json``
    exist? A distinctive marker of this repo, present in linked worktrees
    too. Consulted whenever Signal A did not MATCH (missing origin,
    failed call, or a non-``nexus`` basename alike).

Known, accepted residual risks (stated, not defended further): an
unrelated repo literally named ``nexus`` gets nexus policy (basename
match); a push using a raw URL or a remote named other than ``origin``
in a checkout that also lacks the marker is treated as foreign. Cost of
the OR (vs the first cut's short-circuit): a foreign repo's push now
pays one extra git call (``rev-parse --show-toplevel``, 2s cap) plus an
``isfile`` — once per hook invocation, push commands only.

Any failure / undeterminable state resolves to False (NOT nexus). This is
deliberately fail-OPEN for foreign repos: the whole point of this guard is
that a repo it has never heard of must not be governed by rules that
assume it is nexus -- the inverse of every OTHER fail-open in this file,
which exists to avoid bricking a push this project's own policy should
allow. The scope check runs ONLY when the command actually contains a
``git push`` segment: rule 1 needs no git subprocess at all, and paying
for `remote get-url` on every Bash call would burn into the <300ms budget
for the common non-push case for no reason. When a push segment exists and
the repo is NOT nexus, rules 2 and 3 are skipped entirely -- the command is
treated exactly as though it carried no push segments, so only rule 1 can
still fire.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
import _lib  # noqa: E402

RULE_NAME = "git_add_all_redirects_to_explicit_paths"


def _has_wildcard_add(segment_tokens: list[str]) -> bool:
    """Return True iff this segment is ``git add`` with a wildcard form."""
    if len(segment_tokens) < 2:
        return False
    if segment_tokens[0] != "git" or segment_tokens[1] != "add":
        return False
    for token in segment_tokens[2:]:
        if token == ".":
            return True
        if token == "--all":
            return True
        # ``-A`` or any short-flag group containing ``A``.
        if token.startswith("-") and not token.startswith("--") and "A" in token:
            return True
    return False


def _scan_command(command: str) -> bool:
    """Return True iff any sub-segment is a wildcard ``git add``."""
    segments = re.split(r"(?:&&|\|\||;|\s\|\s|\bthen\b|\bdo\b)", command)
    for segment in segments:
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        if _has_wildcard_add(tokens):
            return True
    return False


def _redirect_message() -> str:
    return (
        "git add wildcard forms (`-A`, `.`, `--all`) pull in unrelated "
        "untracked drafts. Stage by explicit path instead:\n"
        "  git add <path1> <path2> ...\n"
        "Standing rule: feedback_no_git_add_all.md.\n"
        "To override, append `# routing-allow: <reason>` (>=8 chars)."
    )


#: Branch names treated as protected. ``master`` included so a repo that has
#: not renamed is covered by the same rule rather than silently unguarded.
_PROTECTED: frozenset[str] = frozenset({"main", "master"})

#: ``git push`` flags that take a VALUE argument, which must be skipped when
#: scanning positional args for a refspec.
_VALUED_PUSH_FLAGS: frozenset[str] = frozenset({
    "--repo", "--exec", "--receive-pack", "--push-option", "-o",
})


def _push_tokens(command: str) -> list[list[str]]:
    """Every ``git push`` segment in *command*, tokenised."""
    out: list[list[str]] = []
    for segment in re.split(r"(?:&&|\|\||;|\s\|\s|\bthen\b|\bdo\b)", command):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        if len(tokens) >= 2 and tokens[0] == "git":
            # Skip global flags (`git -C path push`) to find the subcommand.
            i = 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 2 if tokens[i] in {"-C", "-c"} else 1
            if i < len(tokens) and tokens[i] == "push":
                out.append(tokens[i:])
    return out


def _current_branch(cwd: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001 — undeterminable: caller fails open
        return None
    if r.returncode != 0:
        return None
    name = r.stdout.strip()
    return name or None


def _upstream_branch(cwd: str) -> str | None:
    """The remote branch the current branch tracks, e.g. ``main``."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001 — undeterminable: caller fails open
        return None
    if r.returncode != 0:
        return None
    ref = r.stdout.strip()          # "origin/main"
    return ref.split("/", 1)[1] if "/" in ref else (ref or None)


def _url_last_path_component(url: str) -> str:
    """Normalize *url* and return its last path component, lowercased
    (nexus-vscgz). Handles https/ssh URLs
    (``https://github.com/Hellblazer/nexus.git``), scp-style remotes
    (``git@github.com:Hellblazer/nexus.git``), and local bare-repo paths
    (``/tmp/x/nexus``) uniformly: strip a trailing ``/``, THEN a trailing
    ``.git``, then any ``/`` that strip exposed, then split on BOTH ``/``
    and ``:`` so the scp-style host:path separator and an ordinary path
    separator yield the same last segment. The slash strip must come FIRST:
    ``.../nexus.git/`` otherwise keeps its ``.git`` suffix (endswith
    misses) and a genuine nexus remote reads as foreign — review finding,
    reproduced live.
    """
    s = url.strip().lower().rstrip("/")
    if s.endswith(".git"):
        s = s[: -len(".git")]
    s = s.rstrip("/")
    parts = [p for p in re.split(r"[/:]", s) if p]
    return parts[-1] if parts else ""


def _repo_scope_is_nexus(cwd: str) -> bool:
    """True iff *cwd* is (a checkout of) the nexus repo (nexus-vscgz).

    See the module docstring's REPO-SCOPE GUARD section for the two-signal
    OR and why every undeterminable case resolves to False -- fail OPEN for
    foreign repos, the opposite direction of this file's other fail-opens
    (which protect a push nexus policy should allow, not exempt one it
    shouldn't).

    The two signals are an OR, not primary-then-fallback (nexus-w3apo,
    substantive-critic Critical on the first cut): Signal A resolving
    DETERMINATELY to a non-``nexus`` basename must still consult the
    marker, or a genuine nexus checkout with a renamed/local origin
    (scratch clone, mirror -- the exact shape this file's own test
    fixtures use) silently loses the vduer guard INSIDE nexus, the one
    direction this scoping must never fail. Both git calls carry an
    explicit 2s timeout (review finding: `_git_out`'s bare 10s default is
    unbounded by the wall-clock discipline the nexus-4av2n round-3 work
    exists to enforce, inside a hook with a 5s PreToolUse ceiling).
    """
    origin_url = _git_out(cwd, "remote", "get-url", "origin", timeout=2)
    if origin_url is not None and _url_last_path_component(origin_url) == "nexus":
        return True
    # Origin missing, unreadable, or named something else -- the marker
    # file decides. Present in linked worktrees too.
    toplevel = _git_out(cwd, "rev-parse", "--show-toplevel", timeout=2)
    if toplevel is None:
        return False
    marker = os.path.join(toplevel, "conexus", ".claude-plugin", "plugin.json")
    try:
        return os.path.isfile(marker)
    except OSError:
        return False


def _targets_protected(tokens: list[str], cwd: str) -> bool:
    """True iff this ``git push`` would update a protected branch.

    Resolution order mirrors git's own: an explicit refspec wins; otherwise the
    push inherits the current branch's upstream. The second case is the one the
    incident actually took.
    """
    positional: list[str] = []
    skip_next = False
    for tok in tokens[1:]:                       # drop "push"
        if skip_next:
            skip_next = False
            continue
        if tok in _VALUED_PUSH_FLAGS:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        positional.append(tok)

    # positional = [remote, refspec...]; refspecs may be "src:dst".
    refspecs = positional[1:] if len(positional) > 1 else []

    # TAG FLAGS DO NOT EXEMPT A BRANCH PUSH. The first cut of this returned
    # False the instant `--tags` or `--follow-tags` appeared anywhere in the
    # command, BEFORE refspecs were even computed. Verified against real git
    # (dry-run, checkout on main with an upstream):
    #
    #   git push --follow-tags        ->  main -> main  AND the tags
    #   git push --tags origin main   ->  main -> main  AND the tags
    #   git push --tags               ->  tags only
    #
    # So two of the three exempted forms push the branch, and `--follow-tags`
    # is BY DEFINITION a branch push that also carries tags -- it is the
    # incident's own bare-push shape with a flag appended. Only a BARE `--tags`
    # with no non-tag refspec is a pure tag push.
    #
    # Found by review, not by the tests: this hook shipped with zero
    # `--follow-tags` coverage, so the guard for the 2026-07-23 direct-push
    # incident exempted the exact case it exists to block.
    if "--follow-tags" not in tokens and "--tags" in tokens and not refspecs:
        return False
    if refspecs:
        for spec in refspecs:
            if spec.startswith("refs/tags/") or re.fullmatch(r"v\d+\.\d+\.\d+", spec):
                continue                          # tag push
            dst = spec.split(":")[-1].lstrip("+")
            dst = dst.rsplit("/", 1)[-1]          # refs/heads/main -> main
            if dst in _PROTECTED:
                return True
        return False

    # No refspec: the effective target is the upstream of the current branch.
    # THIS is the incident's shape — a bare `git push` from a checkout that was
    # already sitting on main.
    upstream = _upstream_branch(cwd)
    if upstream is not None:
        return upstream in _PROTECTED
    branch = _current_branch(cwd)
    if branch is not None:
        # No upstream configured; `push.default` would use the same name.
        return branch in _PROTECTED
    return False                                   # undeterminable -> fail open


def _push_deny_message(target_hint: str) -> str:
    return (
        f"Direct push to {target_hint} is blocked (nexus-vduer). PRs only — "
        f"`main` carries the plugin marketplace surface and the develop split "
        f"protects it from in-flight churn.\n"
        f"Open a PR against `develop` instead. Releases promote develop to main "
        f"by MERGE; the single sanctioned direct commit is the release version "
        f"bump (docs/contributing.md § Release Process).\n"
        f"Tag pushes are unaffected — `git push origin vX.Y.Z` still works.\n"
        f"If this IS the release flow, append `# routing-allow: <reason>` "
        f"(>=8 chars) so the exception is auditable in the routing log."
    )


# ---------------------------------------------------------------------------
# nexus-4av2n scope (b) round 2: tag-push / release-branch destination
# resolution, independent of _targets_protected's own inline parsing (left
# untouched -- it already produces the correct answer for push-to-main
# purposes; see module docstring "Tag pushes" for why this is a deliberate,
# separate implementation rather than a shared refactor).
# ---------------------------------------------------------------------------

_TAG_REFSPEC_RE = re.compile(r"^(?:v\d+\.\d+\.\d+|engine-service-v\d+\.\d+\.\d+)$")


def _push_positional_and_refspecs(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Positional args after ``push`` (remote + refspecs), skipping
    value-carrying flags. Mirrors ``_targets_protected``'s inline logic but
    kept as a separate copy (see module docstring)."""
    positional: list[str] = []
    skip_next = False
    for tok in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if tok in _VALUED_PUSH_FLAGS:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        positional.append(tok)
    refspecs = positional[1:] if len(positional) > 1 else []
    return positional, refspecs


def _is_pure_tag_push(tokens: list[str]) -> bool:
    """True iff this push segment pushes ONLY tag ref(s), no branch.

    Superset of ``_targets_protected``'s inline tag matching: also
    recognizes ``engine-service-vX.Y.Z`` (nexus-4av2n round 2 Critical-3a).
    Same truth table as the sibling check for ``--tags``/``--follow-tags``
    (see ``_targets_protected``'s docstring for the git-verified detail).
    """
    _, refspecs = _push_positional_and_refspecs(tokens)
    if "--follow-tags" not in tokens and "--tags" in tokens and not refspecs:
        return True
    if refspecs:
        return all(
            spec.startswith("refs/tags/") or _TAG_REFSPEC_RE.fullmatch(spec)
            for spec in refspecs
        )
    return False


def _push_destination_branches(tokens: list[str], cwd: str) -> list[str]:
    """Non-tag destination branch name(s) for this push segment.

    Empty list for a pure tag push or an undeterminable target. Used only
    for the release-branch exemption (nexus-4av2n round 2 Critical-2).
    """
    if _is_pure_tag_push(tokens):
        return []
    _, refspecs = _push_positional_and_refspecs(tokens)
    if refspecs:
        out = []
        for spec in refspecs:
            if spec.startswith("refs/tags/") or _TAG_REFSPEC_RE.fullmatch(spec):
                continue
            dst = spec.split(":")[-1].lstrip("+")
            # Strip ONLY a literal `refs/heads/` prefix -- NOT the last
            # path segment. `_targets_protected`'s sibling rsplit(-1) is
            # correct for ITS purpose (bare names like "main" never
            # contain "/"), but a release branch's name IS "release/vX.Y.Z"
            # and rsplit would silently discard the "release/" part this
            # function exists to detect (found live, nexus-4av2n round 2).
            if dst.startswith("refs/heads/"):
                dst = dst[len("refs/heads/"):]
            out.append(dst)
        return out
    upstream = _upstream_branch(cwd)
    if upstream:
        return [upstream]
    branch = _current_branch(cwd)
    return [branch] if branch else []


def _release_branch_exempt_message(branches: list[str]) -> str:
    names = ", ".join(sorted(set(branches)))
    return (
        f"INFO: push targets release branch ({names}) — exempt from the "
        f"review-coverage gate (nexus-4av2n). Release branches are PR-gated "
        f"to main and require a human releaser by policy (AGENTS.md § "
        f"Cutting a release); this push is not the review boundary."
    )


# ---------------------------------------------------------------------------
# nexus-4av2n scope (b): review-coverage gate.
# ---------------------------------------------------------------------------

#: Gated path prefixes. Mirrors AGENTS.md's own module boundaries -- prefix,
#: not glob, deliberately. ROUND 2: ``tests/`` moved from exempt to gated
#: (Significant-c, both reviewers) -- a standalone test-quality regression
#: is squarely in scope for the reviewers this gate exists to enforce.
#: ``docs/`` (and anything else) stays exempt.
_GATED_PREFIXES: tuple[str, ...] = ("src/", "service/src/main/", "conexus/", "tests/")

_BEAD_ID_RE = re.compile(r"\bnexus-[a-z0-9]+\b", re.IGNORECASE)

#: Cap on commits scanned per push (newest-first from `git rev-list`, so a
#: capped scan still favors the commits nearest HEAD). ROUND 2: truncation
#: is now surfaced via the returned total-vs-scanned counts (Significant-a)
#: instead of being silent. Overridable via NX_PUSH_GATE_MAX_COMMITS_SCANNED
#: (test seam, same pattern as phase_review_close_requires_gate.py's
#: NX_FAKE_CLAUDE_PID) so tests can exercise truncation without creating 50+
#: real commits.
_MAX_COMMITS_SCANNED = int(os.environ.get("NX_PUSH_GATE_MAX_COMMITS_SCANNED", "50") or "50")

#: Wall-clock deadline for the ENTIRE review-coverage phase (git scanning +
#: T1 + T2), nexus-4av2n round 3 Critical (substantive-critic closure
#: verification): the round-2 count-based T2 budget bounded call COUNT, not
#: WALL-CLOCK time, and the worst case (5-8 uncovered gated commits -- the
#: real 2026-07-31 incident's own shape) measured 6.49s-6.91s live, exceeding
#: hooks.json's 5s PreToolUse timeout for this hook. 3.5s leaves margin for
#: git subprocess overhead on top of the `nx` calls, while staying
#: comfortably under the 5s ceiling. Overridable via
#: NX_PUSH_GATE_DEADLINE_SECONDS (test seam, same pattern as
#: NX_PUSH_GATE_MAX_COMMITS_SCANNED above) so tests can exercise the
#: deadline-trip path deterministically with a slow stub `nx` rather than
#: waiting out a real 3.5s in CI.
_DEADLINE_SECONDS = float(os.environ.get("NX_PUSH_GATE_DEADLINE_SECONDS", "3.5") or "3.5")


class _Deadline:
    """Wall-clock budget tracker for one hook invocation. The hook enforces
    its OWN deadline and returns a deterministic verdict rather than ever
    letting the harness's PreToolUse timeout make that call for it
    (nexus-4av2n round 3): a harness-killed hook is a defect either way it
    defaults -- fail-open silently reproduces the exact 2026-07-31 vacuity
    via a NEW mechanism (timeout) on precisely the motivating multi-commit
    case; fail-closed bricks every such push. Exceeding this deadline
    DENIES (see ``_review_coverage_check``), distinct from ``nx`` itself
    being unreachable (a true capability gap), which still ALLOWS with a
    warning."""

    def __init__(self, seconds: float) -> None:
        self._start = time.monotonic()
        self._seconds = seconds

    def exceeded(self) -> bool:
        return (time.monotonic() - self._start) >= self._seconds

    def remaining(self) -> float:
        return max(0.0, self._seconds - (time.monotonic() - self._start))


def _clamp_timeout(deadline: "_Deadline", *, floor: float = 0.5, ceiling: float = 15.0) -> float:
    """Per-call subprocess timeout clamped to the deadline's remaining
    budget (nexus-4av2n round 3): defense in depth so a single hanging
    ``nx`` call cannot itself consume the whole hook budget even though the
    pre-call ``deadline.exceeded()`` check already tries to skip the call
    entirely once the budget is gone."""
    return max(floor, min(ceiling, deadline.remaining()))


#: Marker entry header shape (src/nexus/commands/scratch.py `list_cmd`):
#:   [<8-char-id>] <tags-csv-or-dash>  flagged=<True|False>
#:     <content, first 120 chars>
#: Two lines per entry. Mirrored (not shared code -- different runtimes) in
#: pre_close_verification_hook.sh's embedded python; keep both in sync by
#: hand if this shape ever changes.
_SCRATCH_HEADER_RE = re.compile(r"^\[[0-9a-fA-F]+\]\s+(.*?)\s{2,}flagged=", re.IGNORECASE)


def _git_out(cwd: str, *args: str, timeout: float = 10) -> str | None:
    """Run ``git <args>`` in *cwd*; return stripped stdout, or None on any
    failure (nonzero exit, missing git, timeout). Every caller treats None
    as "undeterminable" and degrades gracefully -- never raises."""
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def _push_before_ref(cwd: str) -> str | None:
    """Resolve the 'before' tip of the outgoing push range.

    Tries, in order: ``@{push}`` (git's own "what would `git push` update"
    ref), ``@{upstream}`` (fallback when push/pull remotes differ or
    push.default has not been exercised yet), then a merge-base against a
    candidate integration branch's remote-tracking ref (the first-push-of-
    a-branch case, where neither special ref exists). None means nothing
    resolved at all -- caller degrades to checking only the tip commit.
    """
    for ref in ("@{push}", "@{upstream}"):
        sha = _git_out(cwd, "rev-parse", "--verify", ref)
        if sha:
            return sha
    for candidate in ("origin/develop", "origin/main"):
        if _git_out(cwd, "rev-parse", "--verify", candidate) is None:
            continue
        base = _git_out(cwd, "merge-base", "HEAD", candidate)
        if base:
            return base
    return None


def _outgoing_commits(cwd: str) -> tuple[list[str], int]:
    """Shas in the outgoing push range, newest-first, capped -- and the
    TOTAL count before capping, so the caller can surface truncation
    (nexus-4av2n round 2 Significant-a) instead of silently scanning a
    subset. ``(shas, total)`` with ``total == len(shas)`` when uncapped.
    Empty list means nothing to check (undeterminable range AND no
    resolvable HEAD -- e.g. cwd is not a repo at all)."""
    before = _push_before_ref(cwd)
    if before is not None:
        out = _git_out(cwd, "rev-list", f"{before}..HEAD", timeout=15)
        if out is not None:
            shas = [s for s in out.split() if s]
            return shas[:_MAX_COMMITS_SCANNED], len(shas)
    # Nothing resolves (offline / brand-new repo, no remote history at all):
    # last resort, check just the tip so the guard does SOMETHING rather
    # than silently skipping every push from such a repo.
    head = _git_out(cwd, "rev-parse", "HEAD")
    return ([head], 1) if head else ([], 0)


def _commit_parent_count(cwd: str, sha: str) -> int:
    out = _git_out(cwd, "rev-list", "--parents", "-n1", sha)
    if not out:
        return 0
    return max(0, len(out.split()) - 1)


def _commit_gated_paths(cwd: str, sha: str) -> list[str]:
    """Paths this commit touches that fall under a gated prefix.

    ROUND 2 fix (Critical-3b): a MERGE commit (>=2 parents) is diffed
    against its FIRST PARENT (what the merge actually integrates into the
    branch), not via bare ``diff-tree`` -- which returns NOTHING for merge
    commits by git's documented default, silently treating every merge
    (including AGENTS.md's own mandated back-merge) as ungated regardless
    of content. ``-m`` (union of diffs against every parent) was considered
    and rejected: it would also re-flag paths already covered by their own
    separate outgoing commit on the other side of the merge, which is not
    something the merge commit itself introduces.
    """
    if _commit_parent_count(cwd, sha) >= 2:
        out = _git_out(cwd, "diff", "--no-commit-id", "--name-only", f"{sha}^1..{sha}")
    else:
        out = _git_out(cwd, "diff-tree", "--no-commit-id", "--name-only", "-r", sha)
    if not out:
        return []
    return [
        p for p in out.splitlines()
        if p.strip() and any(p.startswith(prefix) for prefix in _GATED_PREFIXES)
    ]


def _commit_subject(cwd: str, sha: str) -> str:
    return _git_out(cwd, "log", "-1", "--format=%s", sha) or ""


def _commit_bead_ids(cwd: str, sha: str) -> list[str]:
    body_text = _git_out(cwd, "log", "-1", "--format=%B", sha) or ""
    seen: set[str] = set()
    ids: list[str] = []
    for m in _BEAD_ID_RE.finditer(body_text):
        tok = m.group(0).lower()
        if tok not in seen:
            seen.add(tok)
            ids.append(tok)
    return ids


def _scratch_tags(tags_line: str) -> list[str]:
    m = _SCRATCH_HEADER_RE.match(tags_line)
    body_text = m.group(1) if m else tags_line
    return [t.strip().lower() for t in body_text.split(",") if t.strip()]


def _entry_covers_bead(bead_id: str, tags_line: str, content_line: str) -> bool:
    """Entry-anchored match (nexus-4av2n): the SAME marker entry must carry
    both the review-completed tag and this bead id -- never two independent
    whole-listing greps (the cross-marker vacuity class the close-hook fix
    also closes)."""
    tags = _scratch_tags(tags_line)
    if "review-completed" not in tags:
        return False
    if bead_id in tags:
        return True
    pat = re.compile(r"(?<![A-Za-z0-9-])" + re.escape(bead_id) + r"(?![A-Za-z0-9-])", re.IGNORECASE)
    return bool(pat.search(content_line))


def _entry_covers_range(tip_sha: str, tags_line: str, content_line: str) -> bool:
    """Per-range marker: 'review-completed: range <tip-sha>' covers every
    gated commit in the push at once (multi-bead pushes)."""
    tags = _scratch_tags(tags_line)
    if "review-completed" not in tags:
        return False
    combined = f"{tags_line} {content_line}".lower()
    if "range" not in combined:
        return False
    return tip_sha.lower() in combined or tip_sha[:12].lower() in combined


def _nx_cli_env(payload: dict[str, Any]) -> dict[str, str]:
    """Mirrors pre_close_verification_hook.sh's session-id export (nexus-36q84):
    this hook is detached from any live nx-mcp process, so it forces
    NX_SESSION_ID from the hook payload rather than relying on env
    inheritance, and opts into the pooled CLI-scope fallback (nexus-6a19f /
    nexus-f7xyq) so a marker written via `nx scratch put` in the shared
    CLI-dedicated scope is still visible here. Shared by both the T1
    (scratch) and T2 (memory) CLI calls -- harmless no-op for memory, which
    is not session-scoped."""
    env = dict(os.environ)
    session_id = payload.get("session_id")
    if session_id:
        env["NX_SESSION_ID"] = str(session_id)
        env["NX_T1_ALLOW_SHARED_FALLBACK"] = "1"
    return env


def _t1_scratch_entries(
    cwd: str, payload: dict[str, Any], deadline: "_Deadline"
) -> tuple[bool, list[tuple[str, str]]]:
    """Returns (reachable, entries). reachable=False means nx is missing OR
    the call failed/timed out -- a CAPABILITY gap, distinct from
    reachable=True with an empty/non-matching entries list (a real absence
    of a marker IN T1 -- the caller still tries T2 before concluding
    absence overall, see ``_review_coverage_check``)."""
    if shutil.which("nx") is None:
        return False, []
    try:
        r = subprocess.run(
            ["nx", "scratch", "list"], cwd=cwd, capture_output=True, text=True,
            timeout=_clamp_timeout(deadline), env=_nx_cli_env(payload),
        )
    except Exception:  # noqa: BLE001
        return False, []
    if r.returncode != 0:
        return False, []
    lines = r.stdout.splitlines()
    entries: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("["):
            tags_line = lines[i]
            content_line = lines[i + 1] if i + 1 < len(lines) else ""
            entries.append((tags_line, content_line))
            i += 2
        else:
            i += 1
    return True, entries


def _t2_memory_search(
    query: str, cwd: str, payload: dict[str, Any], *, timeout: float
) -> tuple[bool, list[tuple[str, str]]]:
    """T2 fallback (nexus-4av2n round 2 Critical-1): ``nx memory search
    <query>`` -- T2 is the cross-process shared bus (PG-backed) per
    AGENTS.md, visible regardless of which T1 scope a marker was written
    into. UNSCOPED (no ``-p <project>``): this hook ships in a plugin used
    across arbitrary repos with no reliable way to derive a T2 project
    name from cwd, and the query itself (a full bead id or git sha) is
    specific enough that a cross-project collision is not a real risk.
    ``timeout`` is always the caller's deadline-clamped budget (nexus-4av2n
    round 3) -- never a flat constant, so a single slow call cannot itself
    exceed the hook's own wall-clock allowance.
    Returns (reachable, entries); entries are (header_line, content_line)
    pairs mirroring the T1 scratch parser's shape (see
    src/nexus/commands/memory.py search_cmd for the exact two-line format).
    """
    if shutil.which("nx") is None:
        return False, []
    try:
        r = subprocess.run(
            ["nx", "memory", "search", query], cwd=cwd, capture_output=True,
            text=True, timeout=timeout, env=_nx_cli_env(payload),
        )
    except Exception:  # noqa: BLE001
        return False, []
    if r.returncode != 0:
        return False, []
    lines = r.stdout.splitlines()
    entries: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("["):
            header = lines[i]
            content = lines[i + 1] if i + 1 < len(lines) else ""
            entries.append((header, content))
            i += 2
        else:
            i += 1
    return True, entries


def _t2_entry_covers_bead(bead_id: str, header: str, content: str) -> bool:
    combined = f"{header} {content}".lower()
    if "review-completed" not in combined:
        return False
    pat = re.compile(r"(?<![A-Za-z0-9-])" + re.escape(bead_id) + r"(?![A-Za-z0-9-])", re.IGNORECASE)
    return bool(pat.search(combined))


def _t2_entry_covers_range(tip_sha: str, header: str, content: str) -> bool:
    combined = f"{header} {content}".lower()
    if "review-completed" not in combined or "range" not in combined:
        return False
    return tip_sha.lower() in combined or tip_sha[:12].lower() in combined


class _T2Budget:
    """Tracks the LAZY T2-fallback lookups for one hook invocation, bounded
    by wall-clock ``_Deadline`` (nexus-4av2n round 3 -- replaces round 2's
    call-COUNT cap, which did not actually bound wall-clock time; see the
    module docstring's LATENCY section)."""

    def __init__(self, cwd: str, payload: dict[str, Any], deadline: "_Deadline") -> None:
        self._cwd = cwd
        self._payload = payload
        self.deadline = deadline
        self._cache: dict[str, tuple[bool, list[tuple[str, str]]]] = {}

    def lookup(self, query: str) -> tuple[bool, list[tuple[str, str]]] | None:
        """Returns (reachable, entries), or None if the deadline is already
        exceeded -- caller must treat that as its own DEADLINE status
        (round 3: deny), never MISSING (a real absence) and never the older
        UNCERTAIN (a capability gap where nx itself failed)."""
        if query in self._cache:
            return self._cache[query]
        if self.deadline.exceeded():
            return None
        result = _t2_memory_search(
            query, self._cwd, self._payload, timeout=_clamp_timeout(self.deadline)
        )
        self._cache[query] = result
        return result


def _bead_status(bead_id: str, t1_entries: list[tuple[str, str]], budget: "_T2Budget") -> str:
    """Returns "covered" | "missing" | "uncertain" | "deadline" for one
    bead id. "uncertain" = T2 was reachable but nx itself failed for this
    query (capability gap, still allows with a warning). "deadline" = the
    wall-clock budget ran out before T2 could even be attempted (round 3:
    denies -- indistinguishable in effect from "would have found
    nothing")."""
    if any(_entry_covers_bead(bead_id, t, c) for t, c in t1_entries):
        return "covered"
    if budget.deadline.exceeded():
        return "deadline"
    t2 = budget.lookup(bead_id)
    if t2 is None:
        return "deadline"
    reachable, entries = t2
    if not reachable:
        return "uncertain"
    if any(_t2_entry_covers_bead(bead_id, h, c) for h, c in entries):
        return "covered"
    return "missing"


def _range_status(tip: str | None, t1_entries: list[tuple[str, str]], budget: "_T2Budget") -> str:
    """Returns "covered" | "absent" | "uncertain" | "deadline" for the
    per-range marker -- see ``_bead_status`` for the state meanings."""
    if not tip:
        return "absent"
    if any(_entry_covers_range(tip, t, c) for t, c in t1_entries):
        return "covered"
    if budget.deadline.exceeded():
        return "deadline"
    t2 = budget.lookup(tip)
    if t2 is None:
        return "deadline"
    reachable, entries = t2
    if not reachable:
        return "uncertain"
    if any(_t2_entry_covers_range(tip, h, c) for h, c in entries):
        return "covered"
    return "absent"


def _uncovered_message(
    uncovered: list[tuple[str, str, list[str]]],
    deadline_hit: list[tuple[str, str, list[str]]],
    uncertain: list[tuple[str, str, list[str]]],
    tip: str | None,
    truncation_note: str,
) -> str:
    lines = []
    if truncation_note:
        lines.append(truncation_note)
    if uncovered:
        lines.append(
            "Push blocked: the following commit(s) touch gated paths with no "
            "review-completed marker in T1 scratch OR T2 memory (nexus-4av2n):"
        )
        for sha, subject, paths in uncovered:
            short = sha[:12]
            path_list = ", ".join(paths[:5]) + (", ..." if len(paths) > 5 else "")
            lines.append(f"  {short}  {subject}\n    gated paths: {path_list}")
    if deadline_hit:
        lines.append(
            f"Push blocked: coverage could not be VERIFIED within the hook's "
            f"{_DEADLINE_SECONDS}s wall-clock budget for the following "
            f"commit(s) -- NOT confirmed missing, just unchecked. The hook "
            f"stops itself deterministically here rather than risk the "
            f"harness's own PreToolUse timeout killing it mid-check "
            f"(nexus-4av2n round 3):"
        )
        for sha, subject, _paths in deadline_hit:
            lines.append(f"  {sha[:12]}  {subject}")
    if uncertain:
        lines.append(
            "Additionally UNCERTAIN (T2 unreachable for these specific "
            "queries -- a capability gap, not counted as missing, not "
            "blocking on its own):"
        )
        for sha, subject, _paths in uncertain:
            lines.append(f"  {sha[:12]}  {subject}")
    lines.append(
        "Remedy (fastest first): write ONE per-range marker covering the "
        "WHOLE push -- a single lookup regardless of push size, the "
        "designed path for multi-commit pushes:\n"
        f"  nx scratch put \"review-completed: range {tip or '<tip-sha>'}\" "
        f"--tags \"review-completed\"\n"
        "or write a per-bead marker (to T1 and/or T2 -- write to T2 when "
        "the CLI T1 lease is known-stale):\n"
        "  nx scratch put \"review-completed: <bead-id>\" --tags \"review-completed,<bead-id>\"\n"
        "  nx memory put \"review-completed: <bead-id>\" -p <project> -t review-<bead-id>\n"
        "Deliberate override (audited): set NX_REVIEW_GATE_OVERRIDE=1 and re-run, "
        "or append `# routing-allow: <reason>` (>=8 chars)."
    )
    return "\n".join(lines)


def _uncertain_only_message(
    uncertain: list[tuple[str, str, list[str]]], truncation_note: str
) -> str:
    lines = []
    if truncation_note:
        lines.append(truncation_note)
    shas = ", ".join(f"{s[:12]} ({subj})" for s, subj, _p in uncertain)
    lines.append(
        f"WARNING: could not verify review-completed coverage for gated "
        f"commit(s) {shas} -- T2 was reachable in general but this "
        f"specific lookup failed (a capability gap, not a time-budget "
        f"issue). Pushing anyway (a broken verification path must not "
        f"brick every push) but this is NOT the same as verified review "
        f"coverage. To silence this deliberately, set "
        f"NX_REVIEW_GATE_OVERRIDE=1."
    )
    return "\n".join(lines)


def _cap_truncation_note(total: int, scanned: int) -> str:
    return (
        f"WARNING: outgoing range has {total} commits; only the {scanned} "
        f"nearest HEAD were scanned for gated-path coverage ({total - scanned} "
        f"older commit(s) NOT checked). Treat this push's coverage as partial."
    )


def _deadline_scan_message(scanned: int, total: int, tip: str | None) -> str:
    return (
        f"Push blocked: coverage could not be verified within the hook's "
        f"{_DEADLINE_SECONDS}s wall-clock budget -- only scanned {scanned} "
        f"of {total} outgoing commit(s) for gated paths before the "
        f"deadline (nexus-4av2n round 3: the hook stops itself "
        f"deterministically rather than risk a harness timeout on an "
        f"open-ended scan).\n"
        "Remedy (fastest first): write ONE per-range marker covering the "
        "WHOLE push -- a single lookup regardless of push size:\n"
        f"  nx scratch put \"review-completed: range {tip or '<tip-sha>'}\" "
        f"--tags \"review-completed\"\n"
        "Deliberate override (audited): set NX_REVIEW_GATE_OVERRIDE=1 and re-run, "
        "or append `# routing-allow: <reason>` (>=8 chars)."
    )


def _scan_gated_commits(
    cwd: str, shas: list[str], deadline: "_Deadline"
) -> tuple[dict[str, list[str]], int, bool]:
    """Returns (gated, scanned_count, hit_deadline). ``hit_deadline=True``
    means the scan stopped early -- the caller must treat the unscanned
    remainder as INDETERMINATE, never as "nothing gated there" (nexus-4av2n
    round 3: the hook must never let an open-ended scan risk a harness
    kill, git-overhead phase included, not just the ``nx`` call phase)."""
    gated: dict[str, list[str]] = {}
    for i, sha in enumerate(shas):
        if deadline.exceeded():
            return gated, i, True
        paths = _commit_gated_paths(cwd, sha)
        if paths:
            gated[sha] = paths
    return gated, len(shas), False


def _review_coverage_check(cwd: str, payload: dict[str, Any]) -> tuple[str, str]:
    """Returns (decision, message). decision is one of:
      "ok"   -- nothing to check, or everything covered, uncapped. message "".
      "warn" -- allow, but with a loud advisory (capability gap, truncation,
                override, or a release-branch exemption). message non-empty.
      "deny" -- block the push. message is the deny reason.

    ROUND 3 (nexus-4av2n, substantive-critic closure verification): the
    whole phase is now bounded by a single wall-clock ``_Deadline``
    (``_DEADLINE_SECONDS``), and the per-commit T1-only pass runs BEFORE
    any T2 call is even considered -- restoring the 1-call fast path when
    T1 alone covers everything (round 2 regressed this to 2 calls via an
    unconditional range-tip T2 lookup that ran first).
    """
    deadline = _Deadline(_DEADLINE_SECONDS)

    shas, total = _outgoing_commits(cwd)
    if not shas:
        return "ok", ""
    truncation_note = _cap_truncation_note(total, len(shas)) if total > len(shas) else ""

    tip = _git_out(cwd, "rev-parse", "HEAD", timeout=_clamp_timeout(deadline))

    gated, _scanned, hit_deadline_scanning = _scan_gated_commits(cwd, shas, deadline)
    if hit_deadline_scanning:
        msg = _deadline_scan_message(_scanned, len(shas), tip)
        if truncation_note:
            msg = truncation_note + "\n\n" + msg
        if os.environ.get("NX_REVIEW_GATE_OVERRIDE") == "1":
            return "warn", "OVERRIDE (NX_REVIEW_GATE_OVERRIDE=1): " + msg
        return "deny", msg

    if not gated:
        return ("warn", truncation_note) if truncation_note else ("ok", "")

    t1_reachable, t1_entries = _t1_scratch_entries(cwd, payload, deadline)

    # PASS 1: per-commit T1-only coverage -- zero EXTRA `nx` calls beyond
    # the single unconditional T1 fetch above. Restores the 1-call fast
    # path for the common single-bead-covered-by-T1 case.
    still_uncovered: dict[str, tuple[list[str], list[str]]] = {}
    for sha, paths in gated.items():
        bead_ids = _commit_bead_ids(cwd, sha)
        if bead_ids and any(
            _entry_covers_bead(bid, t, c) for bid in bead_ids for t, c in t1_entries
        ):
            continue
        still_uncovered[sha] = (paths, bead_ids)

    if not still_uncovered:
        return ("warn", truncation_note) if truncation_note else ("ok", "")

    uncovered: list[tuple[str, str, list[str]]] = []
    uncertain: list[tuple[str, str, list[str]]] = []
    deadline_hit: list[tuple[str, str, list[str]]] = []

    if deadline.exceeded():
        # No budget left even to ATTEMPT T2 for the first gap -- PASS 2
        # never runs at all.
        for sha, (paths, _bids) in still_uncovered.items():
            deadline_hit.append((sha, _commit_subject(cwd, sha), paths))
    else:
        budget = _T2Budget(cwd, payload, deadline)

        # PASS 2: range-tip T2 check ONCE -- only reached because pass 1
        # left gaps. A single marker can clear the WHOLE remaining set.
        range_status = _range_status(tip, t1_entries, budget)
        if range_status == "covered":
            still_uncovered = {}

        items = list(still_uncovered.items())
        for idx, (sha, (paths, bead_ids)) in enumerate(items):
            if deadline.exceeded():
                for sha2, (paths2, _b2) in items[idx:]:
                    deadline_hit.append((sha2, _commit_subject(cwd, sha2), paths2))
                break
            if not bead_ids:
                # No id to check anywhere, and the range marker already
                # missed -- nothing left to look up.
                if range_status in ("uncertain", "deadline"):
                    (deadline_hit if range_status == "deadline" else uncertain).append(
                        (sha, _commit_subject(cwd, sha), paths)
                    )
                else:
                    uncovered.append((sha, _commit_subject(cwd, sha), paths))
                continue
            statuses = [_bead_status(bid, t1_entries, budget) for bid in bead_ids]
            if "covered" in statuses:
                continue
            if "deadline" in statuses:
                deadline_hit.append((sha, _commit_subject(cwd, sha), paths))
            elif "uncertain" in statuses:
                uncertain.append((sha, _commit_subject(cwd, sha), paths))
            else:
                uncovered.append((sha, _commit_subject(cwd, sha), paths))

    if not uncovered and not uncertain and not deadline_hit and not truncation_note:
        return "ok", ""

    if uncovered or deadline_hit:
        msg = _uncovered_message(uncovered, deadline_hit, uncertain, tip, truncation_note)
        if os.environ.get("NX_REVIEW_GATE_OVERRIDE") == "1":
            return "warn", "OVERRIDE (NX_REVIEW_GATE_OVERRIDE=1): " + msg
        return "deny", msg

    if uncertain:
        return "warn", _uncertain_only_message(uncertain, truncation_note)

    return "warn", truncation_note


def body(payload: dict[str, Any]) -> None:
    command = _lib.get_bash_command(payload)
    if not command:
        _lib.allow()

    # nexus-mzvwa.8: match FIRST, escape SECOND. Pre-fix the escape check ran
    # before the matcher, so ANY '# routing-allow:'-annotated Bash command
    # logged a phantom escape against this rule (6,130 over the RDR-121 soak
    # window, zero of which contained a git-add wildcard) -- destroying the
    # esc% telemetry. An escape event now means exactly "this command WOULD
    # have been denied and the operator overrode it".
    wildcard_add = _scan_command(command)

    push_to_main = False
    review_decision, review_message = "ok", ""
    if not wildcard_add:
        # Only pay for the git subprocesses when the cheap check missed.
        cwd = str(payload.get("cwd") or "") or os.getcwd()
        push_segments = _push_tokens(command)
        if push_segments and not _repo_scope_is_nexus(cwd):
            # nexus-vscgz: rules 2 (push-to-main) and 3 (review-coverage)
            # are nexus-repo policy, not a global standing rule -- no-op
            # in any other repo. Treated as if there were no push segments
            # at all; rule 1 (wildcard add) is untouched by this branch.
            push_segments = []
        push_to_main = any(_targets_protected(t, cwd) for t in push_segments)
        if push_segments and not push_to_main:
            non_tag_segments = [t for t in push_segments if not _is_pure_tag_push(t)]
            if not non_tag_segments:
                # nexus-4av2n round 2 Critical-3a: every segment is a pure
                # tag push -- exempt, matching the push-to-main check's own
                # tag handling (silent ok; a tag doesn't create new commits).
                pass
            else:
                dest_branches = [
                    _push_destination_branches(t, cwd) for t in non_tag_segments
                ]
                all_release = bool(dest_branches) and all(
                    dsts and all(d.startswith("release/") for d in dsts)
                    for dsts in dest_branches
                )
                if all_release:
                    # nexus-4av2n round 2 Critical-2: release/* is exempt,
                    # but LOUD (release branches are PR-gated + human-
                    # releaser by policy, not silently magic).
                    flat = [d for dsts in dest_branches for d in dsts]
                    review_decision = "warn"
                    review_message = _release_branch_exempt_message(flat)
                else:
                    review_decision, review_message = _review_coverage_check(cwd, payload)

    if not wildcard_add and not push_to_main and review_decision != "deny":
        if review_decision == "warn" and review_message:
            _lib.log_routing_event(
                rule=RULE_NAME, outcome="warn", tool_name="Bash",
                command_fragment=command,
            )
            _lib.warn(review_message)
        _lib.allow()

    if _lib.should_skip_for_reason(command):
        _lib.log_routing_event(
            rule=RULE_NAME, outcome="escape", tool_name="Bash",
            command_fragment=command,
            escape_reason=_lib.extract_escape_reason(command),
        )
        _lib.allow()

    _lib.log_routing_event(
        rule=RULE_NAME, outcome="deny", tool_name="Bash",
        command_fragment=command,
    )
    # Each check keeps its OWN message: consolidating the rules must not
    # consolidate the diagnosis an operator sees.
    if push_to_main:
        _lib.deny(
            _push_deny_message("main"),
            summary="direct push to main blocked: open a PR against develop (nexus-vduer).",
        )
    if review_decision == "deny":
        _lib.deny(
            review_message,
            summary="push blocked: gated commit(s) lack review-completed coverage (nexus-4av2n).",
        )
    _lib.deny(
        _redirect_message(),
        summary="git add wildcard blocked: stage by explicit path (feedback_no_git_add_all).",
    )


if __name__ == "__main__":
    _lib.run_hook(body, fail_closed=False, rule_name=RULE_NAME)
