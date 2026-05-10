# SPDX-License-Identifier: AGPL-3.0-or-later
"""Async Qwen dispatch — drop-in alternative to ``claude_dispatch``.

Single responsibility: deliver one schema-bounded synthesis call to a
locally-hosted llama.cpp / OpenAI-compat backend, return parsed JSON.

The qwen-coprocessor-stack supervisor
(https://github.com/Hellblazer/qwen-coprocessor-stack) defined the
SpawnOpts surface this dispatch mirrors: ``thinking_mode`` disabled by
default (Qwen3.6's chain-of-thought adds ~6× output token bloat per
Artificial Analysis 2026-04, impractical for synthesis), JSON Schema
rendered into the system prompt as a directive, defensive markdown
fence stripping post-response.

No grammar enforcement (yet); relies on Qwen3.6's instruction-following
on JSON output. Bench measurement against the seed cases at
qwen-coprocessor-stack/scripts/bench/ shows 10/10 schema-conforming
output. Retry-on-parse-failure is bounded by ``max_attempts``.

Resolution chain for backend URL/model (all optional, fall through
left-to-right):

* explicit args (``backend_url=``, ``model=``)
* ``QWEN_BACKEND_URL`` / ``QWEN_MODEL`` env vars
* first backend in ``~/.qwen-coprocessor-stack/config.json`` (or the
  ``QWEN_CONFIG_DIR`` override path)
* hardcoded default (``http://localhost:8080/v1``,
  ``qwen3.6-35b-a3b``)

Same operator-chooses pattern as ``claude_dispatch``'s auth: the
operator declares intent at the system level; nexus consumes whatever
is configured.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx

__all__ = [
    "qwen_dispatch",
    "QwenOperatorError",
    "QwenOperatorTimeoutError",
    "QwenOperatorOutputError",
]


class QwenOperatorError(RuntimeError):
    """Raised when llama-server returns a non-2xx response or the body
    payload doesn't match the OpenAI chat-completions shape."""


class QwenOperatorTimeoutError(asyncio.TimeoutError):
    """Raised when the dispatch HTTP request exceeds *timeout*."""


class QwenOperatorOutputError(QwenOperatorError):
    """Raised when the response is not valid JSON after fence-stripping
    on every attempt — i.e. the model emitted prose despite the system
    prompt + retry."""


# Module-level concurrency cap. llama-server serves one request at a
# time; if nexus parallelizes plan execution across sessions, multiple
# dispatches would queue at the backend with no benefit. Default 1
# matches single-server reality. Operator can raise via env when a
# multi-backend pool is configured upstream.
_QWEN_CONCURRENCY = max(1, int(os.environ.get("NEXUS_QWEN_CONCURRENCY", "1")))
_QWEN_SEMAPHORE: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    """Lazy-init the module semaphore so it binds to the running loop.

    Creating an ``asyncio.Semaphore`` at module-import time bound it to
    whatever loop existed when the import ran — which in test harnesses
    is often a different loop than the one running the dispatch. Lazy
    creation defers binding to first use.
    """
    global _QWEN_SEMAPHORE
    if _QWEN_SEMAPHORE is None:
        _QWEN_SEMAPHORE = asyncio.Semaphore(_QWEN_CONCURRENCY)
    return _QWEN_SEMAPHORE


_DEFAULT_BACKEND_URL = "http://localhost:8080/v1"
_DEFAULT_MODEL = "qwen3.6-35b-a3b"


def _read_qwen_stack_config() -> dict[str, Any] | None:
    """Read the operator's qwen-coprocessor-stack config when present.

    Same path the qwen-stack supervisor reads. We don't import from that
    package — keeping nexus free of a runtime dep on a TypeScript repo.
    Failures (file missing, JSON invalid, OS error) all return ``None``
    so callers fall through to env / hardcoded.
    """
    override = os.environ.get("QWEN_CONFIG_DIR")
    cfg_path = (
        Path(override) / "config.json"
        if override
        else Path.home() / ".qwen-coprocessor-stack" / "config.json"
    )
    try:
        return json.loads(cfg_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _resolve_backend_url(explicit: str | None) -> str:
    if explicit:
        return explicit.rstrip("/")
    env = os.environ.get("QWEN_BACKEND_URL")
    if env:
        return env.rstrip("/")
    cfg = _read_qwen_stack_config()
    if cfg and isinstance(cfg.get("backends"), list) and cfg["backends"]:
        url = cfg["backends"][0].get("url")
        if isinstance(url, str) and url:
            return url.rstrip("/")
    return _DEFAULT_BACKEND_URL


def _resolve_model(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("QWEN_MODEL")
    if env:
        return env
    cfg = _read_qwen_stack_config()
    if cfg and isinstance(cfg.get("backends"), list) and cfg["backends"]:
        model = cfg["backends"][0].get("model")
        if isinstance(model, str) and model:
            return model
    return _DEFAULT_MODEL


# Mirror of ``stripCodeFences`` in qwen-coprocessor-stack/src/server.ts.
# Anchored at both ends so mid-prose fenced blocks are NOT stripped —
# we only undress responses where the model wrapped its entire JSON
# output in ```json ... ```.
_FENCE_RE = re.compile(r"^```(?:json|JSON)?\s*\n([\s\S]*?)\n?```$")


def _strip_code_fences(raw: str) -> str:
    """Strip surrounding markdown code fences from a candidate JSON string.

    Qwen3.6 frequently wraps schema-conforming JSON in `````json
    ... ````` despite a system-prompt directive forbidding
    fences. The content is right; defending against the jacket is cheaper
    than fighting the model. Returns input unchanged if no fences are
    detected — JSON.loads then runs on the original.
    """
    trimmed = raw.strip()
    m = _FENCE_RE.match(trimmed)
    if m:
        return m.group(1).strip()
    return raw


def _build_system_prompt(json_schema: dict[str, Any]) -> str:
    """Render the schema-as-system-prompt directive used by qwen-stack v0.8.

    Positive framing ("begin with ``{`` or ``[``") performs better than
    negative directives ("no fences") on Qwen3.6's instruction-following.
    The closing fallback for unrecoverable inputs gives the model a
    schema-shaped escape hatch instead of free-text apology.
    """
    return (
        "You are operating as a coprocessor under a supervisor that runs you "
        "in single-turn mode for schema-bounded synthesis.\n\n"
        "[Output contract — JSON only]\n"
        "Your final assistant message must START with `{` or `[` and END\n"
        "with `}` or `]`. No preamble, no closing remarks, no explanatory\n"
        "text. ABSOLUTELY no markdown code fences (no triple backticks,\n"
        "no ```json wrappers). The very first character of your response\n"
        "must be `{` or `[`.\n\n"
        "The JSON must conform to this JSON Schema:\n\n"
        + json.dumps(json_schema, indent=2)
        + "\n\nIf the task cannot be completed, return a JSON object with\n"
        '`{"error": "<one-line explanation>"}` rather than free text.'
    )


async def qwen_dispatch(
    prompt: str,
    json_schema: dict[str, Any],
    *,
    timeout: float = 300.0,
    backend_url: str | None = None,
    model: str | None = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """Dispatch one operator call to Qwen via OpenAI-compat ``/v1/chat/completions``.

    Mirrors :func:`nexus.operators.dispatch.claude_dispatch`'s signature:
    takes prompt + JSON Schema, returns parsed dict.

    Differences from ``claude_dispatch``:

    * No subprocess. Uses ``httpx`` against llama-server's OpenAI-compat
      endpoint. Saves ~5 s of subprocess-spawn overhead per call.
    * No ``--json-schema`` enforcement at the server side (yet) —
      Qwen3.6's instruction-following plus :func:`_strip_code_fences`
      is reliable enough on the bench seed cases. Grammar enforcement
      via llama.cpp ``--grammar`` is a future option if measurement
      justifies it.
    * Retries on JSON parse failure, bounded by *max_attempts* (default
      2). The retry uses a tightened user message that names the prior
      parse error.

    Args:
        prompt: Full prompt text.
        json_schema: JSON Schema the response must conform to.
        timeout: Hard timeout per HTTP attempt (default 300 s).
        backend_url: Override the resolved backend URL.
        model: Override the resolved model name.
        max_attempts: Cap on retry-on-parse-failure (default 2).

    Returns:
        Parsed JSON dict from the model response.

    Raises:
        QwenOperatorTimeoutError: HTTP request exceeded *timeout*.
        QwenOperatorError: Non-2xx response or unexpected payload shape.
        QwenOperatorOutputError: All attempts produced non-JSON output.
    """
    backend = _resolve_backend_url(backend_url)
    model_id = _resolve_model(model)
    system = _build_system_prompt(json_schema)
    user = f"/no_think\n\n{prompt}"

    last_raw: str | None = None
    last_parse_err: str | None = None

    async with _semaphore():
        for attempt in range(1, max_attempts + 1):
            messages: list[dict[str, str]] = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            if attempt > 1 and last_parse_err is not None:
                # Retry — name the prior failure so the model has
                # something to fix, not just a re-roll.
                messages.append({
                    "role": "user",
                    "content": (
                        "/no_think\n\n"
                        f"Your previous response was not valid JSON ({last_parse_err}). "
                        "Try again. Begin with `{` or `[`. No preamble. No code fences."
                    ),
                })

            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{backend}/chat/completions",
                        json={
                            "model": model_id,
                            "messages": messages,
                            "temperature": 0.2,
                            "stream": False,
                        },
                    )
            except httpx.TimeoutException as exc:
                raise QwenOperatorTimeoutError(
                    f"qwen_dispatch timed out after {timeout}s "
                    f"(attempt {attempt}/{max_attempts}, backend={backend})"
                ) from exc
            except httpx.HTTPError as exc:
                raise QwenOperatorError(
                    f"qwen_dispatch HTTP error: {exc} "
                    f"(attempt {attempt}/{max_attempts}, backend={backend})"
                ) from exc

            if resp.status_code != 200:
                snippet = resp.text[:300]
                raise QwenOperatorError(
                    f"qwen_dispatch non-2xx ({resp.status_code}): {snippet} "
                    f"(attempt {attempt}/{max_attempts}, backend={backend})"
                )

            try:
                payload = resp.json()
                last_raw = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
                raise QwenOperatorError(
                    f"qwen_dispatch unexpected response shape: {exc} "
                    f"(attempt {attempt}/{max_attempts})"
                ) from exc

            stripped = _strip_code_fences(last_raw)
            try:
                return json.loads(stripped)
            except json.JSONDecodeError as exc:
                last_parse_err = str(exc)
                # fall through to retry (if attempts remain)

    raise QwenOperatorOutputError(
        f"qwen_dispatch produced non-JSON output after {max_attempts} attempts "
        f"(last error: {last_parse_err}); last raw: {(last_raw or '')[:300]}"
    )
