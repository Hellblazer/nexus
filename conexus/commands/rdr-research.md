---
description: Add, track, or verify structured research findings for an active RDR
---

# RDR Research

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

# Extract numeric ID from args (skip subcommand words like "add", "status", "verify")
id_match = re.search(r'\d+', args)

if id_match:
    rdr_file = find_rdr_file(rdr_path, id_match.group(0))
    if rdr_file:
        fm, text = parse_frontmatter(rdr_file)
        title = fm.get('title', fm.get('name', rdr_file.stem))
        rdr_num = re.search(r'\d+', rdr_file.stem)
        t2_key = rdr_num.group(0) if rdr_num else rdr_file.stem

        print(f"### RDR {t2_key}: {title}")
        print(f"**File:** `{rdr_file.name}`")
        print()

        # Show the Research Findings section from the file
        rf_match = re.search(
            r'^## Research Findings\s*\n(.*?)(?=^## |\Z)',
            text, re.MULTILINE | re.DOTALL)
        print("#### Research Findings (from file)")
        print()
        if rf_match:
            section = rf_match.group(1).strip()
            print(section if section else "_No content in Research Findings section yet._")
        else:
            print("_No `## Research Findings` section found in this RDR._")
        print()

        # T2 research findings
        print("### Existing Research Findings (T2)")
        try:
            result = subprocess.run(
                ['nx', 'memory', 'list', '--project', f'{repo_name}_rdr'],
                capture_output=True, text=True, timeout=10)
            list_out = (result.stdout or '').strip()
            research_lines = [l for l in list_out.splitlines()
                              if re.match(rf'^{t2_key}-research', l)]
            print('\n'.join(research_lines) if research_lines else "No research findings recorded yet")
        except Exception as exc:
            print(f"T2 not available: {exc}")
    else:
        print(f"> RDR not found for ID: `{id_match.group(0)}`")
        print()
        rdrs = get_all_rdrs(rdr_path)
        print("### Available RDRs")
        print()
        if rdrs:
            print("| File | Title | Status | Type |")
            print("|------|-------|--------|------|")
            for r in rdrs:
                print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} |")
        else:
            print(f"No RDRs found in `{rdr_dir}`")
else:
    # No ID — show available RDRs
    rdrs = get_all_rdrs(rdr_path)
    print("### Available RDRs")
    print()
    if rdrs:
        print("| File | Title | Status | Type |")
        print("|------|-------|--------|------|")
        for r in rdrs:
            print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} |")
    else:
        print(f"No RDRs found in `{rdr_dir}`")
    print()
    print("> **Usage**: `/nx:rdr-research <id>` or `/nx:rdr-research add <id>` or `/nx:rdr-research verify <id> <seq>`")
PYEOF
}

## Subcommand and Arguments

$ARGUMENTS

## Action

All data is pre-loaded above — no additional tool calls needed.

- RDR directory is shown above (from `.nexus.yml` `indexing.rdr_paths[0]`).
- Subcommands: `add <id>`, `status <id>`, `verify <id> <seq>`.
- Parse subcommand and RDR ID from `$ARGUMENTS`.
- Existing T2 findings and file Research Findings section are pre-loaded above.
- Dispatch `codebase-deep-analyzer` or `deep-research-synthesizer` if investigation (not just recording) is requested.
