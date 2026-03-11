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

# Strip flags from args before extracting ID
reason_match = re.search(r'--reason\s+(\S+)', args)
close_reason = reason_match.group(1) if reason_match else None
force = bool(re.search(r'--force', args))
args_clean = re.sub(r'--reason\s+\S+', '', args)
args_clean = re.sub(r'--force', '', args_clean).strip()

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

# Hard-block: refuse to close unless status is accepted or final (P1 from RDR-001)
if current_status.lower() not in ('accepted', 'final'):
    if force:
        print(f"> **Override**: RDR status is `{current_status}` (not accepted/final). Proceeding with `--force`.")
        print()
    else:
        print(f"> **BLOCKED**: RDR status is `{current_status}`. Close requires status `accepted` or `final`.")
        print(f"> Run `/rdr-gate` to validate, or use `--force` to override.")
        print()
        sys.exit(0)

# T2 metadata (current status)
print("### T2 Metadata (current status)")
print(f"Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"{t2_key}\" to retrieve T2 metadata.")
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
- Update RDR file status field and register close in T2: use **memory_put** tool: project="{repo}_rdr", title="{id}" with updated status fields.
