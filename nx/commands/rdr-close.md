---
description: Close an RDR with optional post-mortem, bead decomposition, and T3 archival
---

# RDR Close

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
    else:
        m = re.search(r'^## Metadata\s*\n(.*?)(?=^##|\Z)', text, re.MULTILINE | re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                kv = re.match(r'-?\s*\*\*(\w[\w\s]*?)\*\*:\s*(.+)', line.strip())
                if kv:
                    meta[kv.group(1).strip().lower()] = kv.group(2).strip()
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
            continue  # prose doc, not an RDR
        rdrs.append({
            'file': f.name,
            'path': f,
            'text': text,
            'title': fm.get('title', fm.get('name', f.stem)),
            'status': doc_status,
            'rtype': rtype,
            'priority': fm.get('priority', '?'),
        })
    return rdrs


if not rdr_path.exists():
    print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
    sys.exit(0)

# Strip --reason flag from args before extracting ID
reason_match = re.search(r'--reason\s+(\S+)', args)
close_reason = reason_match.group(1) if reason_match else None
args_clean = re.sub(r'--reason\s+\S+', '', args).strip()

id_match = re.search(r'\d+', args_clean)

if not id_match:
    print("> **Usage**: `/rdr-close <id> [--reason implemented|reverted|abandoned|superseded]`")
    print()
    rdrs = get_all_rdrs(rdr_path)
    print("### Open/Draft RDRs")
    print()
    if rdrs:
        print("| File | Title | Status | Type |")
        print("|------|-------|--------|------|")
        for r in rdrs:
            print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} |")
    else:
        print(f"No RDRs found in `{rdr_dir}`")
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
if close_reason:
    print(f"**Close Reason:** {close_reason}")
print()

# Pre-check: warn if closing as Implemented but status is not Final
if close_reason and close_reason.lower() == 'implemented' and current_status.lower() not in ('final',):
    print(f"> **Warning**: RDR status is `{current_status}`, not `Final`. Consider running `/rdr-gate` first.")
    print()

# T2 metadata (current status)
print("### T2 Metadata (current status)")
try:
    result = subprocess.run(
        ['nx', 'memory', 'get', '--project', f'{repo_name}_rdr', '--title', t2_key],
        capture_output=True, text=True, timeout=10)
    t2_out = (result.stdout or '').strip()
    print(t2_out if t2_out else "No T2 record — will use file metadata")
except Exception as exc:
    print(f"T2 not available: {exc}")
print()

# Implementation Plan section (for bead decomposition)
print("### Implementation Plan (for bead decomposition)")
ip_match = re.search(
    r'^## Implementation Plan\s*\n(.*?)(?=^## |\Z)',
    text, re.MULTILINE | re.DOTALL)
if ip_match:
    section = ip_match.group(1).strip()
    # Limit to first 60 lines to avoid overwhelming output
    lines = section.splitlines()[:60]
    print('\n'.join(lines))
    if len(section.splitlines()) > 60:
        print(f"... ({len(section.splitlines()) - 60} more lines)")
else:
    print("_No `## Implementation Plan` section found in this RDR._")
print()

# Active beads
print("### Active Beads")
try:
    result = subprocess.run(
        ['bd', 'list', '--status=in_progress', '--limit=5'],
        capture_output=True, text=True, timeout=10)
    bd_out = (result.stdout or '').strip()
    print(bd_out if bd_out else "No in-progress beads")
except Exception as exc:
    print(f"Beads not available: {exc}")
PYEOF
}

## RDR to Close

$ARGUMENTS

## Action

All data is pre-loaded above — no additional tool calls needed.

- RDR directory is shown above (from `.nexus.yml` `indexing.rdr_paths[0]`).
- Parse RDR ID and close reason from `$ARGUMENTS` (e.g. `003 --reason implemented`).
- Pre-check warning is shown above if status is not Final when reason is Implemented.
- **Implemented**: review Implementation Plan above for divergences, optionally create post-mortem, decompose into beads.
- **Reverted / Abandoned**: offer post-mortem, no bead decomposition.
- **Superseded**: prompt for superseding RDR ID, cross-link both files.
- Post-mortem archive location: `{rdr_dir}/post-mortem/NNN-kebab-title.md`.
- Update RDR file status field and register close in T2: `nx memory put ... --project {repo}_rdr --title {id}`.
