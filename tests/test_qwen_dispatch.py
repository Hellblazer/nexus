# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nexus.operators.qwen_dispatch``.

httpx is mocked via the standard ``respx``-style approach; we patch
``httpx.AsyncClient.post`` to return fake ``httpx.Response`` objects.
This keeps the test independent of any real backend at qwentescence
and runs offline.

Coverage:
  * Happy path — schema-conforming JSON → parsed dict
  * Markdown fence-wrapping → stripped + parsed
  * Parse-failure retry → second attempt succeeds
  * Parse-failure exhausts attempts → QwenOperatorOutputError
  * Non-2xx HTTP response → QwenOperatorError
  * Timeout → QwenOperatorTimeoutError
  * Backend URL / model resolution: explicit > env > config > default
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from nexus.operators.qwen_dispatch import (
    QwenOperatorError,
    QwenOperatorOutputError,
    QwenOperatorTimeoutError,
    _build_system_prompt,
    _estimate_sonnet_cost_usd,
    _resolve_backend_url,
    _resolve_model,
    _SONNET_INPUT_USD_PER_MTOK,
    _SONNET_OUTPUT_USD_PER_MTOK,
    _strip_code_fences,
    qwen_dispatch,
)


# ── Helpers ───────────────────────────────────────────────────────────────


def _resp(status: int, body: str | dict) -> httpx.Response:
    """Build an httpx.Response carrying *body* as either text or JSON dict."""
    if isinstance(body, dict):
        return httpx.Response(status, json=body)
    return httpx.Response(status, text=body)


def _chat_completion(content: str) -> dict:
    """OpenAI-compat chat-completions response shape with one choice."""
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }


_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


# ── Env isolation ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Strip env vars that would otherwise pollute resolution and
    semaphore size; also point QWEN_CONFIG_DIR at a fresh tmpdir so
    the resolver does NOT fall through to the operator's real
    ``~/.qwen-coprocessor-stack/config.json`` and pick up qwentescence
    coordinates that would mask test assertions on the hardcoded
    default."""
    for var in ("QWEN_BACKEND_URL", "QWEN_MODEL", "NEXUS_QWEN_CONCURRENCY"):
        monkeypatch.delenv(var, raising=False)
    # Empty tmpdir → no config.json → resolver falls through to default.
    monkeypatch.setenv("QWEN_CONFIG_DIR", str(tmp_path))


# ── Pure-function helpers ─────────────────────────────────────────────────


class TestStripCodeFences:
    """Mirror of qwen-coprocessor-stack/tests/server.test.ts coverage."""

    def test_strips_json_fenced_block(self) -> None:
        assert _strip_code_fences('```json\n{"a":1}\n```') == '{"a":1}'

    def test_strips_plain_fenced_block(self) -> None:
        assert _strip_code_fences('```\n{"a":1}\n```') == '{"a":1}'

    def test_tolerates_outer_whitespace(self) -> None:
        assert _strip_code_fences('  ```json\n{"a":1}\n```  ') == '{"a":1}'

    def test_does_not_strip_when_no_fences(self) -> None:
        assert _strip_code_fences('{"a":1}') == '{"a":1}'

    def test_does_not_strip_mid_prose_fences(self) -> None:
        s = 'here:\n```json\n{"a":1}\n```\nnice'
        assert _strip_code_fences(s) == s


class TestSystemPromptShape:
    def test_includes_schema_json(self) -> None:
        sp = _build_system_prompt(_SCHEMA)
        assert '"answer"' in sp
        assert '"required"' in sp

    def test_includes_no_fences_directive(self) -> None:
        sp = _build_system_prompt(_SCHEMA)
        # Positive framing is the load-bearing part.
        assert "must START with `{` or `[`" in sp


# ── Resolution chain (env > config > default) ─────────────────────────────


class TestBackendUrlResolution:
    def test_explicit_arg_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QWEN_BACKEND_URL", "http://from-env:1234/v1")
        assert (
            _resolve_backend_url("http://explicit:9999/v1")
            == "http://explicit:9999/v1"
        )

    def test_env_wins_over_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QWEN_BACKEND_URL", "http://from-env:1234/v1/")
        # Trailing slash is stripped.
        assert _resolve_backend_url(None) == "http://from-env:1234/v1"

    def test_falls_through_to_default(self) -> None:
        assert _resolve_backend_url(None) == "http://localhost:8080/v1"


class TestModelResolution:
    def test_explicit_arg_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QWEN_MODEL", "from-env")
        assert _resolve_model("explicit") == "explicit"

    def test_env_wins_over_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QWEN_MODEL", "qwen3.6-test")
        assert _resolve_model(None) == "qwen3.6-test"

    def test_falls_through_to_default(self) -> None:
        assert _resolve_model(None) == "qwen3.6-35b-a3b"


# ── Happy path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_returns_parsed_json_on_clean_response() -> None:
    fake_post = AsyncMock(
        return_value=_resp(200, _chat_completion('{"answer":"hello"}'))
    )
    with patch("httpx.AsyncClient.post", fake_post):
        result = await qwen_dispatch(
            "What is hello?",
            _SCHEMA,
            backend_url="http://test:1234/v1",
            model="qwen-test",
        )
    assert result == {"answer": "hello"}
    fake_post.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_strips_markdown_fences_before_parse() -> None:
    fake_post = AsyncMock(
        return_value=_resp(
            200,
            _chat_completion('```json\n{"answer":"fenced"}\n```'),
        )
    )
    with patch("httpx.AsyncClient.post", fake_post):
        result = await qwen_dispatch(
            "fenced", _SCHEMA, backend_url="http://test:1234/v1"
        )
    assert result == {"answer": "fenced"}


@pytest.mark.asyncio
async def test_dispatch_sends_no_think_prefix_in_user_message() -> None:
    """Qwen3.6 thinking mode is disabled via /no_think on user turns."""
    fake_post = AsyncMock(
        return_value=_resp(200, _chat_completion('{"answer":"x"}'))
    )
    with patch("httpx.AsyncClient.post", fake_post):
        await qwen_dispatch("test prompt", _SCHEMA, backend_url="http://x/v1")
    sent_body = fake_post.await_args.kwargs["json"]
    user_msg = next(m for m in sent_body["messages"] if m["role"] == "user")
    assert user_msg["content"].startswith("/no_think\n\n")
    assert "test prompt" in user_msg["content"]


@pytest.mark.asyncio
async def test_dispatch_sends_schema_in_system_prompt() -> None:
    fake_post = AsyncMock(
        return_value=_resp(200, _chat_completion('{"answer":"x"}'))
    )
    with patch("httpx.AsyncClient.post", fake_post):
        await qwen_dispatch("p", _SCHEMA, backend_url="http://x/v1")
    sent_body = fake_post.await_args.kwargs["json"]
    sys_msg = next(m for m in sent_body["messages"] if m["role"] == "system")
    assert '"answer"' in sys_msg["content"]
    assert "must START" in sys_msg["content"]


# ── Retry on parse failure ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_retries_on_json_parse_failure() -> None:
    # First call returns prose; second returns valid JSON.
    fake_post = AsyncMock(
        side_effect=[
            _resp(200, _chat_completion("Sorry, I cannot do that.")),
            _resp(200, _chat_completion('{"answer":"on retry"}')),
        ]
    )
    with patch("httpx.AsyncClient.post", fake_post):
        result = await qwen_dispatch(
            "p", _SCHEMA, backend_url="http://x/v1", max_attempts=2
        )
    assert result == {"answer": "on retry"}
    assert fake_post.await_count == 2


@pytest.mark.asyncio
async def test_dispatch_raises_after_max_attempts_of_parse_failure() -> None:
    fake_post = AsyncMock(
        return_value=_resp(200, _chat_completion("definitely not json"))
    )
    with patch("httpx.AsyncClient.post", fake_post):
        with pytest.raises(QwenOperatorOutputError, match="non-JSON output"):
            await qwen_dispatch(
                "p", _SCHEMA, backend_url="http://x/v1", max_attempts=2
            )
    assert fake_post.await_count == 2


@pytest.mark.asyncio
async def test_retry_user_message_names_prior_parse_error() -> None:
    """Retry isn't a blind reroll — it tells the model what failed."""
    fake_post = AsyncMock(
        side_effect=[
            _resp(200, _chat_completion("not json")),
            _resp(200, _chat_completion('{"answer":"ok"}')),
        ]
    )
    with patch("httpx.AsyncClient.post", fake_post):
        await qwen_dispatch(
            "p", _SCHEMA, backend_url="http://x/v1", max_attempts=2
        )
    second_body = fake_post.await_args_list[1].kwargs["json"]
    # The second call should include an extra user message naming the
    # parse failure.
    user_msgs = [m for m in second_body["messages"] if m["role"] == "user"]
    assert len(user_msgs) == 2
    assert "previous response was not valid JSON" in user_msgs[1]["content"]


# ── Error paths ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_raises_on_non_2xx() -> None:
    fake_post = AsyncMock(return_value=_resp(500, "internal server error"))
    with patch("httpx.AsyncClient.post", fake_post):
        with pytest.raises(QwenOperatorError, match=r"non-2xx \(500\)"):
            await qwen_dispatch("p", _SCHEMA, backend_url="http://x/v1")


@pytest.mark.asyncio
async def test_dispatch_raises_on_timeout() -> None:
    fake_post = AsyncMock(side_effect=httpx.ConnectTimeout("connect timeout"))
    with patch("httpx.AsyncClient.post", fake_post):
        with pytest.raises(QwenOperatorTimeoutError, match="timed out"):
            await qwen_dispatch("p", _SCHEMA, backend_url="http://x/v1")


@pytest.mark.asyncio
async def test_dispatch_raises_on_unexpected_payload_shape() -> None:
    # Missing "choices" → KeyError → QwenOperatorError surfaces it.
    fake_post = AsyncMock(return_value=_resp(200, {"weird": "shape"}))
    with patch("httpx.AsyncClient.post", fake_post):
        with pytest.raises(QwenOperatorError, match="unexpected response shape"):
            await qwen_dispatch("p", _SCHEMA, backend_url="http://x/v1")


# ── Cost telemetry ────────────────────────────────────────────────────────


def _chat_completion_with_usage(
    content: str, prompt_tokens: int, completion_tokens: int
) -> dict:
    """OpenAI-compat response with a populated ``usage`` block."""
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


class TestEstimateSonnetCost:
    def test_rate_card_matches_documented_constants(self) -> None:
        # If Anthropic publishes a new Sonnet rate card and the constants
        # change, this test fails loudly and the comment in
        # qwen_dispatch.py demands a refresh of the date stamp too.
        assert _SONNET_INPUT_USD_PER_MTOK == 3.0
        assert _SONNET_OUTPUT_USD_PER_MTOK == 15.0

    def test_estimate_matches_rate_math(self) -> None:
        # 1_000_000 input tokens → $3, 1_000_000 output → $15, total $18.
        assert _estimate_sonnet_cost_usd(1_000_000, 1_000_000) == pytest.approx(
            18.0
        )

    def test_estimate_scales_linearly(self) -> None:
        # 1500 input + 500 output:
        #   1500 * 3 / 1e6  + 500 * 15 / 1e6 = 0.0045 + 0.0075 = 0.012
        assert _estimate_sonnet_cost_usd(1500, 500) == pytest.approx(0.012)

    def test_estimate_returns_none_when_input_missing(self) -> None:
        assert _estimate_sonnet_cost_usd(None, 500) is None

    def test_estimate_returns_none_when_output_missing(self) -> None:
        assert _estimate_sonnet_cost_usd(1500, None) is None


class _LogCapture:
    """Drop-in for the module ``_log``: records each ``info(...)`` call as
    ``(event, kwargs)``. Avoids depending on whichever structlog bridge
    (stdlib / PrintLoggerFactory) is configured in the test harness."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.entries.append((event, kwargs))

    def debug(self, *a: object, **kw: object) -> None:  # pragma: no cover
        pass

    def warning(self, *a: object, **kw: object) -> None:  # pragma: no cover
        pass

    def error(self, *a: object, **kw: object) -> None:  # pragma: no cover
        pass


@pytest.mark.asyncio
async def test_dispatch_logs_would_have_cost_when_usage_present() -> None:
    """qwen_dispatch must emit a structured cost-telemetry log line
    carrying the would-have-cost estimate derived from the usage block."""
    fake_post = AsyncMock(
        return_value=_resp(
            200,
            _chat_completion_with_usage(
                '{"answer":"ok"}', prompt_tokens=2000, completion_tokens=400
            ),
        )
    )
    cap = _LogCapture()
    with patch("nexus.operators.qwen_dispatch._log", cap), patch(
        "httpx.AsyncClient.post", fake_post
    ):
        await qwen_dispatch(
            "p",
            _SCHEMA,
            backend_url="http://x/v1",
            operator_name="operator_summarize",
        )

    cost_entries = [e for e in cap.entries if e[0] == "operator_dispatch_cost"]
    assert cost_entries, f"expected cost log entry; got {cap.entries!r}"
    _, kw = cost_entries[-1]
    assert kw["dispatch_engine"] == "qwen"
    assert kw["dispatch_operator"] == "operator_summarize"
    assert kw["dispatch_input_tokens"] == 2000
    assert kw["dispatch_output_tokens"] == 400
    assert kw["dispatch_cost_usd"] == 0.0
    # 2000*3/1e6 + 400*15/1e6 = 0.006 + 0.006 = 0.012
    assert kw["dispatch_would_have_cost_usd"] == pytest.approx(0.012)


@pytest.mark.asyncio
async def test_dispatch_logs_null_cost_when_usage_block_missing() -> None:
    """Missing usage block must not crash — log with would_have_cost=None."""
    fake_post = AsyncMock(
        return_value=_resp(200, _chat_completion('{"answer":"ok"}'))
    )
    cap = _LogCapture()
    with patch("nexus.operators.qwen_dispatch._log", cap), patch(
        "httpx.AsyncClient.post", fake_post
    ):
        result = await qwen_dispatch("p", _SCHEMA, backend_url="http://x/v1")
    assert result == {"answer": "ok"}

    cost_entries = [e for e in cap.entries if e[0] == "operator_dispatch_cost"]
    assert cost_entries
    _, kw = cost_entries[-1]
    assert kw["dispatch_input_tokens"] is None
    assert kw["dispatch_output_tokens"] is None
    assert kw["dispatch_would_have_cost_usd"] is None
    assert kw["dispatch_engine"] == "qwen"
