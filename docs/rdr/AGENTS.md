# `docs/rdr/` — AGENTS.md

**RDRs are decision archaeology, not API documentation.**

## When to read RDRs

Read an RDR when you're **researching design intent** — "why is the catalog modelled this way?", "why was X rejected when Y was chosen?". The RDR carries the alternatives-considered and the rationale, which the code does not.

## When NOT to read RDRs

If your question is "how does X work *right now*?", read these in order:

1. **The code.** Module docstrings + the relevant module's `AGENTS.md`.
2. **`CHANGELOG.md`.** What changed and when, with PR links.
3. **`docs/architecture.md`.** The current module map and post-store hook contracts.
4. **`docs/cli-reference.md`.** Live CLI surface.

Only after that, if you still want the *why*, reach for an RDR — and check its `status:` field first.

## RDR lifecycle

Frontmatter `status:` field carries one of six values (Sam's ruling, RDR-201):

- `draft` — entry state; under discussion or revision, **don't quote as authoritative**.
- `accepted` — gate passed; current design intent, implementation may start.
- `deferred` — parked; resumes to `draft` only, never directly to `accepted`.
- `closed` — implemented and shipped; RDR is historical record (terminal).
- `superseded` — replaced by a named successor RDR (terminal).
- `abandoned` — not going to happen; merges the retired `scrapped` value (terminal).

This is a **checked table, not prose** (RDR-201): the authoritative source is the
packaged `src/nexus/tables/rdr-lifecycle.toml` (loaded via
`nexus.tables.load.load_packaged_table`, so it is reachable from any installed
`conexus`, not just a checkout). `nx rdr set-status` enforces
every transition against that table's state machine; `nx rdr preamble
rdr-audit` reports any on-disk status outside the table's domain as a
finding. A lint test (`tests/test_tables_lint.py`) asserts this list matches
the table's `status` domain exactly, so this section cannot silently drift
from the table again.

The **only** way to retire an RDR is the `status:` flip. **Never delete an RDR file** — they're the project's permanent decision record.

## RDR scale and scope

`docs/rdr/` is large (~2.7MB). Most of that is draft and historical content from earlier design cycles. Don't load every RDR you find — the directory's volume can dominate context budgets.

If you need to find an RDR by topic, prefer the index in `docs/rdr/README.md` over a wide grep. The index lists every RDR with its current status; an RDR that isn't in the index is suspect.

## Authoring a new RDR

Use the lifecycle skills: `/conexus:rdr-create` → `/conexus:rdr-research` → `/conexus:rdr-gate` → `/conexus:rdr-accept` → `/conexus:rdr-close`. List existing with `/conexus:rdr-list`; show one with `/conexus:rdr-show NNN`.

The numbering is monotonic; pick the next unused integer. The frontmatter shape is enforced by `/conexus:rdr-audit`.

## Frontmatter quoting — `#` is comment-start in YAML

When listing PR / issue / bead refs in YAML frontmatter, **always quote them.** YAML treats `#` as comment-start at any token-start position, so an unquoted flow sequence like `prs: [#381, #382]` silently parses as an empty list followed by a comment that eats the closing `]`. The scanner then runs off the end of the frontmatter and raises `ScannerError: while parsing a flow sequence … got '<stream end>'`. The indexer marks the RDR `failed` (since nexus-qr9d) and skips it; before that fix it hung.

```yaml
# ❌ broken — # makes the rest of the line a comment
references:
  prs: [#381, #382, #383]

# ✅ flow form, quoted
references:
  prs: ["#381", "#382", "#383"]

# ✅ block form, quoted
references:
  prs:
    - "#381"
    - "#382"
```

Run `nx rdr lint` before committing to catch this hazard.

## Joint decision records (`docs/rdr/joint/`)

A rule that two or more RDRs would each carry a copy of lives once, as
`docs/rdr/joint/JDR-NNN-<slug>.md` (template: `joint/TEMPLATE.md`), and
the owning records cite the number. The registry is append-only: never
merge, split or renumber a JDR, because citations anchor on the number
and a moved anchor is the duplicate-contract drift this exists to end
(a rule living in two places until the stale one wins). Amend in place
with a dated Revision History line; retire with `status: retired` and a
pointer to what replaced it. `tests/test_docs_reference_rot.py` (lint
bucket) fails on any `JDR-NNN` cited under `docs/` that has no file here.
JDRs are not RDRs: `nx rdr` lifecycle verbs, the README index, and
`nx index rdr` do not see them; they are documentation of a seam.
Seeded 2026-09-04 (nexus-vuiid) with JDR-001, the T1 three-scopes contract
shared by RDR-105, RDR-149 and RDR-184.

