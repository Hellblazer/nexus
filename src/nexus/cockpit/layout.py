# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cockpit auto-layout primitive (RDR-111 §Phase 3, nexus-ut5r).

V1 is a fixed vertical stack with truncation rules. Each input
``RenderedPanel`` becomes one section: a header band (``=== title ===``),
the body lines truncated to width, and a blank trailer.

The Bakke-style multi-pane layout engine (responsive columns, panel
weights, focus management) is a deliberate follow-up. Keeping v1 narrow
makes the dashboard ship today without entangling the Phase 3 surface
with the larger layout-engine design discussion.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class RenderedPanel:
    """Input to the layout engine: a panel that already knows its lines."""

    title: str
    lines: list[str]


@dataclasses.dataclass(frozen=True)
class LayoutSection:
    """One section of a laid-out descriptor: title band + body lines."""

    title: str
    lines: list[str]


@dataclasses.dataclass(frozen=True)
class LayoutDescriptor:
    """Structured layout output: list of sections, plus the chosen width."""

    width: int
    sections: list[LayoutSection]


_PLACEHOLDER_EMPTY = "(empty -- no data)"


def _truncate(line: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(line) <= width:
        return line
    # Reserve one character for an ellipsis indicator. Use ".." rather
    # than the unicode ellipsis to keep ascii-only output guaranteed.
    if width <= 2:
        return line[:width]
    return line[: width - 2] + ".."


def layout_vertical_stack(
    panels: list[RenderedPanel],
    *,
    width: int,
) -> LayoutDescriptor:
    """Stack *panels* vertically; truncate each line to *width*.

    Empty panels render with a single placeholder line so the section
    still appears on the dashboard (so the user can see "yes, this panel
    ran -- and the answer was 'nothing here'").
    """
    sections: list[LayoutSection] = []
    for p in panels:
        if not p.lines:
            body = [_PLACEHOLDER_EMPTY]
        else:
            body = [_truncate(ln, width) for ln in p.lines]
        sections.append(LayoutSection(title=p.title, lines=body))
    return LayoutDescriptor(width=width, sections=sections)


def render_text(descriptor: LayoutDescriptor) -> str:
    """Render a :class:`LayoutDescriptor` to a single text block.

    Header band is ``=== title ===`` padded with ``=`` to the layout
    width. The body lines follow verbatim, then one blank line as a
    separator before the next section.
    """
    chunks: list[str] = []
    width = max(descriptor.width, 8)
    for section in descriptor.sections:
        label = f" {section.title} "
        # Header band: leading "== ", label, trailing "=" to width.
        prefix = "== "
        remaining = max(0, width - len(prefix) - len(label))
        header = prefix + label + ("=" * remaining)
        chunks.append(_truncate(header, width))
        for line in section.lines:
            chunks.append(line)
        chunks.append("")
    return "\n".join(chunks).rstrip("\n") + "\n"
