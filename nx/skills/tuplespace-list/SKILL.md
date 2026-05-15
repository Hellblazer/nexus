---
name: tuplespace-list
description: Use when surveying registered tuple-space subspaces, schemas, or active claims before posting or taking tuples
effort: low
---

# Tuplespace List

Introspection skill for the RDR-110 tuple space. Use this before posting or taking to confirm which subspaces exist and what dimensions they expect.

## Commands

List every registered subspace template:

```
nx tuplespace list-subspaces
nx tuplespace list-subspaces --json
```

Show the resolved schema for a concrete or template subspace:

```
nx tuplespace show-schema tasks/nexus
nx tuplespace show-schema locks/<resource> --json
```

The schema output includes `dimensions`, `embed_from`, `take` mode, `read` defaults, `retention_seconds`, and `tiers`.

## When to use

- A new agent does not know what subspaces are available.
- You suspect a schema mismatch caused a `SubspaceSchemaError` and need to inspect the registered dimensions.
- You want to verify that a custom subspace YAML loaded correctly via `NX_TUPLESPACE_BUILTIN_DIR`.

## Pitfalls

- Subspace names in the registry use template form (for example, `tasks/<project>`). When you post or take you supply a concrete name (`tasks/nexus`). `show-schema` accepts either form and resolves to the template.
- An unknown subspace returns exit code 1 with a clear error rather than an empty schema.

## Related

- `/nx:tuplespace-stats` for per-subspace tuple counts.
- `/nx:tuplespace-tasks`, `/nx:tuplespace-mailbox`, `/nx:tuplespace-lock`, `/nx:tuplespace-events`, `/nx:tuplespace-barriers` for the consumer patterns.
