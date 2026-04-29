# DEVONthink In-App Scripts for `nx dt index`

DEVONthink can host AppleScript files in its own toolbar, Scripts
menu, and contextual menu. This puts a one-click "send to Nexus"
action right next to the records you want to index, with no Claude
Code or terminal detour. The smart-rule path
([`devonthink-smart-rules.md`](devonthink-smart-rules.md)) covers
automatic indexing on import; this doc covers user-triggered actions
that operate on the current selection or current group.

`nx dt install-scripts` ships and installs the scripts; this doc
explains what each one does, where it lives, and how to use it.

## What ships

Three AppleScript files (DT4 only):

| File | Where it lives | What it does |
|------|----------------|--------------|
| `Index Selection in nx` | Toolbar + Scripts menu | Calls `nx dt index --selection` for whatever's highlighted in the front viewer window. |
| `Index Selection in nx (Knowledge)` | Scripts menu only | Prompts for a corpus name, then calls `nx dt index --selection --collection knowledge__<name>`. |
| `Index Current Group in nx` | Toolbar + Scripts menu | Recursively walks the current group, builds one `nx dt index --uuid <U> --uuid <V> …` call per group. |

All three share the same housekeeping: they probe a small set of
common `nx` install paths (Homebrew, uv tool, pipx), background the
shell call so DT's UI stays responsive, and log to
`~/Library/Logs/nexus-dt-scripts.log`. A `tail -f` of that log is
the canonical debugging surface; DT's UI surfaces only an exit
status.

## Install

```bash
# Default: into both Toolbar/ and Menu/.
nx dt install-scripts

# Toolbar buttons only.
nx dt install-scripts --target toolbar

# Menu items only.
nx dt install-scripts --target menu

# Preview without writing.
nx dt install-scripts --dry-run
```

The verb writes into
`~/Library/Application Scripts/com.devon-technologies.think/<subdir>/`
where DT4 expects them.

After install, **quit and reopen DEVONthink**. Toolbar scripts only
become draggable in `View > Customize Toolbar…` after a restart.
Menu scripts are picked up on the next menu open.

## Use

### From the toolbar

1. Open `View > Customize Toolbar…` in any DEVONthink window.
2. Drag `Index Selection in nx` and/or `Index Current Group in nx`
   onto the toolbar.
3. Click the toolbar button. A notification reports the record count
   and that the index call has been backgrounded.

### From the Scripts menu

1. DEVONthink's own Scripts menu sits to the left of Help in the
   menu bar (a script icon, no text).
2. Pick `Index Selection in nx`, `Index Selection in nx
   (Knowledge)`, or `Index Current Group in nx`.

For the `(Knowledge)` variant, you'll get a `display dialog` asking
for the bare collection name (the `knowledge__` prefix is added
automatically). Examples:

- type `papers` → records land in `knowledge__papers`
- type `delos` → records land in `knowledge__delos`

Cancel the dialog to abort with no side effects.

### "Selection" vs "current group"

DEVONthink's `selection` only covers records highlighted in the item
list. A group selected in the Navigate sidebar is **not** part of
the selection; it sets the front window's root. The two scripts are
the two cases:

- `Index Selection in nx` → use after multi-selecting in the item list.
- `Index Current Group in nx` → use after picking a group in the
  sidebar; the script walks every record under it.

`Index Current Group in nx` recurses into subgroups. Smart groups
are skipped (they're virtual collections, not durable records).

## Verify a run

```bash
tail -f ~/Library/Logs/nexus-dt-scripts.log
```

Each invocation writes one line announcing the action and the record
count, then `nx dt index`'s own stdout/stderr. A successful run
ends with `nx dt`'s summary line:

```
[Tue Apr 29 12:34:56 PDT 2026] Index Selection: 3 record(s)
indexed: 3 / 3, skipped: 0, stamped: 3 / 3
```

`stamped: N / M` reports the catalog `source_uri = x-devonthink-item://<UUID>`
stamping success rate. A miss means the catalog entry was created but
the DT identity wasn't attached; rerun `nx catalog update --source-uri`
on that path to recover.

## Troubleshooting

**"nx not found. See log."**
The script's path probe couldn't find `nx` in any of
`/opt/homebrew/bin/nx`, `/usr/local/bin/nx`, `~/.local/bin/nx`. Run
`which nx` and either symlink the binary into one of those paths, or
edit the installed `.applescript` file's `findNxBinary` handler to
add your install path.

**Toolbar button does nothing.**
Most often: you installed the script but didn't restart DEVONthink.
Quit DT (cmd-Q, not just close the window), reopen it, then re-drag
the script onto the toolbar via `View > Customize Toolbar…`.

**`Voyage credential missing` in the log.**
Same root cause as the smart-rule case: AppleScript's `do shell
script` runs with a bare environment, so a `VOYAGE_API_KEY` set in
`.zshrc` is invisible. Put the key in
`~/.config/nexus/credentials.json` instead; `nx` reads that
regardless of shell env.

**Script says "Group is empty" but I can see records in the group.**
"Current group" is read from the front window only. If you triggered
the script with no DT window focused, the call falls back to the
front window's root, which may be a different group. Click into a
record in the intended group first, then trigger the script.

## Uninstall

```bash
nx dt install-scripts --uninstall
```

Removes every shipped script from both `Toolbar/` and `Menu/`. The
operation is idempotent: running it on a clean tree exits 0 with
"0 removed".

To remove only one slot, pass `--target toolbar` or `--target menu`.

## Editing a shipped script

The installed `.applescript` files are plain text. To customize one
(e.g. add your install path to the `findNxBinary` handler), edit
the file in place at
`~/Library/Application Scripts/com.devon-technologies.think/Menu/<name>.applescript`
and re-launch DEVONthink. Re-running `nx dt install-scripts` without
`--force` will see the existing file and prompt before overwriting,
so your edits survive a casual reinstall.

To restore the shipped version, run `nx dt install-scripts --force`.

## Cross-references

- CLI reference for `nx dt install-scripts` and the underlying
  `nx dt index` / `nx dt open` verbs:
  [`docs/cli-reference.md § nx dt`](cli-reference.md#nx-dt).
- Hands-off auto-indexing on import / tag / move: smart-rule recipe
  in [`docs/devonthink-smart-rules.md`](devonthink-smart-rules.md).
- Design rationale and acceptance criteria for the `nx dt` CLI:
  [RDR-099](rdr/rdr-099-devonthink-integration.md).
