#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Wait until PyPI's SIMPLE index advertises a package version (nexus-r433b).

PyPI's upload API acknowledges a release BEFORE the CDN-backed simple index
serves it. Resolvers (uv, pip) read the SIMPLE index — not the JSON API — so
for a window measured at ~10-25 minutes on four consecutive conexus releases
(7.22.0, 7.23.0, 7.24.1, 7.25.0), any resolution pinned ``==``/``>=`` the
just-published version hard-fails with "no matching version" while
``https://pypi.org/pypi/<pkg>/json`` already reports it. Polling the JSON API
therefore proves nothing about installability; this script polls the
resolver-visible signal:

    GET <index-url>/<package>/
    Accept: application/vnd.pypi.simple.v1+json          (PEP 691)

and succeeds when the requested version appears in the payload's
``versions`` list.

Callers:
- ``.github/workflows/release.yml`` — between the PyPI publish and the
  GitHub-release step, so the announcement (and the ``.mcpb`` download it
  carries) never precedes installability.
- ``tests/e2e/fresh-install-mvv.sh --published X.Y.Z`` — so the post-publish
  shakedown waits out the window instead of failing once and normalizing a
  manual-rerun ritual.

Transient failures (non-200, network error, malformed payload) are treated
as "not yet" and polled through — the deadline is the only terminal failure.
Exit 0 = version served; exit 1 = deadline exceeded (fail loud, never a
silent pass).

Stdlib-only on purpose: release.yml runs it via bare ``python3`` and the
e2e shell gates must not need a synced venv for it.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_INDEX_URL = "https://pypi.org/simple"
SIMPLE_V1_JSON = "application/vnd.pypi.simple.v1+json"


def probe_versions(url: str, timeout: float = 10.0) -> list[str] | None:
    """One GET against the simple index. Returns the advertised versions,
    or None on any transient failure (treated as "not yet" by the caller)."""
    req = urllib.request.Request(
        url,
        headers={"Accept": SIMPLE_V1_JSON, "User-Agent": "conexus-release-gate"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        versions = payload["versions"]
    except (urllib.error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(versions, list):
        return None
    return [str(v) for v in versions]


def _numeric_prefix(v: str) -> tuple[int, ...]:
    """Cheap PEP 440 numeric-prefix extract: '5.0.1.dev0' -> (5, 0, 1).

    Same shape as ``mcpb/src/server.py``'s ``_parse_version``. Used only
    for the below-max fast-exit's CONSERVATIVE strict-less-than compare —
    an unparseable/ambiguous version yields () and never fast-exits.
    """
    parts: list[int] = []
    for chunk in v.split("."):
        digits = ""
        for c in chunk:
            if c.isdigit():
                digits += c
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def wait_for_version(
    package: str,
    version: str,
    index_url: str = DEFAULT_INDEX_URL,
    timeout_seconds: float = 1800.0,
    poll_seconds: float = 30.0,
    require_served: bool = False,
) -> int:
    url = f"{index_url.rstrip('/')}/{package}/"
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    while True:
        attempt += 1
        versions = probe_versions(url)
        if versions is not None and version in versions:
            print(
                f"OK: simple index serves {package}=={version} "
                f"(attempt {attempt})"
            )
            return 0
        if versions is None:
            status = "index unreachable or payload unreadable (transient)"
        else:
            newest = versions[-1] if versions else "<none>"
            status = f"index serves up to {newest}, not yet {version}"
            # Below-max fast exit: propagation lag only ever affects the
            # NEWEST release — an index already serving versions strictly
            # past the requested one will never gain it later, so there is
            # nothing to wait for. Exit 0 and let the caller's resolver
            # render its own (loud) verdict; this is how an operator typo
            # (`--published 7.9.0`) fails in seconds with uv's own error
            # instead of burning the full poll budget. Strictly-less-than
            # on confident numeric prefixes only; ambiguous parses keep
            # polling (conservative).
            wanted = _numeric_prefix(version)
            served_max = max(
                (_numeric_prefix(v) for v in versions), default=()
            )
            if wanted and served_max and wanted < served_max:
                if require_served:
                    # release.yml passes --require-served (substantive-critic
                    # finding, 2026-08-31): its next step CREATES the GitHub
                    # release — the announcement — and has no downstream
                    # check, so "absent and will never appear" must fail
                    # loud there, never proceed. Realistic trigger: the
                    # workflow_dispatch retry of an old tag after a newer
                    # version shipped, with that old upload missing from
                    # the index — proceeding would reopen the exact
                    # announce-before-installable bug this script closes.
                    print(
                        f"FAIL: index already serves up to {newest} "
                        f"(> {version}) and {package}=={version} is absent — "
                        "it will not appear by waiting, and --require-served "
                        "forbids proceeding without it. Verify the upload "
                        "actually landed on PyPI before creating the "
                        "announcement.",
                        file=sys.stderr,
                    )
                    return 1
                print(
                    f"NOT A PROPAGATION WAIT: index already serves up to "
                    f"{newest} (> {version}); {package}=={version} is absent "
                    "and will not appear by waiting. Proceeding — the "
                    "caller's resolver will report its own verdict."
                )
                return 0
        remaining = deadline - time.monotonic()
        if remaining <= poll_seconds:
            print(
                f"FAIL: PyPI simple index never served {package}=={version} "
                f"within {timeout_seconds:.0f}s ({attempt} attempts; last: {status}). "
                "The propagation window (~10-25 min measured, nexus-r433b) was "
                "exceeded — this is unusual; check https://status.python.org/ and "
                "re-run (the release workflow's workflow_dispatch retry path "
                "re-publishes safely via skip-existing).",
                file=sys.stderr,
            )
            return 1
        print(
            f"waiting on PyPI propagation: {status} "
            f"(attempt {attempt}, ~{remaining:.0f}s left, next poll in {poll_seconds:.0f}s)",
            flush=True,
        )
        time.sleep(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--package", default="conexus")
    parser.add_argument("--version", required=True)
    parser.add_argument("--index-url", default=DEFAULT_INDEX_URL)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument(
        "--require-served",
        action="store_true",
        help="fail (exit 1) instead of proceeding when the index is already "
        "past the requested version but does not serve it — for callers "
        "whose next step is an irreversible announcement (release.yml)",
    )
    args = parser.parse_args(argv)
    return wait_for_version(
        package=args.package,
        version=args.version,
        index_url=args.index_url,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
        require_served=args.require_served,
    )


if __name__ == "__main__":
    sys.exit(main())
