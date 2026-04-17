# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-085: Project-vocabulary resolver for the topic labeler.

Two call sites:

  * ``relabel_topics`` in :mod:`nexus.commands.taxonomy_cmd` — run once
    per command invocation, cached per batch.
  * The post-discover auto-label banner in :mod:`nexus.commands.index`
    — same pathway via ``relabel_topics``.

Load order (highest wins):

  1. ``.nexus.yml#taxonomy.glossary`` — authoritative, one-place config.
  2. ``docs/glossary.md`` — bulleted markdown list.
  3. empty dict — opt-in; labeler proceeds without a vocabulary
     preamble, matching pre-RDR-085 behaviour exactly.

A malformed config or parse failure logs a debug line and returns
``{}`` — the labeler must never block on glossary problems.
"""
from __future__ import annotations

import re
from pathlib import Path


def load_glossary(
    project_root: Path,
    collection: str | None = None,
) -> dict[str, str]:
    """Return a ``{term: expansion}`` map from the project's glossary.

    Args:
        project_root: Repo root containing ``.nexus.yml`` / ``docs/``.
        collection: Reserved for a future per-collection override
            (``<collection>.glossary.md``). Unused in v1.
    """
    # Priority 1: .nexus.yml#taxonomy.glossary
    yml = project_root / ".nexus.yml"
    if yml.exists():
        try:
            import yaml  # pyyaml is already a nexus dep

            data = yaml.safe_load(yml.read_text()) or {}
        except Exception:
            data = {}
        if isinstance(data, dict):
            section = data.get("taxonomy")
            if isinstance(section, dict):
                gloss = section.get("glossary")
                if isinstance(gloss, dict):
                    return {str(k): str(v) for k, v in gloss.items() if v}

    # Priority 2: docs/glossary.md — bulleted markdown, one term per line
    md = project_root / "docs" / "glossary.md"
    if md.exists():
        return _parse_markdown_glossary(md.read_text())

    return {}


_BULLET_ENTRY = re.compile(
    r"""
    ^\s*[-*+]\s*                   # bullet
    (?:\*\*)?(?P<term>[A-Za-z0-9_\-]+)(?:\*\*)?  # optional **bold**
    \s*[:\-—]\s*                   # separator: ':' or '-' or '—'
    (?P<defn>.+?)\s*$              # definition (rest of line)
    """,
    re.VERBOSE,
)


def _parse_markdown_glossary(text: str) -> dict[str, str]:
    """Extract ``- TERM: expansion`` / ``- **TERM**: expansion`` pairs."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _BULLET_ENTRY.match(line)
        if m:
            out[m.group("term")] = m.group("defn").strip()
    return out


def format_for_prompt(terms: dict[str, str], max_tokens: int = 500) -> str:
    """Render a glossary as a prompt preamble, bounded at ``max_tokens``.

    Output shape::

        Project vocabulary (use these expansions when an acronym matches):
        - TERM: expansion
        - TERM: expansion
        ...

    Token budget is a rough 4-chars-per-token proxy — the labeler's
    prompt is short-form content, so the estimate is accurate enough
    to keep the preamble from dominating. Excess entries are silently
    dropped rather than truncating mid-line.
    """
    if not terms:
        return ""

    header = "Project vocabulary (use these expansions when an acronym matches):\n"
    budget_chars = max(0, max_tokens * 4 - len(header))

    lines: list[str] = []
    used = 0
    for term, defn in terms.items():
        line = f"- {term}: {defn}"
        if used + len(line) + 1 > budget_chars:
            break
        lines.append(line)
        used += len(line) + 1  # +1 for the '\n'

    if not lines:
        return ""
    return header + "\n".join(lines) + "\n"
