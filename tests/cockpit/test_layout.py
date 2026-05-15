# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the cockpit auto-layout primitive (RDR-111 Phase 3).

V1 is intentionally a fixed vertical stack with truncation rules; the
Bakke-style multi-pane engine is a follow-up. These tests pin the v1
contract: every panel renders as a header band + body rows, body rows
truncated to width, no overflow.
"""

from __future__ import annotations


def test_layout_stacks_panels_vertically():
    from nexus.cockpit.layout import RenderedPanel, layout_vertical_stack

    panels = [
        RenderedPanel(title="P1", lines=["a", "b"]),
        RenderedPanel(title="P2", lines=["c"]),
    ]
    descriptor = layout_vertical_stack(panels, width=40)
    assert descriptor.width == 40
    assert len(descriptor.sections) == 2
    assert descriptor.sections[0].title == "P1"
    assert descriptor.sections[1].title == "P2"


def test_layout_truncates_long_lines():
    from nexus.cockpit.layout import RenderedPanel, layout_vertical_stack

    long = "x" * 100
    panels = [RenderedPanel(title="wide", lines=[long])]
    descriptor = layout_vertical_stack(panels, width=20)
    body = descriptor.sections[0].lines
    assert len(body) == 1
    assert len(body[0]) <= 20


def test_layout_render_text_produces_block():
    from nexus.cockpit.layout import RenderedPanel, layout_vertical_stack, render_text

    panels = [
        RenderedPanel(title="A", lines=["one", "two"]),
        RenderedPanel(title="B", lines=["three"]),
    ]
    descriptor = layout_vertical_stack(panels, width=40)
    text = render_text(descriptor)
    # Header bands present
    assert "A" in text
    assert "B" in text
    # Body lines preserved
    assert "one" in text
    assert "three" in text
    # No em-dashes in output (project rule)
    assert "—" not in text


def test_layout_empty_panel_renders_placeholder():
    from nexus.cockpit.layout import RenderedPanel, layout_vertical_stack, render_text

    panels = [RenderedPanel(title="empty", lines=[])]
    descriptor = layout_vertical_stack(panels, width=30)
    text = render_text(descriptor)
    assert "empty" in text
    # Placeholder when no rows: a clear no-data line
    assert "no data" in text.lower() or "(empty)" in text.lower()
