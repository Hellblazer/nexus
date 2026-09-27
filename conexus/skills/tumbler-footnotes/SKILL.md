---
name: tumbler-footnotes
description: "Use when a markdown file cites the catalog as `[label](nx://catalog/<tumbler>)` links and needs those converted to footnotes that GitHub/GitLab/VS Code actually render, or checked/reversed — drives nx catalog footnotes."
effort: low
---

# Tumbler Footnotes

Thin wrapper over `nx catalog footnotes` (GH #896 / nexus-sxiay). No mainstream
markdown renderer resolves the `nx://catalog/<tumbler>` URI scheme, so a doc
citing the catalog this way reads as a dead hyperlink everywhere except
`nx doc render`'s own `.rendered.md` sidecar. This command fixes the SOURCE
file itself, in place.

## Convert a file

```bash
nx catalog footnotes docs/rdr/rdr-200-example.md
```

Every `[label](nx://catalog/<tumbler>)` becomes `label[^tumbler-<slug>]`, and a
`## Footnotes` section is appended (or refreshed) at the bottom with one
definition per unique tumbler — title, content type, indexed date, a working
link (or a plain `(repo-relative)` label when it can't be confirmed to
resolve), any merge note, and outbound links. Fenced code blocks are never
touched.

Re-running is safe and idempotent: with an unchanged catalog state it's a
byte-for-byte no-op; with drift (a title edit, a merge) only the footnote
BODIES are rewritten — markers already assigned in the body never move. A
tumbler that no longer resolves is left as a link (never silently dropped)
and reported on stderr as `file:line: unresolved tumbler <tumbler>`.

## Check before committing

```bash
nx catalog footnotes docs/rdr/*.md --check
```

Exits non-zero if any file is not already in current, converted form (a
dangling reference also counts) — writes nothing. Use this as a pre-commit /
CI gate for docs that cite the catalog, mirroring `nx doc validate`'s role for
the render-time half of GH #896.

## Preview without writing

```bash
nx catalog footnotes docs/rdr/rdr-200.md --dry-run
```

Prints a unified diff of what would change; writes nothing.

## Reverse conversion

```bash
nx catalog footnotes docs/rdr/rdr-200.md --to-links
```

Expands every footnote marker back into its `nx://catalog/<tumbler>` link and
drops the `## Footnotes` section — no catalog access needed, the tumbler is
read straight out of the footnote body. Use this when a doc's footnote form
needs to go back to the authoring-time link form (e.g. before a structural
edit that would otherwise fight the footnote layout).

## When to use this

- Before committing an RDR, design note, or ops brief that embeds
  `nx://catalog/<tumbler>` links — convert first, so readers on GitHub see a
  working footnote instead of a dead link.
- As a `--check` gate in CI / pre-commit for any docs tree that cites the
  catalog.
- After a catalog merge or title edit that could have staled existing
  footnote bodies in already-converted docs — re-running refreshes them.

Full flag reference: `docs/cli-reference.md` § `nx catalog footnotes`.

## Agent-Specific PRODUCE

No agent dispatch — this is a direct CLI invocation, not a relay. If the
conversion or check surfaces something worth remembering across the session
(e.g. a systematically dangling owner whose docs keep citing deleted
tumblers), note it in T1 scratch:

```
mcp__plugin_conexus_nexus__scratch(action="put", content="tumbler-footnotes: <finding>", tags="tumbler-footnotes")
```
