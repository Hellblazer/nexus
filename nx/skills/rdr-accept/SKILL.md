---
name: rdr-accept
description: Use when a gated RDR returned PASSED and you want to officially accept it for implementation
---

# RDR Accept Skill

Accepts an RDR after it passes the gate. This is the author/reviewer decision point between gate validation and implementation.

## When This Skill Activates

- User says "accept this RDR", "mark as accepted", "approve the RDR"
- User invokes `/rdr-accept`
- Gate returns PASSED and user confirms acceptance

## Input

- RDR ID (required) — e.g., `003`

## Behavior

1. **Verify gate result** — read `{id}-gate-latest` from T2. Block if outcome is not PASSED.
2. **Update T2** (process authority) — set `status: "accepted"`, `accepted_date: "YYYY-MM-DD"`.
3. **Update file** — change frontmatter `status: draft` to `status: accepted`, add `accepted_date`.
4. **Update reviewed-by** — set to `self` if empty.
5. **Regenerate README** — update `{rdr_dir}/README.md` index.
6. **Stage files** — `git add` modified files.

## Agent Invocation

This skill executes directly — no agent delegation. All state mutations are performed by the skill itself using `nx memory`, filesystem writes, and `git add`.

## Success Criteria

- [ ] T2 gate result verified as PASSED before accepting
- [ ] T2 metadata updated with status=accepted and accepted_date
- [ ] File frontmatter updated to match T2
- [ ] README index regenerated
- [ ] Files staged via git add

## Agent-Specific PRODUCE

Outputs produced directly by this skill (no agent delegation):

- **T2 memory**: Updated status record via memory_put tool: project="{repo}_rdr", title="NNN", ttl="permanent", tags="rdr,accepted"
- **Filesystem**: Updated RDR markdown (frontmatter `status: accepted`, `accepted_date`), regenerated `{rdr_dir}/README.md`
- **T1 scratch**: Use scratch tool: action="put", content="RDR NNN: accepted YYYY-MM-DD" for ephemeral tracking during multi-step acceptance flow
