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
