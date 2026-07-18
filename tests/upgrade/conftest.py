# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared upgrade-ladder test fixtures (RDR-186 .12: ladder.db is retired —
tests that need a durable CompletionLedger use this in-memory stand-in; the
real durable backend is the engine's ladder_completions table, whose
semantics are tested in the Java LadderHandlerTest)."""
from __future__ import annotations

from contextlib import contextmanager

from nexus.upgrade_ladder.completion import CompletionRecord


class InMemoryCompletionLedger:
    """Minimal CompletionLedger — no substrate, protocol-conformant."""

    def __init__(self) -> None:
        self.records: dict[str, CompletionRecord] = {}

    def record_verified(self, rung_name: str, *, package_version: str, detail: str = "") -> None:
        self.records[rung_name] = CompletionRecord(
            rung_name=rung_name,
            verified_at="t0",
            package_version=package_version,
            detail=detail,
        )

    def verified_rungs(self) -> frozenset[str]:
        return frozenset(self.records)

    def completions(self) -> dict[str, CompletionRecord]:
        return dict(self.records)


@contextmanager
def ledger_ctx():
    """Context-manager shim preserving the retired CompletionStore's
    `with ... as store:` call shape in older test files."""
    yield InMemoryCompletionLedger()
