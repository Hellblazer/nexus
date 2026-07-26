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
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
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

    # A pure tag push is not a branch update. `--tags` carries no branch
    # refspec, and `git push origin vX.Y.Z` is the release publish step.
    if any(t in {"--tags", "--follow-tags"} for t in tokens):
        return False

    # positional = [remote, refspec...]; refspecs may be "src:dst".
    refspecs = positional[1:] if len(positional) > 1 else []
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
    if not wildcard_add:
        # Only pay for the git subprocesses when the cheap check missed.
        cwd = str(payload.get("cwd") or "") or os.getcwd()
        push_to_main = any(
            _targets_protected(tokens, cwd) for tokens in _push_tokens(command)
        )

    if not wildcard_add and not push_to_main:
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
    # Each half keeps its OWN message: consolidating the rules must not
    # consolidate the diagnosis an operator sees.
    if push_to_main:
        _lib.deny(
            _push_deny_message("main"),
            summary="direct push to main blocked: open a PR against develop (nexus-vduer).",
        )
    _lib.deny(
        _redirect_message(),
        summary="git add wildcard blocked: stage by explicit path (feedback_no_git_add_all).",
    )


if __name__ == "__main__":
    _lib.run_hook(body, fail_closed=False, rule_name=RULE_NAME)
