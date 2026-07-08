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
"""
from __future__ import annotations

import os
import re
import shlex
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


def body(payload: dict[str, Any]) -> None:
    command = _lib.get_bash_command(payload)
    if not command:
        _lib.allow()

    # nexus-mzvwa.8: match FIRST, escape SECOND. Pre-fix the escape check ran
    # before the matcher, so ANY '# routing-allow:'-annotated Bash command
    # logged a phantom escape against this rule (6,130 over the RDR-121 soak
    # window, zero of which contained a git-add wildcard) — destroying the
    # esc% telemetry. An escape event now means exactly "this command WOULD
    # have been denied and the operator overrode it".
    if not _scan_command(command):
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
    # Explicit summary so the transcript banner stays a terse one-liner
    # independent of _redirect_message's first line; full text reaches the model.
    _lib.deny(
        _redirect_message(),
        summary="git add wildcard (-A/./--all) blocked: stage by explicit path.",
    )


if __name__ == "__main__":
    _lib.run_hook(body, fail_closed=False, rule_name=RULE_NAME)
