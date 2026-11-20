# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every repo harness that registers a collection sends ``model_version`` (RDR-229 P2.5 D1).

Since D1 the engine answers 400 to ``POST /v1/catalog/collections/upsert`` when the request
would create a new row with a blank or absent ``model_version``. The Python client always sends
one (``register_collection`` defaults to ``"v1"``), but shell gates and spike scripts build the
JSON body by hand: ``service/native-smoke.sh``, ``tests/e2e/local-service-gate.sh`` and
``tests/e2e/dual-embedder-gate.sh`` all registered without it, and each would have turned a
release leg red. This scan finds every line that SENDS the request and requires
``model_version`` in the lines that build its body.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO = Path(__file__).resolve().parents[1]

#: Where hand-built request bodies live. The Python client under src/nexus goes through
#: register_collection, which always sends a version, so it is not scanned.
SCANNED_GLOBS: tuple[str, ...] = ("service/*.sh", "tests/e2e/**/*.sh", "scripts/**/*.sh", "scripts/**/*.py")

#: A line that sends the request: a shell request helper or curl, or a Python post call.
_SEND = re.compile(r"collections/upsert")
_SENDER = re.compile(r"(_request\b|\bcurl\b|\.post\(|_post\(|urlopen|requests\.)")

#: How many lines above the send to look for the body's model_version (a shell body is built first),
#: and below it (a Python call passes its dict literal after the path).
LOOKBACK = 12
LOOKAHEAD = 4


def unversioned_sends(text: str) -> list[int]:
    """1-based line numbers of upsert sends whose preceding body lines name no model_version."""
    lines = text.splitlines()
    out: list[int] = []
    for i, line in enumerate(lines):
        if not _SEND.search(line) or line.lstrip().startswith("#"):
            continue
        window = lines[max(0, i - LOOKBACK) : i + 1 + LOOKAHEAD]
        if not any(_SENDER.search(w) for w in lines[max(0, i - 2) : i + 1]):
            continue
        if not any("model_version" in w for w in window):
            out.append(i + 1)
    return out


def _scanned() -> list[Path]:
    seen: dict[Path, None] = {}
    for g in SCANNED_GLOBS:
        for p in sorted(REPO.glob(g)):
            if p.is_file():
                seen[p] = None
    return list(seen)


def test_every_harness_upsert_sends_model_version() -> None:
    files = _scanned()
    sends = 0
    bad: list[str] = []
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        n = sum(1 for line in text.splitlines() if _SEND.search(line) and not line.lstrip().startswith("#"))
        sends += n
        for ln in unversioned_sends(text):
            bad.append(f"{p.relative_to(REPO)}:{ln}")
    # Non-vacuity: the three shell gates above send this request today.
    assert sends >= 4, f"scanned {len(files)} files and found only {sends} upsert lines; the scan reads too little"
    assert not bad, "collection upsert without model_version (a 400 since RDR-229 D1): " + ", ".join(bad)


def test_the_scan_catches_a_body_without_model_version() -> None:
    body = (
        "BODY=\"$(python -c \"import json;print(json.dumps({'name':'x','embedding_model':'m'}))\")\"\n"
        "smoke_request POST /v1/catalog/collections/upsert \"$BODY\"\n"
    )
    assert unversioned_sends(body) == [2]
    fixed = body.replace("'m'}", "'m','model_version':'v1'}")
    assert unversioned_sends(fixed) == []
