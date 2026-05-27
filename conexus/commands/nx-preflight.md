---
allowed-tools: Bash
description: Check that all conexus plugin dependencies are correctly installed and configured
disable-model-invocation: false
---

!`nx command-context nx-preflight -- "$ARGUMENTS"`

## Summary

Based on the preflight output above, produce a summary table:

| Dependency | Status | Action needed |
|-----------|--------|---------------|
| nx CLI | — | — |
| nx doctor | — | — |
| bd (beads) | — | — |
| uv | — | — |
| Node.js / npx | — | — |
| CLAUDE.md | — | — |

Fill in each row from the check results above. Use "PASS", "FAIL", or "WARN" for Status. Leave Action needed blank for passing checks; for failures/warnings, provide the install command or link.

If all checks pass: print "conexus plugin is ready"
If any check fails: print "Fix the above before using nx agents"
