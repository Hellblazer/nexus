#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Locked Rev 4 8-pattern divergence-language scan for
divergence-language-guard.sh (RDR-065 Gap 2).

Prints one "  line N: <text>" row per hit; empty output means clean.
Lives in a sibling file rather than a heredoc because bash 5.3 pipes
heredoc bodies and a >512-byte body deadlocks when macOS degrades pipe
buffers (bead nexus-2gcqk; tests/hooks/test_heredoc_pipe_budget.py).
"""
import re
import sys

bank = re.compile(
    r'divergence|workaround|limitation|deferred|follow-up\s+RDR|'
    r'Phase\s+\d+\s+(deferred|required)|out\s+of\s+scope|not\s+in\s+scope',
    re.IGNORECASE,
)
results = []
try:
    path = sys.argv[1]
    with open(path, encoding='utf-8', errors='replace') as f:
        for i, line in enumerate(f, 1):
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            if stripped.startswith('|') and stripped.endswith('|'):
                continue
            if bank.search(stripped):
                results.append(f"  line {i}: {stripped[:120]}")
except Exception:
    pass
for r in results:
    print(r)
