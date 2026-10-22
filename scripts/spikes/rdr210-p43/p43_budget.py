# SPDX-License-Identifier: AGPL-3.0-or-later
"""Voyage token accounting and the spend cap. Pure.

Every Voyage-bound step charges its ESTIMATED tokens (characters / 3.3, the
figure the 210-research-17 spike bounded its embed spend with) BEFORE it sends,
so a run that would cross the cap stops without sending. The engine also
reports actual usage in the ``X-Nexus-Usage-Tokens`` response header; the
harness records those beside the estimate (:meth:`TokenBudget.note_reported`)
but the cap binds on the estimate, which is the conservative side.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

CHARS_PER_TOKEN = 3.3
DEFAULT_CAP_TOKENS = 1_500_000


class BudgetExceeded(RuntimeError):
    """A Voyage-bound step would take the spend past the cap."""


def chars_to_tokens(chars: int) -> int:
    """Estimated tokens for *chars* characters, rounded up."""
    if chars < 0:
        raise ValueError("chars must be non-negative")
    return math.ceil(chars / CHARS_PER_TOKEN)


def rerank_tokens(query: str, docs: list[str]) -> int:
    """Estimated Voyage rerank tokens for one request: each document is billed
    with the query prepended (query + document, both counted)."""
    q = len(query)
    return sum(chars_to_tokens(q + len(d)) for d in docs)


def check_cap(estimate: int, cap: int) -> None:
    """Refuse (raise) when *estimate* exceeds *cap*. Used by ``--plan`` and at
    the start of a run, before anything is sent."""
    if estimate > cap:
        raise BudgetExceeded(
            f"estimated Voyage spend {estimate:,} tokens exceeds the cap {cap:,}; "
            f"raise it with --cap-tokens only if that spend is intended"
        )


@dataclass
class TokenBudget:
    cap: int
    spent: int = 0                  # estimated, charged before each send
    reported: int = 0               # engine-reported (X-Nexus-Usage-Tokens)
    by_label: dict[str, int] = field(default_factory=dict)

    def charge(self, est_tokens: int, label: str) -> None:
        """Account *est_tokens* for a step about to be sent, or raise."""
        if est_tokens < 0:
            raise ValueError("est_tokens must be non-negative")
        if self.spent + est_tokens > self.cap:
            raise BudgetExceeded(
                f"{label}: {est_tokens:,} tokens would take the spend to "
                f"{self.spent + est_tokens:,}, over the cap {self.cap:,}"
            )
        self.spent += est_tokens
        self.by_label[label] = self.by_label.get(label, 0) + est_tokens

    def note_reported(self, tokens: int) -> None:
        self.reported += max(0, tokens)

    def remaining(self) -> int:
        return self.cap - self.spent

    def snapshot(self) -> dict:
        return {
            "cap_tokens": self.cap,
            "estimated_tokens_charged": self.spent,
            "engine_reported_tokens": self.reported,
            "by_step": dict(sorted(self.by_label.items())),
        }
