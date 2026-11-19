# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine release the cloud runs, and the one each wire-ledger entry needs (nexus-h4hge).

The pre-tag cloud leg (tests/e2e/lib/cloud_leg.py) skips a ledger route whose engine tag is later than the cloud's
engine, read from ``release_version`` of the edge's unauthenticated ``GET /version``. Standard library only.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any


class EngineUnreadable(Exception):
    """GET /version gave no X.Y.Z release; cloud_leg turns this into a refusal."""


_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
_ENGINE_TAG_RE = re.compile(r"engine-service-v(\d+)\.(\d+)\.(\d+)")


def parse_engine_version(text: str) -> tuple[int, int, int] | None:
    """``X.Y.Z`` (an optional leading ``v``) to a tuple; anything else, a ``-SNAPSHOT`` included, is ``None``."""
    m = _VERSION_RE.fullmatch(text.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def entry_engine(entry: Any) -> tuple[int, int, int] | None:
    """The engine release an entry's routes need: its ``engine tag`` (Unshipped) or its ``engine half`` (Shipped), as
    parse_ledger reads them. ``None`` for an entry that names no engine tag."""
    m = _ENGINE_TAG_RE.fullmatch(entry.engine_tag or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def version_text(v: tuple[int, int, int]) -> str:
    return ".".join(str(p) for p in v)


def read_cloud_engine(service_url: str, timeout: float = 15.0) -> str:
    """The engine release the cloud runs, as ``X.Y.Z``: ``release_version`` of ``GET <service_url>/version``, the
    unauthenticated handshake the edge passes through (no credential is sent). Anything else (a failed request, a body
    that is not a JSON object, a missing or unparseable version) refuses: an engine the leg cannot name is never read as
    one that carries a route."""
    url = service_url.rstrip("/") + "/version"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "nexus-cloud-leg"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - the https edge check-target pinned
            raw = resp.read()
    except (urllib.error.URLError, OSError) as exc:
        raise EngineUnreadable(f"could not read the cloud engine from GET /version: {type(exc).__name__}: {exc}") from exc
    try:
        body = json.loads(raw)
    except ValueError:
        raise EngineUnreadable("could not read the cloud engine: GET /version did not answer JSON") from None
    rv = body.get("release_version") if isinstance(body, dict) else None
    parsed = parse_engine_version(rv) if isinstance(rv, str) else None
    if parsed is None:
        raise EngineUnreadable(f"could not read the cloud engine: GET /version answered release_version {rv!r}, not X.Y.Z")
    return version_text(parsed)
