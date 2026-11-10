# SPDX-License-Identifier: AGPL-3.0-or-later
"""The commit a CI board notice names (nexus-1d6pz.11.3).

Split from :mod:`nexus.mcp.channel`, whose size ceiling it would pass. The channel calls
:func:`ci_done_sha` for each board post and puts a result in the notice's meta.
"""
from __future__ import annotations

import json
import re

from nexus.db.t2.records import TupleRow

#: nexus-1d6pz.11.3: the CI board's result topic (one per repo and branch) and the only
#: sha a notice repeats: exactly 40 lowercase hex digits, as the conexus adapter writes it.
CI_DONE_TOPIC = re.compile(r"board/ci/[^/]+--done")
_SHA40 = re.compile(r"[0-9a-f]{40}")
#: A --done body is at most 1024 bytes (the board-ci template); a larger one is not parsed on
#: the event loop, and its notice names no sha.
_CI_DONE_BODY_MAX = 4096


def ci_done_sha(subspace: str, row: TupleRow) -> str | None:
    """nexus-1d6pz.11.3: the commit a ``board/ci/*--done`` row reports, carried in the
    notice's meta (``sha``; ``shas`` on a batch) so a session can tell which push a wake is
    for without a read. Meta only, never the notice text: the text stays built from
    identifiers the server controls (T2 nexus_rdr/211-decision-push-reference-2026-09-17).
    The row's ``sha`` dim when it is set (the adapter writes it once engine v0.1.184 carries
    the board-ci template, nexus-1d6pz.11.1), else the body's ``sha``. Notices are otherwise
    reference-only because a body is unattested, so nothing of it passes but a value that is
    exactly 40 lowercase hex digits; anything else gives None and the notice names no sha."""
    if not CI_DONE_TOPIC.fullmatch(subspace):
        return None
    dim = row.dims.get("sha") if isinstance(row.dims, dict) else None
    if isinstance(dim, str) and _SHA40.fullmatch(dim):
        return dim
    raw = row.body
    if not isinstance(raw, str) or len(raw) > _CI_DONE_BODY_MAX:
        return None
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    sha = body.get("sha") if isinstance(body, dict) else None
    return sha if isinstance(sha, str) and _SHA40.fullmatch(sha) else None


__all__: list[str] = ["CI_DONE_TOPIC", "ci_done_sha"]
