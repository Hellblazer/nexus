---
name: dt-open-result
description: Use when opening a Nexus search result back in DEVONthink, given a catalog tumbler or raw DEVONthink UUID, to inspect the source record in DT's UI.
effort: low
---

# DEVONthink Open Result

Wraps the `nx dt open <tumbler-or-uuid>` CLI command. Resolve a Nexus tumbler (or raw DEVONthink UUID) to a `x-devonthink-item://` URL and open it in DEVONthink.

## When This Skill Activates

- User invokes `/dt:open-result <tumbler-or-uuid>`
- User wants to jump from a Nexus search hit (tumbler `1.2.3`) to the underlying DT record
- User has a raw DEVONthink UUID and wants to open it without copying it into the DT URL by hand

## Underlying Command

```bash
nx dt open <tumbler-or-uuid>
```

Argument resolution:

- A UUID-shaped argument (`8-4-4-4-12` hex) is converted directly to `x-devonthink-item://<UUID>`. No catalog hit, no osascript.
- A tumbler (e.g., `1.2.3`) is resolved through the catalog. The CLI prefers `meta.devonthink_uri` and falls back to `source_uri` when the entry was registered with a DT identity.
- Anything else fails with an explanatory message.

## Behavior

1. Take the tumbler or UUID from the argument list.
2. Run `nx dt open <arg>`. The CLI handles platform check, catalog lookup, and URL construction.
3. On success, the macOS opener hands the URL to DEVONthink and the record opens. Confirm to the user that the open call returned cleanly.
4. On a tumbler that resolves to no DT identity, surface the CLI's error message so the user knows the catalog entry was not registered through `nx dt index`.

## Examples

Open a tumbler from a recent search result:

```
/dt:open-result 1.2.3
```

Open by raw DEVONthink UUID (skips the catalog):

```
/dt:open-result 12345678-1234-1234-1234-123456789012
```

## Success Criteria

- [ ] CLI invoked with the user-supplied argument verbatim
- [ ] Successful open confirmed back to the user
- [ ] Tumbler-to-DT-identity miss reported explicitly so the user knows the entry was not indexed via `nx dt`
- [ ] Friendly error on non-macOS

## Notes

- This skill does not duplicate CLI logic. Catalog resolution, URL construction, and platform checks all live in `nx dt open`.
- For tumbler resolution to work, the entry must have been indexed via `nx dt index ...` (which stamps `source_uri = x-devonthink-item://<UUID>` and `meta.devonthink_uri`). Entries indexed via `nx index` from a local filesystem path have no DT identity and will not resolve.
- Use T1 scratch (`mcp__plugin_nx_nexus__scratch`) if the user is bouncing between several Nexus tumblers and DT records in one session.
