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


def _is_selftest_record(rule: str, outcome: str, record: dict[str, Any]) -> bool:
    """True for fail-ladder self-test noise (nexus-mzvwa.9).

    Matches (a) the labeled ``selftest_*`` rules the suite writes now,
    (b) the historical ``test_rule`` deny_fail_closed rows, and (c) the
    historical unlabeled twin: ``rule=unknown`` + ``allow_fail_open`` +
    NO tool_name (a real production fail-open always carries the
    payload's tool_name; the ladder stub ran with an empty payload).
    Pre-fix the unit suite wrote a (b)+(c) pair into the LIVE log on
    every run — 312 pairs over the 48-day soak.

    CAVEAT: signature (c) assumes every production hook passes
    ``rule_name`` to ``run_hook`` (all current ones do). A future hook
    that forgets it AND hits malformed stdin AND fails open would have
    its telemetry silently classified as self-test noise — if you're
    debugging a hook whose fail-opens seem to vanish, check its
    ``run_hook(..., rule_name=...)`` wiring first.
    """
    if rule.startswith("selftest_"):
        return True
    if rule == "test_rule" and outcome == "deny_fail_closed":
        return True
    return (
        rule == "unknown"
        and outcome == "allow_fail_open"
        and not record.get("tool_name")
    )


def aggregate_detailed(
    path: pathlib.Path | None = None,
) -> tuple[dict[str, RuleStats], int]:
    """Aggregate the routing log; returns ``(per-rule stats, excluded)``.

    ``excluded`` counts fail-ladder self-test records dropped from the
    stats (see :func:`_is_selftest_record`) so the CLI can footnote the
    exclusion instead of silently reporting phantom rules. Returns an
    empty dict when the log is absent or unreadable; never raises.
    Records missing ``rule`` or ``outcome`` are dropped uncounted.
    """
    log_path = path if path is not None else default_log_path()
    stats: dict[str, RuleStats] = {}
    excluded = 0
    for record in _iter_records(log_path):
        rule = record.get("rule")
        outcome = record.get("outcome")
        if not isinstance(rule, str) or not isinstance(outcome, str):
            continue
        if _is_selftest_record(rule, outcome, record):
            excluded += 1
            continue
        bucket = stats.setdefault(rule, RuleStats(rule=rule))
        bucket.add(outcome)
    return stats, excluded


def aggregate(path: pathlib.Path | None = None) -> dict[str, RuleStats]:
    """Aggregate the routing log into per-rule stats (self-test rows excluded)."""
    return aggregate_detailed(path)[0]


def registered_rules(hooks_json: pathlib.Path | None = None) -> set[str] | None:
    """Rule names currently registered in the plugin's hooks.json.

    The registration surface is hooks.json, NOT registry.yaml (which is
    documentation — the mzvwa.7 soak review's own miss). Rule names equal
    the routing script stems referenced by hook commands. Returns ``None``
    when no hooks.json can be located/parsed (the caller should then skip
    the cross-check rather than mark everything unregistered).

    BOTH plugins are probed and their rules UNIONED — conexus and sn each
    own routing rules (RDR-125 per-plugin ownership) writing to the same
    log, so a conexus-only probe would mark legitimately-registered
    sn rules "(unregistered)" (mzvwa.9 critique H2). Resolution per
    plugin: the installed marketplace copy
    (``~/.claude/plugins/marketplaces/nexus-plugins/<plugin>/hooks/hooks.json``),
    then the repo-relative ``<plugin>/hooks/hooks.json`` (dev checkout).
    Returns the union when AT LEAST ONE hooks.json was readable.
    """
    def _rules_from(cand: pathlib.Path) -> set[str] | None:
        try:
            if not cand.is_file():
                return None
            data = json.loads(cand.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — best-effort hooks.json probe; unreadable/malformed counts as absent
            return None
        rules: set[str] = set()

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                cmd = node.get("command")
                if isinstance(cmd, str) and "/routing/" in cmd:
                    stem = cmd.rsplit("/", 1)[-1]
                    if stem.endswith(".py"):
                        rules.add(stem[:-3])
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)
        _walk(data)
        return rules

    if hooks_json is not None:
        return _rules_from(hooks_json)

    marketplace = (
        pathlib.Path.home()
        / ".claude" / "plugins" / "marketplaces" / "nexus-plugins"
    )
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    found: set[str] | None = None
    for plugin in ("conexus", "sn"):
        for base in (marketplace, repo_root):
            rules = _rules_from(base / plugin / "hooks" / "hooks.json")
            if rules is not None:
                found = (found or set()) | rules
                break  # first readable copy wins for this plugin
    return found


def escape_events(path: pathlib.Path | None = None) -> list[dict[str, Any]]:
    """The escape events with their reasons, oldest first (nexus-mzvwa.9 M3).

    The whole point of logging ``# routing-allow:`` reasons is auditing
    over-use — so the review surface must show the REASONS, not just an
    escape count (pre-fix a soak reviewer had to hand-grep the JSONL).
    Events logged before the ``escape_reason`` field existed carry
    ``reason=""`` (their reason was truncated away with the fragment).
    """
    log_path = path if path is not None else default_log_path()
    out: list[dict[str, Any]] = []
    for record in _iter_records(log_path):
        if record.get("outcome") != "escape":
            continue
        rule = record.get("rule")
        if not isinstance(rule, str):
            continue
        out.append({
            "ts": record.get("ts", ""),
            "rule": rule,
            "reason": record.get("escape_reason", "") or "",
        })
    return out


def stats_to_json(stats: dict[str, RuleStats]) -> dict[str, dict[str, Any]]:
    """Serialize the per-rule stats to plain dicts for JSON output."""
    return {rule: s.to_dict() for rule, s in stats.items()}
