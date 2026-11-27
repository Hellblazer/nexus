# SPDX-License-Identifier: AGPL-3.0-or-later
"""Language detection for the preamble's CLAUDE.md agent-readiness check (GH #1689).

Split out of :mod:`nexus.commands.command_context` (over its size cap).

Every name matches case-insensitively on word boundaries, so ``python`` and ``typescript`` in a command line still count
and ``java`` does not match inside ``JavaScript``, nor ``rust`` inside ``rusty``.  Go is the exception, because "go" is
also an English verb: it matches only on a cue that cannot be the verb.  The cues are ``Golang``, a version (``Go 1.22``),
``go.mod``, "written in Go" and "a Go service", and a go tool command (``go build|test|mod|vet|run``) inside a code span or
at a line start, which covers a fence's lines.  "We go run the suite" and "go run through the list" in prose do not match.
"""
from __future__ import annotations

import re

_GO_TOOLS = r"go (?:build|test|mod|vet|run)\b"

LANGUAGE_CUES: list[tuple[str, re.Pattern[str]]] = [
    (label, re.compile(pattern, 0 if label == "Go" else re.IGNORECASE))
    for label, pattern in [
        ("Python", r"\bpython\b"),
        ("Java", r"\bjava\b"),
        ("JavaScript", r"\bjavascript\b"),
        (
            "Go",
            r"(?i:\bgolang\b)"
            r"|\bGo\s+\d+(?:\.\d+)*\b"
            r"|\bgo\.mod\b"
            r"|\b(?:written|implemented|built|coded)\s+(?:in|with)\s+Go\b"
            r"|\b(?i:a)\s+Go\s+(?:service|module|project|codebase|binary|package|library|program)\b"
            rf"|(?m:^[ \t]*(?:[-*>$][ \t]+)?{_GO_TOOLS})"
            rf"|`[^`\n]*\b{_GO_TOOLS}[^`\n]*`",
        ),
        ("Rust", r"\brust\b|\bCargo\.toml\b"),
        ("TypeScript", r"\btypescript\b"),
        ("Node.js", r"\bnode\.?js\b"),
        ("C++", r"C\+\+"),
        ("C#", r"\bC#"),
        ("Ruby", r"\bruby\b"),
        ("Kotlin", r"\bkotlin\b"),
        ("Swift", r"\bswift\b"),
        ("Scala", r"\bscala\b"),
        ("Lua", r"\blua\b"),
    ]
]


def detect_language(content: str) -> str | None:
    """The label of the language cue that appears first in *content*, or ``None``."""
    found = [(m.start(), label) for label, rx in LANGUAGE_CUES if (m := rx.search(content))]
    return min(found)[1] if found else None
