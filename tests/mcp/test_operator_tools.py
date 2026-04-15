# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-079 P3.2-P3.5 — operator_rank, _compare, _summarize, _generate.

Shares the fake-pool pattern from test_operator_extract.py. Each
operator has a fixed contract shape (per-operator pool handles the
schema); this test batch covers contract validation (happy path,
missing-key, wrong-type, schema-version guard) for all four.

operator_extract is tested separately in test_operator_extract.py.
"""
from __future__ import annotations

import pytest


def _install_fake_pool(monkeypatch, dispatch_result):
    """Install a fake operator pool that returns ``dispatch_result``
    regardless of input. Same pattern as test_operator_extract."""
    from nexus import mcp_infra

    class FakePool:
        def __init__(self):
            self.last_call = None

        async def dispatch_with_rotation(self, prompt, timeout=60.0, operator_role=None):
            self.last_call = {"prompt": prompt, "timeout": timeout}
            return dispatch_result

    fake = FakePool()

    def fake_get(operator_name=None, *, operator_role=None, json_schema=None):
        fake.last_get = {
            "operator_name": operator_name,
            "role": operator_role,
            "schema": json_schema,
        }
        return fake

    monkeypatch.setattr(mcp_infra, "get_operator_pool", fake_get)
    return fake


# ── operator_rank (P3.2) ──────────────────────────────────────────────────


def test_operator_rank_happy_path(monkeypatch) -> None:
    fake = _install_fake_pool(monkeypatch, {
        "ranked": [
            {"rank": 1, "score": 0.9, "input_index": 2, "justification": "most relevant"},
            {"rank": 2, "score": 0.7, "input_index": 0, "justification": "partial match"},
            {"rank": 3, "score": 0.3, "input_index": 1, "justification": "tangential"},
        ],
    })
    from nexus.mcp.core import operator_rank

    result = operator_rank(
        criterion="distributed consensus",
        inputs='["paxos", "raft", "zab"]',
    )
    assert "ranked" in result
    assert len(result["ranked"]) == 3
    assert result["ranked"][0]["rank"] == 1
    # Prompt composition: criterion + inputs reach the worker
    assert "distributed consensus" in fake.last_call["prompt"]
    assert "paxos" in fake.last_call["prompt"]
    # Correct pool selected
    assert fake.last_get["operator_name"] == "rank"


def test_operator_rank_raises_on_missing_key(monkeypatch) -> None:
    _install_fake_pool(monkeypatch, {"wrong_key": []})
    from nexus.mcp.core import operator_rank
    from nexus.plans.runner import PlanRunOperatorOutputError

    with pytest.raises(PlanRunOperatorOutputError, match="ranked"):
        operator_rank(criterion="x", inputs="[]")


def test_operator_rank_raises_on_non_list(monkeypatch) -> None:
    _install_fake_pool(monkeypatch, {"ranked": "not a list"})
    from nexus.mcp.core import operator_rank
    from nexus.plans.runner import PlanRunOperatorOutputError

    with pytest.raises(PlanRunOperatorOutputError, match="list"):
        operator_rank(criterion="x", inputs="[]")


def test_operator_rank_schema_version_guard(monkeypatch) -> None:
    _install_fake_pool(monkeypatch, {"ranked": []})
    from nexus.mcp.core import operator_rank
    from nexus.plans.runner import PlanRunOperatorSchemaVersionError

    with pytest.raises(PlanRunOperatorSchemaVersionError):
        operator_rank(criterion="x", inputs="[]", schema_version=2)


# ── operator_compare (P3.3) ───────────────────────────────────────────────


def test_operator_compare_happy_path(monkeypatch) -> None:
    fake = _install_fake_pool(monkeypatch, {
        "agreements": ["both discuss consensus"],
        "conflicts": ["paxos assumes stable leader; raft elects one"],
        "gaps": ["neither addresses Byzantine failures"],
    })
    from nexus.mcp.core import operator_compare

    result = operator_compare(
        inputs='["paxos paper summary", "raft paper summary"]',
        criterion="fault-tolerance assumptions",
    )
    assert result == {
        "agreements": ["both discuss consensus"],
        "conflicts": ["paxos assumes stable leader; raft elects one"],
        "gaps": ["neither addresses Byzantine failures"],
    }
    assert fake.last_get["operator_name"] == "compare"
    assert "fault-tolerance assumptions" in fake.last_call["prompt"]


def test_operator_compare_without_criterion(monkeypatch) -> None:
    """Criterion is optional; tool composes a generic comparison prompt."""
    fake = _install_fake_pool(monkeypatch, {
        "agreements": [], "conflicts": [], "gaps": [],
    })
    from nexus.mcp.core import operator_compare

    operator_compare(inputs='["a", "b"]')  # no criterion
    assert "Criterion:" not in fake.last_call["prompt"]


def test_operator_compare_raises_on_missing_any_key(monkeypatch) -> None:
    from nexus.mcp.core import operator_compare
    from nexus.plans.runner import PlanRunOperatorOutputError

    # Missing each key in turn
    for incomplete in (
        {"agreements": [], "conflicts": []},  # missing gaps
        {"agreements": [], "gaps": []},  # missing conflicts
        {"conflicts": [], "gaps": []},  # missing agreements
    ):
        _install_fake_pool(monkeypatch, incomplete)
        with pytest.raises(PlanRunOperatorOutputError):
            operator_compare(inputs='["x"]')


def test_operator_compare_raises_on_non_list_value(monkeypatch) -> None:
    _install_fake_pool(monkeypatch, {
        "agreements": "not a list", "conflicts": [], "gaps": [],
    })
    from nexus.mcp.core import operator_compare
    from nexus.plans.runner import PlanRunOperatorOutputError

    with pytest.raises(PlanRunOperatorOutputError, match="list"):
        operator_compare(inputs='["x"]')


# ── operator_summarize (P3.4) ─────────────────────────────────────────────


def test_operator_summarize_happy_path(monkeypatch) -> None:
    fake = _install_fake_pool(monkeypatch, {
        "text": "Consensus protocols ensure state-machine agreement.",
        "citations": [{"input_index": 0, "span": "Paxos section 2"}],
    })
    from nexus.mcp.core import operator_summarize

    result = operator_summarize(
        inputs='["paper summary"]',
        mode="evidence",
    )
    assert result["text"].startswith("Consensus")
    assert len(result["citations"]) == 1
    assert fake.last_get["operator_name"] == "summarize"
    assert "Mode: evidence" in fake.last_call["prompt"]


def test_operator_summarize_default_mode_is_short(monkeypatch) -> None:
    fake = _install_fake_pool(monkeypatch, {"text": "t", "citations": []})
    from nexus.mcp.core import operator_summarize

    operator_summarize(inputs="[]")
    assert "Mode: short" in fake.last_call["prompt"]


def test_operator_summarize_raises_on_missing_text(monkeypatch) -> None:
    _install_fake_pool(monkeypatch, {"citations": []})
    from nexus.mcp.core import operator_summarize
    from nexus.plans.runner import PlanRunOperatorOutputError

    with pytest.raises(PlanRunOperatorOutputError, match="text"):
        operator_summarize(inputs="[]")


def test_operator_summarize_raises_on_non_string_text(monkeypatch) -> None:
    _install_fake_pool(monkeypatch, {"text": 123, "citations": []})
    from nexus.mcp.core import operator_summarize
    from nexus.plans.runner import PlanRunOperatorOutputError

    with pytest.raises(PlanRunOperatorOutputError, match="non-string"):
        operator_summarize(inputs="[]")


def test_operator_summarize_accepts_missing_citations(monkeypatch) -> None:
    """Citations default to empty list when absent — soft requirement,
    the text is the primary contract."""
    _install_fake_pool(monkeypatch, {"text": "hi"})
    from nexus.mcp.core import operator_summarize

    result = operator_summarize(inputs="[]")
    assert result["text"] == "hi"
    assert result["citations"] == []


# ── operator_generate (P3.5) ──────────────────────────────────────────────


def test_operator_generate_happy_path(monkeypatch) -> None:
    fake = _install_fake_pool(monkeypatch, {
        "text": "The Delos paper describes a virtualized consensus layer...",
        "citations": [
            {"input_index": 0, "span": "Section 3.1"},
            {"input_index": 1, "span": "Figure 2"},
        ],
    })
    from nexus.mcp.core import operator_generate

    result = operator_generate(
        outline="How Delos virtualizes consensus",
        inputs='["delos paper", "related work"]',
    )
    assert "Delos" in result["text"]
    assert len(result["citations"]) == 2
    assert fake.last_get["operator_name"] == "generate"
    assert "How Delos virtualizes consensus" in fake.last_call["prompt"]


def test_operator_generate_with_citations_false_relaxes_requirement(
    monkeypatch,
) -> None:
    """The role prompt mentions citations as optional when with_citations=False.
    Verified by grepping the instantiated pool's role for the relaxation phrase."""
    fake = _install_fake_pool(monkeypatch, {"text": "x", "citations": []})
    from nexus.mcp.core import operator_generate

    operator_generate(outline="o", inputs="[]", with_citations=False)
    role = fake.last_get["role"] or ""
    assert "optional" in role.lower()


def test_operator_generate_raises_on_missing_text(monkeypatch) -> None:
    _install_fake_pool(monkeypatch, {"citations": []})
    from nexus.mcp.core import operator_generate
    from nexus.plans.runner import PlanRunOperatorOutputError

    with pytest.raises(PlanRunOperatorOutputError, match="text"):
        operator_generate(outline="o", inputs="[]")


def test_operator_generate_schema_version_guard(monkeypatch) -> None:
    from nexus.mcp.core import operator_generate
    from nexus.plans.runner import PlanRunOperatorSchemaVersionError

    _install_fake_pool(monkeypatch, {"text": "x", "citations": []})
    with pytest.raises(PlanRunOperatorSchemaVersionError):
        operator_generate(outline="o", inputs="[]", schema_version=99)


# ── Registration (all 4 must be in default-mode tool list) ────────────────


def test_all_four_operators_registered_in_default_mode() -> None:
    from nexus.mcp.core import mcp

    tools = set(mcp._tool_manager._tools.keys())
    for name in ("operator_rank", "operator_compare",
                 "operator_summarize", "operator_generate"):
        assert name in tools, f"{name} not registered in default mode"


def test_all_four_operators_excluded_in_worker_mode() -> None:
    """SC-12 / invariant I-2: pool-recursion guard covers all operators."""
    import os
    import subprocess
    import sys

    env = os.environ.copy()
    env["NEXUS_MCP_WORKER_MODE"] = "1"
    result = subprocess.run(
        [
            sys.executable, "-c",
            "from nexus.mcp.core import mcp; "
            "tools = set(mcp._tool_manager._tools.keys()); "
            "names = ['operator_rank','operator_compare',"
            "'operator_summarize','operator_generate']; "
            "print(all(n not in tools for n in names))",
        ],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "True" in result.stdout
