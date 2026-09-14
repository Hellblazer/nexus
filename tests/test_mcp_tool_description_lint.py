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
from typing import Callable

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
# the catalog server's house style) -- ``_parse_return_key_claims`` finds
# every TOP-LEVEL brace span (``_iter_balanced_brace_spans``, a brace-depth
# walk, not a flat regex -- see its own docstring for why a claim with any
# nested brace needs that) and reduces each top-level comma-separated
# segment (``_split_top_level``, which never splits inside a nested
# ``{...}``/``[...]``) to its leading key name, discarding any ``: TYPE`` /
# ``: VALUE`` tail (``"created": bool`` -> ``created``) since the tail is
# illustrative, never part of the key identity being asserted. A bare ``...``
# segment marks the WHOLE set non-exhaustive (a lower bound, e.g. nx_answer's
# open-ended envelope) rather than an exact match. A brace span immediately
# preceded by ``[`` (``list[{item_id, quote, role}]``) describes a NESTED
# object's shape, not this tool's own top-level keys, and is skipped --
# without this, operator_check's per-evidence-item shape would be checked
# against operator_check's own (unrelated) top-level return. A segment that
# NAMES a nested shape (``evidence: list[{item_id, ...}]``) still reduces to
# its own leading key (``evidence``) at the OUTER level -- only the nested
# braces inside that segment are exempt from becoming separate keys.
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
# builds its envelope in a nested closure spanning ~2000 lines. `_resolve_
# return_keys` chases what it safely can via one-hop AST derivation
# (`_DERIVED_RETURN_KEY_RESOLVERS`: `search` -> `_search_render`'s
# structured-branch literal unioned with `search()`'s own
# `structured_content` assignment; `tuple_stats` -> `_tuple_census_to_dict`'s
# literal; `nx_answer` -> its nested `_result` closure's literal), so a
# future edit to any of those three's return shape is caught automatically
# instead of silently drifting from a table nothing forces anyone to
# re-verify (nexus-dszac gap 1, the ORIGINAL regression this bead names:
# `search` was itself one of the hand-typed overrides). The two that
# genuinely cannot be derived -- `tuple_registry` (`db.tuples.registry()`
# is a real HTTP call to a service; the shape lives server-side, not in
# this process at all) and `show` (both branches build via assignment +
# subscript, `return d`, never a literal `return {...}`) -- stay in the
# small, explicit, hand-verified `_RETURN_KEY_OVERRIDES` table, each pinned
# against a second in-repo source
# (`test_tuple_registry_override_matches_http_store_contract`,
# `test_show_override_matches_catalog_show_ast_derivation`,
# `test_show_override_owner_keys_never_collide_with_catalog_entry_to_dict`)
# so a human still has to re-verify the override by hand on a source-shape
# change, but a drift between the override and its pin fails loud rather
# than needing a human to notice unprompted.
#
# A KNOWN, ACCEPTED gap in the `search`/`nx_answer` derivations (code-
# review finding, nexus-dszac fix round 2 item 4): `_search_render`'s
# ZERO-HIT branch returns `_structured_no_results(diag)` -- a CALL, not a
# dict literal -- so its extra keys (`no_results_reason`,
# `threshold_dropped`, and, when a threshold actually dropped a
# candidate, `closest_dropped`) are invisible to `_derive_search_return_
# keys` exactly the way any other call-returning branch is invisible to
# `_ast_return_keys`. `nx_answer`'s one-hop chase has the same shape of
# gap in principle (a branch that returns via a call rather than a
# literal would be equally invisible to `_nested_fn_return_keys`), though
# nx_answer's actual early-return paths were checked and all resolve back
# through `_result`'s own literal (see `_budget_exhausted_response`,
# itself nested in `nx_answer` and ending in `return _result(...)`) --
# there is no live instance of this gap for nx_answer today, only the
# same STRUCTURAL exposure. This is NOT a silent-pass risk: the failure
# direction is the SAFE one. A description that documents ONLY the base
# keys (the common case, and the only case in this tree today) is
# unaffected -- the derivation's keys are still exactly the base set. A
# description that additionally, exhaustively claims one of the zero-hit-
# only keys would make this check OVER-STRICT (a false mismatch on a key
# that genuinely is returned, just on a branch this check cannot see),
# never under-strict (silently accepting a real drift). Over-strict fails
# loud and visibly; that is what "safe" means here.
#
# A handful more (`operator_check`, `operator_verify`) return whatever an
# LLM subprocess produces against a JSON schema (`claude_dispatch`), not a
# Python dict literal at all -- their ground truth is the schema module
# (`nexus.operators.schemas`), a different and out-of-scope verification.
# They are named in `_SCHEMA_DRIVEN_RETURN_TOOLS` and skipped outright.
#
# Any OTHER tool whose description names return keys but whose return this
# check can neither resolve via override, derivation, nor AST fails LOUD
# (never a silent pass) -- the nexus-moht0 non-vacuity doctrine applied
# here: an unverifiable claim is not evidence of a correct one. The same
# doctrine gates the check's OWN applicability
# (`test_documented_return_keys_match_actual_returns`'s
# `_MIN_CHECKED_RETURN_KEY_TOOLS` floor, nexus-dszac gap 2): the mismatch
# assertion alone passes vacuously if zero tools yield a parseable claim
# (e.g. every description drifting off brace notation), so the floor
# asserts a minimum count of tools actually checked, not merely that none
# of however-many-that-turns-out-to-be failed.

_RETURN_KEY_SEGMENT_RE = re.compile(r'^"?([A-Za-z_][A-Za-z0-9_]*)"?')


def _iter_balanced_brace_spans(text: str) -> list[tuple[int, int]]:
    """Yield ``(start, end)`` spans of TOP-LEVEL ``{...}`` groups in *text*
    (``text[start:end]`` is the full ``{...}`` substring, closing brace
    included).

    A brace-depth walk, not a regex: a group containing further ``{``/``}``
    nesting (``{ok, evidence: list[{item_id, ...}]}``) is still yielded as
    ONE span covering the whole outer group — the prior regex
    (``\\{[^{}]*\\}``) could only ever match a FLAT group, so a claim with
    any nested brace never matched at its own (outer) level at all; it
    silently fell through to matching the innermost nested group instead,
    which the ``preceded by "["`` check below then (correctly, but for the
    wrong span) skipped — the outer keys were simply never seen. This walk
    reports the outer span so its own top-level keys are checked, while
    the nested span stays reachable as its own independent top-level span
    whenever it stands alone (``list[{item_id, ...}]`` with no enclosing
    outer claim) — both cases covered by one mechanism.

    An unbalanced trailing ``{`` with no matching ``}`` yields nothing for
    that span, never a partial or garbage match.
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    spans.append((start, i + 1))
                    start = -1
    return spans


def _split_top_level(s: str, sep: str) -> list[str]:
    """Split *s* on *sep* only where bracket/brace/paren depth is zero.

    Needed because a return-key segment can itself carry a nested shape
    (``evidence: list[{item_id, quote, role}]``) whose internal commas
    must not fragment that ONE segment into several.
    """
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in s:
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _parse_return_key_claims(text: str) -> list[tuple[frozenset[str], bool]]:
    """Extract every ``{key, key, ...}``-shaped return-key claim from *text*.

    Returns one ``(keys, exhaustive)`` pair per TOP-LEVEL brace span (see
    ``_iter_balanced_brace_spans``) that reduces to a flat key list.
    ``exhaustive=False`` means the span carried a bare ``...`` segment (a
    documented lower bound, not the full set). A span that isn't a flat
    key list (a segment with no leading identifier at all) or whose only
    key is ``error`` is dropped, not returned. A segment describing a
    nested shape (``evidence: list[{item_id, ...}]``) still reduces to its
    OWN leading key (``evidence``) — the nested braces inside it are never
    split into separate segments (``_split_top_level``) and never produce
    their own claim from this span.
    """
    claims: list[tuple[frozenset[str], bool]] = []
    for start, end in _iter_balanced_brace_spans(text):
        if text[:start].rstrip().endswith("["):
            continue  # nested shape (``list[{...}]``), not this tool's own keys
        keys: set[str] = set()
        exhaustive = True
        parseable = True
        for segment in _split_top_level(text[start + 1:end - 1], ","):
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


def _iter_own_nodes(node: ast.AST):
    """Yield every descendant of *node* that is in ITS OWN scope.

    Never descends into a nested ``def``/``async def``/``lambda``/``class``
    -- a sibling closure's shape (an unrelated helper, or an edge-case
    branch factored into its own function) must never leak into the
    enclosing tool's own documented contract. Shared walk underlying both
    ``_iter_own_returns`` (below) and any other own-scope AST scan this
    module needs.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield child
        yield from _iter_own_nodes(child)


def _iter_own_returns(node: ast.AST):
    """Yield every ``ast.Return`` in *node*'s own scope (see ``_iter_own_nodes``)."""
    for child in _iter_own_nodes(node):
        if isinstance(child, ast.Return):
            yield child


def _dict_literal_return_keys(scope_node: ast.AST) -> frozenset[str] | None:
    """Union of string keys from every dict-LITERAL ``return`` in
    *scope_node*'s own scope. Shared by ``_ast_return_keys`` (top-level
    function scope) and ``_nested_fn_return_keys`` (a named nested closure's
    scope) so both apply the identical literal/spread/singleton-error rules.
    ``None`` means no such literal was found anywhere in scope.
    """
    keys: set[str] = set()
    found_any = False
    for ret in _iter_own_returns(scope_node):
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


def _ast_return_keys(fn) -> frozenset[str] | None:
    """Union of string keys from every dict-LITERAL *fn* directly returns.

    ``None`` means *fn* has no such literal anywhere in its own scope (every
    return is a bare name, a call, or a singleton ``{"error": ...}``) -- the
    caller falls back to ``_RETURN_KEY_OVERRIDES`` / a derived resolver. A
    dict literal built with a ``**spread`` contributes only its own literal
    keys; the spread's keys are not statically knowable and are never
    asserted about.
    """
    try:
        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        return None
    return _dict_literal_return_keys(tree.body[0])


def _nested_fn_return_keys(fn, nested_name: str) -> frozenset[str] | None:
    """One-hop call-graph resolution: chase a NAMED nested closure inside
    *fn*'s own source (e.g. ``nx_answer``'s ~2000-line ``_result`` closure,
    which every real return funnels through) and union ITS OWN dict-literal
    returns -- same rules as ``_ast_return_keys``, scoped to the nested
    function rather than *fn* itself.

    ``_ast_return_keys``/``_iter_own_nodes`` deliberately never descend into
    a nested ``def`` at all, to keep an UNRELATED closure's shape from
    leaking into the enclosing tool's contract; this is the explicit,
    targeted exception for a tool whose entire documented contract is
    known (by the override/derivation table, never guessed) to funnel
    through one named closure.

    Requires EXACTLY ONE ``def``/``async def`` named *nested_name* anywhere
    in *fn*'s AST (nexus-dszac fix round 2 item 2): zero or more-than-one
    matches raise ``AssertionError`` rather than silently returning
    ``None`` (indistinguishable from "genuinely nothing to chase") or
    silently picking whichever def ``ast.walk`` happens to visit first --
    exactly the risk of a second, differently-scoped closure sharing the
    same name (an unrelated helper renamed into a collision, or a copy-
    pasted branch) being substituted for the real one with no signal at
    all. ``None`` is reserved for the case this function genuinely cannot
    even attempt the chase: *fn*'s own source could not be read/parsed.
    """
    try:
        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        return None
    matches = [
        node for node in ast.walk(tree.body[0])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == nested_name
    ]
    if len(matches) != 1:
        fn_label = getattr(fn, "__qualname__", getattr(fn, "__name__", repr(fn)))
        raise AssertionError(
            f"expected exactly one nested def named {nested_name!r} inside "
            f"{fn_label}, found {len(matches)} -- refusing to silently "
            f"treat this as unresolvable (zero) or guess which one is the "
            f"real contract (more than one)"
        )
    return _dict_literal_return_keys(matches[0])


def _assigned_dict_literal_keys(fn, var_name: str) -> frozenset[str] | None:
    """Union of string keys from every ``<var_name> = {...}`` dict-LITERAL
    assignment in *fn*'s own scope (see ``_iter_own_nodes`` for the
    non-leak rule). Used to resolve extra keys a tool adds to an
    indirection's dict AFTER receiving it (``search()``'s
    ``structured_content = {**data, "truncated": ..., ...}``) -- a shape
    ``_ast_return_keys`` cannot see because the assignment, not the
    ``return``, is where the literal lives. A ``**spread`` key contributes
    nothing statically knowable and is silently ignored, same convention
    as ``_ast_return_keys``. ``None`` means no such assignment was found.
    """
    try:
        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        return None
    keys: set[str] = set()
    found_any = False
    for node in _iter_own_nodes(tree.body[0]):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        if not any(isinstance(t, ast.Name) and t.id == var_name for t in node.targets):
            continue
        literal_keys = {
            k.value for k in node.value.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        if literal_keys:
            keys |= literal_keys
            found_any = True
    return frozenset(keys) if found_any else None


def _subscript_assigned_keys(fn, var_name: str) -> frozenset[str] | None:
    """Union of string-literal keys assigned via ``<var_name>[<key>] = ...``
    subscript assignment in *fn*'s own scope (see ``_iter_own_nodes`` for
    the non-leak rule). Used to resolve keys a tool adds to a dict AFTER
    building it from an external call it does not control the shape of
    (``catalog_show``'s ``d = entry.to_dict()`` then
    ``d["links_from"] = ...`` / ``d["links_to"] = ...`` / (owner branch)
    ``d["document_count"] = ...``) -- a shape neither ``_ast_return_keys``
    nor ``_assigned_dict_literal_keys`` can see, since it is neither a
    literal ``return {...}`` nor a ``<var> = {...}`` assignment. ``None``
    means no such subscript assignment to *var_name* was found anywhere in
    *fn*'s own scope.
    """
    try:
        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        return None
    keys: set[str] = set()
    found_any = False
    for node in _iter_own_nodes(tree.body[0]):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == var_name
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                keys.add(target.slice.value)
                found_any = True
    return frozenset(keys) if found_any else None


def _mcp_core_module():
    """Lazily import ``nexus.mcp.core`` -- same deferred-import convention
    ``_iter_tool_descriptions``/``_iter_tool_fns`` already use, kept out of
    this module's own import time."""
    return importlib.import_module("nexus.mcp.core")


def _derive_search_return_keys() -> frozenset[str] | None:
    """Two-hop AST derivation of ``search()``'s full return-key ground
    truth, replacing what was a hand-typed override (nexus-dszac): the
    ``_search_render`` structured-branch dict-literal (the 6 machine-
    channel keys: ``ids``/``tumblers``/``distances``/``collections``/
    ``chunk_collections``/``chunk_text_hash``), unioned with the extra keys
    ``search()`` itself adds while building ``structured_content``
    (``truncated``/``truncated_chars``/``text``) -- the ``**`` spread of
    ``_search_render``'s own dict there contributes nothing statically
    knowable, which is exactly why the first hop is needed at all. ``None``
    on either hop failing to resolve, which fails the calling tool loud via
    the existing "unresolvable -- add an override" path rather than
    silently keeping a stale value.

    nexus-dszac fix round 2 item 1: ``search()`` ALSO hand-types a SECOND
    6-key literal, ``empty_shape`` -- the fallback used when
    ``_search_render(structured=True)`` returns something other than a
    dict (the zero-hit / off-page text-error paths). Nothing else checked
    that literal against ``_search_render``'s own structured-branch keys,
    so the two could silently drift apart from each other even while this
    derivation stayed internally consistent. Asserted equal here (raises
    ``AssertionError``, not a silent ``None``, on either an unreadable
    ``empty_shape`` literal or a genuine mismatch) rather than merely
    read and returned unchecked.
    """
    core = _mcp_core_module()
    render_keys = _ast_return_keys(core._search_render)
    if render_keys is None:
        return None
    tool = core.mcp._tool_manager._tools.get("search")
    if tool is None:
        return None
    extra_keys = _assigned_dict_literal_keys(tool.fn, "structured_content")
    if extra_keys is None:
        return None
    empty_shape_keys = _assigned_dict_literal_keys(tool.fn, "empty_shape")
    if empty_shape_keys is None:
        raise AssertionError(
            "search() no longer builds an `empty_shape = {...}` fallback "
            "literal -- update this derivation (and the empty_shape pin "
            "it enforces) or restore the literal"
        )
    if empty_shape_keys != render_keys:
        raise AssertionError(
            f"search()'s `empty_shape` fallback {sorted(empty_shape_keys)} "
            f"no longer matches _search_render's structured-branch keys "
            f"{sorted(render_keys)} -- the two 6-key literals drifted "
            f"apart independently of each other"
        )
    return render_keys | extra_keys


def _derive_tuple_stats_return_keys() -> frozenset[str] | None:
    """One-hop AST derivation of ``tuple_stats()``'s return-key ground
    truth: ``tuple_stats`` itself only ever returns ``_tuple_census_to_dict(c)``
    (a call, not a literal) or the bare-error singleton, so the real shape
    lives in that helper's own dict literal -- chased directly rather than
    hand-typed (nexus-dszac), so a future edit to the helper's keys is
    caught automatically instead of needing a human to notice and
    re-verify a frozen table entry.
    """
    core = _mcp_core_module()
    return _ast_return_keys(core._tuple_census_to_dict)


def _derive_nx_answer_return_keys() -> frozenset[str] | None:
    """One-hop AST derivation of ``nx_answer()``'s return-key ground truth:
    every real return funnels through the ``_result`` closure nested inside
    it, which builds one envelope dict literal -- chased via
    ``_nested_fn_return_keys`` rather than hand-typed (nexus-dszac), so an
    edit to that envelope automatically updates the ground truth this check
    compares against.
    """
    core = _mcp_core_module()
    tool = core.mcp._tool_manager._tools.get("nx_answer")
    if tool is None:
        return None
    return _nested_fn_return_keys(tool.fn, "_result")


#: Tools whose structured return this check derives from code rather than
#: hand-typing (nexus-dszac): each resolver chases a NAMED indirection
#: (see the module comment's INDIRECTION section) via AST, so a future
#: edit to the underlying return shape is caught automatically instead of
#: silently drifting from a frozen table entry that nothing forces anyone
#: to re-verify.
_DERIVED_RETURN_KEY_RESOLVERS: dict[str, Callable[[], frozenset[str] | None]] = {
    "search": _derive_search_return_keys,
    "tuple_stats": _derive_tuple_stats_return_keys,
    "nx_answer": _derive_nx_answer_return_keys,
}

#: Tools whose structured return genuinely cannot be derived from code in
#: this process at all -- the shape crosses a process boundary (an HTTP
#: response) or is assembled through calls/subscripts this check
#: deliberately does not chase (see the module comment's INDIRECTION
#: section). Hand-verified against the current source; re-verify by hand
#: on any change to these functions' return shape. Each entry is pinned
#: against a second, independently-checked source below
#: (``test_tuple_registry_override_matches_http_store_contract``,
#: ``test_show_override_matches_catalog_show_ast_derivation``,
#: ``test_show_override_owner_keys_never_collide_with_catalog_entry_to_dict``)
#: so a drift between this table and that second source fails loud rather
#: than needing a human to notice.
_RETURN_KEY_OVERRIDES: dict[str, frozenset[str]] = {
    # tuple_registry(): returns `db.tuples.registry()`, an HTTP call whose
    # shape is defined server-side -- genuinely outside this process, not
    # merely inconvenient to chase. Pinned against
    # `HttpTupleStore.registry()`'s own docstring contract.
    "tuple_registry": frozenset({"digest", "sources", "templates"}),
    # catalog_show (registered as "show"): the owner-prefix branch builds
    # its dict as `{"kind": "owner", **owner}` then adds `document_count`
    # by subscript; the document branch starts from `entry.to_dict()` (an
    # external call) and adds `links_from`/`links_to` by subscript. Never
    # a literal `return {...}`, so `_ast_return_keys` cannot chase it
    # directly -- but ALL FOUR keys here ARE pinned against a second
    # source, not merely the two owner-branch keys a narrower earlier pin
    # covered:
    #   - `kind` -- AST-derivable one hop out via `_assigned_dict_literal_
    #     keys(fn, "d")` (the `d = {"kind": "owner", **owner}` literal).
    #   - `document_count`/`links_from`/`links_to` -- AST-derivable one hop
    #     out via `_subscript_assigned_keys(fn, "d")` (each is a real
    #     `d["..."] = ...` subscript assignment in catalog_show's own
    #     source).
    # `test_show_override_matches_catalog_show_ast_derivation` asserts the
    # union of those two hops equals this exact set. A second, narrower
    # test additionally confirms the owner-branch keys (`kind`,
    # `document_count`) never collide with a real `CatalogEntry.to_dict()`
    # field name -- the merge/subscript order above would otherwise
    # silently shadow it instead of adding a distinct key.
    "show": frozenset({"kind", "document_count", "links_from", "links_to"}),
}


def _resolve_return_keys(name: str, fn) -> frozenset[str] | None:
    """Ground-truth resolution order for *name*'s return keys: the
    hand-verified override table first (the tools genuinely outside this
    process's reach), then a targeted derivation resolver (a named
    indirection chased via AST), then the general single-function AST
    scan. ``None`` means unresolvable by any of the three -- the caller's
    existing non-vacuity path then fails loud.
    """
    if name in _RETURN_KEY_OVERRIDES:
        return _RETURN_KEY_OVERRIDES[name]
    if name in _DERIVED_RETURN_KEY_RESOLVERS:
        return _DERIVED_RETURN_KEY_RESOLVERS[name]()
    return _ast_return_keys(fn)


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
        actual = _resolve_return_keys(name, fn)
        if actual is None:
            # Non-vacuity (nexus-moht0): a documented claim this check
            # cannot verify is not a pass -- it needs a human-verified
            # entry in _RETURN_KEY_OVERRIDES or a derivation resolver in
            # _DERIVED_RETURN_KEY_RESOLVERS, same as the tools above.
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


def _checked_tool_count(tool_fns: list[tuple[str, str, str, object]]) -> int:
    """Count of tools whose description actually yielded a parsed
    return-key claim -- i.e. tools this check has some chance of verifying
    at all, mirroring ``_return_key_mismatches``'s own "did this tool
    participate" condition without duplicating its resolution/comparison
    logic. The applicability floor for
    ``test_documented_return_keys_match_actual_returns``: without it, a
    future description-formatting change that moves every tool off brace
    notation would make ``_parse_return_key_claims`` return ``[]``
    everywhere, and the mismatch check would pass having verified nothing.
    """
    return sum(
        1
        for _server, name, description, _fn in tool_fns
        if name not in _SCHEMA_DRIVEN_RETURN_TOOLS and _parse_return_key_claims(description)
    )


#: Floor for ``_checked_tool_count`` (nexus-dszac gap 2): the count of
#: tools whose description carried a parseable ``{key, ...}`` return-key
#: claim, measured against this tree. A drop below this floor means
#: description formatting drifted away from brace notation somewhere --
#: fails loud rather than silently checking fewer tools each time one
#: stops asserting a return shape. Adding a brace-documented tool only
#: ever raises the live count above this floor, never fails it; it is
#: SAFE to bump this constant upward when the live count grows, never
#: downward without also explaining why coverage genuinely shrank. Measured
#: 19 on this tree (nexus-dszac); set to that exact current count per the
#: bead's instruction, not a padded estimate.
_MIN_CHECKED_RETURN_KEY_TOOLS = 19


def test_documented_return_keys_match_actual_returns() -> None:
    tool_fns = _iter_tool_fns()
    checked = _checked_tool_count(tool_fns)
    assert checked >= _MIN_CHECKED_RETURN_KEY_TOOLS, (
        f"only {checked} tool(s) had a parseable return-key claim to "
        f"verify (floor {_MIN_CHECKED_RETURN_KEY_TOOLS}) -- a description-"
        f"format drift away from brace notation would otherwise silently "
        f"zero this check's real coverage while it keeps reporting green"
    )
    mismatches = _return_key_mismatches(tool_fns)
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


def test_nested_brace_claim_still_extracts_its_own_outer_keys() -> None:
    """The gap nexus-dszac's gap 3 names directly: the OLD flat-only regex
    (``\\{[^{}]*\\}``) could never match a claim with any nested brace at
    its own outer level at all -- ``{ok, evidence: list[{item_id, ...}]}``
    yielded NOTHING for `ok`/`evidence`, so a tool documenting exactly this
    shape could drop or rename `ok`/`evidence` in code with this check
    still reporting green throughout. Proven here by a planted MISMATCH:
    the tool's description claims `{ok, evidence: ...}` but the code never
    returns `evidence` at all -- this must fire now that the outer span is
    a real top-level match, not silently pass as it would have under the
    old regex (which would have found no outer claim to compare in the
    first place)."""

    def _planted_tool() -> dict:
        """Planted tool.

        Returns `{ok, evidence: list[{item_id, quote, role}]}`.
        """
        return {"ok": True}  # `evidence` claimed but never returned

    mismatches = _return_key_mismatches(
        [("nexus", "planted_tool", _planted_tool.__doc__, _planted_tool)]
    )
    assert len(mismatches) == 1
    server, name, documented, actual = mismatches[0]
    assert (server, name) == ("nexus", "planted_tool")
    # The outer claim resolved to its own leading keys, `ok` and
    # `evidence` -- the nested `item_id`/`quote`/`role` segment never
    # split into separate top-level keys of this claim.
    assert documented == ["evidence", "ok"]
    assert actual == ["ok"]


def test_planted_zero_parseable_claims_fails_the_applicability_floor() -> None:
    """Non-vacuity for the floor itself (nexus-dszac gap 2): a world where
    every tool description drifted off brace notation yields a checked
    count of zero, below the real floor -- the exact silent-coverage-loss
    regression the floor exists to catch, proven here without needing to
    fake the whole live tool census."""
    fake_tools: list[tuple[str, str, str, object]] = [
        ("nexus", "planted_tool_a", "No brace-shaped return claim in this one.", lambda: {"a": 1}),
        ("nexus", "planted_tool_b", "Also no braces here, just prose.", lambda: {"b": 2}),
    ]
    checked = _checked_tool_count(fake_tools)
    assert checked == 0
    assert checked < _MIN_CHECKED_RETURN_KEY_TOOLS


def test_search_empty_shape_matches_structured_render_keys() -> None:
    """Pin for ``search()``'s ``empty_shape`` fallback (nexus-dszac fix
    round 2 item 1): a SECOND hand-typed 6-key literal alongside
    ``_search_render``'s structured-branch literal, used only when
    ``_search_render(structured=True)`` returns something other than a
    dict. ``_derive_search_return_keys`` now asserts the two match
    internally on every call; this test exercises that assertion in
    isolation against the real live source, so a drift here fails on its
    own line rather than only inside the larger census test."""
    assert _derive_search_return_keys() is not None


def test_tuple_registry_override_matches_http_store_contract() -> None:
    """Pin for the ``tuple_registry`` override (nexus-dszac gap 1): the
    override table is hand-typed because the real shape crosses an HTTP
    call this check cannot chase, but ``HttpTupleStore.registry()``'s own
    docstring states the SAME brace-shaped contract -- parsed with the
    identical machinery this check uses on tool descriptions, so a human
    editing one without the other is caught here rather than trusted by
    comment alone."""
    core = _mcp_core_module()
    from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — deferred import, matches this module's lazy-import convention

    doc = HttpTupleStore.registry.__doc__ or ""
    claims = _parse_return_key_claims(doc)
    assert claims, (
        "HttpTupleStore.registry()'s docstring no longer states a "
        "brace-shaped return contract to pin the tuple_registry override "
        "against -- update the docstring or this pin"
    )
    documented = frozenset().union(*(keys for keys, _ in claims))
    assert documented == _RETURN_KEY_OVERRIDES["tuple_registry"]
    # Sanity: the tool this override actually serves still exists and
    # still claims return keys this pin is meaningful for.
    assert core.mcp._tool_manager._tools.get("tuple_registry") is not None


def test_show_override_matches_catalog_show_ast_derivation() -> None:
    """Full pin for the ``show`` override (nexus-dszac fix round 2 item 3):
    the collision test below only ever proved `kind`/`document_count`
    don't COLLIDE with `CatalogEntry.to_dict()`'s fields -- `links_from`/
    `links_to` were checked against nothing at all, narrower coverage than
    the comment above the override table used to claim.

    Both of ``catalog_show``'s own dict-construction idioms are
    AST-derivable one hop out even though neither is a literal ``return
    {...}``: ``d = {"kind": "owner", **owner}`` (a ``<var> = {...}``
    assignment -- ``_assigned_dict_literal_keys``) and
    ``d["document_count"] = ...`` / ``d["links_from"] = ...`` /
    ``d["links_to"] = ...`` (subscript assignment --
    ``_subscript_assigned_keys``). Their union is asserted to equal the
    override table's ``show`` entry EXACTLY, so a change to
    ``catalog_show`` that adds, drops, or renames any of these four keys
    is caught here rather than trusted by comment alone."""
    catalog_mod = importlib.import_module("nexus.mcp.catalog")
    tool = catalog_mod.mcp._tool_manager._tools.get("show")
    assert tool is not None

    literal_keys = _assigned_dict_literal_keys(tool.fn, "d")
    assert literal_keys is not None, (
        "catalog_show() no longer assigns `d = {...}` -- update this pin"
    )
    subscript_keys = _subscript_assigned_keys(tool.fn, "d")
    assert subscript_keys is not None, (
        "catalog_show() no longer assigns `d[...] = ...` -- update this pin"
    )
    assert literal_keys | subscript_keys == _RETURN_KEY_OVERRIDES["show"]


def test_show_override_owner_keys_never_collide_with_catalog_entry_to_dict() -> None:
    """Pin for the ``show`` override (nexus-dszac gap 1): the override is
    hand-typed because both of `catalog_show`'s branches build their dict
    via assignment + subscript rather than a literal `return {...}`, but
    `CatalogEntry.to_dict()` -- a real, in-repo dict literal one hop away
    -- gives a second source to pin against: the owner-branch keys
    (`kind`, `document_count`) must never appear in `to_dict()`'s own
    field set, or `{"kind": "owner", **owner}`'s merge / `d["document_
    count"] = ...`'s subscript would silently shadow a real metadata
    field instead of adding a distinct one."""
    from nexus.catalog.types import CatalogEntry  # noqa: PLC0415 — deferred import, matches this module's lazy-import convention

    to_dict_keys = _ast_return_keys(CatalogEntry.to_dict)
    assert to_dict_keys is not None, (
        "CatalogEntry.to_dict() no longer returns a resolvable dict "
        "literal -- this pin for the show override needs a new second "
        "source, or the override needs a note that none exists"
    )
    owner_branch_keys = {"kind", "document_count"}
    assert owner_branch_keys <= _RETURN_KEY_OVERRIDES["show"]
    assert not (owner_branch_keys & to_dict_keys), (
        f"owner-branch keys {owner_branch_keys} collide with a real "
        f"CatalogEntry.to_dict() field: {owner_branch_keys & to_dict_keys}"
    )


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


def test_nested_fn_return_keys_raises_on_ambiguous_duplicate_name() -> None:
    """Non-vacuity (nexus-dszac fix round 2 item 2): `_nested_fn_return_keys`
    previously picked whichever def `ast.walk` visited FIRST when more
    than one nested def shared a name -- a second, differently-scoped
    closure (an unrelated helper renamed into a collision, or a copy-
    pasted branch) would be silently substituted for the real one with no
    signal. Proven here by mutation: a planted outer function with TWO
    nested defs named `_inner` must raise, not silently resolve to the
    first one's keys."""

    def _outer_two_matches():
        def _inner():
            return {"a": 1}

        def _inner():  # noqa: F811 -- deliberate duplicate name; the ambiguity this test proves is caught
            return {"b": 2}

        return _inner()

    with pytest.raises(AssertionError, match="found 2"):
        _nested_fn_return_keys(_outer_two_matches, "_inner")


def test_nested_fn_return_keys_raises_on_zero_matches() -> None:
    """Companion to the duplicate-name case above: zero matches also fails
    loud (raises), rather than returning `None` and relying on the
    caller's separate "unresolvable" path to notice -- both ends of
    "exactly one" are enforced directly at the point of ambiguity."""

    def _outer_no_match():
        def _something_else():
            return {"a": 1}

        return _something_else()

    with pytest.raises(AssertionError, match="found 0"):
        _nested_fn_return_keys(_outer_no_match, "_inner")


def test_nested_fn_return_keys_resolves_the_sole_match() -> None:
    """Negative control: exactly one match still resolves normally (the
    common, non-ambiguous case is unaffected by the stricter check)."""

    def _outer_one_match():
        def _inner():
            return {"a": 1, "b": 2}

        return _inner()

    assert _nested_fn_return_keys(_outer_one_match, "_inner") == frozenset({"a", "b"})
