#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Post a CI run's state to the ``board/ci-develop`` tuple topic (nexus-dotwy).

Sessions sharing a box used to wait on develop CI with ``gh run watch``, one
loop each; on 2026-09-26 several of them together tripped GitHub's secondary
rate limit and every Actions API call 403'd (T2
``nexus/github-api-usage-research-2026-09-26``). CI now publishes its own
state instead: ``ci.yml`` calls this script once when a develop run starts
(``--kind ci-pending``) and once when it ends (``--kind ci-verdict``), and a
session that wants the verdict subscribes to ``board/ci-develop``, so nothing
polls GitHub.

The tuple, per the ``board/<topic>`` template:

- ``keys``: ``{"topic": "ci-develop"}``
- ``dims``: ``{"from": "ci", "kind": "ci-pending" | "ci-verdict"}``
- ``nonce``: ``<sha>:<run id>:<attempt>:<kind>``, so a retried step lands on
  the same tuple instead of posting twice
- ``body``: compact JSON ``{"sha", "run", "attempt", "workflow",
  "conclusion", "failed", "url"}``; ``conclusion`` is ``pending``,
  ``success``, ``failure`` or ``cancelled``; ``failed`` names the jobs whose
  result was ``failure`` (or ``cancelled``), trimmed to fit the 1024-byte
  body cap.

Environment: ``NX_SERVICE_URL`` (the engine) and ``NX_BOARD_TOKEN`` (the
bearer). Stdlib only, so the runner needs no ``uv sync``.

Advisory, not a gate: a refused or failed post prints a GitHub ``::warning::``
annotation and exits 0, because a red board job on a green run would read as
a CI failure. A missing endpoint or token is also a warning; the board is
simply not written. Exit 2 is reserved for bad arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

TOPIC: str = "ci-develop"
SUBSPACE: str = f"board/{TOPIC}"
#: The board template's max_body_bytes (tuple_registry, board/<topic>).
MAX_BODY_BYTES: int = 1024
CONCLUSIONS: frozenset[str] = frozenset({"pending", "success", "failure", "cancelled"})


def verdict_from_results(results: dict[str, str]) -> tuple[str, list[str]]:
    """Fold ``needs.<job>.result`` values into one conclusion.

    ``failure`` wins over ``cancelled``, which wins over success; ``skipped``
    counts as success, as it does for branch protection (the doc-only fast
    lane skips jobs on purpose). Returns ``(conclusion, failed_job_names)``.
    """
    failed = sorted(j for j, r in results.items() if r == "failure")
    cancelled = sorted(j for j, r in results.items() if r == "cancelled")
    if failed:
        return "failure", failed + cancelled
    if cancelled:
        return "cancelled", cancelled
    return "success", []


def build_body(*, sha: str, run: str, attempt: str, workflow: str,
               conclusion: str, failed: list[str], url: str) -> str:
    """Compact JSON body, trimming ``failed`` until it fits the cap."""
    names = list(failed)
    while True:
        body = json.dumps(
            {"sha": sha, "run": run, "attempt": attempt, "workflow": workflow,
             "conclusion": conclusion, "failed": names, "url": url},
            separators=(",", ":"),
        )
        if len(body.encode()) <= MAX_BODY_BYTES or not names:
            return body
        names = names[:-1]


def post(base_url: str, token: str, payload: dict, timeout: float = 20.0) -> str:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/tuples/out",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https endpoint from config
        return str(json.loads(resp.read().decode()).get("id", ""))


def _warn(msg: str) -> int:
    print(f"::warning title=board/{TOPIC}::{msg}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Post a CI run's state to board/ci-develop.")
    ap.add_argument("--kind", required=True, choices=["ci-pending", "ci-verdict"])
    ap.add_argument("--sha", required=True)
    ap.add_argument("--run", required=True, help="GitHub run id")
    ap.add_argument("--attempt", default="1")
    ap.add_argument("--workflow", default="CI")
    ap.add_argument("--url", default="")
    ap.add_argument("--results", default="{}",
                    help="JSON object of job -> result (toJSON of needs, reduced)")
    args = ap.parse_args(argv)

    if args.kind == "ci-pending":
        conclusion, failed = "pending", []
    else:
        try:
            raw = json.loads(args.results)
        except json.JSONDecodeError as exc:
            print(f"--results is not JSON: {exc}", file=sys.stderr)
            return 2
        # Accept both {"job": "success"} and toJSON(needs)'s
        # {"job": {"result": "success", "outputs": {...}}}.
        results = {k: (v.get("result", "") if isinstance(v, dict) else str(v))
                   for k, v in raw.items()}
        if not results:
            print("--results is empty: a verdict needs at least one job result",
                  file=sys.stderr)
            return 2
        conclusion, failed = verdict_from_results(results)

    base_url = os.environ.get("NX_SERVICE_URL", "").strip()
    token = os.environ.get("NX_BOARD_TOKEN", "").strip()
    if not base_url or not token:
        return _warn("NX_SERVICE_URL or NX_BOARD_TOKEN is not set; board not written")

    body = build_body(sha=args.sha, run=args.run, attempt=args.attempt,
                      workflow=args.workflow, conclusion=conclusion,
                      failed=failed, url=args.url)
    payload = {
        "subspace": SUBSPACE,
        "keys": {"topic": TOPIC},
        "dims": {"from": "ci", "kind": args.kind},
        "nonce": f"{args.sha}:{args.run}:{args.attempt}:{args.kind}",
        "body": body,
    }
    try:
        tuple_id = post(base_url, token, payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200].replace(token, "<redacted>")
        return _warn(f"POST /v1/tuples/out refused: HTTP {exc.code} {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _warn(f"POST /v1/tuples/out failed: {type(exc).__name__}: {exc}")
    print(f"{SUBSPACE} {args.kind} {conclusion} sha={args.sha} tuple={tuple_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
