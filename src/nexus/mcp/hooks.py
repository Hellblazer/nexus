# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tool-tier hook registration on ``nx-mcp`` (RDR-215 Approach items 1 and 4).

Every conexus ``hooks.json`` ``mcp_tool`` entry calls a ``hook_<name>`` tool
registered here. This module was the registration *mechanism* only (bead
nexus-q02nx.3); today it carries ``hook_auto_approve``
(``nexus.hooks.auto_approve``) and ``hook_subagent_start``
(``nexus.hooks.subagent_start``). Every port plugs in with one line: a new
:class:`HookToolSpec` appended to :data:`HOOK_TOOLS`, naming the ported
module's ``run(payload) -> HookResult``
(``src/nexus/_hook_runtime/_io.py``).

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
stdout before its first ``nexus.*`` import; that module lived under the
plugin directory, was never on ``nx-mcp``'s import path, and solved a
problem this tier does not have. It was deleted at nexus-z9cz2).

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
import threading as _threading
from dataclasses import dataclass, field
from typing import Annotated, Any

import structlog

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import Field as _PydanticField

from nexus._hook_runtime._io import HookResult, never_fail
from nexus.hooks.auto_approve import run as _run_auto_approve
from nexus.hooks.subagent_start import run as _run_subagent_start

__all__ = [
    "DECIDING_HOOKS",
    "HOOK_TOOLS",
    "HookToolSpec",
    "flatten_field_name",
    "nest_payload",
    "register_hook_tools",
]


#: Hooks whose whole job is to return a VERDICT -- a PreToolUse
#: ``permissionDecision``, a PermissionRequest ``behavior``, or a Stop /
#: SubagentStop ``decision``. They may register here (a verdict is useful
#: as data, and these tools are how the gate gets exercised in diagnosis),
#: but ``hooks.json`` must WIRE them on the command tier, because an
#: ``mcp_tool`` hook cannot decide anything.
#:
#: That is a property of the harness, not of this code. Claude Code's
#: hooks guide names four hook types that carry a decision -- ``prompt``,
#: ``agent``, ``command`` and ``http`` -- and ``mcp_tool`` is not one of
#: them; the only posture the guide states for it is "non-blocking error",
#: and its output is read as context. Measured 2026-09-20 on CLI 2.1.278
#: against the conexus 7.55.0 pin, which wired the deciding hooks as
#: ``mcp_tool``: a ``bd close`` naming a bead with NO review-completed
#: marker was allowed through to ``bd`` and closed it, while the same
#: payload handed straight to the hook's ``run()`` returned a correct,
#: fully-worded deny. The gate was inert for the life of that release and
#: nothing said so, because a hook that declines to speak and a hook whose
#: verdict is discarded look identical from outside. (That bd-close gate was
#: deleted at cleanup step A2, nexus-0r1uz; the hazard it measured stays.)
#:
#: ``tests/test_deciding_hooks_are_command_tier.py`` walks the real
#: ``hooks.json`` and fails if any name here is wired as an ``mcp_tool``,
#: and asserts each has a command-tier verb to be wired AS. A new
#: verdict-returning hook is added here in the same change that writes it.
DECIDING_HOOKS: frozenset[str] = frozenset(
    {
        "auto_approve",
    }
)

#: How long a hook tool's ``run()`` may take before the tool answers without
#: it (nexus-5dcky).
#:
#: THE BUG. On native Windows with no service endpoint — which is every
#: Windows box, since the PG bundle has no Windows target and `nx init`
#: refuses — the first storage-touching MCP tool call in a server process
#: never returned. Measured 2026-09-22 on qwentescence: `tuple_registry` and
#: `hook_stop_verification` both blocked past 300s, while `hook_auto_approve`
#: and `hook_stop_failure`, which touch no storage, returned in 0.0s (the
#: two stop hooks were deleted at cleanup step A3, nexus-0r1uz). The
#: blocked thread sat in `T2Database.__init__` importing numpy's C extension;
#: the same import outside that process takes 0.08s, including from a worker
#: thread under an asyncio loop, and the same tool on Linux returns its
#: endpoint error in 0.5s. `hooks.json` wired `hook_stop_verification` on
#: Stop, so `claude -p` answered and then sat there — the reported symptom.
#:
#: WHY A BOUND RATHER THAN A CURE FOR THAT IMPORT. The import pathology is
#: real and still unexplained, and it is not the only way a hook can block.
#: A hook is ADVISORY: it warns, and the harness that called it already
#: carries its own `timeout` in `hooks.json`. A hook still running past that
#: budget cannot affect anything — the harness has stopped waiting — so the
#: only thing it can still do is hold a tool call open. Answering without it
#: is strictly better than holding the session, whatever the cause.
#:
#: CHOOSING A VALUE — derived, not invented. A wired hook's bound is the
#: `timeout` its own `hooks.json` entry declares, MINUS a second, so the tool
#: answers just before the harness stops listening rather than just after.
#:
#: The margin is not tidiness. `nexus-dgvsz` measured what a late answer
#: costs on this transport: the client abandons the request id at its own
#: timeout, the server answers afterwards, and the late reply arrives as an
#: unknown message id and tears the stdio connection down. A bound equal to
#: the budget is a coin flip on exactly that, so it has to land inside it.
#:
#: `tests/hooks/test_hook_tool_timeout.py` reads `hooks.json` and fails if
#: any spec's bound reaches what its own events allow, so this stays derived
#: rather than merely having been derived once.
#:
#: The DEFAULT applies to a spec `hooks.json` does not wire as an
#: `mcp_tool` — the `DECIDING_HOOKS` above take the command tier, where the
#: harness kills the process outright and nothing here is reachable. It
#: exists so an unwired spec still has a bound, not because 30s means
#: anything in particular.
#:
#: WHAT A TIMEOUT LEAVES BEHIND. Python cannot kill a thread, so the blocked
#: `run()` keeps running, and on Windows it stays blocked for the life of the
#: process. That is a leaked worker thread per timed-out call, which is the
#: price of not hanging the session, and it is bounded by how many times a
#: hook fires. It is stated here rather than discovered later.
#:
#: One sharper edge of that, found in review: CPython holds a per-module
#: import lock, so a worker abandoned MID-IMPORT keeps that module
#: unimportable for the life of the process, and a later import of it hangs
#: outside this bound's reach. The trade is still the right one — a call
#: that returns beats a call that never does — but it is a trade, not a
#: clean win. Recorded with its evidence in `nexus-fd3zf`.
DEFAULT_HOOK_TOOL_TIMEOUT_S: float = 30.0

_log = structlog.get_logger(__name__)


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

    ``timeout_s`` bounds how long ``run`` may take before the tool answers
    without it; see :data:`DEFAULT_HOOK_TOOL_TIMEOUT_S` for why a bound
    exists at all and how to choose one.
    """

    name: str
    run: Callable[[dict[str, Any] | None], HookResult]
    fields: tuple[str, ...] = ()
    field_docs: Mapping[str, str] = field(default_factory=dict)
    summary: str = ""
    structured_fields: frozenset[str] = frozenset()
    timeout_s: float = DEFAULT_HOOK_TOOL_TIMEOUT_S


# One entry per ported hook module (RDR-215 Approach item 4). The first real
# port is bead nexus-q02nx.4 (auto-approve-nx-mcp.sh -> hook_auto_approve).
# Adding a hook after that is one line: append a HookToolSpec here.
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
    HookToolSpec(
        name="subagent_start",
        # SubagentStart wires this at 10s in hooks.json; the bound sits a
        # second under that — see DEFAULT_HOOK_TOOL_TIMEOUT_S for why the
        # margin exists (nexus-dgvsz: a late answer tears down the transport).
        timeout_s=9.0,
        run=_run_subagent_start,
        fields=('session_id', 'agent_id', 'agent_type', 'task'),
        field_docs={
            "session_id": (
                "Forced onto every subprocess this hook spawns. It runs detached, so without it the T2 scan and scratch read resolve a sibling session's machine-wide pointer."
            ),
            "agent_id": "The framework-assigned id for the starting subagent.",
            "agent_type": (
                "Routes which context sections are assembled. Matched against the locked classification regexes, carried verbatim from the script — a dropped alternative silently removes guidance from a real dispatch."
            ),
            "task": (
                "The dispatch's task text. Pattern-matched to decide whether catalog, phase-gate, operator or code-navigation context is worth its bytes."
            ),
        },
        summary=(
            "assembles the context a starting subagent needs — linked RDRs, T2 memory, T1 scratch and the tool guidance its agent type calls for — under a measured byte budget"
        ),
    ),
)


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


def _run_bounded(
    spec: HookToolSpec, payload: dict[str, Any] | None, tool_name: str
) -> HookResult:
    """``spec.run(payload)``, or a silent result once ``spec.timeout_s`` passes.

    See :data:`DEFAULT_HOOK_TOOL_TIMEOUT_S` for the measurement this exists
    for and for what the abandoned thread costs.

    A DAEMON THREAD, not a ThreadPoolExecutor, and the difference is the
    whole fix rather than a style choice. ``concurrent.futures.thread``
    registers a process-wide ``atexit`` handler that ``join()``s every live
    pool thread unconditionally; ``shutdown(wait=False)`` does not exempt it.
    Measured on this project's own interpreter: a pool whose worker is
    abandoned keeps the PROCESS from exiting (``timeout 8`` -> rc 124) after
    ``main()`` returned in a second, while the same shape on a daemon thread
    exits in 0.26s. nx-mcp relies on a clean exit at stdin EOF to run its T1
    shutdown, so a pool here would have traded a hang at the Stop hook for a
    hang at session end — the orphaned-process symptom this bead opened with,
    moved to a later moment. Found in review of the first cut.

    The timed-out shape is ``HookResult(crashed=True)`` — the SAME shape
    :func:`never_fail` produces for a hook that raised. That is deliberate:
    both mean "this hook said nothing", the harness already treats that as
    proceed, and inventing a third shape would make the tool boundary carry
    a distinction no caller acts on. The log event differs, which is where
    the distinction belongs.
    """
    box: dict[str, Any] = {}

    def _call() -> None:
        try:
            box["result"] = spec.run(payload)
        except BaseException as exc:  # noqa: BLE001 — re-raised in the caller; see below
            box["error"] = exc

    worker = _threading.Thread(
        target=_call, name=f"{tool_name}-bounded", daemon=True
    )
    worker.start()
    worker.join(spec.timeout_s)

    if worker.is_alive():
        _log.warning(
            "hook_tool_timed_out",
            hook=tool_name,
            timeout_s=spec.timeout_s,
            msg=(
                "the hook exceeded its bound and the tool answered without "
                "it; the worker thread is abandoned and may still be running"
            ),
        )
        return HookResult(crashed=True)

    # Re-raised HERE, in the calling thread, so never_fail sees it exactly as
    # it would have without the bound. That is what keeps its documented
    # passthrough intact: KeyboardInterrupt, SystemExit and CancelledError
    # (including wrapped in a BaseExceptionGroup) must reach the caller
    # rather than being turned into a silent result, and an exception left
    # sitting in a worker thread would reach nobody.
    if "error" in box:
        raise box["error"]
    return box["result"]


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
        result = never_fail(
            lambda: _run_bounded(spec, nest_payload(kwargs) or None, tool_name),
            hook=tool_name,
        )
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
