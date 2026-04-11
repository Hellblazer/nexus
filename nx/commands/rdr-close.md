---
description: Close an RDR with optional post-mortem, bead status gate, and T3 archival
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
# --pointers may be single-quoted, double-quoted, or a single unquoted token
pointers_match = (re.search(r"--pointers\s+'([^']+)'", args)
                  or re.search(r'--pointers\s+"([^"]+)"', args)
                  or re.search(r'--pointers\s+(\S+)', args))
pointers_arg = pointers_match.group(1) if pointers_match else None
args_clean = re.sub(r'--reason\s+\S+', '', args)
args_clean = re.sub(r'--force', '', args_clean)
args_clean = re.sub(r"--pointers\s+'[^']+'", '', args_clean)
args_clean = re.sub(r'--pointers\s+"[^"]+"', '', args_clean)
args_clean = re.sub(r'--pointers\s+\S+', '', args_clean).strip()

id_match = re.search(r'\d+', args_clean)

if not id_match:
    print("> **Usage**: `/nx:rdr-close <id> [--reason implemented|reverted|abandoned|superseded]`")
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
        print(f"> Run `/nx:rdr-gate` to validate, or use `--force` to override.")
        print()
        sys.exit(0)

# === Gap 1: Two-pass Problem Statement Replay (RDR-065) ===
# HARD ENFORCEMENT SURFACE per HA-5: this preamble — not SKILL.md text — is the
# only place an agent cannot reason past. Pass 1 enumerates gaps; Pass 2 validates
# per-gap file:line pointers. Grandfathering is ID-based (rdr_id_int < 65), never
# date-based — see CA-4 in docs/rdr/rdr-065-close-time-funnel-hardening.md.
if (close_reason or '').lower() == 'implemented':
    def _extract_section(doc, heading):
        idx = doc.find(heading)
        if idx == -1:
            return ''
        rest = doc[idx + len(heading):]
        nxt = re.search(r'\n## ', rest)
        return rest[:nxt.start()] if nxt else rest

    def _parse_pointers(s):
        out = {}
        for tok in s.split(','):
            tok = tok.strip()
            if '=' in tok:
                k, _, v = tok.partition('=')
                out[k.strip()] = v.strip()
        return out

    try:
        rdr_id_int = int(t2_key)
    except ValueError:
        rdr_id_int = -1
    rdr_id_label = t2_key
    problem_stmt = _extract_section(text, '## Problem Statement')
    # Regex permits parenthetical context between number and colon
    # (e.g., `#### Gap 4 (prerequisite for Gap 1): <title>`) — author convenience.
    gap_matches = re.findall(r'^#### Gap (\d+)([^\n:]*):\s*(.*)$', problem_stmt, re.MULTILINE)
    gap_count = len(gap_matches)

    if rdr_id_int < 65 and gap_count == 0:
        # GRANDFATHERING: legacy RDR with no structured gaps — warn and proceed.
        print("> **WARN**: This RDR predates structured gaps; no action required — gate does not apply.")
        print()
    elif rdr_id_int >= 65 and gap_count == 0:
        # MALFORMED-NEW: post-policy RDR is missing the required gap headings — block.
        print(f"> **ERROR**: RDR-{rdr_id_label} has no `#### Gap N: <title>` headings in `## Problem Statement`.")
        print(r"> Expected format: `#### Gap 1: <gap title>` (regex: `^#### Gap \d+:`)")
        print()
        sys.exit(0)
    elif gap_count > 0 and not pointers_arg:
        # PASS 1: enumerate gaps and instruct re-invoke with --pointers.
        print("### Problem Statement Gaps")
        print()
        for num, qual, title in gap_matches:
            qual_str = qual.strip()
            qual_disp = f" {qual_str}" if qual_str else ""
            print(f"- Gap{num}{qual_disp}: {title.strip()}")
        print()
        print("**Re-invoke with per-gap closure pointers:**")
        print()
        example = ",".join(f"Gap{num}=path/to/file.py:LINE" for num, _q, _t in gap_matches)
        print(f"```\n/nx:rdr-close {rdr_id_label} --reason implemented --pointers '{example}'\n```")
        print()
        sys.exit(0)
    else:
        # PASS 2: validate that every gap has a pointer and the file exists.
        pointers = _parse_pointers(pointers_arg)
        failures = []
        for num, _qual, _title in gap_matches:
            gap_key = f"Gap{num}"
            if gap_key not in pointers:
                failures.append(f"{gap_key}: no pointer supplied")
                continue
            file_part = pointers[gap_key].partition(':')[0]
            if not (Path(repo_root) / file_part).exists():
                failures.append(f"{gap_key}: file '{file_part}' does not exist in repo")
        if failures:
            print("> **ERROR**: Problem Statement pointer validation failed:")
            for f in failures:
                print(f">   - {f}")
            print(">")
            print("> The gate verifies you have committed to a specific file:line pointer per gap.")
            print("> It does not verify the pointer is semantically correct.")
            print()
            sys.exit(0)
        # Validation passed — emit framing and set T1 active-close marker.
        print("### PROBLEM STATEMENT REPLAY: validation passed")
        print()
        for gap_key, ptr in sorted(pointers.items()):
            print(f"- {gap_key} → {ptr}")
        print()
        print("> The gate verifies you have committed to a specific file:line pointer per gap.")
        print("> It does not verify the pointer is semantically correct. Correctness is your responsibility.")
        print()
        try:
            subprocess.run(
                ['nx', 'scratch', 'put', rdr_id_label,
                 '--tags', f'rdr-close-active,rdr-{rdr_id_label}'],
                capture_output=True, timeout=5)
        except Exception:
            pass

# T2 metadata (current status)
print("### T2 Metadata (current status)")
print(f"Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"{t2_key}\" to retrieve T2 metadata.")
print()

# Bead status gate
print("### Bead Status Advisory")
print(f"Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"{t2_key}\" to check for `epic_bead` field.")
print(f"If `epic_bead` exists, run `bd show <epic-id>` to display child bead statuses.")
print(f"If no `epic_bead`, check command output for open beads listed below.")
print()

# Active beads — check for open beads linked to this RDR's epic
has_open_beads = False
print("### Active Beads")
try:
    # Check epic bead children first
    result = subprocess.run(
        ['bd', 'list', '--status=open,in_progress', '--limit=20'],
        capture_output=True, text=True, timeout=10)
    bd_out = (result.stdout or '').strip()
    if bd_out and bd_out != "No issues found.":
        has_open_beads = True
        print(bd_out)
    else:
        print("No open or in-progress beads.")
except Exception as exc:
    print(f"Beads not available: {exc}")

if has_open_beads:
    print()
    print("> **⚠ WARNING: Open beads exist.** You MUST ask the user for explicit")
    print("> confirmation before closing this RDR. Do NOT proceed without their approval.")
    print("> Show them the open beads above and ask: \"Close RDR with these beads still open?\"")
    print()
PYEOF
}

## RDR to Close

$ARGUMENTS

## Action

All data is pre-loaded above — no additional tool calls needed.

- RDR directory is shown above (from `.nexus.yml` `indexing.rdr_paths[0]`).
- Parse RDR ID and close reason from `$ARGUMENTS` (e.g. `003 --reason implemented`).
- Pre-check warning is shown above if status is not Final when reason is Implemented.
- **Implemented**: review for divergences, optionally create post-mortem, gate on open bead status.
- **Reverted / Abandoned**: offer post-mortem.
- **Superseded**: prompt for superseding RDR ID, cross-link both files.
- Post-mortem archive location: `{rdr_dir}/post-mortem/NNN-kebab-title.md`.
- Update RDR file status field and register close in T2: use **memory_put** tool: project="{repo}_rdr", title="{id}" with updated status fields.
