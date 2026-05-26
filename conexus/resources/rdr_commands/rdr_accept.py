#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discovery header for the /conexus:rdr-accept slash command.

Extracted from conexus/commands/rdr-accept.md so the !{ } command block
invokes a script by path instead of wrapping a Python heredoc. Claude
Code's command runner emits a heredoc-bearing !{ } block as raw source
instead of executing it (nexus-t1b1k); a plain `python3 <path>` call
matches the working echo/-c form. ARGUMENTS arrive via NEXUS_RDR_ARGS.
"""
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
    print("> **Usage**: `/conexus:rdr-accept <id>` — e.g. `/conexus:rdr-accept 003` or `/conexus:rdr-accept RDR-003`")
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
print(f"   If no gate record exists, run `/conexus:rdr-gate {t2_key}` first.")
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
