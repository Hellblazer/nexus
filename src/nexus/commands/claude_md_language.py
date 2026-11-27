# SPDX-License-Identifier: AGPL-3.0-or-later
"""Language detection for the preamble's CLAUDE.md agent-readiness check (GH #1689).

Split out of :mod:`nexus.commands.command_context` (over its size cap).

A name that is also an English word must read as a proper name or as a tool, never as the word:

* ``python``, ``java``, ``javascript``, ``typescript``, ``node``/``nodejs`` and ``lua`` match in any case, on word
  boundaries, because their lowercase form is a tool or a command (``java`` does not match inside ``JavaScript``).
* ``Rust``, ``Swift``, ``Ruby`` and ``Scala`` match only capitalised: "a swift response", "the rust on the pipe", "ruby
  red" are not languages.  ``Cargo.toml`` is a Rust cue in any case.
* ``Go`` matches only on a cue that cannot be the verb: ``Golang``, a version (``Go 1.22``), ``go.mod``, "written in
  Go" and "a Go service", and a go tool command (``go build|test|mod|vet|run``) that reads as a shell line: inside a
  backtick span or a fence, after a ``$ `` prompt, or at a line start with a path or flag after it (``go test ./...``,
  ``go build -v``).  "We go run the suite" and a hard-wrapped prose line that starts "go run through the list" do not
  match.

When a project names both TypeScript and JavaScript, TypeScript is the answer: a TypeScript project mentions JavaScript
in passing (its build output, its runtime), and the reverse is not true.
"""
from __future__ import annotations

import re

_GO_TOOLS = r"go (?:build|test|mod|vet|run)\b"

#: Cues for Go outside a fence: the position of the first match is the language's position.
_GO_CUES = re.compile(
    r"(?i:\bgolang\b)"
    r"|\bGo\s+\d+(?:\.\d+)*\b"
    r"|\bgo\.mod\b"
    r"|\b(?:written|implemented|built|coded)\s+(?:in|with)\s+Go\b"
    r"|\b(?i:a)\s+Go\s+(?:service|module|project|codebase|binary|package|library|program)\b"
    rf"|(?m:^[ \t]*(?:[-*>][ \t]+)?\$[ \t]+{_GO_TOOLS})"
    rf"|(?m:^[ \t]*(?:[-*>][ \t]+)?{_GO_TOOLS}(?=[ \t]+[-.]))"
    rf"|`[^`\n]*\b{_GO_TOOLS}[^`\n]*`"
)
#: A go tool command at a line start, which inside a fence is a command whatever follows it.
_GO_IN_FENCE = re.compile(rf"(?m:^[ \t]*(?:\$[ \t]+)?{_GO_TOOLS})")
_FENCE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", re.DOTALL | re.MULTILINE)

_NAME_CUES: list[tuple[str, re.Pattern[str]]] = [
    ("Python", re.compile(r"\bpython\b", re.IGNORECASE)),
    ("Java", re.compile(r"\bjava\b", re.IGNORECASE)),
    ("JavaScript", re.compile(r"\bjavascript\b", re.IGNORECASE)),
    ("Rust", re.compile(r"\bRust\b|(?i:\bCargo\.toml\b)")),
    ("TypeScript", re.compile(r"\btypescript\b", re.IGNORECASE)),
    ("Node.js", re.compile(r"\bnode\.?js\b", re.IGNORECASE)),
    ("C++", re.compile(r"C\+\+")),
    ("C#", re.compile(r"\bC#")),
    ("Ruby", re.compile(r"\bRuby\b")),
    ("Kotlin", re.compile(r"\bkotlin\b", re.IGNORECASE)),
    ("Swift", re.compile(r"\bSwift\b")),
    ("Scala", re.compile(r"\bScala\b")),
    ("Lua", re.compile(r"\blua\b", re.IGNORECASE)),
]


def _go_position(content: str) -> int | None:
    """Where Go is first named in *content*, or ``None``."""
    found = [m.start() for m in [_GO_CUES.search(content)] if m]
    for fence in _FENCE.finditer(content):
        if (m := _GO_IN_FENCE.search(fence.group(1))) is not None:
            found.append(fence.start(1) + m.start())
    return min(found) if found else None


def detect_language(content: str) -> str | None:
    """The language *content* names first, or ``None``; TypeScript beats JavaScript when both appear."""
    found = {label: m.start() for label, rx in _NAME_CUES if (m := rx.search(content))}
    if (go := _go_position(content)) is not None:
        found["Go"] = go
    if "TypeScript" in found:
        found.pop("JavaScript", None)
    return min(found, key=lambda label: found[label]) if found else None
