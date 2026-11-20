# SPDX-License-Identifier: AGPL-3.0-or-later
"""The inline planner's tool reference names each operator's output fields, and the planner writes
``$stepN.<field>`` from them. A documented field the operator's reply does not carry makes every such
reference a PlanRunStepRefError (RDR-255 finding 23: ``generate`` was documented as ``{text}`` while it
returns ``{output}``).

Every operator entry under the reference's ``=== Operators`` section is read from the reference itself, so an
entry added later is checked too, and each must have a ``build_<name>_request`` builder. For each, the
documented top-level keys must include every key the reply schema requires, and every documented key must be
one the reply can carry: a schema property, or a key the host adds when it binds a witness (check, verify).
"""
from __future__ import annotations

import inspect
import re

import pytest

from nexus.mcp import operator_requests as ops
from nexus.mcp.core import _PLANNER_TOOL_REFERENCE
from nexus.plans import runner


def _section(title: str) -> str:
    m = re.search(rf"^=== {re.escape(title)} ===$(.*?)(?=^=== )", _PLANNER_TOOL_REFERENCE, flags=re.M | re.S)
    assert m is not None, f"no '=== {title} ===' section in _PLANNER_TOOL_REFERENCE"
    return m.group(1)


_OPERATOR_SECTION = _section("Operators (LLM-backed)")
_OPERATOR_NAMES = sorted(set(re.findall(r"^  (\w+)\(", _OPERATOR_SECTION, flags=re.M)))


def _entry(name: str) -> str:
    m = re.search(rf"^  {name}\(.*?(?=^  \w+\(|\Z)", _OPERATOR_SECTION, flags=re.M | re.S)
    assert m is not None, f"no entry for {name}"
    return m.group(0)


def _documented_keys(name: str) -> set[str]:
    """The top-level keys of the ``Output: {...}`` object in *name*'s entry, across line breaks."""
    entry = _entry(name)
    start = entry.find("Output: {")
    assert start >= 0, f"{name}: no 'Output: {{' line"
    i, depth, body = start + len("Output: "), 0, ""
    for ch in entry[i:]:
        depth += ch in "{["
        depth -= ch in "}]"
        body += ch
        if depth == 0:
            break
    assert depth == 0, f"{name}: Output object never closes"
    top, depth, part = [], 0, ""
    for ch in body[1:-1]:
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        if ch == "," and depth == 0:
            top.append(part)
            part = ""
        else:
            part += ch
    top.append(part)
    return {m.group(1) for p in top if (m := re.match(r"\s*(\w+)", p))}


def _reply_schema(name: str) -> dict:
    builder = getattr(ops, f"build_{name}_request", None)
    assert builder is not None, f"the reference documents operator {name!r} but operator_requests has no builder"
    params = [p for p in inspect.signature(builder).parameters.values() if p.default is inspect.Parameter.empty]
    _, schema = builder(*["[]" for _ in params])
    return schema


def _host_bound_keys(name: str) -> set[str]:
    """Keys the runner adds when it binds a witness reply (empty for operators it does not bind)."""
    if name not in ("check", "verify"):
        return set()
    return set(runner._zero_evidence_witness(f"operator_{name}", "x"))


def test_the_reference_documents_operators() -> None:
    assert len(_OPERATOR_NAMES) >= 7, _OPERATOR_NAMES


@pytest.mark.parametrize("name", _OPERATOR_NAMES)
def test_each_documented_output_matches_the_reply_the_operator_returns(name: str) -> None:
    schema = _reply_schema(name)
    documented = _documented_keys(name)
    required = set(schema["required"])
    carried = set(schema.get("properties", {})) | _host_bound_keys(name)
    assert required <= documented, f"{name}: reference documents {sorted(documented)}, the reply requires {sorted(required)}"
    assert documented <= carried, f"{name}: reference documents {sorted(documented - carried)}, which the reply never carries"
