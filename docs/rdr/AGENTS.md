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

Frontmatter `status:` field carries one of:

- `draft` — under discussion, may be wrong, **don't quote as authoritative**.
- `accepted` — current design intent.
- `closed` — work shipped, RDR is historical record.
- `superseded` — replaced by a later RDR (frontmatter names the successor).

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
