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

Hal's personal git-policy PreToolUse hook: wildcard-add + push-to-main +
foreign-tip amend.

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
RULE 3: deny ``git commit --amend`` in the PRIMARY checkout when the tip
commit is not this session's own -- nexus-9wxu6, 2026-09-07.

THE INCIDENT (2026-09-07, five sessions in one checkout). A session ran
``git commit --amend`` in the shared primary to fold a follow-up into
"its" last commit; by then HEAD was a peer's commit, and the amend
rewrote it (restored by SHA). Worktrees are private, so the rule only
fires when the cwd's git dir IS the common dir (a linked worktree has a
``.git/worktrees/<name>`` git dir and is never blocked).

"This session's own" is read from a record the companion PostToolUse
hook (``hal_record_session_commits_hook.py``) appends to after every
``git commit``: one file per Claude Code ``session_id`` under
``~/.config/nexus/session_commits/`` (override: ``NX_SESSION_COMMITS_DIR``),
one HEAD sha per line. An amend is allowed when HEAD is in the current
session's file; anything else (no session id, no file, HEAD not listed)
is denied. Denied means the tip is not provably yours; restore-by-SHA is
the only recovery once the rewrite has happened, so the rule fails closed.

--------------------------------------------------------------------------
RULE 5: deny a ``git commit`` that does not name its paths (no ``--
<paths>``, or a pathspec naming a directory/glob rather than specific
files), and deny ``git commit -a``, in a primary checkout that HAS LINKED
WORKTREES -- the observable signature of a checkout several sessions share.

A bare ``git commit`` commits the whole INDEX, so a file a peer session
left staged rides your commit. That is nexus-bbriq: commit 0249b0c98
carried a peer's unpushed 740-line RDR-212 draft to origin/develop, and
nothing caught it because the nexus-9wxu6 push script vouches commits,
not index contents. Exempt: linked worktrees, ``--allow-empty``, and any
merge/cherry-pick/revert/rebase in progress, where git itself refuses a
partial commit.

RULE 4: deny a bare ``git push`` whose effective target is ``develop`` in a
repo that ships ``scripts/git-push-develop.sh`` -- nexus-9wxu6.

The vouched push script is the control; a rule that lives only in a memory
file is the failure class rule 2 already names. Scoped by the script's
presence at the repo toplevel so repos without it are untouched. The
script's own inner ``git push`` is invisible to this hook (the tool
command is the script), so the script is the only unblocked path.

--------------------------------------------------------------------------
Escape hatch (all rules): append ``# routing-allow: <reason>`` (>=8
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


def _degraded_token_variants(segment: str) -> list[list[str]]:
    """Rough tokenizations of a segment ``shlex`` rejected for unbalanced
    quoting (nexus-2e874). ``except ValueError: continue`` silently DROPPED
    the whole segment, so a single stray quote anywhere in the command
    fully bypassed both rules (``git push origin main --receive-pack="x``
    was ALLOWed with zero warning).

    Two variants, because neither alone keeps every anchor visible:
    quote-chars-as-whitespace keeps a quote glued to a token BOUNDARY
    splitting (``--receive-pack="x``), while quote-chars-removed keeps a
    quote INSIDE a verb from fracturing it (``gi"t push``). A match in
    EITHER variant counts -- the safe, over-inclusive direction; only
    quoting fidelity inside VALUES is lost."""
    blanked = segment.replace('"', " ").replace("'", " ").split()
    stripped = segment.replace('"', "").replace("'", "").split()
    return [blanked] if blanked == stripped else [blanked, stripped]


def _scan_command_for_wildcard_add(command: str) -> bool:
    """Return True iff any sub-segment is a wildcard ``git add``."""
    for segment in re.split(_SEGMENT_SPLIT_RE, command):
        try:
            candidates = [shlex.split(segment, posix=True)]
        except ValueError:
            candidates = _degraded_token_variants(segment)  # nexus-2e874
        if any(_has_wildcard_add(tokens) for tokens in candidates):
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
            candidates = [shlex.split(segment, posix=True)]
        except ValueError:
            candidates = _degraded_token_variants(segment)  # nexus-2e874
        for tokens in candidates:
            # Skip a leading NAME=VALUE env-assignment prefix before
            # requiring "git" -- `SOME_VAR=1 git push ...` must still be
            # recognised as a push segment.
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
                    break  # one entry per segment, first matching variant
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
# Rule 3: `git commit --amend` in the primary checkout on a foreign tip
# (nexus-9wxu6).
# ---------------------------------------------------------------------------

_DEFAULT_SESSION_COMMITS_DIR = pathlib.Path.home() / ".config" / "nexus" / "session_commits"


def _session_commits_dir() -> pathlib.Path:
    override = os.environ.get("NX_SESSION_COMMITS_DIR")
    return pathlib.Path(override) if override else _DEFAULT_SESSION_COMMITS_DIR


def _git_verb_segments(command: str, verb: str) -> list[tuple[list[str], str | None]]:
    """Every ``git [-C dir] <verb> ...`` segment in *command*: the tokens
    from the verb onward, plus the ``-C`` directory if one was given."""
    out: list[tuple[list[str], str | None]] = []
    for segment in re.split(_SEGMENT_SPLIT_RE, command):
        try:
            candidates = [shlex.split(segment, posix=True)]
        except ValueError:
            candidates = _degraded_token_variants(segment)  # nexus-2e874
        for tokens in candidates:
            i = 0
            while i < len(tokens) and _ENV_ASSIGN_RE.match(tokens[i]):
                i += 1
            tokens = tokens[i:]
            if len(tokens) < 2 or tokens[0] != "git":
                continue
            c_dir: str | None = None
            j = 1
            while j < len(tokens) and tokens[j].startswith("-"):
                if tokens[j] in {"-C", "-c"}:
                    if tokens[j] == "-C" and j + 1 < len(tokens):
                        c_dir = tokens[j + 1]
                    j += 2
                else:
                    j += 1
            if j < len(tokens) and tokens[j] == verb:
                out.append((tokens[j:], c_dir))
                break
    return out


def _amend_segments(command: str) -> list[tuple[list[str], str | None]]:
    return [
        (tokens, c_dir)
        for tokens, c_dir in _git_verb_segments(command, "commit")
        if "--amend" in [t.rstrip(")") for t in _strip_shell_redirections(tokens)]
    ]


def _effective_cwd(payload_cwd: str, c_dir: str | None) -> str:
    if c_dir is None:
        return payload_cwd
    return c_dir if os.path.isabs(c_dir) else os.path.join(payload_cwd, c_dir)


def _is_primary_checkout(cwd: str) -> bool:
    """True iff *cwd* is inside the primary checkout of a repo (its git dir
    is the common dir). A linked worktree, or a non-repo, returns False."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return False
    if r.returncode != 0:
        return False
    lines = r.stdout.strip().splitlines()
    if len(lines) != 2:
        return False
    git_dir = os.path.realpath(os.path.join(cwd, lines[0]))
    common = os.path.realpath(os.path.join(cwd, lines[1]))
    return git_dir == common


def _head_sha(cwd: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _session_owns(session_id: str, sha: str) -> bool:
    if not session_id or not sha:
        return False
    path = _session_commits_dir() / session_id
    try:
        return sha in path.read_text(encoding="utf-8").split()
    except OSError:
        return False


def _amend_on_foreign_tip(command: str, payload: dict[str, Any]) -> str | None:
    """The offending HEAD sha when *command* amends a foreign tip in the
    primary checkout, else None."""
    segments = _amend_segments(command)
    if not segments:
        return None
    payload_cwd = str(payload.get("cwd") or "") or os.getcwd()
    session_id = str(payload.get("session_id") or "")
    for _tokens, c_dir in segments:
        cwd = _effective_cwd(payload_cwd, c_dir)
        if not _is_primary_checkout(cwd):
            continue
        head = _head_sha(cwd)
        if head is None:
            continue                               # unborn branch: nothing to rewrite
        if not _session_owns(session_id, head):
            return head
    return None


def _amend_message(head: str) -> str:
    return (
        f"git commit --amend in the shared primary checkout is blocked: HEAD "
        f"{head[:12]} is not recorded as this session's own commit "
        f"(nexus-9wxu6, 2026-09-07: an amend here rewrote a peer's commit).\n"
        f"Make a new commit instead, or amend from a worktree you own. If HEAD "
        f"really is yours (committed before this guard was installed), append "
        f"`# routing-allow: <reason>` (>=8 chars); the escape is logged."
    )


# ---------------------------------------------------------------------------
# Rule 4: a bare `git push` to the integration branch in a repo that ships
# the vouched push script (nexus-9wxu6).
# ---------------------------------------------------------------------------

_VOUCHED_PUSH_SCRIPT = os.path.join("scripts", "git-push-develop.sh")
_INTEGRATION: frozenset[str] = frozenset({"develop"})


def _toplevel(cwd: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def _push_target(tokens: list[str], cwd: str) -> str | None:
    """The branch a ``git push`` segment would update, or None for a tag
    push / undeterminable. Same resolution as rule 2: explicit refspec,
    else the upstream, else the current branch name."""
    positional: list[str] = []
    skip_next = False
    for tok in _strip_shell_redirections(tokens[1:]):
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
    if "--follow-tags" not in tokens and "--tags" in tokens and not refspecs:
        return None
    if refspecs:
        for spec in refspecs:
            if spec.startswith("refs/tags/") or re.fullmatch(r"v\d+\.\d+\.\d+", spec):
                continue
            dst = spec.split(":")[-1].lstrip("+")
            return dst.rsplit("/", 1)[-1]
        return None
    return _upstream_branch(cwd) or _current_branch(cwd)


def _bare_push_to_integration(command: str, cwd: str) -> str | None:
    """The integration branch a bare ``git push`` in *command* would update
    when the repo at *cwd* ships the vouched push script, else None. The
    script's own inner push is never seen here: the tool command is the
    script, not ``git push``."""
    segments = _push_tokens(command)
    if not segments:
        return None
    top = _toplevel(cwd)
    if top is None or not os.path.exists(os.path.join(top, _VOUCHED_PUSH_SCRIPT)):
        return None
    for tokens in segments:
        target = _push_target(tokens, cwd)
        if target in _INTEGRATION:
            return target
    return None


def _bare_push_message(branch: str) -> str:
    return (
        f"A bare `git push` to `{branch}` is blocked in this repo (nexus-9wxu6): "
        f"the checkout is shared and every session commits as the same user, so "
        f"the outbound range must be vouched.\n"
        f"Use  scripts/git-push-develop.sh <sha> [<sha> ...]  naming the commits "
        f"you made; it fetches, reads origin/{branch}..{branch}, and pushes only "
        f"when the range equals your list (NX_PUSH_SOURCE=HEAD from a detached "
        f"worktree).\n"
        f"To override, append `# routing-allow: <reason>` (>=8 chars); the escape "
        f"is logged."
    )


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------



#: ``git commit`` short-flag groups that stage every tracked modification.
#: ``-a`` in any group (``-a``, ``-am``, ``-av``) sweeps the working tree into
#: the commit, which in a shared checkout is the widest possible version of
#: the RULE 5 defect.
_COMMIT_STAGE_ALL_LONG = {"--all"}


def _commit_tokens_clean(tokens: list[str]) -> list[str]:
    return [t.rstrip(")") for t in _strip_shell_redirections(tokens)]


def _commit_stages_everything(tokens: list[str]) -> bool:
    """True iff this ``git commit`` carries ``-a``/``--all`` (in any short
    group). Such a commit cannot take a pathspec at all -- git rejects the
    combination -- so there is no scoped form to redirect the author to."""
    for tok in _commit_tokens_clean(tokens):
        if tok == "--":
            break
        if tok in _COMMIT_STAGE_ALL_LONG:
            return True
        if len(tok) > 1 and tok[0] == "-" and tok[1] != "-" and "a" in tok[1:]:
            return True
    return False


#: Glob metacharacters. A pathspec carrying one of these names an unknown
#: set, which is the thing this rule exists to refuse.
_GLOB_CHARS = set("*?[")


def _is_specific_path(token: str, cwd: str) -> bool:
    """True iff *token* names ONE file rather than a set of them."""
    if not token or token in {".", "..", "./"}:
        return False
    if token.endswith("/"):
        return False
    if _GLOB_CHARS & set(token):
        return False
    candidate = token if os.path.isabs(token) else os.path.join(cwd, token)
    return not os.path.isdir(candidate)


def _commit_has_pathspec(tokens: list[str], cwd: str) -> bool:
    """True iff the segment names at least one pathspec after ``--`` and
    EVERY name is a specific file.

    A bare directory, ``.``, or a glob is not a scoped commit. Measured
    2026-09-18 in a throwaway repo, reproducing the founding incident's
    exact shape: with ``docs/rdr/mine.md`` and a peer's
    ``docs/rdr/rdr-212-peer.md`` both staged, ``git commit -m x --
    docs/rdr/`` committed BOTH. ``-- .`` committed everything, including a
    file at the repo root. Git takes the working-tree content of every
    path matching the pathspec, so a directory name is functionally
    ``-a`` scoped to a subtree -- and this rule hunts down ``-a`` by name
    a few lines above while letting its equivalent through.

    Rejecting a directory costs the caller nothing they should not be
    paying: naming the files is the entire point, and if the list is long
    enough to be annoying that is itself the signal the commit is too
    broad to eyeball.
    """
    toks = _commit_tokens_clean(tokens)
    if "--" not in toks:
        return False
    named = [t for t in toks[toks.index("--") + 1:] if t]
    if not named:
        return False
    return all(_is_specific_path(t, cwd) for t in named)


def _in_progress_operation(cwd: str) -> str | None:
    """The in-flight merge/cherry-pick/revert/rebase, or None.

    A partial commit is IMPOSSIBLE during these -- git itself refuses with
    "fatal: cannot do a partial commit during a merge" -- so demanding a
    pathspec would block conflict resolution and the mandatory post-release
    back-merge. RULE 5 stands down rather than ship a guard whose only
    escape is the override.
    """
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    git_dir = os.path.join(cwd, r.stdout.strip())
    found: str | None = None
    for name, label in (
        ("MERGE_HEAD", "merge"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
    ):
        if os.path.exists(os.path.join(git_dir, name)):
            found = label
            break
    if found is None:
        return None
    # The state file's EXISTENCE is not enough. It persists with no TTL until
    # the operation is committed or aborted, so an ordinary `git merge
    # --no-commit` -- a sanctioned "inspect before committing" step -- would
    # leave MERGE_HEAD lying around and disable this rule for every session
    # sharing the checkout, indefinitely. That reopens the exact defect the
    # rule exists to close, with no adversarial intent required.
    #
    # So require the operation to be LIVE: an unmerged index entry. That is
    # what makes a partial commit impossible, which is the only reason the
    # exemption exists. A merge whose conflicts are all resolved and staged
    # can take a pathspec again -- and by then naming paths is exactly what
    # the caller should be doing.
    try:
        u = subprocess.run(
            ["git", "diff", "--cached", "--diff-filter=U", "--name-only"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return found
    if u.returncode != 0:
        return found
    return found if u.stdout.strip() else None


#: Heredoc introducers: ``<<WORD``, ``<<-WORD``, ``<<'WORD'``, ``<<"WORD"``.
#: ``<<<`` (a here-STRING) is deliberately excluded -- it has no body to skip.
_HEREDOC_RE = re.compile(r"<<-?\s*([\"']?[A-Za-z_][A-Za-z0-9_]*[\"']?)(?!<)")

def _strip_heredoc_bodies(command: str) -> str:
    """*command* with every heredoc BODY removed, delimiters and all.

    A PreToolUse hook sees one blob of shell text, and ``shlex`` has no idea
    that the lines between ``<<'PY'`` and ``PY`` are a Python program rather
    than more shell. So a script that merely MENTIONS a git command -- a test
    fixture, a docs snippet, a patch script writing a deny message -- gets
    tokenised as if it were running one.

    That is not hypothetical and it is not rare: writing THIS rule's own
    tests, twice in five minutes, a ``python3 - <<'PY'`` heredoc whose body
    contained the string ``git commit -m x`` was refused as an unscoped
    commit in the primary. RULE 5 is far more exposed to this than rules 1-4
    because ``git commit`` is ordinary prose in test and doc text, where
    ``git add -A`` and ``git push origin main`` are not.

    Handles ``<<WORD``, ``<<-WORD``, ``<<'WORD'`` and ``<<"WORD"``. A body
    whose terminator never arrives is dropped to end-of-input, which is the
    conservative direction: text that was never going to execute as a command
    cannot then be read as one.
    """
    out: list[str] = []
    lines = command.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        markers = _HEREDOC_RE.findall(line)
        i += 1
        for raw_marker in markers:
            marker = raw_marker.strip("\"'")
            while i < len(lines) and lines[i].strip() != marker:
                i += 1
            i += 1  # skip the terminator itself
    return "\n".join(out)


#: Shell operators that end a command, as raw text outside quotes.
_TOPLEVEL_OPS = ("&&", "||", ";;", ";", "|", "\n")

#: Openers that begin a NESTED command whose body is still a command:
#: command substitution, a subshell, a process substitution.
_NESTED_OPENERS = ("$(", "<(", ">(", "`", "(")


def _split_shell_toplevel(command: str) -> list[str]:
    """Split *command* into command-sized pieces, respecting QUOTES.

    Rule 5 cannot reuse ``_SEGMENT_SPLIT_RE``: that regex runs against raw
    text, so it cannot tell an operator from the same characters inside a
    quoted argument. Two measured consequences, both on 2026-09-18:

    * A multi-line ``git commit -m "...."`` had its MESSAGE's newlines
      treated as separators, so the ``-- <paths>`` landed in a different
      piece than the verb and a correctly-scoped commit was refused. This
      repo writes long multi-line commit messages by convention, so the
      rule refused the sanctioned form (found by nexus-01).
    * ``x=$(git commit -m sneaky)`` produced NO segment at all -- ``git``
      was never the first token of a piece -- and was silently allowed,
      which is the bead's own defect wearing a subshell.

    So this walks the string once, tracking single quotes, double quotes
    and backslash escapes, and cuts only on operators found OUTSIDE them.
    A nested-command opener also cuts, so the command inside a
    substitution is examined on its own rather than disappearing into an
    argument. Closing ``)`` and backticks are dropped as separators too;
    the pieces they bound are what matters, not the punctuation.
    """
    pieces: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(command)
    in_single = False
    in_double = False
    restore_double: list[str] = []
    while i < n:
        ch = command[i]
        if in_single:
            if ch == "'":
                in_single = False
            buf.append(ch)
            i += 1
            continue
        if in_double:
            if ch == "\\" and i + 1 < n:
                buf.append(ch)
                buf.append(command[i + 1])
                i += 2
                continue
            # Command substitution is LIVE inside double quotes -- `echo
            # "$(git commit -m x)"` runs the commit. Only single quotes make
            # it literal. Treating a double-quoted region as inert text let
            # exactly that spelling through (measured 2026-09-18).
            nested_dq = next(
                (o for o in ("$(", "`") if command.startswith(o, i)), None
            )
            if nested_dq is not None:
                pieces.append("".join(buf))
                buf = []
                in_double = False
                restore_double.append(")" if nested_dq == "$(" else "`")
                i += len(nested_dq)
                continue
            if ch == '"':
                in_double = False
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue
        nested = next((o for o in _NESTED_OPENERS if command.startswith(o, i)), None)
        if nested is not None:
            pieces.append("".join(buf))
            buf = []
            i += len(nested)
            continue
        if ch in ")`":
            pieces.append("".join(buf))
            buf = []
            # If this closer ends a substitution that began inside double
            # quotes, the rest of that quoted string resumes.
            if restore_double and restore_double[-1] == ch:
                restore_double.pop()
                in_double = True
            i += 1
            continue
        op = next((o for o in _TOPLEVEL_OPS if command.startswith(o, i)), None)
        if op is not None:
            pieces.append("".join(buf))
            buf = []
            i += len(op)
            continue
        buf.append(ch)
        i += 1
    pieces.append("".join(buf))
    return [p for p in (piece.strip() for piece in pieces) if p]


def _commit_segments_quote_aware(command: str) -> list[tuple[list[str], str | None]]:
    """``git commit`` segments found with quote-aware splitting.

    Mirrors ``_git_verb_segments``'s contract (tokens from the verb onward,
    plus any ``-C`` directory) but sources its pieces from
    :func:`_split_shell_toplevel`. Kept separate rather than changing
    ``_git_verb_segments`` itself, because rules 1-4 and their ~90 tests
    are built on that function's current behaviour and this rule's bugs
    are not theirs to inherit.
    """
    out: list[tuple[list[str], str | None]] = []
    for piece in _split_shell_toplevel(command):
        try:
            candidates = [shlex.split(piece, posix=True)]
        except ValueError:
            candidates = _degraded_token_variants(piece)
        for tokens in candidates:
            k = 0
            while k < len(tokens) and _ENV_ASSIGN_RE.match(tokens[k]):
                k += 1
            tokens = tokens[k:]
            if len(tokens) < 2 or tokens[0] != "git":
                continue
            c_dir: str | None = None
            j = 1
            while j < len(tokens) and tokens[j].startswith("-"):
                if tokens[j] in {"-C", "-c"}:
                    if tokens[j] == "-C" and j + 1 < len(tokens):
                        c_dir = tokens[j + 1]
                    j += 2
                else:
                    j += 1
            if j < len(tokens) and tokens[j] == "commit":
                out.append((tokens[j:], c_dir))
                break
    return out


def _has_linked_worktrees(cwd: str) -> bool:
    """True iff this repo has at least one LINKED worktree besides the primary.

    Rule 5 is about a checkout several sessions share. ``_is_primary_checkout``
    answers a narrower question -- "primary rather than linked worktree" --
    which is true of every ordinary single-checkout repo on the machine, so
    the rule fired in throwaway repos that no one else can possibly be
    committing into. Measured within five minutes of the rule going live: a
    ``git commit`` in a ``mktemp -d`` repo was refused.

    A linked worktree is the observable signature of the setup this rule
    exists for. It is a heuristic, not a proof -- a shared checkout with no
    worktrees goes uncovered here -- and that gap is deliberate: the
    push-time scope audit still covers it, which is why both halves were
    built (Sam, 2026-09-18).
    """
    try:
        r = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return False
    if r.returncode != 0:
        return False
    return sum(1 for line in r.stdout.splitlines() if line.startswith("worktree ")) > 1


def _cd_target_before_commit(command: str, payload_cwd: str) -> str | None:
    """The directory a leading ``cd`` moves to before the first ``git commit``.

    A PreToolUse hook is handed the SESSION's cwd, not the directory the
    command will actually run in, so ``cd /tmp/scratch && git commit -m x``
    is judged against the session's checkout. Measured 2026-09-18, by this
    rule refusing a commit in a throwaway ``mktemp -d`` repo purely because
    the session happened to be sitting in the shared primary -- a false
    positive, and the kind that gets a guard switched off.

    Only a ``cd`` that appears BEFORE the first ``git commit`` counts:
    ``git commit && cd elsewhere`` must still be judged where the commit
    runs. A relative target resolves against the session cwd.
    """
    lowered = command.find("git ")
    head = command if lowered < 0 else command[:command.find("commit", lowered)]
    target: str | None = None
    for segment in re.split(_SEGMENT_SPLIT_RE, head):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        i = 0
        while i < len(tokens) and _ENV_ASSIGN_RE.match(tokens[i]):
            i += 1
        tokens = tokens[i:]
        if len(tokens) >= 2 and tokens[0] == "cd":
            candidate = tokens[1]
            if candidate not in {"-", "--"}:
                target = candidate
    if target is None:
        return None
    return target if os.path.isabs(target) else os.path.join(payload_cwd, target)


def _unscoped_commit_in_primary(command: str, payload: dict[str, Any]) -> str | None:
    """``"stage-all"`` or ``"bare"`` when *command* commits the whole index in
    the primary checkout, else None.

    RULE 5. See the module docstring.
    """
    # Heredoc bodies out first, then NEWLINES become ordinary separators.
    # _SEGMENT_SPLIT_RE knows && || ; | then do -- not "\n" -- so in a
    # multi-line command everything after the first line landed in one
    # segment whose first token was not `git`, and the rule saw nothing.
    # Measured while writing this rule's own tests: a `cat > f <<'EOF' ...
    # EOF` followed by a real `git commit -m x` on the next line was
    # ALLOWED. Any script with a setup line above its commit was a bypass.
    command = _strip_heredoc_bodies(command)
    segments = _commit_segments_quote_aware(command)
    if not segments:
        return None
    payload_cwd = str(payload.get("cwd") or "") or os.getcwd()
    cd_target = _cd_target_before_commit(command, payload_cwd)
    if cd_target is not None and os.path.isdir(cd_target):
        payload_cwd = cd_target
    for tokens, c_dir in segments:
        cwd = _effective_cwd(payload_cwd, c_dir)
        if not _is_primary_checkout(cwd):
            continue
        if not _has_linked_worktrees(cwd):
            # Not a shared checkout by any observable signal -- see
            # _has_linked_worktrees for why that is the signal chosen and
            # what it deliberately leaves to the push-time audit.
            continue
        if _commit_stages_everything(tokens):
            return "stage-all"
        if _commit_has_pathspec(tokens, cwd):
            continue
        if "--" in _commit_tokens_clean(tokens):
            return "broad-pathspec"
        toks = _commit_tokens_clean(tokens)
        if "--allow-empty" in toks or "--allow-empty-message" in toks:
            continue
        if _in_progress_operation(cwd) is not None:
            continue
        return "bare"
    return None


def _unscoped_commit_message(kind: str) -> str:
    if kind == "broad-pathspec":
        lead = (
            "That pathspec names a SET of files, not specific ones. A bare "
            "directory, `.`, or a glob makes `git commit` take the working-"
            "tree content of everything matching it -- which is `-a` scoped "
            "to a subtree, and `-a` is refused here by name. Name the files."
            "\n\nMeasured 2026-09-18 in a throwaway repo, reproducing this "
            "incident exactly: with `docs/rdr/mine.md` and a peer's "
            "`docs/rdr/rdr-212-peer.md` both staged, `git commit -m x -- "
            "docs/rdr/` committed BOTH."
        )
    elif kind == "stage-all":
        lead = (
            "`git commit -a` stages every tracked modification in the working "
            "tree, including files a peer session is midway through editing. "
            "It cannot take a pathspec -- git rejects the combination -- so "
            "stage what you mean with `git add <path>` and commit with an "
            "explicit `-- <paths>`."
        )
    else:
        lead = (
            "A bare `git commit` in the SHARED PRIMARY checkout commits the "
            "whole index, not just what you staged. Name your paths: "
            "`git commit -m \"...\" -- path/a.py path/b.py`."
        )
    return (
        f"{lead}\n"
        "\n"
        "Why (nexus-bbriq): on 2026-09-17 a peer had staged a 740-line "
        "docs/rdr/rdr-212-*.md draft in this same index. An accept commit ran "
        "`git add <two paths>` then a bare `git commit`, so the peer's "
        "unreviewed draft rode commit 0249b0c98 to origin/develop. The "
        "nexus-9wxu6 push script vouches COMMITS, not index contents, so "
        "nothing downstream caught it.\n"
        "\n"
        "TWO THINGS A PATHSPEC DOES NOT BUY YOU, both measured here:\n"
        "  1. `git commit -- <path>` commits the WORKING TREE version of that "
        "path. On a file two sessions are BOTH editing it will quietly carry "
        "the other session's uncommitted text under your message (nexus-01, "
        "2026-09-18). A pathspec protects the paths you did not name; it "
        "guarantees nothing about one you did.\n"
        "  2. Naming only the new path of a rename STRANDS THE DELETE, and "
        "the commit ships both copies. `git mv` stages two index entries and "
        "you must name BOTH (2026-09-18, b439089a1 before its amend).\n"
        "\n"
        "Check what you are about to ship with `git status --short`, then "
        "name every path. In a linked worktree this rule does not apply; a "
        "merge, cherry-pick, revert or rebase in progress is exempt, because "
        "git itself refuses a partial commit there.\n"
        "\n"
        "Escape (audited, logged): append `# routing-allow: <reason>`."
    )

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

    amend_head: str | None = None
    bare_push: str | None = None
    if not wildcard_add and not push_to_main:
        amend_head = _amend_on_foreign_tip(command, payload)
        if amend_head is None:
            cwd = str(payload.get("cwd") or "") or os.getcwd()
            bare_push = _bare_push_to_integration(command, cwd)

    unscoped_commit: str | None = None
    if not wildcard_add and not push_to_main and amend_head is None and bare_push is None:
        unscoped_commit = _unscoped_commit_in_primary(command, payload)

    if (
        not wildcard_add
        and not push_to_main
        and amend_head is None
        and bare_push is None
        and unscoped_commit is None
    ):
        _allow()

    if _should_skip_for_reason(command):
        _log_event("escape", command_fragment=command, escape_reason=_extract_escape_reason(command))
        _allow()

    _log_event("deny", command_fragment=command)
    # Each check keeps its OWN message.
    if unscoped_commit is not None:
        _deny(
            _unscoped_commit_message(unscoped_commit),
            summary=(
                "unscoped git commit in the shared primary blocked: commit with "
                "an explicit `-- <paths>` (nexus-bbriq)."
            ),
        )
    if bare_push is not None:
        _deny(
            _bare_push_message(bare_push),
            summary=f"bare git push to {bare_push} blocked: use scripts/git-push-develop.sh <sha>... (nexus-9wxu6).",
        )
    if amend_head is not None:
        _deny(
            _amend_message(amend_head),
            summary="git commit --amend on a foreign tip in the primary checkout blocked (nexus-9wxu6).",
        )
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
