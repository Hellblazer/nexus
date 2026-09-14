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

import ast
import importlib
import inspect
import re
import textwrap

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


def _iter_tool_fns() -> list[tuple[str, str, str, object]]:
    """Yield (server, tool_name, description, underlying_fn) for every
    registered tool. ``fn`` is the plain callable FastMCP wraps -- the same
    object ``inspect.getsource``/``ast.parse`` can read (nexus-w9jxf)."""
    out: list[tuple[str, str, str, object]] = []
    for module_path, server_name in _SERVER_MODULES:
        mod = importlib.import_module(module_path)
        for name, tool in sorted(mod.mcp._tool_manager._tools.items()):
            out.append((server_name, name, tool.description or "", tool.fn))
    return out


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


# ── Documented return keys vs. actual returns (nexus-w9jxf) ────────────────
#
# Origin: a cnzei.5 critic pass found the shape lint above checks length and
# parameter coverage but never checks a description's CONTENT against the
# code -- ``search``'s rewrite once dropped ``truncated``, ``truncated_chars``,
# ``text`` and ``chunk_collections`` from its documented return shape, and
# ``search_topic_scoped``'s rewrite dropped ``contents``; the existing checks
# above are blind to both, since neither is a length, coverage, bead-id or
# date defect.
#
# Descriptions name return keys with a ``{key, key, ...}`` brace form, either
# bare (``{ids, tumblers}``) or quoted (``{"ids": ..., "tumblers": ...}``,
# the catalog server's house style) -- ``_parse_return_key_claims`` extracts
# every such brace span from a tool's description and reduces each
# comma-separated segment to its leading key name, discarding any ``: TYPE``
# / ``: VALUE`` tail (``"created": bool`` -> ``created``) since the tail is
# illustrative, never part of the key identity being asserted. A bare ``...``
# segment marks the WHOLE set non-exhaustive (a lower bound, e.g. nx_answer's
# open-ended envelope) rather than an exact match. A brace span immediately
# preceded by ``[`` (``list[{item_id, quote, role}]``) describes a NESTED
# object's shape, not this tool's own top-level keys, and is skipped --
# without this, operator_check's per-evidence-item shape would be checked
# against operator_check's own (unrelated) top-level return.
#
# Ground truth comes from ``ast``: ``_ast_return_keys`` walks a tool
# function's OWN statement scope (never descending into a nested def/lambda,
# so an unrelated closure's return shape can never leak in) collecting every
# ``return {...}`` dict-LITERAL's string keys. A dict spread (``**other``)
# contributes nothing (its keys are not statically knowable) and is silently
# ignored rather than flagged -- this check only ever compares keys it can
# prove, in either direction. A literal whose ONLY key is ``"error"`` is
# dropped before comparison: the generic ``except Exception: return
# {"error": ...}`` MCP-boundary shape appears on most tools and documenting
# it is not this bead's concern (a RICHER error dict that also carries real
# payload keys, e.g. store_get_many's ``{contents, missing, error}``, is
# NOT dropped -- only the bare singleton is). Multiple documented claims
# and multiple literal returns on one tool (a success shape and a decorated
# error shape, as `nx_answer_report` and `store_get_many` both have) are
# UNIONED before comparing -- each individual claim need not by itself
# equal the whole return surface, only the total documented surface must
# equal the total returned surface.
#
# INDIRECTION (the bead's explicit escape hatch): a handful of tools thread
# their return through another function -- `search()` returns
# `_search_render()`'s result or wraps it in a `CallToolResult`,
# `tuple_registry`/`tuple_stats` return an HTTP client method's result,
# `catalog_show`'s owner-branch builds its dict via `**owner`, `nx_answer`
# builds its envelope in a nested closure spanning ~2000 lines. Chasing
# these statically would mean either resolving arbitrary call graphs (out
# of scope, and a call to a genuinely external service like
# `db.tuples.registry()` cannot be resolved locally at all) or naively
# unioning every reachable literal (which pulls in unrelated edge-case
# shapes -- `_search_render`'s zero-hit payload adds keys that have nothing
# to do with what `search()`'s docstring is describing). Both are worse
# than a small, explicit, hand-verified table: ``_RETURN_KEY_OVERRIDES``.
# A change to any of these five tools' return shape needs a human to
# re-verify the override by hand -- exactly the enforcement this bead wants
# for exactly the tools static analysis cannot reach.
#
# A handful more (`operator_check`, `operator_verify`) return whatever an
# LLM subprocess produces against a JSON schema (`claude_dispatch`), not a
# Python dict literal at all -- their ground truth is the schema module
# (`nexus.operators.schemas`), a different and out-of-scope verification.
# They are named in `_SCHEMA_DRIVEN_RETURN_TOOLS` and skipped outright.
#
# Any OTHER tool whose description names return keys but whose return this
# check can neither resolve via AST nor find in the override table fails
# LOUD (never a silent pass) -- the nexus-moht0 non-vacuity doctrine applied
# here: an unverifiable claim is not evidence of a correct one.

_RETURN_KEY_BRACE_RE = re.compile(r"\{[^{}]*\}")
_RETURN_KEY_SEGMENT_RE = re.compile(r'^"?([A-Za-z_][A-Za-z0-9_]*)"?')


def _parse_return_key_claims(text: str) -> list[tuple[frozenset[str], bool]]:
    """Extract every ``{key, key, ...}``-shaped return-key claim from *text*.

    Returns one ``(keys, exhaustive)`` pair per brace span that reduces to a
    flat key list. ``exhaustive=False`` means the span carried a bare ``...``
    segment (a documented lower bound, not the full set). A span that isn't
    a flat key list (a segment with no leading identifier at all) or whose
    only key is ``error`` is dropped, not returned.
    """
    claims: list[tuple[frozenset[str], bool]] = []
    for m in _RETURN_KEY_BRACE_RE.finditer(text):
        if text[: m.start()].rstrip().endswith("["):
            continue  # nested shape (``list[{...}]``), not this tool's own keys
        keys: set[str] = set()
        exhaustive = True
        parseable = True
        for segment in m.group(0)[1:-1].split(","):
            segment = segment.strip()
            if not segment:
                continue
            if segment == "...":
                exhaustive = False
                continue
            seg_match = _RETURN_KEY_SEGMENT_RE.match(segment)
            if not seg_match:
                parseable = False
                break
            keys.add(seg_match.group(1))
        if not parseable or not keys or keys == {"error"}:
            continue
        claims.append((frozenset(keys), exhaustive))
    return claims


def _iter_own_returns(node: ast.AST):
    """Yield every ``ast.Return`` in *node*'s own scope.

    Never descends into a nested ``def``/``async def``/``lambda``/``class``
    -- a sibling closure's return shape (an unrelated helper, or an
    edge-case branch factored into its own function) must never leak into
    the enclosing tool's own documented contract.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(child, ast.Return):
            yield child
        yield from _iter_own_returns(child)


def _ast_return_keys(fn) -> frozenset[str] | None:
    """Union of string keys from every dict-LITERAL *fn* directly returns.

    ``None`` means *fn* has no such literal anywhere in its own scope (every
    return is a bare name, a call, or a singleton ``{"error": ...}``) -- the
    caller falls back to ``_RETURN_KEY_OVERRIDES``. A dict literal built
    with a ``**spread`` contributes only its own literal keys; the spread's
    keys are not statically knowable and are never asserted about.
    """
    try:
        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        return None
    fn_def = tree.body[0]
    keys: set[str] = set()
    found_any = False
    for ret in _iter_own_returns(fn_def):
        if not isinstance(ret.value, ast.Dict):
            continue
        literal_keys = {
            k.value for k in ret.value.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        if literal_keys and literal_keys != {"error"}:
            keys |= literal_keys
            found_any = True
    return frozenset(keys) if found_any else None


#: Tools whose structured return this check cannot resolve via direct
#: dict-literal AST scanning -- see the module comment above for why each
#: is genuinely indirect rather than merely inconvenient. Hand-verified
#: against the current source; re-verify by hand on any change to these
#: functions' return shape.
_RETURN_KEY_OVERRIDES: dict[str, frozenset[str]] = {
    # search(): `structured=True` returns `_search_render()`'s dict
    # directly (6 keys); the default text-success path additionally wraps
    # a CallToolResult whose structuredContent carries those 6 plus
    # `truncated`/`truncated_chars`/`text`. Verified against
    # `_search_render`'s own structured-branch literal and `search()`'s
    # `structured_content` construction.
    "search": frozenset({
        "ids", "tumblers", "distances", "collections",
        "chunk_collections", "chunk_text_hash",
        "truncated", "truncated_chars", "text",
    }),
    # tuple_registry(): returns `db.tuples.registry()`, an HTTP call whose
    # shape is defined server-side. Verified against
    # `HttpTupleStore.registry()`'s own docstring/contract.
    "tuple_registry": frozenset({"digest", "sources", "templates"}),
    # tuple_stats(): returns `_tuple_census_to_dict(c)`, a same-module
    # helper this check deliberately does not chase through (no
    # cross-function resolution, to avoid pulling in keys from unrelated
    # branches elsewhere in the call graph). Verified against that
    # helper's own literal dict.
    "tuple_stats": frozenset({
        "subspace", "total", "available", "claimed", "dead", "consumed",
        "expired_unpurged", "oldest_created_at", "newest_created_at",
    }),
    # catalog_show (registered as "show"): the owner-prefix branch builds
    # its dict as `{"kind": "owner", **owner}` then adds `document_count`
    # by subscript; the document branch starts from `entry.to_dict()` (an
    # external call) and adds `links_from`/`links_to` by subscript. None
    # of that is a plain literal `return {...}`. Verified by hand.
    "show": frozenset({"kind", "document_count", "links_from", "links_to"}),
    # nx_answer(): every real return funnels through a ~2000-line nested
    # closure that builds one envelope dict literal. Verified against
    # that literal directly.
    "nx_answer": frozenset({
        "final_text", "chunks", "plan_id", "step_count",
        "budget_exhausted_at_step", "steps", "cost_usd", "truncated_chars",
        "budget_warnings", "dropped_reduce_steps", "plan_choice",
        "continuation", "answer_shape",
    }),
}

#: Tools whose return shape is enforced by an LLM JSON schema
#: (`claude_dispatch`), never a Python dict literal -- the schema
#: (`nexus.operators.schemas`) is the real ground truth and verifying it
#: is a different, out-of-scope check.
_SCHEMA_DRIVEN_RETURN_TOOLS = {"operator_check", "operator_verify"}


def _return_key_mismatches(
    tool_fns: list[tuple[str, str, str, object]],
) -> list[tuple[str, str, list[str], list[str]]]:
    """``(server, tool_name, documented_keys, actual_keys)`` for every tool
    whose description's return-key claim(s) do not match its code."""
    mismatches: list[tuple[str, str, list[str], list[str]]] = []
    for server, name, description, fn in tool_fns:
        if name in _SCHEMA_DRIVEN_RETURN_TOOLS:
            continue
        claims = _parse_return_key_claims(description)
        if not claims:
            continue
        actual = _RETURN_KEY_OVERRIDES.get(name)
        if actual is None:
            actual = _ast_return_keys(fn)
        if actual is None:
            # Non-vacuity (nexus-moht0): a documented claim this check
            # cannot verify is not a pass -- it needs a human-verified
            # entry in _RETURN_KEY_OVERRIDES, same as the five above.
            mismatches.append((server, name, ["<unresolvable -- add an override>"], []))
            continue
        documented = frozenset().union(*(keys for keys, _ in claims))
        exhaustive = all(exhaustive for _, exhaustive in claims)
        if exhaustive:
            ok = documented == actual
        else:
            ok = documented <= actual
        if not ok:
            mismatches.append((server, name, sorted(documented), sorted(actual)))
    return mismatches


def test_documented_return_keys_match_actual_returns() -> None:
    mismatches = _return_key_mismatches(_iter_tool_fns())
    assert not mismatches, (
        f"{len(mismatches)} tool(s) document return keys that don't match "
        f"what the code returns:\n  "
        + "\n  ".join(
            f"{s}::{n} documented={d} actual={a}" for s, n, d, a in mismatches
        )
    )


def test_planted_return_key_mismatch_is_detected() -> None:
    """Non-vacuity: a description that drops a real key (the exact search()/
    search_topic_scoped() regression this check exists to catch) fires."""

    def _planted_tool() -> dict:
        """Planted tool.

        Returns `{ids, tumblers, distances}` when structured.
        """
        return {"ids": [], "tumblers": [], "distances": [], "chunk_collections": []}

    mismatches = _return_key_mismatches(
        [("nexus", "planted_tool", _planted_tool.__doc__, _planted_tool)]
    )
    assert len(mismatches) == 1
    server, name, documented, actual = mismatches[0]
    assert (server, name) == ("nexus", "planted_tool")
    assert "chunk_collections" not in documented
    assert "chunk_collections" in actual


def test_planted_return_key_match_is_not_flagged() -> None:
    """Negative control: an accurate claim does not fire."""

    def _planted_tool() -> dict:
        """Planted tool.

        Returns `{ids, tumblers}` when structured.
        """
        return {"ids": [], "tumblers": []}

    assert _return_key_mismatches(
        [("nexus", "planted_tool", _planted_tool.__doc__, _planted_tool)]
    ) == []


def test_ellipsis_return_key_claim_is_a_lower_bound() -> None:
    """A trailing `...` segment makes the claim a subset check, not an
    exact one -- extra undocumented keys don't fire, but a claimed key that
    is never actually returned still does."""

    def _open_ended_tool() -> dict:
        """Planted tool.

        Returns `{final_text, plan_id, ...}` when structured.
        """
        return {"final_text": "", "plan_id": "", "extra_field": None}

    assert _return_key_mismatches(
        [("nexus", "open_ended_tool", _open_ended_tool.__doc__, _open_ended_tool)]
    ) == []

    def _open_ended_tool_missing_key() -> dict:
        """Planted tool.

        Returns `{final_text, never_returned, ...}` when structured.
        """
        return {"final_text": ""}

    mismatches = _return_key_mismatches(
        [("nexus", "x", _open_ended_tool_missing_key.__doc__, _open_ended_tool_missing_key)]
    )
    assert len(mismatches) == 1


def test_nested_brace_return_key_claim_is_not_checked_against_top_level() -> None:
    """A brace span describing a NESTED item's shape (`list[{...}]`) must
    not be checked against the tool's own top-level return (the
    operator_check case: its per-evidence-item shape is unrelated to its
    own, entirely dynamic, top-level return)."""

    def _planted_tool() -> dict:
        """Planted tool.

        Returns `{ok, evidence: list[{item_id, quote, role}]}`.
        """
        return {"ok": True, "evidence": []}

    # "item_id"/"quote"/"role" are never returned at this tool's top level,
    # but the nested-shape span must be skipped rather than compared.
    assert _return_key_mismatches(
        [("nexus", "planted_tool", _planted_tool.__doc__, _planted_tool)]
    ) == []


def test_unresolvable_return_key_claim_fails_loud_without_an_override() -> None:
    """Non-vacuity: a claim this check can neither resolve via AST nor find
    in the override table is a failure, never a silent pass."""

    def _opaque_tool() -> dict:
        """Planted tool.

        Returns `{ids, tumblers}` when structured.
        """
        return _some_other_function()  # noqa: F821 -- deliberately unresolvable, never called

    mismatches = _return_key_mismatches(
        [("nexus", "opaque_planted_tool", _opaque_tool.__doc__, _opaque_tool)]
    )
    assert len(mismatches) == 1
    assert mismatches[0][0:2] == ("nexus", "opaque_planted_tool")
