#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read the evidence batch.sh left per run and print one verdict row each.

    summarize.py <evidence-dir>

A run directory holds ``stream.jsonl`` (claude -p --output-format
stream-json), ``hook.log`` (one line per probe call), ``rc`` (the exit code
of ``timeout claude``), ``elapsed``, ``version.txt``, ``run.txt`` and
``settings.json``, plus ``ran.marker`` when the probed command
(``touch HOOKASK_PROBE.ran``, pre-approved by an allow rule in the ``rule``
runs) actually created its file.

Outcome vocabulary (``classify``):

* ``HUNG``          timeout killed the session (rc 124 or 137): it was waiting.
* ``NOT-EXERCISED`` the probe hook never fired, or claude reported on stderr
                    that it ignored the allow rule (untrusted workspace).
                    Says nothing about ``ask``.
* ``RAN``           hook fired and the command executed (ran.marker exists).
* ``DENIED``        hook fired, command did not run, and the stream shows a
                    refusal (an error tool_result or a permission_denials
                    entry).
* ``NO-ATTEMPT``    hook fired but nothing in the stream shows a refusal and
                    the command did not run.
* ``MISSING``       no evidence for the run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HUNG_RCS = {124, 137}


def _events(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.is_file():
        return out
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _flatten(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        )
    return ""


def _tool_results(events: list[dict[str, Any]]) -> list[tuple[bool, str]]:
    found: list[tuple[bool, str]] = []
    for ev in events:
        if ev.get("type") != "user":
            continue
        msg = ev.get("message") or {}
        blocks = msg.get("content")
        if not isinstance(blocks, list):
            continue
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                found.append((bool(b.get("is_error")), _flatten(b.get("content"))))
    return found


def classify(run_dir: Path) -> dict[str, Any]:
    """Return the facts and the outcome for one run directory."""
    row: dict[str, Any] = {"id": run_dir.name}
    if not (run_dir / "rc").is_file():
        row["outcome"] = "MISSING"
        return row
    rc = int((run_dir / "rc").read_text().strip() or -1)
    elapsed = (run_dir / "elapsed").read_text().strip() if (run_dir / "elapsed").is_file() else "?"
    hook_lines = (
        [x for x in (run_dir / "hook.log").read_text().splitlines() if x.strip()]
        if (run_dir / "hook.log").is_file()
        else []
    )
    events = _events(run_dir / "stream.jsonl")
    results = _tool_results(events)
    finals = [e for e in events if e.get("type") == "result"]
    final = finals[-1] if finals else {}
    # A background subagent's denial lands on an earlier result event than the
    # last one, so count them across every result.
    denials = [d for e in finals for d in (e.get("permission_denials") or [])]
    errors = [t for is_err, t in results if is_err]
    stderr_file = run_dir / "stderr.txt"
    stderr = stderr_file.read_text(errors="replace") if stderr_file.is_file() else ""
    row.update(
        rc=rc,
        elapsed_s=elapsed,
        version=(run_dir / "version.txt").read_text().strip() if (run_dir / "version.txt").is_file() else "?",
        hook_fired=len(hook_lines),
        callers=sorted({w.split("=", 1)[1] for ln in hook_lines for w in ln.split() if w.startswith("caller=")}),
        ran=(run_dir / "ran.marker").is_file(),
        allow_rule_ignored="permissions.allow" in stderr and "Ignoring" in stderr,
        permission_denials=len(denials),
        error_results=len(errors),
        refusal_text=(errors[0] if errors else "")[:300].replace("\n", " "),
        final_text=str(final.get("result", ""))[:80].replace("\n", " "),
        stderr_tail=stderr[-200:].replace("\n", " "),
    )
    if row["allow_rule_ignored"]:
        row["outcome"] = "NOT-EXERCISED"
    elif rc in HUNG_RCS:
        row["outcome"] = "HUNG"
    elif not hook_lines:
        row["outcome"] = "NOT-EXERCISED"
    elif row["ran"]:
        row["outcome"] = "RAN"
    elif denials or errors:
        row["outcome"] = "DENIED"
    else:
        row["outcome"] = "NO-ATTEMPT"
    return row


def main(argv: list[str]) -> int:
    root = Path(argv[1])
    dirs = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    if not dirs:
        print(f"summarize.py: no run directories under {root}", file=sys.stderr)
        return 2
    rows = [classify(d) for d in dirs]
    hdr = f"{'run':<14} {'outcome':<14} {'hook':>4} {'rc':>4} {'sec':>4} {'denials':>7} {'errs':>4}  caller / refusal text"
    print(hdr)
    for r in rows:
        print(
            f"{r['id']:<14} {r['outcome']:<14} {r.get('hook_fired', '-'):>4} {r.get('rc', '-'):>4} "
            f"{r.get('elapsed_s', '-'):>4} {r.get('permission_denials', '-'):>7} {r.get('error_results', '-'):>4}  "
            f"{','.join(r.get('callers', []))} | {r.get('refusal_text', '')}"
        )
    versions = sorted({r["version"] for r in rows if "version" in r})
    print(f"claude: {', '.join(versions)}")
    bad = [r["id"] for r in rows if r["outcome"] in {"MISSING", "NOT-EXERCISED"}]
    if bad:
        print(f"UNVERIFIED rows (no usable evidence): {', '.join(bad)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
