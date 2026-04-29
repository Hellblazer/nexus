---
name: dt-index-selection
description: Use when indexing the current DEVONthink selection into Nexus, capturing whatever the user has highlighted in DT's UI as T3 documents.
effort: low
---

# DEVONthink Index Selection

Wraps the `nx dt index --selection` CLI command. Index whatever records are currently selected in DEVONthink's UI into Nexus T3, with optional collection or corpus targeting.

## When This Skill Activates

- User invokes `/dt:index-selection`
- User asks to index the current DEVONthink selection
- User wants to capture DT-side highlights into a Nexus T3 collection without copying UUIDs by hand

## Underlying Command

```bash
nx dt index --selection [--collection knowledge__<name>] [--corpus <name>] [--database <db>] [--dry-run]
```

Flag forwarding:

- `--collection knowledge__<name>` routes to a `knowledge__*` collection (use for external reference corpora).
- `--corpus <name>` routes to a `docs__<corpus>` collection (use for project-internal docs).
- `--database <db>` scopes selectors to one open DEVONthink library.
- `--dry-run` previews the records without writing to T3.

If neither `--collection` nor `--corpus` is provided, records land in `docs__default`.

## Behavior

1. Confirm DEVONthink is running. The CLI fails fast on non-darwin platforms; surface that as a friendly error if the user is not on macOS.
2. Decide collection routing:
   - If the user passed a collection or corpus argument, forward it.
   - Otherwise, ask whether to target `knowledge__<name>` (external reference) or `docs__<corpus>` (project doc), defaulting to `docs__default` on no answer.
3. Run `nx dt index --selection` with the resolved flags. Show the count of indexed records and the collection name from the CLI's summary line.
4. On `--dry-run`, report the records that would be indexed and stop.
5. Catalog stamping happens in the CLI: every entry gets `source_uri = x-devonthink-item://<UUID>` and `meta.devonthink_uri`. Surface any stamp failures from the CLI summary so the user can rerun with `nx catalog update --source-uri` if needed.

## Examples

Index whatever is selected, default routing:

```
/dt:index-selection
```

Index selection into a knowledge collection:

```
/dt:index-selection --collection knowledge__delos
```

Preview without writing to T3:

```
/dt:index-selection --dry-run
```

Scope to one DEVONthink database:

```
/dt:index-selection --database "Reference Library" --collection knowledge__papers
```

## Success Criteria

- [ ] CLI invoked with `--selection` and any forwarded flags
- [ ] Indexed record count reported back to the user
- [ ] Stamp failures (if any) surfaced explicitly from the CLI summary
- [ ] `--dry-run` previews without writing
- [ ] Friendly error on non-macOS or when DEVONthink is not running

## Notes

- This skill does not duplicate CLI logic. The actual indexing, chunking, embedding, and catalog stamping all happen inside `nx dt index`.
- Use T1 scratch (`mcp__plugin_nx_nexus__scratch`) to capture indexing decisions during a multi-step research session if the user is processing several selections in a row.
