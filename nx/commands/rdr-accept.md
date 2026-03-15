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
rdr_num = re.search(r'\d+', rdr_file.stem)
t2_key = rdr_num.group(0) if rdr_num else rdr_file.stem

print(f"### RDR: {rdr_file.name}")
print(f"**Title:** {title}  **Current Status:** {current_status}")
print()

# Check current status — only Draft can be accepted (but check T2 for two-way idempotency)
if current_status.lower() not in ('draft', 'accepted'):
    print(f"> **BLOCKED**: RDR status is `{current_status}`. Only Draft RDRs can be accepted.")
    sys.exit(0)

# Check T2 status for idempotency / self-healing
# Use memory_get tool: project="{repo_name}_rdr", title="{t2_key}" to retrieve T2 status
t2_status = None
print(f"**T2 lookup needed**: Use memory_get tool: project=\"{repo_name}_rdr\", title=\"{t2_key}\"")
print()

# Two-way idempotency: both agree on accepted → true no-op
if current_status.lower() == 'accepted' and t2_status == 'accepted':
    print(f"> RDR is already accepted (file and T2 agree). Nothing to do.")
    sys.exit(0)
# Self-healing: file=accepted but T2 behind → flag for Action to repair T2
elif current_status.lower() == 'accepted' and t2_status != 'accepted':
    print(f"> **Self-healing needed**: file shows `accepted` but T2 shows `{t2_status}`.")
    print(f"> Action will update T2 to match file.")
    print()
# Self-healing: T2=accepted but file behind → flag for Action to repair file
elif t2_status == 'accepted' and current_status.lower() != 'accepted':
    print(f"> **Self-healing needed**: T2 shows `accepted` but file shows `{current_status}`.")
    print(f"> Action will update file to match T2.")
    print()

# Check T2 gate result
print("### T2 Gate Result")
print(f"Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"{t2_key}-gate-latest\" to retrieve gate result.")
print(f"If no gate record exists, run `/nx:rdr-gate {t2_key}` first.")
print()

# T2 metadata (for context)
print("### T2 Metadata")
print(f"Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"{t2_key}\" to retrieve T2 metadata.")
print()

print(f"**RDR file path:** `{rdr_file}`")
print()

# Phase count auto-detection for planning handoff
ip_match = re.search(
    r'^## Implementation Plan\s*\n(.*?)(?=^## |\Z)',
    text, re.MULTILINE | re.DOTALL)
phase_count = 0
if ip_match:
    phase_count = len(re.findall(r'^### Phase', ip_match.group(1), re.MULTILINE))

print("### Planning Handoff")
print(f"**Phase count detected:** {phase_count}")
if phase_count >= 2:
    print("**Recommendation:** Invoke strategic planner (multi-phase RDR)")
    print("**Default:** yes")
else:
    print("**Recommendation:** Skip planning (single-phase or no implementation plan)")
    print("**Default:** no")
print()
PYEOF
}

## RDR to Accept

$ARGUMENTS

## Action

All data is pre-loaded above — no additional tool calls needed.

- **Verify gate**: Check that the T2 Gate Result above shows `outcome: "PASSED"`. If it shows `BLOCKED` or is missing, report **BLOCKED** and stop.
- **Self-healing check**: If T2 metadata already shows `status: "accepted"` but the file shows `draft`, update the file to match T2 (repair the file). Report the repair and stop.
- **Update T2 first** (T2 is the process authority):
  Use **memory_put** tool: content="... (same fields from T2 Metadata above, with status: \"accepted\", accepted_date: \"YYYY-MM-DD\")", project="{repo_name}_rdr", title="{id}", ttl="permanent", tags="rdr,{type}"
- **Update the RDR file**: Change `status: draft` to `status: accepted` in the YAML frontmatter. Add `accepted_date: YYYY-MM-DD` if not present.
- **Update `reviewed-by`**: If `reviewed-by` is empty or placeholder, set to `self` (solo review).
- **Regenerate README**: Update `{rdr_dir}/README.md` index to reflect the new status.
- **Stage files**: `git add` the modified RDR file and README.
- **Step 7 (Planning handoff)**: Use the phase count and recommendation above.
  - Ask: "Invoke strategic planner to build execution beads? (y/n) [default from above]"
  - **If no:** Skip — accept is complete. Print: `> RDR {id} accepted. Ready for implementation.`
  - **If yes — execute the full chain (3 sequential dispatches):**

    **Step 7a — Write T1 context:**
    Write T1 scratch entry: Use scratch tool: action="put", content="RDR {id}: planning context for {title}. RDR file: {rdr_file}", tags="rdr-planning-context,rdr-{id}"

    **Step 7b — Dispatch strategic-planner:**
    Dispatch `nx:strategic-planner` agent (via Agent tool, subagent_type="nx:strategic-planner") with prompt:
    > Create phased execution plan for RDR-{id}: {title}. RDR file: {rdr_file}. Read the RDR content for implementation phases. Create epic and task beads with dependencies.
    **Wait for the planner to complete before proceeding.**
    Note the plan file path and bead IDs from the planner's output.

    **Step 7c — Dispatch plan-auditor:**
    After the planner completes, dispatch `nx:plan-auditor` agent (via Agent tool, subagent_type="nx:plan-auditor") with prompt:
    > Audit the execution plan just created for RDR-{id}: {title}. Check T1 scratch for rdr-planning-context. Validate the plan against the codebase. Check beads created by the planner.
    **Wait for the auditor to complete before proceeding.**

    **Step 7d — Dispatch plan-enricher:**
    After the auditor completes, dispatch `nx:plan-enricher` agent (via Agent tool, subagent_type="nx:plan-enricher") with prompt:
    > Enrich all beads for RDR-{id}: {title} with audit findings from T1 scratch. Write epic bead ID to T2.
    **Wait for the enricher to complete.**

    **Step 7e — Report chain completion:**
    Print: `> RDR {id} accepted. Planning chain complete: planner → auditor → enricher. Use 'bd ready' to see executable tasks.`
