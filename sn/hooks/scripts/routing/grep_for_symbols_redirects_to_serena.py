#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-121 Phase 2 hook 1: redirect grep / rg on code files to Serena.

When the user runs ``grep`` or ``rg`` with an identifier-shaped pattern
against code files (``*.py *.swift *.java *.ts *.tsx *.go *.rs``), deny
with a redirect to Serena's symbol-navigation MCP tools.

Allowed identifier shapes (deny):
- Single identifier:        ``MyClass``
- Dotted-id chain:          ``Module.Class.method``
- Pipe-alternation of ids:  ``MyClass|YourClass``

Disqualifiers (allow):
- ``TODO`` / ``FIXME`` / ``XXX`` / ``HACK`` (all-uppercase short tokens)
- Whitespace anywhere in the pattern
- Regex metachars besides the pipe in alternation
"""
from __future__ import annotations

import os
import re
import shlex
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
import _lib  # noqa: E402

RULE_NAME = "grep_for_symbols_redirects_to_serena"

CODE_EXTENSIONS = (".py", ".swift", ".java", ".ts", ".tsx", ".go", ".rs")
DISQUALIFIED_TOKENS = frozenset({"TODO", "FIXME", "XXX", "HACK", "NOTE", "WIP"})

# Regex meta chars that disqualify a pattern from being "identifier-shaped"
# (pipe is allowed only in the alternation shape, handled separately).
_META_CHARS = set(r".*+?^$()[]{}\\")

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")


def _strip_quotes(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _is_identifier_pattern(pattern: str) -> bool:
    """Return True iff ``pattern`` matches one of the three symbol shapes."""
    p = _strip_quotes(pattern).strip()
    if not p:
        return False
    if any(ch.isspace() for ch in p):
        return False
    if p.upper() in DISQUALIFIED_TOKENS:
        return False

    # Single identifier
    if _IDENT.match(p):
        return True

    # Dotted identifier chain (no other meta chars allowed)
    if _DOTTED.match(p):
        return True

    # Pipe alternation: split on '|' and require every segment to be a
    # bare identifier with no other meta chars in the original pattern.
    if "|" in p:
        # No other regex meta chars besides '|'.
        if any(ch in _META_CHARS for ch in p):
            return False
        parts = p.split("|")
        if len(parts) >= 2 and all(_IDENT.match(seg) for seg in parts if seg):
            return True

    return False


def _parse_grep_invocation(command: str) -> tuple[str, list[str]] | None:
    """Return (pattern, file_args) for the first grep/rg call in *command*.

    Splits the command on shell separators (``;`` ``&&`` ``||`` ``|``)
    and finds the first sub-command whose first token is ``grep`` or
    ``rg``. Returns None when no such call is found or parsing fails.
    """
    # Cheap split on top-level shell separators. This is a best-effort
    # parse; the hook is advisory and we tolerate false negatives.
    # Only split on multi-char shell separators (&&, ||, ;) plus a
    # whitespace-flanked single pipe. A bare `|` inside a quoted regex
    # (`grep -E 'A|B'`) must not split the command.
    segments = re.split(r"(?:&&|\|\||;|\s\|\s|\bthen\b|\bdo\b)", command)
    for segment in segments:
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        if not tokens:
            continue
        if tokens[0] not in ("grep", "rg"):
            continue
        # Drop flags; the first non-flag positional is the pattern.
        positional: list[str] = []
        i = 1
        while i < len(tokens):
            t = tokens[i]
            if t == "--":
                positional.extend(tokens[i + 1 :])
                break
            if t.startswith("-"):
                # Flags that consume a value
                if t in ("-e", "--regexp", "-f", "--file", "--type", "-t",
                         "--glob", "-g", "--include", "--exclude",
                         "--max-count", "-m"):
                    i += 2
                    continue
                i += 1
                continue
            positional.append(t)
            i += 1
        if not positional:
            return None
        pattern = positional[0]
        files = positional[1:]
        return pattern, files
    return None


def _has_code_file(files: list[str]) -> bool:
    if not files:
        return False
    return any(f.lower().endswith(CODE_EXTENSIONS) for f in files)


def _redirect_message(pattern: str, files: list[str]) -> str:
    file_arg = files[0] if files else "<file>"
    target = " ".join(files) if files else "<file>"
    return (
        f"Blocked: `grep {pattern} {target}` searches a code file for an "
        f"identifier-shaped pattern ('{pattern}'). That is a symbol-"
        "navigation task, so it was redirected to a structural tool that "
        "resolves definitions and callers instead of returning raw text "
        "lines.\n\n"
        "Why blocked: grep/rg for a bare identifier on a code file misses "
        "overloads, hits string/comment false positives, and gives you no "
        "call graph. Do one of these instead:\n\n"
        "  1. Serena (best for symbols). Tool names vary by backend, so "
        "load whichever resolves, then call it:\n"
        "       ToolSearch(\"select:mcp__plugin_sn_serena__jet_brains_find_symbol,"
        "mcp__plugin_sn_serena__find_symbol\")\n"
        f"     - Definition: find_symbol(name_path_pattern=\"{pattern}\")\n"
        f"     - Callers:    find_referencing_symbols(name_path=\"{pattern}\", "
        f"relative_path=\"{file_arg}\")\n"
        f"     - File map:   get_symbols_overview(relative_path=\"{file_arg}\")\n"
        "     (JetBrains backend prefixes jet_brains_; LSP backend is "
        "unprefixed. See the sn serena-code-nav skill for the full table.)\n"
        "  2. The built-in Grep tool: faster than bash grep and "
        "structured, if you genuinely want text matches not symbols.\n"
        "  3. Keep bash grep by appending an escape reason (>=8 chars):\n"
        f"     grep {pattern} {target}  # routing-allow: <why text search fits>"
    )


def body(payload: dict[str, Any]) -> None:
    command = _lib.get_bash_command(payload)
    if not command:
        _lib.allow()

    if _lib.should_skip_for_reason(command):
        _lib.log_routing_event(
            rule=RULE_NAME, outcome="escape", tool_name="Bash",
            command_fragment=command,
        )
        _lib.allow()

    parsed = _parse_grep_invocation(command)
    if parsed is None:
        _lib.allow()
    pattern, files = parsed

    if not _has_code_file(files):
        _lib.allow()

    if not _is_identifier_pattern(pattern):
        _lib.allow()

    _lib.log_routing_event(
        rule=RULE_NAME, outcome="deny", tool_name="Bash",
        command_fragment=command,
    )
    summary = (
        f"grep for symbol '{pattern}' on a code file -> use Serena "
        "(find_symbol / find_referencing_symbols), the built-in Grep tool, "
        "or append '# routing-allow: <reason>' to keep bash grep."
    )
    _lib.deny(_redirect_message(pattern, files), summary=summary)


if __name__ == "__main__":
    _lib.run_hook(body, fail_closed=False, rule_name=RULE_NAME)
