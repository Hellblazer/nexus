# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tool-tier hook registration on ``nx-mcp`` (RDR-215 Approach items 1 and 4).

Every conexus ``hooks.json`` entry other than ``SessionStart`` and
``phase_review_close_requires_gate`` becomes an ``mcp_tool`` call against a
``hook_<name>`` tool registered here. This module was the registration
*mechanism* only (bead nexus-q02nx.3); bead nexus-q02nx.4 lands the first
real port, ``hook_auto_approve`` (``nexus.hooks.auto_approve``), still
registered here with the ``hooks.json`` re-declaration deferred to beads
nexus-q02nx.21/.22 -- the tool answers live on ``nx-mcp`` today, but the
bash script it ports (``conexus/hooks/scripts/auto-approve-nx-mcp.sh``) is
still what ``hooks.json`` actually wires. Every port after this one plugs
in with one line: a new :class:`HookToolSpec` appended to :data:`HOOK_TOOLS`,
naming the ported module's ``run(payload) -> HookResult``
(``src/nexus/_hook_runtime/_io.py``).

**Never registers ``phase_review_close_requires_gate`` here.** That hook is
the routing framework's one ``fail_closed: true`` rule
(``conexus/hooks/scripts/routing/registry.yaml``; see ``_io.py``'s module
docstring). On this tier a crash reads as ``isError=False``/allow (below),
which is exactly the wrong answer for a hook whose contract is "a crash
still emits a deny envelope" -- it belongs on the command tier (``nx-hook``)
instead, where the process can still write that envelope before it exits.
:func:`register_hook_tools` refuses that name outright (see
``_NEVER_TOOL_TIER``) rather than relying on nobody ever adding it by habit
or via a future "walk every hook module" sweep.

**The tool boundary.** Each registered tool calls the module's ``run()``
through :func:`nexus._hook_runtime._io.never_fail` -- the shared swallow
primitive ``_io.py`` documents as the deliberate replacement for bash's missing
``set -e``. A crash renders as an empty ``TextContent`` with ``isError=False``,
the same thing a hook that silently declined to say anything produces; the
event proceeds exactly as it would past a bash script with no ``set -e``.
``never_fail`` also logs the swallowed exception via structlog, which lands
in ``nx-mcp``'s own configured log sink (``<config>/logs/mcp.log`` --
``main()`` already calls ``configure_logging("mcp")`` before any hook tool
is ever invoked, so there is no separate "logged to the hook log" step to
perform here the way a bash-launched Python hook script needs
``conexus/hooks/scripts/_hook_logging.py`` to bridge structlog away from
stdout before its first ``nexus.*`` import; that module lives under the
plugin directory, is not on ``nx-mcp``'s import path, and solves a problem
this tier does not have).

**Field names.** A hook module's payload fields are named the way the
contract map (T2 ``nexus_rdr/215-hook-contract-map``) records them, dotted
for a nested field (``tool_input.command``). A dot is not a legal Python
parameter name, so :func:`flatten_field_name` maps each dotted path to a
double-underscore-joined tool parameter name (``tool_input__command``) --
the identifier the tool's input schema actually exposes, and the key a
``hooks.json`` ``input`` map entry would carry on its LEFT side (the RIGHT
side keeps the dotted ``${tool_input.command}`` substitution: the two
namespaces are independent). :func:`nest_payload` is the exact inverse,
reassembling the flat tool arguments back into the nested dict shape
``run()`` reads on both tiers. Whether Claude Code's own ``${path}``
substitution reaches a *nested* field faithfully through that ``input`` map
was an open question when this module was written; bead nexus-q02nx.6 has
since measured it (2026-09-19, CLI 2.1.278, macOS and WSL2): substitution
delivers non-scalar payload fields AS STRUCTURES -- ``${tool_input}``
arrives as a dict, ``${background_tasks}`` as a list, and an absent key as
``''``, so absent stays distinguishable from an empty list. Record: T2
``nexus_rdr/215-phase1-measurements``. Nothing here depended on the answer
either way: the flatten/nest pair is exercised directly, independent of how
(or whether) a real ``hooks.json`` entry populates the flattened arguments.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import Field as _PydanticField

from nexus._hook_runtime._io import HookResult, never_fail
from nexus.hooks.auto_approve import run as _run_auto_approve

__all__ = [
    "HOOK_TOOLS",
    "HookToolSpec",
    "flatten_field_name",
    "nest_payload",
    "register_hook_tools",
]


@dataclass(frozen=True)
class HookToolSpec:
    """One row of the tool-tier registration table.

    ``name`` is the hook's short name; it is registered as ``hook_<name>``.
    ``run`` is the ported module's ``run(payload) -> HookResult`` entry
    point -- the same callable the command tier's ``nx-hook`` registers
    under its own verb table (RDR-215 Approach item 4: one implementation,
    two entries). ``fields`` names the payload fields this tool's input
    schema exposes, dotted for a nested field, in the shape the contract
    map's "stdin fields used" column already records per script. ``summary``
    is a short, human-readable one-liner folded into the tool's description
    (see :func:`_description`) -- MCP has no way to hide a tool from the
    model's tool list, so the description is part of the mitigation the RDR
    names, not incidental documentation.

    ``field_docs`` maps a dotted field name from ``fields`` to its own
    JSON-Schema ``description`` (see :func:`_make_tool_function`). Every
    parameter on a live-registered tool needs one --
    ``tests/test_mcp_tool_description_lint.py::
    test_every_parameter_has_a_schema_description`` walks the actual
    registry, not a fixture, so a field left undocumented here fails that
    lint the moment a spec with fields registers on the real server. A
    field named in ``fields`` but absent from ``field_docs`` still gets a
    generated fallback (see :func:`_field_description`), so this mapping is
    an override for a better description, never a requirement to populate
    every key.

    ``structured_fields`` names the fields that carry a NON-SCALAR payload
    value, and it is deliberately opt-in per field rather than a blanket
    widening (bead nexus-9ifls, critique round). Bead nexus-q02nx.6 measured
    that Claude Code's ``${path}`` substitution delivers ``${tool_input}`` as
    a ``dict`` and ``${background_tasks}`` as a ``list``, so those fields
    must accept a structure; but the shapes are a CLOSED, already-enumerated
    set (T2 ``nexus_rdr/215-hook-contract-map`` records them per script), so
    typing every field ``Any`` to accommodate two of them throws away real
    schema information on the rest. These tools are model-callable and the
    input schema is what the model sees: a field that is provably always a
    string should say so. Listed fields are typed ``Any``; every other field
    stays ``str | None``.
    """

    name: str
    run: Callable[[dict[str, Any] | None], HookResult]
    fields: tuple[str, ...] = ()
    field_docs: Mapping[str, str] = field(default_factory=dict)
    summary: str = ""
    structured_fields: frozenset[str] = frozenset()


# One entry per ported hook module (RDR-215 Approach item 4). The first real
# port is bead nexus-q02nx.4 (auto-approve-nx-mcp.sh -> hook_auto_approve).
# Adding a hook after that is one line: append a HookToolSpec here.
#
# `phase_review_close_requires_gate` NEVER belongs in this tuple -- see the
# module docstring and `_NEVER_TOOL_TIER` below, which refuses it even if a
# future edit adds it by habit or via an automated "every hook module"
# sweep. Do not build such a sweep without carrying that exclusion with it.
HOOK_TOOLS: tuple[HookToolSpec, ...] = (
    HookToolSpec(
        name="auto_approve",
        run=_run_auto_approve,
        fields=("tool_name", "hook_event_name"),
        field_docs={
            "tool_name": (
                "The full mcp__plugin_conexus_<server>__<tool> name Claude "
                "Code is about to invoke."
            ),
            "hook_event_name": (
                "Which event fired this hook: PreToolUse or "
                "PermissionRequest. Defaults to PermissionRequest when "
                "absent, matching the bash script's own default."
            ),
        },
        summary=(
            "auto-approves an explicit allowlist of conexus MCP tools "
            "(plus any hook_ tool) on PreToolUse and PermissionRequest"
        ),
    ),
)


#: Names that must never reach this tier. See the module docstring's
#: "Never registers phase_review_close_requires_gate here" paragraph.
_NEVER_TOOL_TIER = frozenset({"phase_review_close_requires_gate"})


def flatten_field_name(dotted: str) -> str:
    """A payload field path -> a valid tool-parameter / JSON-Schema name.

    ``"tool_input.command"`` -> ``"tool_input__command"``. Inverted by
    :func:`nest_payload`. Bijective for every field name in the RDR-215
    contract map, none of which contain a literal ``__``.
    """
    return dotted.replace(".", "__")


def nest_payload(flat: Mapping[str, Any]) -> dict[str, Any]:
    """Reassemble a tool's flat arguments into the nested payload ``run()`` reads.

    The inverse of :func:`flatten_field_name`: an argument named
    ``tool_input__command`` becomes ``{"tool_input": {"command": ...}}``,
    matching the shape the command tier's full stdin JSON payload has at
    that same path. A field whose value is ``None`` (the tool's own
    optional-parameter default -- nothing was substituted for it) is
    omitted entirely, so an unpopulated field reads exactly as an absent
    key, never an explicit null the bash layer's ``jq``/``python3 -c``
    reads would never have produced.
    """
    payload: dict[str, Any] = {}
    for flat_name, value in flat.items():
        if value is None:
            continue
        *parents, leaf = flat_name.split("__")
        node = payload
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return payload


def _description(spec: HookToolSpec) -> str:
    """A one-line description marking the tool as a hook entry, not a capability.

    MCP has no way to hide a tool from the model's tool list (RDR-215
    Technical Design); the ``hook_`` prefix, this sentence, and the
    auto-approve matcher are the stated mitigation.
    """
    body = spec.summary or f"the {spec.name} Claude Code hook"
    return (
        f"Hook entry point (RDR-215): {body}. Registered so a Claude Code "
        "hooks.json entry can call it as an mcp_tool; not a general-purpose "
        "capability, and not meant to be invoked directly."
    )


def _field_description(spec: HookToolSpec, payload_field: str) -> str:
    """The JSON-Schema ``description`` for one of *spec*'s payload fields.

    Prefers ``spec.field_docs[payload_field]``; falls back to a generic,
    still-non-empty description naming the field and the hook, so
    ``tests/test_mcp_tool_description_lint.py::
    test_every_parameter_has_a_schema_description`` (which walks the live
    registry, not a fixture) never finds a blank schema property just
    because a spec did not bother to document one.
    """
    override = spec.field_docs.get(payload_field)
    if override:
        return override
    return f"Hook payload field {payload_field!r} for the {spec.name} hook."


def _make_tool_function(spec: HookToolSpec) -> Callable[..., CallToolResult]:
    """Build the ``hook_<name>`` tool function FastMCP registers.

    The function itself is generic (``**kwargs``); what makes it look like a
    tool with *named* parameters to FastMCP's schema builder is the
    ``__signature__`` override below -- ``inspect.signature()`` honours an
    explicit override before falling back to introspecting ``__code__``, so
    the pydantic arg-model FastMCP builds from ``inspect.signature(fn)`` sees
    exactly the flattened field names in ``spec.fields``, each an optional
    string parameter carrying its own ``Field(description=...)`` (see
    :func:`_field_description`), while the function body still receives them
    as ordinary keyword arguments.
    """
    tool_name = f"hook_{spec.name}"

    def _tool(**kwargs: Any) -> CallToolResult:
        result = never_fail(lambda: spec.run(nest_payload(kwargs) or None), hook=tool_name)
        return CallToolResult(content=[TextContent(type="text", text=result.stdout or "")], isError=False)

    _tool.__name__ = tool_name
    _tool.__doc__ = _description(spec)
    params = [
        inspect.Parameter(
            flatten_field_name(payload_field),
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=None,
            # Per-field, not blanket (bead nexus-9ifls). A field the spec
            # marks structured is typed ``Any`` because bead nexus-q02nx.6
            # MEASURED that ``${path}`` substitution delivers non-scalars as
            # real structures -- ``${tool_input}`` a ``dict``,
            # ``${background_tasks}`` a ``list`` of ``dict``s carrying a mixed
            # shell/subagent population. Everything else keeps ``str | None``,
            # because the field shapes are a closed set the contract map
            # already enumerates and the model reads this schema.
            #
            # What a REJECTED argument does, measured rather than assumed
            # (2026-09-19, CLI 2.1.278): pydantic rejects it during FastMCP's
            # own argument binding, BEFORE ``_tool`` runs, so ``never_fail``
            # never sees it and this is NOT the ``isError=False`` path the
            # module docstring describes for a crash inside ``run()``. It
            # surfaces as a tool error. For a HOOK that still means the event
            # is not blocked: a hook tool that raised was logged
            # ``Hook PreToolUse:Bash (PreToolUse) error: ...`` and the Bash
            # command ran anyway. So a mistyped field fails OPEN at the event
            # level while being loud at the tool level -- which is why the
            # annotation has to be right rather than merely permissive.
            annotation=Annotated[
                Any if payload_field in spec.structured_fields else str | None,
                _PydanticField(description=_field_description(spec, payload_field)),
            ],
        )
        for payload_field in spec.fields
    ]
    _tool.__signature__ = inspect.Signature(params, return_annotation=CallToolResult)
    return _tool


def _register_one(mcp: FastMCP, spec: HookToolSpec) -> None:
    if spec.name in _NEVER_TOOL_TIER:
        raise ValueError(
            f"{spec.name!r} is the routing framework's one fail_closed hook "
            "(RDR-215 Approach item 1) and must never register as a "
            "tool-tier hook_<name> -- a tool-boundary crash reads as "
            "isError=False/allow here, which is exactly wrong for it. It "
            "takes the command tier (nx-hook) instead, where a crash can "
            "still emit a deny envelope before the process exits."
        )
    mcp.tool(
        name=f"hook_{spec.name}",
        title=f"Hook: {spec.name}",
        description=_description(spec),
        structured_output=False,
    )(_make_tool_function(spec))


def register_hook_tools(mcp: FastMCP, specs: Iterable[HookToolSpec] | None = None) -> None:
    """Register one ``hook_<name>`` tool per entry in *specs*.

    *specs* defaults to :data:`HOOK_TOOLS`, read from this module's current
    global -- not bound at this function's definition time -- so a caller
    (a test, or a future port) that mutates ``nexus.mcp.hooks.HOOK_TOOLS``
    before calling this with no explicit *specs* argument sees that
    mutation, exactly as ``nexus.mcp.core``'s own call site does.
    """
    for spec in (HOOK_TOOLS if specs is None else specs):
        _register_one(mcp, spec)
