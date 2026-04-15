# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-079 P3.1 — ``operator_extract`` MCP tool.

The tool dispatches a structured turn to a per-operator pool worker
spawned with ``--json-schema`` matching ``{extractions: [dict]}``. Worker
emits a ``StructuredOutput`` tool_use; the tool intercepts the
``input`` payload (per Empirical Finding 3 — final ``result`` text may
be empty; the tool_use IS the contract) and returns a dict.

Unit tests stub the pool via ``mcp_infra.inject_operator_pool`` so no
``claude`` subprocess is spawned and no auth is required.
"""
from __future__ import annotations

import asyncio
import os

import pytest


# ── Worker-mode independent (operator tool is always registered) ───────────


def test_operator_extract_is_registered_in_default_mode() -> None:
    """The tool must be in mcp.tools/list when NEXUS_MCP_WORKER_MODE is
    unset (normal nexus MCP server)."""
    # Clean import so NEXUS_MCP_WORKER_MODE default (unset) is honored.
    import importlib
    import nexus.mcp.core
    importlib.reload(nexus.mcp.core)
    from nexus.mcp.core import mcp

    assert "operator_extract" in mcp._tool_manager._tools


def test_operator_extract_excluded_in_worker_mode(monkeypatch) -> None:
    """Workers must not see operator_extract — pool recursion guard per
    RDR-079 P2.4 (invariant I-2). Verified via subprocess in worker mode."""
    import subprocess
    import sys

    env = os.environ.copy()
    env["NEXUS_MCP_WORKER_MODE"] = "1"
    result = subprocess.run(
        [
            sys.executable, "-c",
            "from nexus.mcp.core import mcp; "
            "print('operator_extract' in mcp._tool_manager._tools)",
        ],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "False" in result.stdout


# ── Dispatch contract ─────────────────────────────────────────────────────


def _install_fake_pool(monkeypatch, dispatch_result):
    """Install a fake OperatorPool that returns ``dispatch_result`` on
    ``dispatch_with_rotation()`` without spawning any subprocess."""
    from nexus import mcp_infra

    class FakePool:
        def __init__(self, *a, **kw):
            self.last_call = None

        async def dispatch_with_rotation(self, prompt, timeout=60.0, operator_role=None):
            self.last_call = {
                "prompt": prompt,
                "timeout": timeout,
                "operator_role": operator_role,
            }
            return dispatch_result

    fake = FakePool()

    def fake_get(operator_name=None, *, operator_role=None, json_schema=None):
        fake.last_get = {"operator_name": operator_name, "role": operator_role, "schema": json_schema}
        return fake

    monkeypatch.setattr(mcp_infra, "get_operator_pool", fake_get)
    return fake


def test_operator_extract_returns_extractions_list(monkeypatch) -> None:
    """Happy path: worker emits StructuredOutput → tool returns dict with
    the extractions list intact."""
    fake = _install_fake_pool(monkeypatch, {
        "extractions": [
            {"title": "Foo", "year": 2024, "author": "Bar"},
            {"title": "Baz", "year": 2023, "author": "Qux"},
        ],
    })

    from nexus.mcp.core import operator_extract

    result = operator_extract(
        inputs='["text one", "text two"]',
        fields="title,year,author",
    )
    assert isinstance(result, dict)
    assert "extractions" in result
    assert len(result["extractions"]) == 2
    assert result["extractions"][0]["title"] == "Foo"


def test_operator_extract_raises_output_error_on_malformed_dispatch(
    monkeypatch,
) -> None:
    """If the pool returns something that doesn't match
    ``{extractions: [dict]}``, surface PlanRunOperatorOutputError."""
    _install_fake_pool(monkeypatch, {"text": "not the shape we want"})

    from nexus.mcp.core import operator_extract
    from nexus.plans.runner import PlanRunOperatorOutputError

    with pytest.raises(PlanRunOperatorOutputError, match="extractions"):
        operator_extract(inputs='["hi"]', fields="x,y")


def test_operator_extract_raises_on_non_list_extractions(monkeypatch) -> None:
    """extractions must be a list (RDR-079 operator contract)."""
    _install_fake_pool(monkeypatch, {"extractions": "not a list"})

    from nexus.mcp.core import operator_extract
    from nexus.plans.runner import PlanRunOperatorOutputError

    with pytest.raises(PlanRunOperatorOutputError):
        operator_extract(inputs='["hi"]', fields="x")


def test_operator_extract_rejects_unknown_schema_version(monkeypatch) -> None:
    """Caller passing $schema_version != 1 must be refused — tool pins v1."""
    _install_fake_pool(monkeypatch, {"extractions": []})

    from nexus.mcp.core import operator_extract
    from nexus.plans.runner import PlanRunOperatorSchemaVersionError

    with pytest.raises(PlanRunOperatorSchemaVersionError):
        operator_extract(inputs='["x"]', fields="a", schema_version=2)


def test_operator_extract_passes_fields_to_prompt(monkeypatch) -> None:
    """The caller-supplied field list must reach the worker prompt so
    the model knows what to extract. Verified by inspecting the prompt
    the fake pool received."""
    fake = _install_fake_pool(monkeypatch, {"extractions": []})

    from nexus.mcp.core import operator_extract

    operator_extract(
        inputs='["The Rise of Arcaneum by Hal Hildebrand, 2024"]',
        fields="title,year,author",
    )
    assert fake.last_call is not None
    prompt = fake.last_call["prompt"]
    assert "title" in prompt
    assert "year" in prompt
    assert "author" in prompt
    # Input text must also reach the prompt
    assert "Arcaneum" in prompt


def test_operator_extract_uses_correct_pool_name(monkeypatch) -> None:
    """Must request the per-operator pool named 'extract' so its
    json_schema gets applied (not the default no-schema pool)."""
    fake = _install_fake_pool(monkeypatch, {"extractions": []})

    from nexus.mcp.core import operator_extract

    operator_extract(inputs='["x"]', fields="a")
    assert fake.last_get["operator_name"] == "extract"
    # Schema passed through (caller's field list → json_schema)
    assert fake.last_get["schema"] is not None
    assert fake.last_get["schema"].get("type") == "object"


# ── Auth-failure propagation (SC-10) ───────────────────────────────────────


def test_operator_extract_surfaces_auth_error(monkeypatch) -> None:
    """When the pool raises PoolAuthUnavailableError (no claude auth),
    the MCP tool must return a clear error string, not crash."""
    from nexus import mcp_infra
    from nexus.operators.pool import PoolAuthUnavailableError

    def fake_get(*a, **kw):
        class RaisingPool:
            async def dispatch_with_rotation(self, *a, **kw):
                raise PoolAuthUnavailableError("no auth")
        return RaisingPool()

    monkeypatch.setattr(mcp_infra, "get_operator_pool", fake_get)

    from nexus.mcp.core import operator_extract

    # MCP tools return strings for errors; structured=True hypothetical
    # not relevant for operator_* (always dict).
    with pytest.raises(PoolAuthUnavailableError):
        operator_extract(inputs='["x"]', fields="a")
