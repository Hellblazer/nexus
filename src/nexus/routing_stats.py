# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-121 Phase 3: routing-hook telemetry aggregation.

Reads the JSONL log written by routing hooks via
``conexus/hooks/scripts/routing/_lib.log_routing_event`` and computes
per-rule fire / allow / deny / escape counts. Surfaced to the user
by the ``nx hook routing-stats`` CLI subcommand. Used at the 30-day
soak review (mzvwa.7) to decide whether matchers need refinement,
downgrade to warn, or have shipped without ever firing.
"""
from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable


def default_log_path() -> pathlib.Path:
    """Return the routing log path, honoring ``NX_ROUTING_LOG_PATH``."""
    override = os.environ.get("NX_ROUTING_LOG_PATH")
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".config" / "nexus" / "routing_log.jsonl"


@dataclass
class RuleStats:
    """Aggregated outcomes for a single rule."""

    rule: str
    allow: int = 0
    deny: int = 0
    escape: int = 0
    fail_open: int = 0
    fail_closed: int = 0
    extra: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return (
            self.allow + self.deny + self.escape
            + self.fail_open + self.fail_closed
            + sum(self.extra.values())
        )

    @property
    def block_rate(self) -> float:
        total = self.total
        if total == 0:
            return 0.0
        return (self.deny + self.fail_closed) / total

    @property
    def escape_rate(self) -> float:
        total = self.total
        if total == 0:
            return 0.0
        return self.escape / total

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "total": self.total,
            "allow": self.allow,
            "deny": self.deny,
            "escape": self.escape,
            "fail_open": self.fail_open,
            "fail_closed": self.fail_closed,
            "block_rate": round(self.block_rate, 4),
            "escape_rate": round(self.escape_rate, 4),
            **({"extra": self.extra} if self.extra else {}),
        }

    def add(self, outcome: str) -> None:
        match outcome:
            case "allow":
                self.allow += 1
            case "deny":
                self.deny += 1
            case "escape":
                self.escape += 1
            case "allow_fail_open":
                self.fail_open += 1
            case "deny_fail_closed":
                self.fail_closed += 1
            case _:
                self.extra[outcome] = self.extra.get(outcome, 0) + 1


def _iter_records(path: pathlib.Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def aggregate(path: pathlib.Path | None = None) -> dict[str, RuleStats]:
    """Aggregate the routing log into per-rule stats.

    Returns an empty dict when the log is absent or unreadable; never
    raises. Records missing ``rule`` or ``outcome`` are dropped.
    """
    log_path = path if path is not None else default_log_path()
    stats: dict[str, RuleStats] = {}
    for record in _iter_records(log_path):
        rule = record.get("rule")
        outcome = record.get("outcome")
        if not isinstance(rule, str) or not isinstance(outcome, str):
            continue
        bucket = stats.setdefault(rule, RuleStats(rule=rule))
        bucket.add(outcome)
    return stats


def stats_to_json(stats: dict[str, RuleStats]) -> dict[str, dict[str, Any]]:
    """Serialize the per-rule stats to plain dicts for JSON output."""
    return {rule: s.to_dict() for rule, s in stats.items()}
