# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one short result line a cloud-leg step's record keeps.

A step's stdout is not kept (it carries ids and may carry a token the scrub cannot know), so a probe that wants an answer
recorded, such as the reason an engine refusal gave, prints it on a line that starts with ``result: ``. Only those lines
are kept, joined with `` | `` and cut to ``MAX_CHARS`` after the known secrets are scrubbed. Standard library only.
"""
from __future__ import annotations

from collections.abc import Callable

PREFIX = "result: "
MAX_CHARS = 200


def result_line(stdout: str, scrub: Callable[[str], str]) -> str:
    """The ``result: `` lines of *stdout*, scrubbed and bounded; an empty string when a step prints none."""
    kept = [ln[len(PREFIX):].strip() for ln in stdout.splitlines() if ln.startswith(PREFIX)]
    return scrub(" | ".join(kept))[:MAX_CHARS]


def step_results(local: dict, cloud: dict) -> str:
    """The result the report shows under a step's row: one text when both slots recorded the same, else both, labelled."""
    got_l, got_c = local.get("result", ""), cloud.get("result", "")
    if got_l == got_c:
        return got_l
    return f"local: {got_l or '-'} || cloud: {got_c or '-'}"


def render_row(row: dict, width: int) -> list[str]:
    """A report row as the table prints it, then its result (when it has one) on one indented line, so a green run's
    report keeps what the probes recorded."""
    head = (f"{row['step'].ljust(width)}  {row['local']:<14} {row['cloud']:<18} {row['verdict']}"
            + (f"  - {row['reason']}" if row["reason"] else ""))
    return [head, f"    result: {row['result']}"] if row.get("result") else [head]
