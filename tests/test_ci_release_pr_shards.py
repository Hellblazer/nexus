# SPDX-License-Identifier: AGPL-3.0-or-later
"""A release PR's pytest shards take the lab's release scale set (labrun-9no).

Sam, labrun capacity report 2026-11-10: a pull request into ``main`` runs its shards on
``lab-6c18g-release``, which the lab places ahead of the develop shards, so a release is
not queued behind develop's runs. Everything else keeps the old choice: a fork PR and
``NX_CI_POOL=off`` go to ``ubuntu-latest``, every other run to ``lab-6c18g``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
REPO = "Hellblazer/nexus"


def _runs_on() -> str:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["test"]["runs-on"]


def _evaluate(expr: str, ctx: dict[str, str]) -> str:
    """Evaluate the runs-on expression the way Actions does, for the operators it uses.

    It uses only ``&&``, ``||``, ``==``, ``!=``, parentheses, string literals and context
    lookups; Python's ``and``/``or`` return an operand exactly as Actions' do. A context
    value Actions leaves unset is the empty string here, as Actions compares it.
    """
    assert expr.startswith("${{ ") and expr.endswith(" }}"), expr
    body = expr[4:-3]
    names = sorted(set(re.findall(r"\b(?:github|vars)(?:\.\w+)+", body)), key=len, reverse=True)
    unknown = [n for n in names if n not in ctx]
    assert not unknown, f"the test does not model {unknown}"
    for name in names:
        body = body.replace(name, repr(ctx[name]))
    body = body.replace("&&", " and ").replace("||", " or ")
    return eval(body, {"__builtins__": {}}, {})  # noqa: S307 -- a translated literal expression


def _ctx(*, event: str, base_ref: str = "", head_repo: str = REPO, pool: str = "") -> dict[str, str]:
    return {
        "vars.NX_CI_POOL": pool,
        "github.event_name": event,
        "github.event.pull_request.head.repo.full_name": head_repo if event == "pull_request" else "",
        "github.repository": REPO,
        "github.base_ref": base_ref,
    }


@pytest.mark.parametrize(
    ("ctx", "label"),
    [
        (_ctx(event="pull_request", base_ref="main"), "lab-6c18g-release"),
        (_ctx(event="push"), "lab-6c18g"),
        (_ctx(event="pull_request", base_ref="develop"), "lab-6c18g"),
        (_ctx(event="workflow_dispatch"), "lab-6c18g"),
        (_ctx(event="pull_request", base_ref="main", head_repo="someone/fork"), "ubuntu-latest"),
        (_ctx(event="pull_request", base_ref="main", pool="off"), "ubuntu-latest"),
        (_ctx(event="push", pool="off"), "ubuntu-latest"),
    ],
    ids=["release-pr", "develop-push", "pr-into-develop", "dispatch", "fork-pr-into-main", "pool-off-pr", "pool-off-push"],
)
def test_the_shard_runner_label(ctx: dict[str, str], label: str) -> None:
    assert _evaluate(_runs_on(), ctx) == label


def test_only_the_shard_job_takes_the_release_set() -> None:
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    takers = sorted(job_id for job_id, job in jobs.items() if "lab-6c18g-release" in str(job.get("runs-on", "")))
    assert takers == ["test"], takers
