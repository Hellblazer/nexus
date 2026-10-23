# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-model facts the direct Anthropic backend needs: list prices and one
capability flag (nexus-2k66t.3 P2).

Prices are USD per million tokens, from the published price list read through
Context7 (``/websites/platform_claude_en``, ``about-claude/pricing``) on
2026-10-23, including the 2026-10-07 cut of Sonnet 5.5's cache-read price.
This is the ONE table; ``cost_usd`` on a direct call is computed from it and
from nothing else. A model that is not in it gets ``cost_usd=None`` and a
warning, never ``0.0`` (RDR-196: ``0.0`` reads as "this call was free").

Re-check the table whenever a model id is added to
``model_tiers._API_MODEL_IDS``: a price that is missing is a loud ``None``, a
price that is stale is a silent wrong number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens. ``cache_write`` is the 5-minute write price (the
    only kind this backend can cause: it sets no ``cache_control`` TTL).

    A model priced by prompt length carries a second row in *long*, used when
    the prompt (input + cache write + cache read tokens) exceeds
    *long_threshold*."""

    input: float
    output: float
    cache_write: float
    cache_read: float
    long_threshold: int | None = None
    long: "ModelPrice | None" = None

    def for_prompt(self, prompt_tokens: int) -> "ModelPrice":
        if self.long is not None and self.long_threshold is not None and prompt_tokens > self.long_threshold:
            return self.long
        return self

    def cost(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> float:
        row = self.for_prompt(input_tokens + cache_creation_tokens + cache_read_tokens)
        return (
            input_tokens * row.input
            + cache_creation_tokens * row.cache_write
            + cache_read_tokens * row.cache_read
            + output_tokens * row.output
        ) / 1_000_000


_HAIKU_5_5_LONG = ModelPrice(0.50, 2.50, 0.625, 0.05)

#: Canonical model id -> price. Dated ids (``claude-haiku-4-5-20251001``) resolve
#: through :func:`price_for` by dropping the ``-YYYYMMDD`` suffix.
PRICES: Final[dict[str, ModelPrice]] = {
    "claude-fable-5-1": ModelPrice(10.0, 50.0, 12.5, 0.25),
    "claude-fable-5": ModelPrice(10.0, 50.0, 12.5, 1.0),
    "claude-opus-5-5": ModelPrice(4.0, 20.0, 5.0, 0.20),
    "claude-opus-5": ModelPrice(5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-8": ModelPrice(5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-7": ModelPrice(5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-6": ModelPrice(5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-5": ModelPrice(5.0, 25.0, 6.25, 0.50),
    "claude-sonnet-5-5": ModelPrice(2.0, 10.0, 2.5, 0.10),
    "claude-sonnet-5": ModelPrice(2.0, 10.0, 2.5, 0.20),
    "claude-sonnet-4-6": ModelPrice(3.0, 15.0, 3.75, 0.30),
    "claude-sonnet-4-5": ModelPrice(3.0, 15.0, 3.75, 0.30),
    "claude-haiku-5-5": ModelPrice(0.10, 0.50, 0.125, 0.01, long_threshold=100_000, long=_HAIKU_5_5_LONG),
    "claude-haiku-4-5": ModelPrice(1.0, 5.0, 1.25, 0.10),
}

_DATE_SUFFIX = re.compile(r"-\d{8}$")


def canonical_model_id(model_id: str) -> str:
    """*model_id* without a trailing ``-YYYYMMDD`` snapshot date."""
    return _DATE_SUFFIX.sub("", model_id)


def price_for(model_id: str | None) -> ModelPrice | None:
    """The price row for *model_id* (a dated snapshot id counts as its base id),
    or ``None`` when the model is not in :data:`PRICES`."""
    if not model_id:
        return None
    return PRICES.get(model_id) or PRICES.get(canonical_model_id(model_id))


#: Models whose API rejects ``tool_choice`` of type ``tool`` or ``any`` with a
#: 400 (Context7, ``api/errors#forced-tool-use-not-supported``, 2026-10-23).
#: On these the structured-output tool is offered with ``tool_choice: auto``
#: and the system prompt says to call it; the local schema validation and the
#: repair turn are what hold the contract.
FORCED_TOOL_UNSUPPORTED: Final[tuple[str, ...]] = (
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-fable-5-1",
    "claude-mythos-5-1",
)


def supports_forced_tool_choice(model_id: str) -> bool:
    """Whether the model accepts ``tool_choice: {type: tool}``. A model not
    named in :data:`FORCED_TOOL_UNSUPPORTED` is assumed to; the backend also
    retries once with ``auto`` when the API says otherwise."""
    return canonical_model_id(model_id) not in FORCED_TOOL_UNSUPPORTED
