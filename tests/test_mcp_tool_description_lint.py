# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""MCP tool description shape lint (nexus-cnzei.5).

Origin: an audit (T2 nexus/llm-guidance-audit-mcp-2026-09-13) found 13 of
57 registered MCP tool descriptions exceeded the harness's 2048-character
truncation cut (nx_answer alone ran to 15,881 chars), 0 of ~250 schema
properties carried a description (every param doc lived only in the
docstring and vanished past the cut), and 36/57 descriptions carried bead
ids, ISO dates, or incident narrative that belongs in a code comment, not
a wire-facing description. This lint ratchets the fixed shape that bead
established: purpose, when-vs-sibling, returns, 2-4 constraints, moved
history to comments beside the function, one Field description per
parameter.

Non-vacuity (nexus-moht0 doctrine): every check below has a companion
"planted violation" test proving the check actually fires, not merely
that the current tree happens to be clean.
"""
from __future__ import annotations

import importlib
import re

import pytest

pytestmark = pytest.mark.lint

#: The harness's measured hard truncation point (T2 nexus/llm-guidance-
#: audit-mcp-2026-09-13): a description past this length is silently cut
#: mid-sentence, and any parameter documentation past the cut vanishes
#: entirely. Hard failure.
HARNESS_TRUNCATION_LIMIT = 2048

#: This bead's fixed-shape target. Soft ceiling: a description above this
#: still fits well under the harness cut and is not incorrect, but is a
#: signal the shape (purpose / when-vs-sibling / returns / constraints) is
#: drifting long again -- warned, not failed, so a genuinely justified
#: longer description (e.g. tuple_out/tuple_ack's preserved rule text,
#: bead nexus-r7xao) does not need an allowlist entry.
TARGET_LENGTH = 1200

#: nexus-cnzei.5's own no-history-in-descriptions rule. Matches a real bead
#: id (nexus-cnzei, nexus-e59o, ...) but ALSO a legitimate compound like
#: "nexus-catalog" or "nexus-service" -- those are server/component names,
#: not incident references, so they are allowlisted by exact string rather
#: than loosening the regex (a loosened regex would stop catching the
#: shorter real bead ids this rule exists to catch).
BEAD_ID_RE = re.compile(r"nexus-[a-z0-9]{4,}")
_BEAD_ID_ALLOWLIST = {"nexus-catalog", "nexus-service"}

ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

_SERVER_MODULES = (
    ("nexus.mcp.core", "nexus"),
    ("nexus.mcp.catalog", "nexus-catalog"),
)


def _iter_tool_descriptions() -> list[tuple[str, str, str]]:
    """Yield (server, tool_name, description) for every registered tool."""
    out: list[tuple[str, str, str]] = []
    for module_path, server_name in _SERVER_MODULES:
        mod = importlib.import_module(module_path)
        for name, tool in sorted(mod.mcp._tool_manager._tools.items()):
            out.append((server_name, name, tool.description or ""))
    return out


def _iter_param_descriptions() -> list[tuple[str, str, str, str | None]]:
    """Yield (server, tool_name, param_name, description-or-None) for every
    registered tool's every parameter."""
    out: list[tuple[str, str, str, str | None]] = []
    for module_path, server_name in _SERVER_MODULES:
        mod = importlib.import_module(module_path)
        for name, tool in sorted(mod.mcp._tool_manager._tools.items()):
            props = tool.parameters.get("properties", {})
            for pname, pschema in sorted(props.items()):
                out.append((server_name, name, pname, pschema.get("description")))
    return out


def _bead_id_hits(text: str) -> list[str]:
    return [m for m in BEAD_ID_RE.findall(text) if m not in _BEAD_ID_ALLOWLIST]


# ── Non-vacuity floor ────────────────────────────────────────────────────


def test_non_vacuity_floor() -> None:
    tools = _iter_tool_descriptions()
    params = _iter_param_descriptions()
    assert len(tools) >= 50, f"only found {len(tools)} tool descriptions -- census may be broken"
    assert len(params) >= 150, f"only found {len(params)} parameter entries -- census may be broken"


# ── Length ───────────────────────────────────────────────────────────────


def test_no_description_exceeds_the_harness_truncation_limit() -> None:
    over = [
        (server, name, len(desc))
        for server, name, desc in _iter_tool_descriptions()
        if len(desc) > HARNESS_TRUNCATION_LIMIT
    ]
    assert not over, (
        f"{len(over)} tool description(s) exceed the {HARNESS_TRUNCATION_LIMIT}-char "
        f"harness truncation cut -- the harness silently cuts these mid-sentence, "
        f"dropping any parameter docs written into the docstring past that point:\n  "
        + "\n  ".join(f"{s}::{n} ({L} chars)" for s, n, L in over)
    )


def test_descriptions_stay_within_the_target_length() -> None:
    over = [
        (server, name, len(desc))
        for server, name, desc in _iter_tool_descriptions()
        if len(desc) > TARGET_LENGTH
    ]
    if over:
        import warnings

        warnings.warn(
            f"{len(over)} tool description(s) exceed the {TARGET_LENGTH}-char shape "
            "target (still under the hard harness cut): "
            + ", ".join(f"{s}::{n} ({L} chars)" for s, n, L in over),
            stacklevel=1,
        )


def test_planted_oversized_description_is_detected() -> None:
    """Non-vacuity: the length check actually distinguishes long from short."""
    fake = [("nexus", "planted_tool", "x" * (HARNESS_TRUNCATION_LIMIT + 1))]
    over = [(s, n, len(d)) for s, n, d in fake if len(d) > HARNESS_TRUNCATION_LIMIT]
    assert over


# ── Parameter schema coverage ────────────────────────────────────────────


def test_every_parameter_has_a_schema_description() -> None:
    missing = [
        (server, name, pname)
        for server, name, pname, desc in _iter_param_descriptions()
        if not desc
    ]
    assert not missing, (
        f"{len(missing)} parameter(s) have no schema description -- undocumented "
        "past the harness cut and invisible to a client reading inputSchema alone:\n  "
        + "\n  ".join(f"{s}::{n}.{p}" for s, n, p in missing)
    )


def test_planted_missing_param_description_is_detected() -> None:
    fake = [("nexus", "planted_tool", "planted_param", None)]
    missing = [(s, n, p) for s, n, p, d in fake if not d]
    assert missing


# ── No history/bead-ids/dates in descriptions ────────────────────────────


def test_no_bead_id_pattern_in_tool_descriptions() -> None:
    hits = [
        (server, name, m)
        for server, name, desc in _iter_tool_descriptions()
        for m in _bead_id_hits(desc)
    ]
    assert not hits, (
        f"{len(hits)} tool description(s) carry a bead-id pattern -- move history/"
        f"incident references to a code comment beside the function:\n  "
        + "\n  ".join(f"{s}::{n} ({m})" for s, n, m in hits)
    )


def test_no_bead_id_pattern_in_parameter_descriptions() -> None:
    hits = [
        (server, name, pname, m)
        for server, name, pname, desc in _iter_param_descriptions()
        for m in _bead_id_hits(desc or "")
    ]
    assert not hits, (
        f"{len(hits)} parameter description(s) carry a bead-id pattern:\n  "
        + "\n  ".join(f"{s}::{n}.{p} ({m})" for s, n, p, m in hits)
    )


def test_no_iso_date_in_tool_descriptions() -> None:
    hits = [
        (server, name, m)
        for server, name, desc in _iter_tool_descriptions()
        for m in ISO_DATE_RE.findall(desc)
    ]
    assert not hits, (
        f"{len(hits)} tool description(s) carry an ISO date -- move dated history "
        f"to a code comment beside the function:\n  "
        + "\n  ".join(f"{s}::{n} ({m})" for s, n, m in hits)
    )


def test_no_iso_date_in_parameter_descriptions() -> None:
    hits = [
        (server, name, pname, m)
        for server, name, pname, desc in _iter_param_descriptions()
        for m in ISO_DATE_RE.findall(desc or "")
    ]
    assert not hits, (
        f"{len(hits)} parameter description(s) carry an ISO date:\n  "
        + "\n  ".join(f"{s}::{n}.{p} ({m})" for s, n, p, m in hits)
    )


def test_versions_and_rdr_ids_are_not_flagged() -> None:
    """Negative control: a release version, an engine tag or an RDR id is
    legitimate description text and must not trip the bead-id or date
    patterns (cnzei.5 CRE pass)."""
    for text in ("since 7.19.0", "see RDR-197", "engine-service-v0.1.118", "RDR-205 P4b"):
        assert not BEAD_ID_RE.search(text), text
        assert not ISO_DATE_RE.search(text), text


def test_planted_bead_id_is_detected() -> None:
    """Non-vacuity: the bead-id regex fires on a real-shaped id and does
    NOT fire on the allowlisted server-name compounds."""
    assert _bead_id_hits("see nexus-cnzei for context") == ["nexus-cnzei"]
    assert _bead_id_hits("the nexus-catalog server") == []
    assert _bead_id_hits("the nexus-service jar") == []


def test_planted_iso_date_is_detected() -> None:
    assert ISO_DATE_RE.findall("fixed on 2026-09-13 after the incident") == ["2026-09-13"]
    assert ISO_DATE_RE.findall("no date here") == []
