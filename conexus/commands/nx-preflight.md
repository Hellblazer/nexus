---
allowed-tools: Bash
description: Check that all conexus plugin dependencies are correctly installed and configured
disable-model-invocation: false
---

!`nx command-context nx-preflight`

## Summary

The preflight output above is the command's own gathered data when the `!` preamble ran; if it is missing or empty, run `nx command-context nx-preflight` via the Bash tool rather than proceeding on nothing. From it, produce a summary table:

| Dependency | Status | Action needed |
|-----------|--------|---------------|
| nx CLI | — | — |
| nx doctor | — | — |
| bd (beads) | — | — |
| uv | — | — |
| CLAUDE.md | — | — |

Fill in each row from the check results above. Use "PASS", "FAIL", or "WARN" for Status. Leave Action needed blank for passing checks; for failures/warnings, provide the install command or link.

If all checks pass: print "conexus plugin is ready"
If any check fails: print "Fix the above before using nx agents"
