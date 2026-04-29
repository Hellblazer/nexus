# DEVONthink Manual Smoke (RDR-099)

This document captures the **manual** smoke needed to verify the
DEVONthink-side acceptance criteria for [RDR-099](../../docs/rdr/rdr-099-devonthink-integration.md).
Cross-platform CI exercises every fake-helper code path, but a few ACs
require live DEVONthink state that's tedious to mock end-to-end (smart
groups with non-root `search group`, multi-database tag iteration,
recursive group walks, the actual `open` URL handler). Run this
playbook once on macOS before merging any v1 RDR-099 PR.

The companion **gated** integration suite lives at
`tests/test_devonthink_live.py` (RDR-099 P4.1, bead `nexus-mv0p.6`).
That suite reuses the same env vars and fixtures defined here, so
authoring this doc first is a prerequisite for the live suite.

## Required environment

```bash
# Toggle for the gated live-DT pytest suite; without this, the
# darwin-only tests are skipped even on macOS.
export NEXUS_DT_LIVE=1

# Per-AC fixtures (set after creating them in DT; see § Fixtures below).
export NEXUS_DT_TEST_UUID="8EDC855D-213F-40AD-A9CF-9543CC76476B"
export NEXUS_DT_TEST_TAG="nexus-test"
export NEXUS_DT_TEST_GROUP="/AI/2025"
export NEXUS_DT_TEST_SMART_GROUP="Recent Nexus Test PDFs"

# Comma-separated DT databases that carry the test tag (≥2 needed
# for the multi-database AC). The tag must be applied to ≥1 record
# in each database.
export NEXUS_DT_TEST_DATABASES="Inbox,NexusTest"
```

Where to set them: a `.envrc` in the repo root if you use `direnv`, or
prepend each `nx dt …` invocation. The gated pytest suite reads them
via `os.environ`.

## Fixtures (run once)

Create these fixtures in DT before running the smoke. They underwrite
every AC below; without them most tests have nothing to find.

### 1. A second open database (for the multi-DB tag AC)

In DT: `File > New Database…`, name it `NexusTest`, save it next to
your default library. Confirm both `Inbox` and `NexusTest` show up
under the sidebar's *Open Databases* section. Either name works for
the smoke as long as `NEXUS_DT_TEST_DATABASES` matches.

### 2. A test tag in ≥2 databases (for `--tag` multi-DB AC)

Drag any one PDF into `Inbox`, right-click, `Tags > Add…`, type
`nexus-test`. Repeat for at least one PDF inside `NexusTest`. The tag
name must match `NEXUS_DT_TEST_TAG`.

### 3. A nested group (for `--group <path>` AC)

In `NexusTest` (or `Inbox`), create a parent group `AI`, then a child
group `2025` inside it. Drag at least 3 PDFs into `/AI/2025` and at
least 1 PDF into `/AI/`. The recursive walk AC checks that records
under the child group are found via the parent path.

### 4. A smart group with a non-root `search group` (for `--smart-group` AC)

In `NexusTest`, create a sub-group `Library/Recent`. Then create a
smart group `Recent Nexus Test PDFs` whose `search group` is set to
`Library/Recent` (NOT the database root) and whose predicates match
PDFs (`Kind is PDF Document`, `Date Added is within last 30 days`).
Drop 2–3 PDFs into `Library/Recent`.

The point of this fixture is to prove that the helper honours the
user-authored `search group` rather than collapsing to root; the
test fails if it returns PDFs from outside `Library/Recent`.

### 5. A known UUID (for the `--uuid` AC and `nx dt open` smoke)

Right-click any PDF in DT, `Edit > Copy Item Link`, paste it into a
note. The link is `x-devonthink-item://<UUID>`. Set
`NEXUS_DT_TEST_UUID` to that UUID.

## Per-AC manual repros

Each AC corresponds to a bullet in [RDR-099 § Acceptance criteria](../../docs/rdr/rdr-099-devonthink-integration.md#acceptance-criteria).

### AC-1: `nx dt index --selection`

1. In DT, multi-select 3–5 PDFs (Cmd-click).
2. `nx dt index --selection --dry-run`; verify stdout shows
   `Would index N record(s):` and one `<UUID>\t<PATH>` line per
   selected record. No catalog write.
3. `nx dt index --selection`; verify summary line `Indexed N
   record(s) (M skipped).` where `N` matches step 2's count.
4. `nx catalog list --json | jq '.[] | select(.source_uri | startswith("x-devonthink-item://"))'`;
   verify N new entries appear (the JSON filter narrows to DT-keyed
   entries indexed in step 3). `nx catalog list` has no built-in
   `--source-uri-prefix` flag in v1; the JSON pipe is the canonical
   query path.
5. For each new entry, `nx catalog show <tumbler> --json`; verify
   `source_uri == "x-devonthink-item://<UUID>"` and
   `meta.devonthink_uri` matches.

### AC-2: `nx dt index --uuid <UUID>`

```bash
nx dt index --uuid "$NEXUS_DT_TEST_UUID"
nx catalog list --json | jq --arg u "$NEXUS_DT_TEST_UUID" \
  '.[] | select(.source_uri == "x-devonthink-item://" + $u)'
```

Verify: exactly one entry, with `source_uri == x-devonthink-item://$NEXUS_DT_TEST_UUID`
and `meta.devonthink_uri` populated to the same value.

### AC-3: `nx dt index --tag` multi-database

```bash
# Multi-DB default (no --database); must hit both databases.
nx dt index --tag "$NEXUS_DT_TEST_TAG" --dry-run
```

Verify the dry-run prints records from BOTH `Inbox` and `NexusTest`
(the paths should differ between them; DT stores each library at a
different `~/Databases/...` location).

```bash
# Single-database scope; must hit only the named database.
nx dt index --tag "$NEXUS_DT_TEST_TAG" --database NexusTest --dry-run
```

Verify only paths from `NexusTest` appear.

### AC-4: `nx dt index --group <path>` recursive

```bash
nx dt index --group "$NEXUS_DT_TEST_GROUP" --dry-run
```

Verify the dry-run lists exactly the PDFs at the named group and its
descendants. If `NEXUS_DT_TEST_GROUP=/AI/2025`, only the 3 PDFs at
that level should appear; the PDFs in the parent `/AI/` are NOT in
scope. Adjust the fixture if needed: the AC is "PDFs at `<path>` AND
descendants", not "ancestors".

### AC-5: `nx dt index --smart-group <name>` honours `search group`

```bash
nx dt index --smart-group "$NEXUS_DT_TEST_SMART_GROUP" --dry-run
```

Verify the listed paths are exactly the PDFs in `Library/Recent`,
NOT the PDFs in `/AI/2025` or `/AI/` even though those would match
the predicates if scoped to root. If results include records outside
`Library/Recent`, the `search group` property is being collapsed and
the AC fails.

Edge case: edit the smart group, set `search group` to `(none)`
(missing value). Re-run the dry-run. Result should be every PDF in
`NexusTest` matching the predicates; the missing-value scope falls
through to whole-library search.

### AC-6: `nx dt open <tumbler>` and `nx dt open <UUID>`

```bash
# UUID form; should reveal the record in DT immediately.
nx dt open "$NEXUS_DT_TEST_UUID"
```

Switch to DT; verify the record is selected and visible.

```bash
# Tumbler form; uses an entry indexed earlier (e.g. from AC-1).
nx catalog list --json \
  | jq -r '.[] | select(.source_uri | startswith("x-devonthink-item://")) | .tumbler' \
  | head -5
nx dt open <tumbler-from-list>
```

Verify DT activates and reveals the same record.

Negative test: pick a tumbler whose source is `file://` (not a DT
record):

```bash
nx dt open <non-dt-tumbler>
```

Should exit non-zero with `no DEVONthink URI for tumbler <N.N.N>`.

### AC-7: cross-platform CI

This is verified by CI's Linux runner; every test in
`tests/test_devonthink.py` and `tests/test_commands_dt.py` runs on
Linux without invoking `osascript`. Manual confirmation:

```bash
# Force the non-darwin gate even on macOS.
NEXUS_DT_LIVE= python -c "
import sys
sys.platform = 'linux'
from click.testing import CliRunner
from nexus.cli import main
r = CliRunner().invoke(main, ['dt', 'index', '--selection'])
assert r.exit_code != 0
assert 'macOS-only' in r.output
print('OK')
"
```

## Reset / cleanup

After a successful smoke, clean up the catalog entries so a re-run
doesn't see stale data:

```bash
# List DT-keyed entries created during the smoke.
nx catalog list --json \
  | jq '.[] | select(.source_uri | startswith("x-devonthink-item://"))'

# Remove them by tumbler (one at a time, or pipe through xargs).
nx catalog remove <tumbler>
```

DT-side fixtures (databases, tags, groups, smart groups) are
intentionally **not** torn down; they're cheap to leave in place
and let you re-run the smoke without re-creating them. Drop them only
when retiring the test rig.

## Cross-references

- [RDR-099](../../docs/rdr/rdr-099-devonthink-integration.md);
  acceptance criteria, AppleScript surface, risks.
- `tests/test_devonthink.py`; fake-helper unit tests for the
  selector helpers.
- `tests/test_commands_dt.py`; fake-helper unit tests for the CLI
  surface.
- `tests/test_devonthink_live.py`; gated live-DT integration tests
  (added by `nexus-mv0p.6`; reuses every env var defined here).
