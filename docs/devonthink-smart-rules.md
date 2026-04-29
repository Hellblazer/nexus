# DEVONthink Smart Rules and Folder Actions for `nx dt index`

DEVONthink can fire AppleScript when records arrive, change, or match
a saved query (its **Smart Rules** feature). Combined with `nx dt
index --uuid`, this gives you a hands-off path from "I drop a PDF
into DT" to "the PDF is indexed in Nexus" without ever opening a
terminal.

This recipe covers the v1 smart-rule pattern, the macOS folder-action
alternative, error handling, and the concurrency caveat that bites
batch imports.

## Why a smart rule

`nx dt index --selection` is great when you've just clicked through a
batch of new records and want to ingest the lot. A smart rule is for
the other 80% of the time: a paper drops into your Inbox via the DT
clipper, a ZIP arrives in a watched folder, an email rule files an
attachment. You don't want to remember to run `nx dt index` every
time. The smart rule fires automatically.

## v1 recipe: smart rule + `nx dt index --uuid`

### 1. The script

In Script Editor (or any text editor that saves `.scpt`), create
**Index in Nexus.scpt**:

```applescript
on performSmartRule(theRecords)
  repeat with r in theRecords
    try
      do shell script ¬
        "/usr/local/bin/nx dt index --uuid " ¬
        & quoted form of (uuid of r as string) ¬
        & " >> ~/Library/Logs/nexus-smart-rule.log 2>&1 &"
    on error errMsg
      do shell script ¬
        "echo \"[$(date)] " & errMsg & " (uuid=" ¬
        & (uuid of r as string) & ")\" " ¬
        & ">> ~/Library/Logs/nexus-smart-rule.log"
    end try
  end repeat
end performSmartRule
```

Key points:

- `quoted form of` shells the UUID safely (DT UUIDs can't contain
  shell metacharacters, but it's free defence).
- The trailing `&` backgrounds each invocation so the smart rule
  returns control to DT immediately. Without it, DT blocks the UI
  thread for the duration of the index call (Voyage embed + ChromaDB
  upsert; commonly 1-3 seconds per PDF).
- `>> ~/Library/Logs/nexus-smart-rule.log 2>&1` captures both stdout
  and stderr to a known location. DT's smart-rule UI shows only the
  AppleScript exit status; without a log file, debugging a failing
  rule is a guessing game.
- The `try` block catches AppleScript-side errors (DT not running,
  UUID malformed, etc.) and writes them to the same log so you can
  triage the rule itself the same way you triage `nx` failures.

Replace `/usr/local/bin/nx` with the absolute path your `nx` is
installed at. The example placeholder is wrong for most modern Mac
setups. Common locations:

| Install method | Path |
| --- | --- |
| `uv tool install conexus` | `~/.local/bin/nx` |
| Homebrew (Apple Silicon) | `/opt/homebrew/bin/nx` |
| Homebrew (Intel) | `/usr/local/bin/nx` |
| pipx | `~/.local/bin/nx` |

Run `which nx` from your shell once and paste the result. AppleScript's
`PATH` is bare, so the absolute path avoids the "command not found"
silent failure when you run `nx` from a `.zshrc`-style shell.

### 2. Save location

DT looks for smart-rule scripts under:

```
~/Library/Application Scripts/com.devon-technologies.think/Smart Rules/
```

Save **Index in Nexus.scpt** there. The directory may not exist yet;
create it with `mkdir -p` if so.

### 3. Wire the rule to a trigger

In DEVONthink: `Settings > Smart Rules > +`.

- **Search in**: pick a group (e.g. `Inbox`, or a named project group).
- **Match**: any predicate that fires when a record arrives. Common
  patterns:
  - `Date Added is today` for a daily-import rule.
  - `Kind is PDF Document` to scope to PDFs only.
  - `Tag matches "auto-index"` to hand-control with a tag.
- **Perform the following actions**:
  - Set the action to `Run Script` and pick **Index in Nexus.scpt**.

Save the rule. DT fires the smart rule whenever a matching record
appears or changes; you can also right-click the rule and pick **Run
Now** to back-fill existing records.

### 4. Verify

Drop a fresh PDF into the watched group. Within a few seconds:

```bash
tail -f ~/Library/Logs/nexus-smart-rule.log
```

should show the `nx dt index --uuid <UUID>` invocation's stdout. Once
the indexer finishes, confirm the catalog has the entry:

```bash
nx catalog list --json \
  | jq '.[] | select(.source_uri | startswith("x-devonthink-item://"))'
```

`nx catalog list` has no built-in `--source-uri-prefix` flag in v1; the
JSON pipe is the canonical query path. The newest entry (last line of
the jq output) should match the PDF you dropped.

## Error handling

The script writes errors to `~/Library/Logs/nexus-smart-rule.log`.
Treat it like any other application log: `tail` it during debugging,
rotate it occasionally with `logrotate` or by hand. A few specific
failures and how to spot them:

- **`nx: command not found`**: the `do shell script` PATH didn't
  include the directory holding `nx`. Use the absolute path in the
  script.
- **`DEVONthink is not running`**: DT crashed or quit between firing
  the rule and the `do shell script` invocation. Rare in practice
  but logged cleanly. Re-fire the rule via `Run Now` after restart.
- **`Voyage credential missing`**: `nx` couldn't reach Voyage AI.
  The smart rule's invocation has the same env as DT's runtime, so
  if you set `VOYAGE_API_KEY` in `.zshrc`, it WON'T be visible.
  Either add it to `~/.config/nexus/credentials.json` (which `nx`
  reads regardless of shell env) or invoke `nx` via a wrapper
  script that sources the env first.
- **ChromaDB write quota exceeded**: rare for single-record imports.
  Batch imports of `>5` records can hit
  `MAX_CONCURRENT_WRITES = 10` if multiple smart rules race on the
  same collection. See § Concurrency caveat below.

For an interactive trace, change the script to log the command being
run before invoking it:

```applescript
do shell script ¬
  "echo \"[$(date)] indexing " & (uuid of r as string) ¬
  & "\" >> ~/Library/Logs/nexus-smart-rule.log; " ¬
  & "/usr/local/bin/nx dt index --uuid " ¬
  & quoted form of (uuid of r as string) ¬
  & " >> ~/Library/Logs/nexus-smart-rule.log 2>&1 &"
```

## Concurrency caveat

The trailing `&` in `do shell script` backgrounds each `nx dt index`
invocation. For trickle-feed ingest (one PDF every few seconds), this
is exactly right; for a bulk import (you drag 20 PDFs in at once), it
spawns 20 parallel `nx` processes that race on the same T3 ChromaDB
collection.

ChromaDB's per-collection quota is `MAX_CONCURRENT_WRITES = 10` (see
`src/nexus/db/chroma_quotas.py`). A 20-record bulk import will succeed
because the underlying upserts batch internally, but you'll see
`429`-style warnings in the log if your network is slow and the
queues stack up.

Two mitigations:

1. **For known-large batches, prefer `--selection`**. After the bulk
   import finishes, multi-select the new records in DT and run
   `nx dt index --selection`. The single `nx` process serializes the
   embed + upsert, avoiding the contention entirely.
2. **Add a per-rule throttle predicate**. Set the smart rule's match
   to a tag like `auto-index` and apply the tag manually after a
   bulk-import settles. The rule then fires for one record at a time
   as you tag them.

## Folder-action alternative

If you'd rather have macOS itself fire on file arrivals (DT-independent),
attach a Folder Action to a watched directory:

1. In Finder, right-click the folder, `Services > Folder Actions
   Setup…`.
2. Pick **DEVONthink - Import.scpt** (DT bundles this) so files are
   imported into your inbox automatically.
3. Add a smart rule on the import target group (or on a tag the
   import script applies) that runs **Index in Nexus.scpt** as
   above.

This gives you a two-step pipeline: macOS detects the file, DT
imports it and assigns metadata, then the smart rule indexes it into
Nexus. The handoffs are loosely coupled; if any stage fails, the
other stages keep working.

## Cross-references

- [RDR-099](rdr/rdr-099-devonthink-integration.md): the design
  decision behind `nx dt`.
- [`tests/e2e/devonthink-manual.md`](../tests/e2e/devonthink-manual.md):
  the manual smoke runbook used to verify the full surface.
- DT's smart-rule reference:
  `Help > Documentation > Automation > Smart Rules`.
