---
description: List all RDRs with status, type, and priority
---

# RDR List

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

if not rdr_path.exists():
    print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
    sys.exit(0)


def _parse_t2_field(content, field):
    """Extract a field value from T2 content."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{field}:"):
            val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            return val
    return None


def get_rdrs_from_t2(repo_name):
    """Read RDR list from T2 (process authority). Returns list of dicts."""
    rdrs = []
    try:
        result = subprocess.run(
            ['nx', 'memory', 'list', '--project', f'{repo_name}_rdr'],
            capture_output=True, text=True, timeout=10)
        list_out = (result.stdout or '').strip()
        if not list_out:
            return rdrs
        # Each line is a T2 record title; filter to RDR records (pure numeric IDs)
        for line in list_out.splitlines():
            title = line.strip().split()[0] if line.strip() else ''
            if not re.match(r'^\d+$', title):
                continue  # skip gate-latest, research, etc.
            # Fetch full record
            rec = subprocess.run(
                ['nx', 'memory', 'get', '--project', f'{repo_name}_rdr', '--title', title],
                capture_output=True, text=True, timeout=10)
            content = (rec.stdout or '').strip()
            if not content:
                continue
            rdrs.append({
                'id': title,
                'title': _parse_t2_field(content, 'title') or title,
                'status': _parse_t2_field(content, 'status') or '?',
                'rtype': _parse_t2_field(content, 'type') or '?',
                'priority': _parse_t2_field(content, 'priority') or '?',
                'file_path': _parse_t2_field(content, 'file_path') or f'{rdr_dir}/{title}-*.md',
            })
    except Exception:
        pass
    return rdrs


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


EXCLUDED = {'readme.md', 'template.md', 'index.md', 'overview.md', 'workflow.md', 'templates.md'}


def get_all_rdrs_from_files(rdr_path):
    """Fallback: read RDR list from files."""
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
        nums = re.findall(r'\d+', f.stem)
        rdrs.append({
            'id': nums[0] if nums else f.stem,
            'file': f.name,
            'title': fm.get('title', fm.get('name', f.stem)),
            'status': doc_status,
            'rtype': rtype,
            'priority': fm.get('priority', '?'),
        })
    return rdrs


# Primary: read from T2
rdrs = get_rdrs_from_t2(repo_name)
source = 'T2'

# Fallback: read from files if T2 is empty
if not rdrs:
    rdrs = get_all_rdrs_from_files(rdr_path)
    source = 'files'

print(f"### RDRs ({len(rdrs)} found, source: {source})")
print()
if rdrs:
    print("| ID | Title | Status | Type | Priority |")
    print("|----|-------|--------|------|----------|")
    for r in rdrs:
        print(f"| {r['id']} | {r['title']} | {r['status']} | {r['rtype']} | {r['priority']} |")
else:
    print(f"No RDRs found in `{rdr_dir}`")
PYEOF
}

## Filters

$ARGUMENTS

## Action

All data is pre-loaded above — no additional tool calls needed.

Format the pre-gathered data as a clean index table. Apply any filters from `$ARGUMENTS` (e.g. `--status=draft`, `--type=feature`) to the table. The data source is shown (T2 or files fallback). T2 is the process authority; SessionStart reconciliation keeps it in sync with files.
