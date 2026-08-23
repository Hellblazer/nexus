#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Surface scheduled-workflow failures, which otherwise notify nobody.

WHY THIS EXISTS (nexus-x7xjj, 2026-07-27). A failing PR check is impossible to
miss. A failing SCHEDULED run produces no check, no annotation, and no
notification — it is a red entry on a page nobody opens. Three gates rotted for
three days on exactly that:

    guided-upgrade-mvv      red since 2026-07-24, 8+ runs
    era-hop-mvv             red since 2026-07-25, 8+ runs
    local-service-gate      red on main since 2026-07-25

They were found only because an unrelated push happened to match two path
filters and fired two weekly workflows off-schedule. Nothing about the system
would otherwise have said so.

THREE FAILURE MODES, not one. A watchdog that only looks for `conclusion ==
"failure"` misses the two quieter ones:

  failing    the latest run failed — the obvious case
  never-ran  a workflow declares a schedule but has NO runs at all, so the
             schedule is not firing (GitHub disables schedules on repos with
             no recent activity, and a malformed cron is accepted silently)
  stale      the latest run is far older than the declared cadence — the
             schedule stopped firing at some point and the last result, being
             green, looks fine forever

NON-VACUITY IS THE WHOLE POINT. This watchdog is itself a gate that could rot,
and its silent-pass mode is "found zero workflows to check" — a parser change,
a moved directory or a renamed key would produce a serene all-clear. So zero
scheduled workflows is reported as a FINDING, never as success.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import yaml

#: A scheduled workflow whose newest run predates this is reported `stale`.
#: Generous on purpose: the slowest cadence here is weekly, so 16 days is two
#: missed firings plus slack. Tightening this trades false alarms for latency.
DEFAULT_STALE_AFTER_DAYS = 16


@dataclass(frozen=True)
class Finding:
    workflow: str
    path: str
    kind: str  # "failing" | "never-ran" | "stale" | "nothing-to-watch" | "name-claims-cadence"
    detail: str

    def as_markdown(self) -> str:
        return f"- **{self.kind}** — `{self.workflow}` ({self.path}): {self.detail}"


def scheduled_paths(workflow_dir: Path) -> dict[str, Path]:
    """Map ``.github/workflows/x.yml`` -> Path for workflows carrying a schedule.

    YAML parses a bare ``on:`` key as the BOOLEAN True (the Norway problem's
    cousin), so both spellings are checked. Reading it as ``d.get("on")`` alone
    silently finds nothing and the watchdog reports a serene all-clear — which
    is the exact failure this module exists to prevent, so it is worth the two
    extra characters.
    """
    out: dict[str, Path] = {}
    for path in sorted(workflow_dir.glob("*.y*ml")):
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:  # a broken workflow is a finding, not a skip
            raise SystemExit(f"{path}: unparseable workflow: {exc}") from exc
        if not isinstance(doc, dict):
            continue
        triggers = doc.get("on", doc.get(True))
        if isinstance(triggers, dict) and "schedule" in triggers:
            out[f".github/workflows/{path.name}"] = path
    return out


#: nexus-idtjs: cadence claims a workflow NAME can make, each mapped to the
#: trigger key(s) that would honor it. Deliberately keyword-based and
#: approximate — a false positive costs one rename; a false negative costs a
#: journey nobody runs for months (era-hop-mvv sat titled "weekly" with a
#: dispatch-only trigger block for 12 days, nexus-4viey).
_CADENCE_CLAIM_RES: tuple[tuple["re.Pattern[str]", tuple[str, ...]], ...] = (
    (re.compile(r"\bweekly\b", re.IGNORECASE), ("schedule",)),
    (re.compile(r"\bnightly\b", re.IGNORECASE), ("schedule",)),
    (re.compile(r"\bdaily\b", re.IGNORECASE), ("schedule",)),
    (re.compile(r"\bhourly\b", re.IGNORECASE), ("schedule",)),
    (re.compile(r"\bon every pull request\b", re.IGNORECASE), ("pull_request",)),
    (re.compile(r"\bon every push\b", re.IGNORECASE), ("push",)),
    (re.compile(r"\bon [-\w /]+ changes\b", re.IGNORECASE), ("push", "pull_request")),
)


def _trigger_keys(triggers: object) -> set[str]:
    """The `on:` block's trigger names, tolerant of the three YAML shapes
    (`on: push`, `on: [push, pull_request]`, `on: {push: ..., schedule: ...}`)."""
    if isinstance(triggers, dict):
        return {str(k) for k in triggers}
    if isinstance(triggers, str):
        return {triggers}
    if isinstance(triggers, list):
        return {str(t) for t in triggers}
    return set()


def name_claim_findings(workflow_dir: Path) -> list[Finding]:
    """nexus-idtjs: the fourth finding kind — a NAME that promises a cadence
    the `on:` block does not provide. This is scanned over EVERY workflow,
    not just the scheduled ones, because the population `scheduled_paths`
    selects is by construction the population that cannot exhibit the bug:
    the workflows whose names lie are exactly the ones a schedule-keyed
    filter cannot see."""
    findings: list[Finding] = []
    for path in sorted(workflow_dir.glob("*.y*ml")):
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:  # a broken workflow is a finding, not a skip
            raise SystemExit(f"{path}: unparseable workflow: {exc}") from exc
        if not isinstance(doc, dict):
            continue
        name = str(doc.get("name", ""))
        keys = _trigger_keys(doc.get("on", doc.get(True)))
        for rx, satisfying in _CADENCE_CLAIM_RES:
            m = rx.search(name)
            if m and not (keys & set(satisfying)):
                findings.append(Finding(
                    workflow=path.name,
                    path=f".github/workflows/{path.name}",
                    kind="name-claims-cadence",
                    detail=(
                        f"name claims `{m.group(0)}` but the `on:` block has "
                        f"{sorted(keys) if keys else 'no triggers at all'} — the title "
                        "asserts coverage the triggers do not provide; rename it to "
                        "the truth or restore the trigger (nexus-idtjs)"
                    ),
                ))
    return findings


def classify(
    scheduled: dict[str, str],
    latest_run: dict[str, dict | None],
    now: datetime,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
) -> list[Finding]:
    """Pure classification. *scheduled* maps path -> display name; *latest_run*
    maps path -> the newest run dict (or None when the workflow has never run).
    """
    if not scheduled:
        return [
            Finding(
                workflow="(none)",
                path=str(Path(".github/workflows")),
                kind="nothing-to-watch",
                detail=(
                    "no workflow declares an `on.schedule` trigger. Either every "
                    "scheduled gate was removed, or this watchdog stopped "
                    "recognising them — both need a human, neither is an all-clear"
                ),
            )
        ]

    findings: list[Finding] = []
    for path, name in sorted(scheduled.items()):
        run = latest_run.get(path)
        if run is None:
            findings.append(Finding(name, path, "never-ran", (
                "declares a schedule but has NO runs at all — the schedule is not "
                "firing (GitHub disables schedules on inactive repos, and an "
                "invalid cron is accepted without error)"
            )))
            continue

        conclusion = run.get("conclusion")
        created = run.get("created_at", "")
        if conclusion == "failure":
            findings.append(Finding(name, path, "failing", (
                f"latest run {run.get('html_url', '(no url)')} on "
                f"`{run.get('head_branch', '?')}` FAILED at {created}"
            )))
            continue

        age = _age_days(created, now)
        if age is not None and age > stale_after_days:
            findings.append(Finding(name, path, "stale", (
                f"newest run is {age} days old (conclusion `{conclusion}`) — the "
                "schedule appears to have stopped firing, and a stale green "
                "reads as healthy forever"
            )))
    return findings


def _age_days(created_at: str, now: datetime) -> int | None:
    if not created_at:
        return None
    try:
        stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - stamp).days


def render(findings: list[Finding]) -> str:
    if not findings:
        return "All scheduled workflows are green and firing on cadence."
    lines = [
        "Scheduled workflows fail silently: no PR check, no annotation, no "
        "notification. This is the sweep that says so.",
        "",
    ]
    lines += [f.as_markdown() for f in findings]
    lines += [
        "",
        "_Raised by `scripts/scheduled_workflow_watchdog.py` (nexus-x7xjj)._",
    ]
    return "\n".join(lines)


# ── GitHub API (thin; the logic above is pure and tested separately) ─────────


def _api(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "nexus-scheduled-workflow-watchdog",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed api.github.com host
        return json.loads(resp.read())


def fetch_latest_runs(
    repo: str, token: str, paths: list[str], api: Callable[[str], dict] | None = None
) -> dict[str, dict | None]:
    """Newest run per workflow path, across ALL branches.

    Across all branches deliberately: scheduled runs execute from the default
    branch while push-triggered ones land on develop, and the failures that
    motivated this were split across both.
    """
    call = api or (lambda u: _api(u, token))
    out: dict[str, dict | None] = {}
    for path in paths:
        # status=completed is load-bearing (nexus, 2026-08-23). Without it
        # this samples the newest run of ANY status, and the schedules make
        # that an IN-FLIGHT run almost every time: this watchdog fires 6
        # minutes after the nightly gate it watches, which takes 12-25
        # minutes. An in-progress run has conclusion: null -- not "failure",
        # so not flagged -- and created_at is minutes old, so not "stale"
        # either. The result was a clean bill of health over 8 consecutive
        # red nights of local-service-gate-nightly (08-16..08-23), and issue
        # #1457 closed 2026-08-18 saying every scheduled workflow was green.
        # "The last thing this workflow decided" is the newest COMPLETED
        # run; ask the API for that instead of filtering after the fact.
        url = (
            f"https://api.github.com/repos/{repo}/actions/workflows/"
            f"{urllib.parse.quote(path, safe='')}/runs"
            f"?per_page=1&status=completed"
        )
        try:
            data = call(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # GitHub does not know this workflow: it exists on disk but has
                # never been registered — a file added locally and not yet
                # pushed, or one whose schedule has never fired at all. That is
                # precisely the `never-ran` state, so record it as such rather
                # than aborting. Found by running this script for the first
                # time, against itself, before it was pushed.
                out[path] = None
                continue
            # Anything else (500, 403, rate limit) must NOT read as "fine".
            raise SystemExit(f"GitHub API error for {path}: {exc}") from exc
        runs = data.get("workflow_runs") or []
        out[path] = runs[0] if runs else None
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--workflow-dir", default=".github/workflows")
    ap.add_argument("--stale-after-days", type=int, default=DEFAULT_STALE_AFTER_DAYS)
    args = ap.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    if not args.repo or not token:
        print("GITHUB_REPOSITORY and GITHUB_TOKEN are required", file=sys.stderr)
        return 2

    paths = scheduled_paths(Path(args.workflow_dir))
    names = {p: p.rsplit("/", 1)[-1] for p in paths}
    latest = fetch_latest_runs(args.repo, token, list(paths)) if paths else {}
    findings = classify(names, latest, datetime.now(timezone.utc), args.stale_after_days)
    # nexus-idtjs: scanned over ALL workflows, not just the scheduled subset.
    findings += name_claim_findings(Path(args.workflow_dir))

    print(render(findings))
    # Reported on stderr so the workflow can log it without polluting the body.
    print(f"watchdog: {len(paths)} scheduled workflows checked, "
          f"{len(findings)} findings", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
