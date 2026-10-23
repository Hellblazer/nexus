# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live check of the ``anthropic`` backend against the real Messages API
(nexus-2k66t.3 P2). The unit tests replay streams built from the documented
grammar; this is the test that says the grammar, the forced-tool table, the
alias map and the price table match the live service.

Gate: ``NX_LIVE_LLM=1``. Unset, the module is skipped (it spends money). Set
without a key, it FAILS: a gate that skip-passes on the missing dependency it
exists to exercise proves nothing. The key is read from ``NX_LIVE_LLM_KEY`` then
``ANTHROPIC_API_KEY`` at import time (the unit suite's autouse fixture scrubs the
latter from the environment before a test body runs).

Run: ``NX_LIVE_LLM=1 NX_LIVE_LLM_KEY=sk-ant-... uv run pytest -m integration tests/integration/test_llm_live.py``
Each call is a few dozen tokens; three aliases cost well under a cent.
"""
from __future__ import annotations

import asyncio
import os

import pytest
import structlog

from nexus.operators.dispatch import DispatchUsage, model_family_matches
from nexus.operators.llm_anthropic import AnthropicBackend
from nexus.operators.llm_backend import DispatchRequest
from nexus.operators.model_tiers import _API_MODEL_IDS

pytestmark = [pytest.mark.integration, pytest.mark.lived_in]

_LIVE = os.environ.get("NX_LIVE_LLM") == "1"
_KEY = (os.environ.get("NX_LIVE_LLM_KEY") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()

if not _LIVE:
    pytest.skip("NX_LIVE_LLM=1 is not set: the live LLM check spends money", allow_module_level=True)

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "count": {"type": "integer"}},
    "required": ["answer", "count"],
}
PROMPT = "Call the tool with answer set to the word 'ok' and count set to 3."


def _require_key() -> str:
    assert _KEY, "NX_LIVE_LLM=1 needs an API key: set NX_LIVE_LLM_KEY (or ANTHROPIC_API_KEY); a live gate does not skip on a missing key"
    return _KEY


@pytest.mark.parametrize("alias", sorted(_API_MODEL_IDS))
def test_live_dispatch_per_alias(alias: str, monkeypatch) -> None:
    monkeypatch.setenv("NX_LLM_MAX_TOKENS", "512")
    backend = AnthropicBackend(api_key=_require_key())
    sink: list[DispatchUsage] = []
    req = DispatchRequest(prompt=PROMPT, json_schema=SCHEMA, model=alias, timeout=90.0, usage_sink=sink, operator="live_check")
    with structlog.testing.capture_logs() as logs:
        result = asyncio.run(backend.run(req))
    assert isinstance(result.structured["answer"], str) and isinstance(result.structured["count"], int)
    (usage,) = sink
    assert model_family_matches(alias, usage.model), (alias, usage.model)
    assert usage.model == _API_MODEL_IDS[alias], f"alias {alias} reported {usage.model}, the map says {_API_MODEL_IDS[alias]}"
    assert usage.cost_usd is not None and usage.cost_usd > 0
    assert usage.input_tokens and usage.output_tokens
    # The forced-tool table is right: no model needed the runtime fallback.
    assert not any(e["event"] == "llm_forced_tool_choice_rejected" for e in logs), logs


def test_live_text_mode_and_unknown_key_failure(monkeypatch) -> None:
    monkeypatch.setenv("NX_LLM_MAX_TOKENS", "64")
    ok = AnthropicBackend(api_key=_require_key())
    result = asyncio.run(ok.run(DispatchRequest(prompt="Say hello in one word.", json_schema=None, model="haiku", timeout=60.0)))
    assert result.text and result.usage.cost_usd > 0
    from nexus.operators.llm_types import OperatorAPIError

    bad = AnthropicBackend(api_key="sk-ant-api03-not-a-real-key-0000000000000000")
    with pytest.raises(OperatorAPIError) as ei:
        asyncio.run(bad.run(DispatchRequest(prompt="x", json_schema=None, model="haiku", timeout=30.0)))
    assert ei.value.status == 401 and "sk-ant-api03-not-a-real" not in str(ei.value)
