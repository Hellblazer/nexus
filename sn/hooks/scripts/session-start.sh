#!/bin/bash

# sn SessionStart hook: remind main conversation about Serena + Context7
# SubagentStart injects full tool signatures; this is a compact reminder.
# The body lives in session-start-section.md, not a heredoc: a heredoc past
# 512 bytes deadlocks under bash 5.3 when macOS shrinks pipes
# (tests/hooks/test_heredoc_pipe_budget.py).

cat "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/session-start-section.md"
