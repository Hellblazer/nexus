---
name: rdr-research
description: >
  Add, track, and verify structured research findings for an RDR.
  Triggers: user says "add research finding", "RDR research", or /rdr-research
allowed-tools: Task, Read, Write, Edit, Glob, Grep, Bash
---

# RDR Research Skill

Optionally delegates to **deep-research-synthesizer** (sonnet) for evidence gathering, or **codebase-deep-analyzer** (sonnet) for code-specific questions.

## When This Skill Activates

- User says "add research finding", "update RDR research", "verify assumption"
- User invokes `/rdr-research`
- User wants to record or classify a discovery during RDR planning

## Subcommands

### `/rdr-research add <id>`

**Inputs** (prompt the user):
1. **Finding text**: What was discovered
2. **Classification**: Verified (✅) | Documented (⚠️) | Assumed (❓)
3. **Verification method**: Source Search | Spike | Docs Only
4. **Source**: Code path, URL, experiment description

**Behavior:**

1. **Determine next sequence number**:
   ```bash
   nx memory list --project {repo}_rdr
   ```
   Filter entries where title matches `NNN-research-*`. Parse sequence numbers. Next seq = max + 1. If none exist, start at 1.

2. **Write T2 record**:
   ```bash
   nx memory put - --project {repo}_rdr --title NNN-research-{seq} --ttl permanent --tags rdr,research,{classification} <<'EOF'
   rdr_id: "NNN"
   seq: {seq}
   finding: "Finding text here"
   classification: "verified"
   verification_method: "source_search"
   source: "Source description here"
   acknowledged: false
   EOF
   ```

3. **Append to RDR markdown**: Add a formatted entry to the Research Findings > Key Discoveries section:
   ```markdown
   - **✅ Verified** (source search) — Finding text here
     *Source: source description*
   ```

### `/rdr-research status <id>`

1. List T2 entries: `nx memory list --project {repo}_rdr`
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

### `/rdr-research verify <id> <finding-seq>`

1. Read T2 record: `nx memory get --project {repo}_rdr --title NNN-research-{seq}`
2. Prompt for new classification (verified or documented) and updated verification method
3. Update T2 record (overwrite with updated content)
4. Update the emoji marker in the RDR markdown file (e.g., ❓ → ✅)

## Agent Dispatch

When the user asks to *investigate* something (not just record a finding):
- **Code questions** ("how does auth work in our codebase?"): Dispatch `codebase-deep-analyzer` agent, then record the finding
- **External research** ("what embedding models support CCE?"): Dispatch `deep-research-synthesizer` agent, then record the finding
- **Simple recording**: No agent needed — just write the T2 record and update markdown

## Notes

- The markdown document is the authoritative narrative; T2 is the queryable index
- Verification method (Source Search / Spike / Docs Only) is the most load-bearing field — it distinguishes high-confidence from low-confidence findings
- Findings marked "Docs Only" on load-bearing assumptions are the highest risk items
