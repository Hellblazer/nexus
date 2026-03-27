---
description: Accept a gated RDR — verifies gate PASSED in T2, updates status to accepted
---

# RDR Accept

!{
NEXUS_RDR_ARGS="${ARGUMENTS:-}" python3 << 'PYEOF'
import os, sys, re, subprocess
from pathlib import Path

args = os.environ.get('NEXUS_RDR_ARGS', '').strip()

# Repo root and name
try:
    repo_root = subprocess.check_output(
        ['git', 'rev-parse', '--show-toplevel'],
        stderr=subprocess.DEVNULL, text=True).strip()
    repo_name = os.path.basename(repo_root)
except Exception:
    repo_root = os.getcwd()
    repo_name = os.path.basename(repo_root)

# Resolve RDR directory from .nexus.yml (default: docs/rdr)
rdr_dir = 'docs/rdr'
nexus_yml = Path(repo_root) / '.nexus.yml'
if nexus_yml.exists():
    content = nexus_yml.read_text()
    try:
        import yaml
        d = yaml.safe_load(content) or {}
        paths = (d.get('indexing') or {}).get('rdr_paths', ['docs/rdr'])
        rdr_dir = paths[0] if paths else 'docs/rdr'
    except ImportError:
        m_yml = (re.search(r'rdr_paths[^\[]*\[([^\]]+)\]', content) or
                 re.search(r'rdr_paths:\s*\n\s+-\s*(.+)', content))
        if m_yml:
            v = m_yml.group(1)
            parts = re.findall(r'[a-z][a-z0-9/_-]+', v)
            rdr_dir = parts[0] if parts else 'docs/rdr'

rdr_path = Path(repo_root) / rdr_dir

print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
print()


def parse_frontmatter(filepath):
    text = filepath.read_text(errors='replace')
    meta = {}
    if text.startswith('---'):
        parts = text.split('---', 2)
        if len(parts) >= 3:
            block = parts[1]
            try:
                import yaml; meta = yaml.safe_load(block) or {}
            except Exception:
                for line in block.splitlines():
                    if ':' in line:
                        k, _, v = line.partition(':')
                        meta[k.strip().lower()] = v.strip()
    if 'title' not in meta and 'name' not in meta:
        h1 = re.search(r'^#\s+(.+)', text, re.MULTILINE)
        if h1:
            meta['title'] = h1.group(1).strip()
    return meta, text


def find_rdr_file(rdr_path, id_str):
    m = re.search(r'\d+', id_str)
    if not m:
        return None
    num_int = int(m.group(0))
    for f in sorted(rdr_path.glob('*.md')):
        nums = re.findall(r'\d+', f.stem)
        if nums and int(nums[0]) == num_int:
            return f
    return None


EXCLUDED = {'readme.md', 'template.md', 'index.md', 'overview.md', 'workflow.md', 'templates.md'}

def get_all_rdrs(rdr_path):
    """Return list of dicts for all RDRs in the directory."""
    all_md = sorted(rdr_path.glob('*.md'))
    rdrs = []
    for f in all_md:
        if f.name.lower() in EXCLUDED:
            continue
        fm, text = parse_frontmatter(f)
        rtype = fm.get('type', '?')
        doc_status = fm.get('status', '?')
        if doc_status == '?' and rtype == '?':
            continue
        rdrs.append({
            'file': f.name,
            'path': f,
            'title': fm.get('title', fm.get('name', f.stem)),
            'status': doc_status,
            'rtype': rtype,
            'priority': fm.get('priority', '?'),
        })
    return rdrs


if not rdr_path.exists():
    print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
    sys.exit(0)

id_match = re.search(r'\d+', args)

if not id_match:
    print("> **Usage**: `/nx:rdr-accept <id>` — e.g. `/nx:rdr-accept 003` or `/nx:rdr-accept RDR-003`")
    print()
    rdrs = get_all_rdrs(rdr_path)
    draft_rdrs = [r for r in rdrs if r['status'].lower() == 'draft']
    print("### Draft RDRs (eligible for acceptance)")
    print()
    if draft_rdrs:
        print("| File | Title | Status | Type |")
        print("|------|-------|--------|------|")
        for r in draft_rdrs:
            print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} |")
    else:
        print("No Draft RDRs found. Only Draft RDRs can be accepted.")
    sys.exit(0)

rdr_file = find_rdr_file(rdr_path, id_match.group(0))
if not rdr_file:
    print(f"> RDR not found for ID: `{id_match.group(0)}`")
    sys.exit(0)

fm, text = parse_frontmatter(rdr_file)
title = fm.get('title', fm.get('name', rdr_file.stem))
current_status = fm.get('status', '?')
rdr_type = fm.get('type', '?')
rdr_num = re.search(r'\d+', rdr_file.stem)
t2_key = rdr_num.group(0) if rdr_num else rdr_file.stem

print(f"### RDR: {rdr_file.name}")
print(f"**RDR ID:** {t2_key}  **Title:** {title}  **Type:** {rdr_type}  **File Status:** {current_status}")
print()

# Accepted status is allowed — agent handles idempotency in Step 1
if current_status.lower() not in ('draft', 'accepted'):
    print(f"> **BLOCKED**: RDR status is `{current_status}`. Only Draft RDRs can be accepted.")
    sys.exit(0)

# T2 lookups — agent must call these MCP tools and use the results in the Action section
print("### T2 Lookups (call these before executing Action steps)")
print()
print(f"1. **T2 metadata**: Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"{t2_key}\"")
print(f"2. **T2 gate result**: Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"{t2_key}-gate-latest\"")
print(f"   If no gate record exists, run `/nx:rdr-gate {t2_key}` first.")
print()
print(f"**RDR file path:** `{rdr_file}`")
print()

# Step count auto-detection for planning handoff
# Look for any planning-like section, not just "## Implementation Plan"
plan_headers = [
    r'^## Implementation Plan',
    r'^## Approach',
    r'^## Plan',
    r'^## Design',
    r'^## Steps',
    r'^## Execution',
]
plan_section = None
for hdr in plan_headers:
    m = re.search(hdr + r'\s*\n(.*?)(?=^## |\Z)', text, re.MULTILINE | re.DOTALL)
    if m:
        plan_section = m.group(1)
        break

step_count = 0
has_plan = plan_section is not None
if has_plan:
    # Count any numbered sub-headings: Phase, Step, Stage, Part
    step_count = len(re.findall(
        r'^### (?:Phase|Step|Stage|Part)\s', plan_section, re.MULTILINE))
    # Also count ### with leading numbers: ### 1., ### 2.
    if step_count == 0:
        step_count = len(re.findall(r'^### \d', plan_section, re.MULTILINE))

print("### Planning Handoff")
print(f"**Step count detected:** {step_count}")
print(f"**Has plan section:** {'yes' if has_plan else 'no'}")
if step_count >= 2:
    print("**Recommendation:** Invoke strategic planner (multi-step RDR)")
    print("**Default:** yes")
else:
    # Default to YES — false positives (unnecessary planning) are cheap,
    # false negatives (skipping planning on complex work) are expensive
    print("**Recommendation:** Invoke strategic planner")
    print("**Default:** yes")
print()
PYEOF
}

## RDR to Accept

$ARGUMENTS

## Action

> **PROHIBITION — PLANNING CHAIN INTEGRITY**
> You MUST NOT create beads, write plans, enrich beads, or perform any planning/enrichment work yourself.
> You are the **orchestrator only**. Running `bd create`, `bd dep add`, `bd update --description`,
> or writing plan content in the Planning Chain is a HARD STOP — halt and report the error.
> Only the dispatched subagents (strategic-planner, plan-auditor, plan-enricher) do this work.
> Doing it yourself bypasses the audit and enrichment chain, producing unvalidated plans.
>
> **SUBAGENT FAILURE**: If any subagent in the chain fails or returns partial results,
> you MUST NOT compensate by doing the subagent's work yourself. Report the failure,
> state which step broke, and provide the retry command. "Let me finish this directly"
> is the exact behavior this prohibition exists to prevent.
>
> If the Agent tool is not available (e.g., you are a subagent), report:
> "Cannot dispatch planning chain — Agent tool unavailable. Run /nx:rdr-accept from the main conversation."

All RDR metadata is pre-loaded above. Step 8 requires additional tool calls for planning dispatch.

**Notation**: All references to `<ID>` below mean the **RDR ID** value from the script output (e.g. `027`). All references to `<type>` mean the **Type** value (e.g. `design`). Substitute with the actual values.

**Before executing steps below**, call both T2 lookups listed above (memory_get for metadata and gate result). You need these results for Steps 1–2.

- **Step 1 — T2 idempotency and self-healing**: Compare the **File Status** (from script output above) against the **T2 metadata status** (from your memory_get call):
  - **Both show `accepted`**: True no-op. Print `> RDR is already accepted (file and T2 agree). Nothing to do.` and stop.
  - **File shows `accepted`, T2 does not**: Self-healing — update T2 to match file. Print `> Self-healing: file shows accepted but T2 shows <actual-T2-status>. Updating T2.` Use memory_put to set T2 status to `accepted`. Then stop (no further steps needed).
  - **T2 shows `accepted`, file shows `draft`**: Self-healing — update file to match T2. Print `> Self-healing: T2 shows accepted but file shows draft. Updating file.` Change file frontmatter to `status: accepted` and `git add` the updated file. Then stop.
  - **File shows `draft` and T2 shows `draft` (or T2 record not found)**: Normal flow — proceed to Step 2.
- **Step 2 — Verify gate**: Check that the T2 gate result (from your memory_get call) shows `outcome: "PASSED"`. If the record exists but `outcome` is absent or is not `"PASSED"`, treat as BLOCKED. If no gate record exists at all, also BLOCKED. Report **BLOCKED** and stop. Print: `> Run /nx:rdr-gate <ID> first.`
- **Step 3 — Update T2** (T2 is the process authority):
  Use **memory_put** tool: content="status: accepted\naccepted_date: <today YYYY-MM-DD>\ntitle: <title>\ntype: <type>\n(preserve other fields from T2 Metadata lookup)", project="<repo-name>_rdr" (same project as in the T2 Lookups above), title="<ID>", ttl="permanent", tags="rdr,<type>"
- **Step 4 — Update the RDR file**: Change `status: draft` to `status: accepted` in the YAML frontmatter. Add `accepted_date: YYYY-MM-DD` if not present.
- **Step 5 — Update `reviewed-by`**: If `reviewed-by` is empty or placeholder, set to `self` (solo review).
- **Step 6 — Regenerate README**: Update `<rdr-dir>/README.md` (the RDR directory from the script output header) index to reflect the new status.
- **Step 7 — Stage files**: `git add` the modified RDR file and README.
- **Step 8 — Planning handoff**: Use the step count and recommendation from the script output above.
  - **If step_count >= 2**: The planning chain is **MANDATORY**. Do not ask — print `> Multi-step RDR — dispatching planning chain (mandatory).` and proceed to the Planning Chain below.
  - **If step_count < 2**: Ask: "Invoke strategic planner to build execution beads? (y/n) [default: yes]"
    - **If no:** Accept is complete. Print: `> RDR <ID> accepted. Ready for implementation.`
    - **If yes:** Proceed to the Planning Chain below.

---

### Planning Chain (triggered from Step 8 above)

Execute these steps sequentially when the planning handoff triggers (mandatory multi-phase or user opted in). **Reminder: you are the orchestrator. Do NOT create beads or plans yourself.**

**Step 8a — Write T1 context:**
Write T1 scratch entry: Use scratch tool: action="put", content="RDR-<ID>: planning context for <title>. RDR file: <RDR-file-path>", tags="rdr-planning-context,rdr-<ID>"

**Step 8b — Dispatch strategic-planner (MANDATORY — do NOT do this yourself):**
Dispatch `nx:strategic-planner` agent (via Agent tool, subagent_type="nx:strategic-planner") with prompt:
> Create phased execution plan for RDR-<ID>: <title>. RDR file: <RDR-file-path>. Read the RDR content for implementation phases. Create epic and task beads with dependencies.

**Wait for the planner to complete before proceeding.**
Note the plan file path and bead IDs from the planner's output.
**If the planner did not create beads, this is a failure — report it and stop. The RDR acceptance is still valid. To retry the planning chain only, run `/nx:create-plan` manually with the RDR file path.**

**Step 8c — Dispatch plan-auditor (MANDATORY — do NOT skip):**
After the planner completes, dispatch `nx:plan-auditor` agent (via Agent tool, subagent_type="nx:plan-auditor") with prompt:
> Audit the execution plan just created for RDR-<ID>: <title>. Check T1 scratch for rdr-planning-context. Validate the plan against the codebase. Check beads created by the planner.

**Wait for the auditor to complete before proceeding. Do NOT skip this step.**

**Step 8d — Dispatch plan-enricher (MANDATORY — do NOT skip):**
After the auditor completes, dispatch `nx:plan-enricher` agent (via Agent tool, subagent_type="nx:plan-enricher") with prompt:
> Enrich all beads for RDR-<ID>: <title> with audit findings from T1 scratch. Write epic bead ID to T2.

**Wait for the enricher to complete. Do NOT skip this step.**

**Step 8e — Verify chain completion:**
Confirm all three agents ran: planner created beads, auditor validated, enricher enriched.
Print: `> RDR-<ID> accepted. Planning chain complete: planner → auditor → enricher. Use 'bd ready' to see executable tasks.`
**If any step was skipped or failed, report which step broke the chain and provide the retry command.**
