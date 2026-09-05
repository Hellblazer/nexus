---
name: rdr-research
description: Use when adding, tracking, or verifying structured research findings for an active RDR
effort: medium
---

# RDR Research Skill

Optionally delegates to **deep-research-synthesizer** (sonnet) for evidence gathering, or **codebase-deep-analyzer** (sonnet) for code-specific questions.

## When This Skill Activates

- User says "add research finding", "update RDR research", "verify assumption"
- User invokes `/conexus:rdr-research`
- User wants to record or classify a discovery during RDR planning

## Path Detection

Resolve RDR directory from `.nexus.yml` `indexing.rdr_paths[0]`; default `docs/rdr`. Use the Step 0 snippet from the rdr-create skill, stored as `RDR_DIR`. All file paths below use `$RDR_DIR` in place of `docs/rdr`.

## Subcommands

### `/conexus:rdr-research add <id>`

**Inputs** (prompt the user):
1. **Finding text**: What was discovered
2. **Classification**: Verified (✅) | Documented (⚠️) | Assumed (❓)
3. **Verification method**: Source Search | Spike | Docs Only
4. **Source**: Code path, URL, experiment description

**Behavior:**

1. **Record the finding via the Bash tool** (nexus-zu1q0 — this used to be
   computed here as prose: list T2 titles, parse the max sequence, upsert
   the result. That computation raced: two adds in the same session could
   both compute seq 1, and the second `memory_put` silently overwrote the
   first finding. The sequence is now computed and guarded server-side):
   `nx rdr preamble rdr-research -- add <id> <finding text tokens...>`
   (literal argv tokens, per the apostrophe-crash rule above). It derives
   the next sequence from existing `<id>-research-*` T2 titles, writes the
   record, and re-checks the computed title immediately before writing,
   advancing past a collision instead of upserting over it (two adds in
   the same instant can still race; consecutive adds cannot). It prints the recorded title
   (`{repo}_rdr/NNN-research-{seq}`); read that back to fill in step 3 below.
   Classification / verification method / source are not yet parameters of
   this command — append them to the finding text tokens if needed, or
   `memory_put`-amend the record after using its printed title.

3. **Append to RDR markdown**: Add a formatted entry to the Research Findings > Key Discoveries section:
   Prose register (`$RDR_DIR/REGISTER.md`, fallback `$CLAUDE_PLUGIN_ROOT/resources/rdr/REGISTER.md`): findings in plain terms a non-native-speaker expert can follow; say what is known and what is not; a dead end recorded plainly is a finding.
   ```markdown
   - **✅ Verified** (source search) — Finding text here
     *Source: source description*
   ```

4. **Catalog citation link** (optional, if catalog initialized and source is a paper/URL):
   If the source references a paper title or URL that may be indexed in the knowledge store:
   ```
   mcp__plugin_conexus_nexus-catalog__search(query="<source title or keywords>")
   ```
   If a matching catalog entry is found, resolve the RDR's tumbler too (`mcp__plugin_conexus_nexus-catalog__search(query="RDR-NNN")`), then link:
   ```
   mcp__plugin_conexus_nexus-catalog__link(from_tumbler="<rdr-tumbler>", to_tumbler="<paper-tumbler>", link_type="cites", created_by="rdr-research")
   ```
   Skip silently if catalog not initialized, RDR not indexed, or no match found. This enriches the citation graph incrementally as research findings are added.

### `/conexus:rdr-research status <id>`

1. List T2 entries: mcp__plugin_conexus_nexus__memory_get(project="{repo}_rdr", title=""
2. Filter titles matching `NNN-research-*`
3. Parse and display summary:
   ```
   RDR NNN Research Status:
   - Verified (✅): 3 (2 source search, 1 spike)
   - Documented (⚠️): 1 (1 docs only)
   - Assumed (❓): 2 — ⚠ unresolved risks
     - [seq 4] "Library X supports feature Y" (docs only)
     - [seq 6] "Latency under 100ms" (docs only)
   ```

### `/conexus:rdr-research verify <id> <finding-seq>`

1. Read T2 record: mcp__plugin_conexus_nexus__memory_get(project="{repo}_rdr", title="NNN-research-{seq}"
2. Prompt for new classification (verified or documented) and updated verification method
3. Update T2 record (overwrite with updated content)
4. Update the emoji marker in the RDR markdown file (e.g., ❓ → ✅)

## Agent Dispatch

When the user asks to *investigate* something (not just record a finding):
- **Code questions** ("how does auth work in our codebase?"): Dispatch `codebase-deep-analyzer` agent, then record the finding
- **External research** ("what embedding models support CCE?"): Dispatch `deep-research-synthesizer` agent, then record the finding
- **Simple recording**: No agent needed — just write the T2 record and update markdown

### Pre-Dispatch: Seed Link Context

Before dispatching an agent, seed T1 scratch with link targets so the auto-linker can create catalog links when the agent stores findings:

1. Resolve the RDR's tumbler: `mcp__plugin_conexus_nexus-catalog__search(query="RDR-NNN", content_type="rdr")`
2. Write link context to scratch:
   ```
   mcp__plugin_conexus_nexus__scratch(action="put", content='{"targets": [{"tumbler": "<resolved-tumbler>", "link_type": "cites"}], "source_agent": "rdr-research"}', tags="link-context")
   ```
3. If catalog search returns no result, skip seeding (the auto-linker handles empty context gracefully)

## Relay Template (Use This Format)

When dispatching agents (deep-research-synthesizer or codebase-deep-analyzer) via Agent tool, use this exact structure:

```markdown
## Relay: deep-research-synthesizer

**Task**: [1-2 sentence summary: e.g., "Investigate embedding model support for CCE to verify RDR 003 assumption"]
**Bead**: [ID] (status: [status]) or 'none'

### Input Artifacts
- nx store: [document titles or "none"]
- nx memory: {repo}_rdr/NNN (RDR metadata and existing research records)
- nx scratch: [scratch IDs or "none"]
- Files: $RDR_DIR/NNN-*.md

### Deliverable
Research finding with classification, verification method, and source reference ready for T2 storage.

### Quality Criteria
- [ ] Finding has clear classification (Verified/Documented/Assumed)
- [ ] Verification method specified (Source Search/Spike/Docs Only)
- [ ] Source traceable to specific code path, URL, or experiment
```

**Required**: All fields must be present. Agent will validate relay before starting.

For additional optional fields, see [RELAY_TEMPLATE.md](../../agents/_shared/RELAY_TEMPLATE.md).

## Success Criteria

- [ ] Research finding recorded with correct classification and verification method
- [ ] T2 record written with sequential ID (`NNN-research-{seq}`)
- [ ] RDR markdown updated with formatted finding in Key Discoveries section
- [ ] Source traceable (code path, URL, or experiment description)
- [ ] Agent dispatched correctly for investigation tasks (code vs. external research)
- [ ] Existing findings not overwritten when adding new ones

## Agent-Specific PRODUCE

Outputs generated by dispatched agents (deep-research-synthesizer, codebase-deep-analyzer):

- **T3 knowledge**: Research findings via store_put tool: content="# RDR NNN Research: {topic}\n{findings}", collection="knowledge", title="research-rdr-NNN-{topic}", tags="rdr,research"
- **T2 memory**: Finding records via memory_put tool: content="...", project="{repo}_rdr", title="NNN-research-{seq}", ttl="permanent", tags="rdr,research,{classification}"
- **T1 scratch**: Working notes during investigation via scratch tool: action="put", content="RDR NNN research: {hypothesis}", tags="rdr,research" (promoted to T2 on completion)

**Session Scratch (T1)**: Agents use scratch tool for ephemeral working notes during the session. Flagged items auto-promote to T2 at session end.

## Notes

- The markdown document is the authoritative narrative; T2 is the queryable index
- Verification method (Source Search / Spike / Docs Only) is the most load-bearing field — it distinguishes high-confidence from low-confidence findings
- Findings marked "Docs Only" on load-bearing assumptions are the highest risk items

## Known Limitations

**T2 retrieval is O(N):** memory_get tool with project="{repo}_rdr", title="" returns all records (RDR metadata + all research findings). Client-side filtering by title pattern (`NNN-research-*`) and individual memory_get calls for content are required. For projects with many RDRs (30+ at 10 findings each), this may be slow. Validate that parsed records have `rdr_id` and `seq` fields before using them — do not rely solely on title pattern matching.
