#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Batch-label taxonomy topics via a persistent claude tmux session.

Reuses a single claude interactive session to avoid per-call startup
and system-prompt token overhead. Sends batches of 20 topics, parses
numbered responses, and commits labels to T2.

Usage:
    # Start the labeler session (once):
    tmux new-session -d -s labeler
    tmux send-keys -t labeler "claude --model haiku" Enter

    # Run the batch labeler:
    uv run python scripts/batch-label-taxonomy.py

    # Or specify batch size and session name:
    uv run python scripts/batch-label-taxonomy.py --batch 30 --session labeler
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path


def _tmux_send_multiline(session: str, text: str) -> None:
    """Send multiline text to tmux using load-buffer to avoid paste issues."""
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(text)
        f.flush()
        tmp = f.name

    try:
        # Load text into tmux buffer, then paste it, then press Enter
        subprocess.run(["tmux", "load-buffer", tmp], check=True, capture_output=True)
        subprocess.run(["tmux", "paste-buffer", "-t", session], check=True, capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", session, "", "Enter"], check=True, capture_output=True)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _tmux_capture(session: str) -> str:
    """Capture the current tmux pane content."""
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", session, "-p", "-S", "-100"],
        capture_output=True, text=True,
    )
    return result.stdout


def _wait_for_sentinel(session: str, sentinel: str, timeout: float = 90) -> str:
    """Wait until sentinel appears in pane capture, return content."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        content = _tmux_capture(session)
        if sentinel in content:
            return content
        time.sleep(1)
    raise TimeoutError(f"Timeout waiting for {sentinel}")


def _parse_labels(content: str, expected: int) -> list[str | None]:
    """Parse numbered labels from claude's response."""
    results: list[str | None] = [None] * expected
    for line in content.splitlines():
        line = line.strip()
        # Match "1. Label text" or "⏺ 1. Label text"
        m = re.match(r"^[⏺\s]*(\d+)\.\s+(.+)$", line)
        if m:
            idx = int(m.group(1)) - 1
            label = m.group(2).strip().strip('"').strip("'")
            if 0 <= idx < expected and 3 <= len(label) <= 60:
                results[idx] = label
    return results


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Batch-label taxonomy via tmux claude session")
    parser.add_argument("--batch", type=int, default=20, help="Topics per batch (default: 20)")
    parser.add_argument("--session", default="labeler", help="tmux session name (default: labeler)")
    parser.add_argument("--dry-run", action="store_true", help="Print labels without committing")
    args = parser.parse_args()

    # Verify tmux session exists
    try:
        _tmux_capture(args.session)
    except Exception:
        print(f"tmux session '{args.session}' not found. Start it with:")
        print(f"  tmux new-session -d -s {args.session}")
        print(f'  tmux send-keys -t {args.session} "claude --model haiku" Enter')
        sys.exit(1)

    # Load pending topics
    from nexus.commands.taxonomy_cmd import _T2Database, _default_db_path

    with _T2Database(_default_db_path()) as db:
        topics = db.taxonomy.get_unreviewed_topics(limit=5000)
        if not topics:
            print("No pending topics.")
            return

        print(f"{len(topics)} pending topics, batch size {args.batch}", flush=True)

        labeled = 0
        failed = 0
        start = time.monotonic()

        for batch_start in range(0, len(topics), args.batch):
            batch = topics[batch_start : batch_start + args.batch]
            batch_num = batch_start // args.batch + 1
            total_batches = (len(topics) + args.batch - 1) // args.batch

            # Build prompt
            lines = []
            for i, t in enumerate(batch, 1):
                terms = json.loads(t["terms"]) if t.get("terms") else []
                doc_ids = db.taxonomy.get_topic_doc_ids(t["id"], limit=3)
                doc_names = [d.split("/")[-1].split(":")[0][:25] for d in doc_ids]
                lines.append(
                    f"{i}. terms=[{', '.join(terms[:5])}] docs=[{', '.join(doc_names)}]"
                )

            sentinel = f"END_BATCH_{batch_num:04d}"
            prompt = (
                "Label each topic in 3-5 words. Numbered labels only. "
                f"End your reply with: {sentinel}\n"
                + "\n".join(lines)
            )

            # Send to claude and wait for sentinel in response
            _tmux_send_multiline(args.session, prompt)
            try:
                content = _wait_for_sentinel(args.session, sentinel, timeout=90)
            except TimeoutError:
                print(f"  batch {batch_num}: timeout")
                failed += len(batch)
                continue

            labels = _parse_labels(content, len(batch))

            # Apply labels
            batch_labeled = 0
            for t, label in zip(batch, labels):
                if label:
                    if not args.dry_run:
                        db.taxonomy.rename_topic(t["id"], label)
                    batch_labeled += 1
                    labeled += 1
                else:
                    failed += 1

            elapsed = time.monotonic() - start
            rate = labeled / elapsed if elapsed > 0 else 0
            print(
                f"  [{batch_num}/{total_batches}] "
                f"{batch_labeled}/{len(batch)} labeled "
                f"({labeled} total, {rate:.1f}/s)"
            )
            sys.stdout.flush()

        elapsed = time.monotonic() - start
        print(f"\nDone: {labeled} labeled, {failed} failed, {elapsed:.0f}s", flush=True)


if __name__ == "__main__":
    main()
