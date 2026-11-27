# SPDX-License-Identifier: AGPL-3.0-or-later
"""Language detection for the preamble's CLAUDE.md agent-readiness check (GH #1689).

Split out of :mod:`nexus.commands.command_context` (over its size cap).
"""
from __future__ import annotations

import re

# Languages a CLAUDE.md names, each as a case-sensitive proper name with word boundaries (GH #1689: the old
# case-insensitive alternation reported the verb "go" as Go, Java inside JavaScript, Rust inside "rusty").  Go is also an
# English word, so it needs a stronger cue than its name: a version, "Golang", ``go.mod`` or a go tool command.
LANGUAGE_CUES: list[tuple[str, re.Pattern[str]]] = [
    (label, re.compile(pattern))
    for label, pattern in [
        ("Python", r"\bPython\b"),
        ("Java", r"\bJava\b"),
        ("Go", r"\bGolang\b|\bGo\s+\d+(?:\.\d+)*\b|\bgo\.mod\b|\bgo (?:build|test|mod|vet|run)\b"),
        ("Rust", r"\bRust\b|\bCargo\.toml\b"),
        ("TypeScript", r"\bTypeScript\b"),
        ("Node.js", r"\bNode\.js\b"),
        ("C++", r"C\+\+"),
        ("C#", r"\bC#"),
        ("Ruby", r"\bRuby\b"),
        ("Kotlin", r"\bKotlin\b"),
        ("Swift", r"\bSwift\b"),
        ("Scala", r"\bScala\b"),
        ("Lua", r"\bLua\b"),
    ]
]


def detect_language(content: str) -> str | None:
    """The label of the language cue that appears first in *content*, or ``None``."""
    found = [(m.start(), label) for label, rx in LANGUAGE_CUES if (m := rx.search(content))]
    return min(found)[1] if found else None
