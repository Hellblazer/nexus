---
description: Create a new RDR — scaffold from template, assign sequential ID, register in T2
---

# New RDR

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
    print(f"> RDR directory `{rdr_dir}` does not exist — bootstrap required.")
    print()
    print("**Next ID:** `RDR-001`")
    print("**ID style detected:** `RDR-NNN-kebab-title.md` (default — no existing files)")
    print()
    print("### Existing RDRs (0 found)")
    print()
    print("None — this will be the first RDR.")
else:
    rdrs = get_all_rdrs(rdr_path)

    # Detect ID style from existing files
    rdr_prefix_style = False
    numeric_style = False
    for r in rdrs:
        if re.match(r'^RDR-\d+', r['file']):
            rdr_prefix_style = True
            break
        elif re.match(r'^\d+', r['file']):
            numeric_style = True

    if rdr_prefix_style:
        id_style = 'RDR-NNN-kebab-title.md'
    elif numeric_style:
        id_style = 'NNN-kebab-title.md'
    else:
        id_style = 'RDR-NNN-kebab-title.md'  # default for new repos

    # Compute next sequential ID
    max_num = 0
    for r in rdrs:
        nums = re.findall(r'\d+', r['file'])
        if nums:
            max_num = max(max_num, int(nums[0]))
    next_num = max_num + 1

    if rdr_prefix_style:
        next_id = f"RDR-{next_num:03d}"
    else:
        next_id = f"{next_num:03d}"

    print(f"**Next ID:** `{next_id}`")
    print(f"**ID style detected:** `{id_style}`")
    print()
    print(f"### Existing RDRs ({len(rdrs)} found)")
    print()
    if rdrs:
        print("| File | Title | Status |")
        print("|------|-------|--------|")
        for r in rdrs:
            print(f"| {r['file']} | {r['title']} | {r['status']} |")
    else:
        print("None — this will be the first RDR.")

print()

# Active beads (for Related Issues field)
print("### Active Beads (for Related Issues field)")
try:
    result = subprocess.run(
        ['bd', 'list', '--status=in_progress', '--limit=5'],
        capture_output=True, text=True, timeout=10)
    bd_out = (result.stdout or '').strip()
    print(bd_out if bd_out else "No in-progress beads")
except Exception as exc:
    print(f"Beads not available: {exc}")
print()

# PM context
print("### PM Context")
try:
    result = subprocess.run(
        ['nx', 'pm', 'status'],
        capture_output=True, text=True, timeout=10)
    pm_out = (result.stdout or '').strip()
    print(pm_out if pm_out else "No PM initialized")
except Exception as exc:
    print(f"PM not available: {exc}")
PYEOF
}

## Title / Details

$ARGUMENTS

## Action

All data is pre-loaded above — no additional tool calls needed.

- RDR directory is shown above (from `.nexus.yml` `indexing.rdr_paths[0]`).
- Use the existing RDR list to determine the next sequential ID (shown as **Next ID** above).
- Use the detected ID style (`RDR-NNN-*` or `NNN-*`) for the new filename.
- If `$ARGUMENTS` contains a title, pre-fill it; otherwise prompt.
- If the RDR directory does not exist, run bootstrap: create the directory and copy templates from `$CLAUDE_PLUGIN_ROOT/resources/rdr/`.
- Register the new RDR in T2: `nx memory put ... --project {repo}_rdr --title {id}`.
