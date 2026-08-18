#!/usr/bin/env python3
"""FIXTURE-OF-RECORD (tests/test_hal_git_policy_hook.py, nexus-ww9fw,
2026-08-18): this is a checked-in COPY of the standalone user-level hook
delivered to Hal outside this repo (originally written to the session
scratchpad, then Hal's own ``~/.claude/hooks/``) so the rule-1/rule-2
behavior it carries keeps a green CI test even though the real installed
copy lives outside version control and outside CI's reach. Drift between
this fixture and Hal's actually-installed copy is ACCEPTED and expected
over time (Hal may hand-edit his installed copy) -- this file exists to
prove the EXTRACTION was behaviorally correct at the moment of the split,
not to mirror Hal's install forever. Do not silently "fix" this file to
match a later install; if the two diverge, that is fine and not a bug.

Content below this notice is otherwise IDENTICAL to the delivered
extraction (same self-contained, stdlib-only, no-plugin-tree-imports
contract).

--------------------------------------------------------------------------

Hal's personal git-policy PreToolUse hook: wildcard-add + push-to-main.

SCOPE DECISION (Hal, 2026-08-18, nexus-2mb2j): this hook is deliberately
UNSCOPED -- it fires in EVERY repo, with no nexus-repo detection. The
plugin ancestor was repo-scoped (nexus-vscgz) because a marketplace
plugin must not impose one user's branch policy on foreign checkouts;
this personal copy IS that user's branch policy, and ~/.claude/CLAUDE.md
states it globally ("Where no project rule exists: PRs only -- never
push directly to main"). A repo that legitimately needs direct main
pushes uses the audited escape (`# routing-allow: <reason>`).

EXTRACTED FROM: conexus/hooks/scripts/routing/git_add_all_redirects_to_
explicit_paths.py in the nexus repo (Hal decision 2026-08-18, nexus-ww9fw).
That plugin-shipped hook used to enforce THREE checks in one script to
respect the RDR-121/125 four-rule PreToolUse:Bash cap. Hal ruled that two
of the three -- wildcard `git add` staging, and denying a `git push` whose
effective target is `main` -- are HIS OWN standing workflow preferences,
not a general-purpose feature the conexus plugin should ship to every
installer. They moved here: a standalone, personal hook Hal installs
himself, wherever he wants it, independent of any plugin release. The
third check (review-coverage gating on gated source paths) is nexus-
specific and stayed in the plugin.

INSTALL:
  1. Copy this file to ``~/.claude/hooks/nexus-git-policy.py`` (any stable
     path works; this is the conventional one) and make it executable:
       chmod +x ~/.claude/hooks/nexus-git-policy.py
  2. Add a PreToolUse hook entry to ``~/.claude/settings.json`` (create the
     ``hooks`` object if it does not exist yet):

       {
         "hooks": {
           "PreToolUse": [
             {
               "matcher": "Bash",
               "hooks": [
                 {
                   "type": "command",
                   "command": "python3 ~/.claude/hooks/nexus-git-policy.py"
                 }
               ]
             }
           ]
         }
       }

     If a `"matcher": "Bash"` entry already exists (e.g. from another
     plugin's routing hooks), add this as an additional object inside its
     ``hooks`` array rather than duplicating the matcher block.
  3. No further setup: stdlib-only, no dependency on the nexus repo, the
     conexus plugin, or any Python environment beyond ``python3`` itself.

This file is deliberately self-contained (no ``import _lib``, no plugin-
tree imports) so it keeps working regardless of which repos are checked
out or which plugins are installed -- it is Hal's own config, not part of
any project.

--------------------------------------------------------------------------
RULE 1: deny ``git add`` wildcard forms.

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

--------------------------------------------------------------------------
RULE 2: deny ``git push`` whose EFFECTIVE target is ``main`` (or
``master``) -- nexus-vduer, Hal decision 2026-07-25.

THE PUSH INCIDENT (2026-07-23, self-reported, in the nexus repo). The
orchestrator pushed directly to main. Session restarts had left the
working tree on main and verify-branch-before-commit was a MEMORY-ONLY
control, so it failed the way memory-only controls fail. Nobody typed
"main" -- the checkout was already on it, so a bare ``git push`` inherited
the target from the branch's upstream. A matcher looking for the literal
token would have missed the exact event it prevents, which is why the
EFFECTIVE target is resolved (explicit refspec, else upstream) rather
than a string match.

Hal's standing workflow: work lands on ``develop`` (or a repo's own
integration branch); ``main``/``master`` only moves via a PR-gated
release, promoted by merge. Tag pushes stay allowed -- tagging is a
release-publish step, not a branch update, and this check must not block
the one direct-to-main-adjacent action (cutting a release tag) that is
actually sanctioned.

--------------------------------------------------------------------------
Escape hatch (both rules): append ``# routing-allow: <reason>`` (>=8
characters) to the command. Every escape is logged to the routing log
(see ``_log_path`` below) so over-use stays visible.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
from typing import Any

RULE_NAME = "nexus_git_policy"

ESCAPE_TOKEN = "# routing-allow:"
ESCAPE_REASON_MIN_LENGTH = 8
_ESCAPE_RE = re.compile(
    r"#\s*routing-allow\s*:\s*(?P<reason>.+?)\s*$",
    re.MULTILINE,
)

_DEFAULT_LOG_PATH = pathlib.Path.home() / ".config" / "nexus" / "routing_log.jsonl"

#: Split points for compound Bash commands -- ``&&``, ``||``, ``;``, a
#: piped stage, or a ``then``/``do`` keyword inside a control-flow block.
_SEGMENT_SPLIT_RE = r"(?:&&|\|\||;|\s\|\s|\bthen\b|\bdo\b)"

#: Branch names treated as protected. ``master`` included so a repo that
#: has not renamed is covered by the same rule rather than silently
#: unguarded.
_PROTECTED: frozenset[str] = frozenset({"main", "master"})

#: ``git push`` flags that take a VALUE argument, which must be skipped
#: when scanning positional args for a refspec.
_VALUED_PUSH_FLAGS: frozenset[str] = frozenset({
    "--repo", "--exec", "--receive-pack", "--push-option", "-o",
})

#: A BARE shell redirection operator token (optionally fd-prefixed), e.g.
#: ``>``, ``>>``, ``<``, ``&>``, ``2>``, ``1>>`` -- shlex hands this back
#: as its OWN token, with the target (file, or ``&N`` fd-dup) as a
#: SEPARATE following token (``>`` ``/dev/null``).
_REDIRECT_BARE_RE = re.compile(r"^\d*(?:>>|>|<<|<|&>>|&>)$")

#: An ATTACHED redirection form: operator and operand share ONE token,
#: with no intervening whitespace -- ``>file``, ``2>&1`` (fd duplication,
#: no separate operand token at all). Self-contained; drop just this
#: token.
_REDIRECT_ATTACHED_RE = re.compile(r"^\d*(?:>>|>|<<|<|&>>|&>)\S")

#: Leading ``NAME=VALUE`` env-assignment token, e.g. an inline override
#: prefix on ``FOO=1 git push``. Shell-legal identifier on the left, ``=``
#: immediately after.
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


# ---------------------------------------------------------------------------
# Envelope / stdin / logging (inlined from the plugin's _lib.py --
# duplicated deliberately: this file must not import from the nexus repo
# or any plugin tree, see the module docstring).
# ---------------------------------------------------------------------------


def _allow_envelope(context: str = "") -> str:
    payload: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
    }
    if context:
        payload["additionalContext"] = context
    return json.dumps({"hookSpecificOutput": payload})


def _deny_envelope(reason: str, summary: str | None = None) -> str:
    reason = reason.strip() or "(no reason provided)"
    system_message = summary or reason.splitlines()[0]
    payload = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
        "reason": reason,
    }
    return json.dumps({"hookSpecificOutput": payload, "systemMessage": system_message})


def _allow(context: str = "") -> None:
    sys.stdout.write(_allow_envelope(context) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def _deny(reason: str, summary: str | None = None) -> None:
    sys.stdout.write(_deny_envelope(reason, summary) + "\n")
    sys.stdout.flush()
    sys.exit(0)


def _parse_stdin(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _get_bash_command(payload: dict[str, Any]) -> str:
    if payload.get("tool_name") != "Bash":
        return ""
    tool_input = payload.get("tool_input") or {}
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else ""
    return cmd if isinstance(cmd, str) else ""


def _should_skip_for_reason(command: str) -> bool:
    if not command or ESCAPE_TOKEN not in command:
        return False
    match = _ESCAPE_RE.search(command)
    if not match:
        return False
    return len(match.group("reason").strip()) >= ESCAPE_REASON_MIN_LENGTH


def _extract_escape_reason(command: str) -> str:
    if not command or ESCAPE_TOKEN not in command:
        return ""
    match = _ESCAPE_RE.search(command)
    return match.group("reason").strip() if match else ""


def _log_path() -> pathlib.Path:
    override = os.environ.get("NX_ROUTING_LOG_PATH")
    return pathlib.Path(override) if override else _DEFAULT_LOG_PATH


def _log_event(outcome: str, *, command_fragment: str = "", escape_reason: str = "") -> None:
    """Append one JSON line to the routing log. Never raises -- telemetry
    must not crash a hook."""
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "rule": RULE_NAME,
            "outcome": outcome,
        }
        if command_fragment:
            record["command_fragment"] = command_fragment[:200]
        if escape_reason:
            record["escape_reason"] = escape_reason[:300]
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Rule 1: wildcard `git add`.
# ---------------------------------------------------------------------------


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


def _scan_command_for_wildcard_add(command: str) -> bool:
    """Return True iff any sub-segment is a wildcard ``git add``."""
    for segment in re.split(_SEGMENT_SPLIT_RE, command):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        if _has_wildcard_add(tokens):
            return True
    return False


def _wildcard_add_message() -> str:
    return (
        "git add wildcard forms (`-A`, `.`, `--all`) pull in unrelated "
        "untracked drafts. Stage by explicit path instead:\n"
        "  git add <path1> <path2> ...\n"
        "Standing rule: feedback_no_git_add_all.md.\n"
        "To override, append `# routing-allow: <reason>` (>=8 chars)."
    )


# ---------------------------------------------------------------------------
# Rule 2: push-to-main (nexus-vduer).
# ---------------------------------------------------------------------------


def _strip_shell_redirections(tokens: list[str]) -> list[str]:
    """Drop shell redirection tokens (and, for the bare-operator form, the
    SEPARATE operand token that follows) from *tokens*.

    A PreToolUse hook sees the raw command text tokenised by ``shlex``,
    which has no concept of shell redirection semantics -- ``2>&1``, ``>``,
    ``2> /dev/null`` etc. are ordinary tokens to it. Without this, they
    walk straight into the positional-argument / refspec scan as phantom
    refspecs or phantom destination branches, which can defeat the guard
    entirely (e.g. ``git push > /dev/null`` reading its refspec list as
    ``['/dev/null']``, a non-empty list that skips the upstream-branch
    fallback and lets a bare push-to-main through unchecked).
    """
    out: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if _REDIRECT_BARE_RE.fullmatch(tok):
            i += 2  # operator + its separate operand token
            continue
        if _REDIRECT_ATTACHED_RE.match(tok):
            i += 1  # operator+operand (or fd-dup) in one token
            continue
        out.append(tok)
        i += 1
    return out


def _push_tokens(command: str) -> list[list[str]]:
    """Every ``git push`` segment in *command*, tokenised."""
    out: list[list[str]] = []
    for segment in re.split(_SEGMENT_SPLIT_RE, command):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        # Skip a leading NAME=VALUE env-assignment prefix before requiring
        # "git" -- `SOME_VAR=1 git push ...` must still be recognised as a
        # push segment.
        i = 0
        while i < len(tokens) and _ENV_ASSIGN_RE.match(tokens[i]):
            i += 1
        tokens = tokens[i:]
        if len(tokens) >= 2 and tokens[0] == "git":
            j = 1
            while j < len(tokens) and tokens[j].startswith("-"):
                j += 2 if tokens[j] in {"-C", "-c"} else 1
            if j < len(tokens) and tokens[j] == "push":
                out.append(tokens[j:])
    return out


def _current_branch(cwd: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except Exception:
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
    except Exception:
        return None
    if r.returncode != 0:
        return None
    ref = r.stdout.strip()          # "origin/main"
    return ref.split("/", 1)[1] if "/" in ref else (ref or None)


def _targets_protected(tokens: list[str], cwd: str) -> bool:
    """True iff this ``git push`` would update a protected branch.

    Resolution order mirrors git's own: an explicit refspec wins;
    otherwise the push inherits the current branch's upstream -- the
    2026-07-23 incident's own shape (a bare push from a checkout already
    sitting on main).
    """
    positional: list[str] = []
    skip_next = False
    for tok in _strip_shell_redirections(tokens[1:]):     # drop "push"
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

    # TAG FLAGS DO NOT EXEMPT A BRANCH PUSH -- verified against real git:
    #   git push --follow-tags        ->  main -> main  AND the tags
    #   git push --tags origin main   ->  main -> main  AND the tags
    #   git push --tags               ->  tags only
    # Only a BARE `--tags` with no non-tag refspec is a pure tag push.
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

    # No refspec: the effective target is the upstream of the current
    # branch. THIS is the incident's shape.
    upstream = _upstream_branch(cwd)
    if upstream is not None:
        return upstream in _PROTECTED
    branch = _current_branch(cwd)
    if branch is not None:
        # No upstream configured; `push.default` would use the same name.
        return branch in _PROTECTED
    return False                                   # undeterminable -> fail open


def _push_to_main_message(target_hint: str) -> str:
    return (
        f"Direct push to {target_hint} is blocked (nexus-vduer, Hal's "
        f"standing workflow). PRs only.\n"
        f"Work lands on `develop` (or this repo's own integration "
        f"branch); `main`/`master` moves only via a PR-gated release, "
        f"promoted by merge.\n"
        f"Open a PR against `develop` instead. If this repo has a "
        f"documented release process with a sanctioned direct commit "
        f"(e.g. a version bump), that flow is the exception, not this "
        f"push.\n"
        f"Tag pushes are unaffected — `git push origin vX.Y.Z` still "
        f"works.\n"
        f"If this IS the release flow, append `# routing-allow: <reason>` "
        f"(>=8 chars) so the exception is auditable in the routing log."
    )


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def body(payload: dict[str, Any]) -> None:
    command = _get_bash_command(payload)
    if not command:
        _allow()

    # Match FIRST, escape SECOND: an escape token on a non-matching
    # command must not log a phantom escape event.
    wildcard_add = _scan_command_for_wildcard_add(command)

    push_to_main = False
    if not wildcard_add:
        # Only pay for the git subprocess when the cheap check missed.
        cwd = str(payload.get("cwd") or "") or os.getcwd()
        push_segments = _push_tokens(command)
        push_to_main = any(_targets_protected(t, cwd) for t in push_segments)

    if not wildcard_add and not push_to_main:
        _allow()

    if _should_skip_for_reason(command):
        _log_event("escape", command_fragment=command, escape_reason=_extract_escape_reason(command))
        _allow()

    _log_event("deny", command_fragment=command)
    # Each check keeps its OWN message.
    if push_to_main:
        _deny(
            _push_to_main_message("main"),
            summary="direct push to main blocked: open a PR against develop (nexus-vduer).",
        )
    _deny(
        _wildcard_add_message(),
        summary="git add wildcard blocked: stage by explicit path (feedback_no_git_add_all).",
    )


def main() -> None:
    """Fail-open top-level runner: any unexpected exception allows the
    command through rather than bricking every Bash call (matches the
    plugin hook framework's fail-open-by-default contract)."""
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    payload = _parse_stdin(raw)
    try:
        body(payload)
    except SystemExit:
        raise
    except BaseException:
        _log_event("allow_fail_open")
        _allow()
    _allow()


if __name__ == "__main__":
    main()
