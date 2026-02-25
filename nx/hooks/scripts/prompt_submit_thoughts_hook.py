#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""UserPromptSubmit hook: re-inject saved sequential thinking chains after compaction.

Fires before Claude responds to each user message. Uses a lightweight sentinel
file (~/.config/nexus/thought-chain-pending) to avoid T2 overhead on every
prompt when there's nothing to recover.

Flow:
  1. PreCompact hook saves chain to T2 AND creates the sentinel file.
  2. This hook checks for the sentinel — fast no-op if absent.
  3. If sentinel present: fetch chain from T2, print to stdout (injected into
     Claude's context), overwrite T2 entry to consume it, remove sentinel.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SENTINEL = Path.home() / '.config' / 'nexus' / 'thought-chain-pending'


def _project_name() -> str:
    try:
        r = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return Path(r.stdout.strip()).name
    except Exception:
        pass
    return ''


def _fetch_chain(project: str) -> str:
    try:
        r = subprocess.run(
            ['nx', 'memory', 'get',
             '--project', f'{project}_active',
             '--title', 'sequential-thinking-chain.md'],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ''


def _consume(project: str) -> None:
    """Overwrite the T2 entry with a short-lived empty marker."""
    try:
        subprocess.run(
            ['nx', 'memory', 'put', 'CONSUMED',
             '--project', f'{project}_active',
             '--title', 'sequential-thinking-chain.md',
             '--ttl', '1h'],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def main() -> None:
    # Fast path: no sentinel means nothing was saved by PreCompact.
    if not SENTINEL.exists():
        sys.exit(0)

    project = _project_name()
    if not project:
        SENTINEL.unlink(missing_ok=True)
        sys.exit(0)

    chain = _fetch_chain(project)

    # Remove sentinel regardless of whether fetch succeeded.
    SENTINEL.unlink(missing_ok=True)

    if not chain or chain == 'CONSUMED':
        sys.exit(0)

    print('---')
    print('**Sequential thinking chain restored after context compaction.**')
    print('Resume from where the investigation left off.')
    print('')
    print(chain)
    print('---')
    print('')

    _consume(project)


if __name__ == '__main__':
    main()
