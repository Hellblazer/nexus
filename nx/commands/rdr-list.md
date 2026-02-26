---
description: List all RDRs with status, type, and priority
---

# RDR List

!{
python3 << 'PYEOF'
import os, sys, re, subprocess
from pathlib import Path

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
        m = (re.search(r'rdr_paths[^\[]*\[([^\]]+)\]', content) or
             re.search(r'rdr_paths:\s*\n\s+-\s*(.+)', content))
        if m:
            v = m.group(1)
            parts = re.findall(r'[a-z][a-z0-9/_-]+', v)
            rdr_dir = parts[0] if parts else 'docs/rdr'

rdr_path = Path(repo_root) / rdr_dir
print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
print()

if not rdr_path.exists():
    print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
    sys.exit(0)

# Find RDR files — any .md that looks like an RDR (has number prefix or RDR- prefix)
# Excludes README.md, TEMPLATE.md, INDEX.md, and prose docs
EXCLUDED = {'readme.md', 'template.md', 'index.md', 'overview.md',
            'workflow.md', 'templates.md'}
all_md = sorted(rdr_path.glob('*.md'))
rdr_files = [f for f in all_md if f.name.lower() not in EXCLUDED]


def parse_frontmatter(filepath):
    """Parse YAML frontmatter or ## Metadata section; fall back to H1 for title."""
    text = filepath.read_text(errors='replace')
    meta = {}

    # YAML frontmatter (--- ... ---)
    if text.startswith('---'):
        parts = text.split('---', 2)
        if len(parts) >= 3:
            block = parts[1]
            try:
                import yaml
                meta = yaml.safe_load(block) or {}
            except Exception:
                for line in block.splitlines():
                    if ':' in line:
                        k, _, v = line.partition(':')
                        meta[k.strip().lower()] = v.strip()
    else:
        # ## Metadata section with "- **Key**: Value" lines
        m = re.search(r'^## Metadata\s*\n(.*?)(?=^##|\Z)', text,
                      re.MULTILINE | re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                kv = re.match(r'-?\s*\*\*(\w[\w\s]*?)\*\*:\s*(.+)', line.strip())
                if kv:
                    meta[kv.group(1).strip().lower()] = kv.group(2).strip()

    # Fall back to H1 heading for title if metadata has none
    if 'title' not in meta and 'name' not in meta:
        h1 = re.search(r'^#\s+(.+)', text, re.MULTILINE)
        if h1:
            meta['title'] = h1.group(1).strip()

    return meta


rdrs = []
for f in rdr_files:
    fm = parse_frontmatter(f)
    rtype = fm.get('type', '?')
    doc_status = fm.get('status', '?')
    # Skip prose docs that lack RDR metadata (no status or type field)
    if doc_status == '?' and rtype == '?':
        continue
    rdrs.append({
        'file': f.name,
        'title': fm.get('title', fm.get('name', f.stem)),
        'status': doc_status,
        'rtype': rtype,
        'priority': fm.get('priority', '?'),
    })

print(f"### RDR Files ({len(rdrs)} found)")
print()
if rdrs:
    print("| File | Title | Status | Type | Priority |")
    print("|------|-------|--------|------|----------|")
    for r in rdrs:
        print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} | {r['priority']} |")
else:
    print(f"No RDR files found in `{rdr_dir}`")
print()

# T2 records
print("### T2 Records")
try:
    result = subprocess.run(
        ['nx', 'memory', 'list', '--project', f'{repo_name}_rdr'],
        capture_output=True, text=True, timeout=10)
    t2_out = (result.stdout or '').strip()
    print(t2_out if t2_out else "No T2 RDR records")
except Exception as exc:
    print(f"T2 not available: {exc}")
PYEOF
}

## Filters

$ARGUMENTS

## Action

All RDR data is pre-loaded above — no additional tool calls needed.

Format the pre-gathered data as a clean index table. Apply any filters from `$ARGUMENTS` (e.g. `--status=draft`, `--type=feature`, `--has-assumptions`) to the table. Emit drift warnings if T2 records exist without corresponding files, or vice versa.
