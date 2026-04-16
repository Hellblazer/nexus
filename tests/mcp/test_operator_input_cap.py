# SPDX-License-Identifier: AGPL-3.0-or-later
"""Post-review guard: operator ``inputs`` cap (I-4 review finding).

When a plan step passes a wide ``$stepN.tumblers`` list into an
operator, the list used to flow straight into a Haiku prompt where
the inputs dominated the context and the model had no budget for
reasoning. The cap at
:data:`nexus.mcp.core._OPERATOR_MAX_INPUTS` rejects oversized
payloads with a named error before a subprocess spawn.
"""
from __future__ import annotations

import json

import pytest


def test_parse_inputs_json_rejects_oversized_list() -> None:
    """A Python list longer than the cap is rejected at the parse
    boundary — callers (runner dispatch via step-ref resolution) get
    a clear error instead of shipping the payload into the model."""
    from nexus.mcp.core import _OPERATOR_MAX_INPUTS, _parse_inputs_json
    from nexus.plans.runner import PlanRunOperatorOutputError

    oversize = ["tumbler"] * (_OPERATOR_MAX_INPUTS + 1)
    with pytest.raises(PlanRunOperatorOutputError) as exc:
        _parse_inputs_json("extract", oversize)
    assert str(_OPERATOR_MAX_INPUTS) in str(exc.value)
    assert "rank" in str(exc.value).lower()  # hint names a winnowing step


def test_parse_inputs_json_rejects_oversized_json_string() -> None:
    """The cap applies equally to JSON-string inputs (direct MCP
    callers), not just list inputs (runner step-refs)."""
    from nexus.mcp.core import _OPERATOR_MAX_INPUTS, _parse_inputs_json
    from nexus.plans.runner import PlanRunOperatorOutputError

    payload = json.dumps(["x"] * (_OPERATOR_MAX_INPUTS + 5))
    with pytest.raises(PlanRunOperatorOutputError):
        _parse_inputs_json("rank", payload)


def test_parse_inputs_json_accepts_list_at_cap() -> None:
    """The boundary (exactly cap items) is inclusive — no off-by-one."""
    from nexus.mcp.core import _OPERATOR_MAX_INPUTS, _parse_inputs_json

    exactly = ["x"] * _OPERATOR_MAX_INPUTS
    result = _parse_inputs_json("summarize", exactly)
    assert result == exactly
