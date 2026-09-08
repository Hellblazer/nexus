---
allowed-tools: Bash
description: Index DEVONthink records into the T3 knowledge store through nx dt index, with catalog identity on the x-devonthink-item URI and a page-coverage check
---

# DEVONthink Index

!`nx command-context devonthink-index`

## Records to Index

$ARGUMENTS

**Targeted load**: parse the selector from `$ARGUMENTS` (a record UUID, a group path starting with `/`, or a smart-group name) and any `--collection` / `--allow-page-gap` / `--extractor` flags, then run, via the Bash tool, `nx command-context devonthink-index -- <selector> <flags>` with literal argv tokens. Never splice raw `$ARGUMENTS` into a shell-quoted line.

## Action

All data is pre-loaded above — no additional tool calls needed.

- Run the printed `nx dt index ...` line via the Bash tool, with `--collection knowledge__<subject>` naming an existing subject from the list above (a subject area, never a source app or a session). Omit `--collection` only to take the default `knowledge__dt-papers`.
- Read the summary line. `Indexed N record(s)` with no `failed` is done. `page-coverage failed` names the records and their missing pages: re-run those with `--extractor mineru`, or accept with `--allow-page-gap` when the pages are blank by design (cover, figures). `page coverage unverified` means the DEVONthink MCP was unreachable; report it as unverified, never as covered.
- Verify by content: `nx catalog show "<title>" --json` shows `source_uri: x-devonthink-item://<UUID>` and the DEVONthink url and year; a `search` in the target collection returns the document.
- Re-running the same selector is a no-op under the same tumbler; `--force` re-chunks in place.
