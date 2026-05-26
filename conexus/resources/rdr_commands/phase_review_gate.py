#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discovery header for the /conexus:phase-review-gate slash command.

Extracted from conexus/commands/phase-review-gate.md so the !{ } command block
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

# Allow test harness to override RDR directory location.
rdr_dir_override = os.environ.get('NEXUS_RDR_DIR_OVERRIDE', '').strip()
if rdr_dir_override:
    rdr_path = Path(rdr_dir_override)
    repo_root = str(rdr_path.parent.parent) if rdr_path.name == 'rdr' else repo_root
else:
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


def extract_approach_section(text):
    """Extract content under §Approach (handles '### Approach' and variants)."""
    # Match heading with optional parenthetical qualifier
    for pat in [
        r'\n### Approach[^\n]*\n',
        r'\n## Approach[^\n]*\n',
        r'\n#### Approach[^\n]*\n',
    ]:
        m = re.search(pat, text)
        if m:
            start = m.end()
            # End at the next heading of same or higher level
            heading_depth = len(re.match(r'(#+)', m.group(0).strip()).group(1))
            end_pat = r'\n#{1,' + str(heading_depth) + r'} '
            nxt = re.search(end_pat, text[start:])
            return text[start : start + nxt.start()] if nxt else text[start:]
    return ''


def parse_approach_items(approach_text):
    """Parse numbered list items from §Approach.

    Returns list of (item_num: int, label: str, summary: str).
    Label is the bold text after the number; summary is the first line.
    Handles multi-line continuation paragraphs.
    """
    items = []
    # Split on numbered list starters: lines matching /^\d+\. /
    lines = approach_text.splitlines()
    current_num = None
    current_label = ''
    current_lines = []

    for line in lines:
        m = re.match(r'^(\d+)\.\s+\*\*([^*]+)\*\*[:\s]*(.*)', line)
        if m:
            if current_num is not None:
                items.append((current_num, current_label, ' '.join(current_lines).strip()))
            current_num = int(m.group(1))
            current_label = m.group(2).strip()
            current_lines = [m.group(3).strip()] if m.group(3).strip() else []
        elif current_num is not None:
            stripped = line.strip()
            if stripped and not stripped.startswith('-'):
                current_lines.append(stripped)

    if current_num is not None:
        items.append((current_num, current_label, ' '.join(current_lines).strip()))

    return items


def parse_evidence(evidence_str):
    """Parse 'Item1=val1,Item2=val2,...' into {1: 'val1', 2: 'val2'}."""
    out = {}
    for tok in evidence_str.split(','):
        tok = tok.strip()
        if not tok:
            continue
        if '=' in tok:
            k, _, v = tok.partition('=')
            k = k.strip()
            v = v.strip()
            # Accept ItemN or item N (case-insensitive)
            num_m = re.search(r'(\d+)', k)
            if num_m:
                out[int(num_m.group(1))] = v
    return out


# Parse flags
phase_match = re.search(r'--phase\s+(\S+)', args)
phase_arg = phase_match.group(1) if phase_match else None

evidence_match = (re.search(r"--evidence\s+'([^']+)'", args)
                  or re.search(r'--evidence\s+"([^"]+)"', args)
                  or re.search(r'--evidence\s+(\S+)', args))
evidence_arg = evidence_match.group(1) if evidence_match else None

# Strip flags to find RDR ID
args_clean = re.sub(r'--phase\s+\S+', '', args)
args_clean = re.sub(r"--evidence\s+'[^']+'", '', args_clean)
args_clean = re.sub(r'--evidence\s+"[^"]+"', '', args_clean)
args_clean = re.sub(r'--evidence\s+\S+', '', args_clean).strip()

id_match = re.search(r'\d+', args_clean)

if not id_match:
    print("> **Usage**: `/conexus:phase-review-gate <id> --phase <N> [--evidence 'Item1=bead-id,...']`")
    print()
    print("### What this gate does")
    print()
    print("At each phase-review boundary, cross-walk the RDR §Approach sub-items")
    print("against the closing beads. Pass 1 enumerates items; Pass 2 validates evidence.")
    print()
    print("**Pass 1** (no --evidence): list approach items for the phase.")
    print("**Pass 2** (with --evidence): validate every item has an evidence pointer.")
    print()
    print("Evidence format: `Item1=nexus-abc1,Item2=nexus-xyz2,Item3=none`")
    print("Use `none` for items explicitly deferred or acknowledged as out-of-phase scope.")
    sys.exit(0)

rdr_file = find_rdr_file(rdr_path, id_match.group(0))
if not rdr_file:
    print(f"> **ERROR**: RDR not found for ID: `{id_match.group(0)}`")
    print(f"> Looked in: `{rdr_path}`")
    sys.exit(0)

fm, text = parse_frontmatter(rdr_file)
title = fm.get('title', fm.get('name', rdr_file.stem))
rdr_num_m = re.search(r'\d+', rdr_file.stem)
rdr_id_label = rdr_num_m.group(0) if rdr_num_m else rdr_file.stem

print(f"**Repo:** `{repo_name}`  **RDR:** `{rdr_file.name}`")
print(f"**Title:** {title}")
print(f"**Phase:** {phase_arg or '(not specified)'}")
print()

# Extract §Approach items
approach_text = extract_approach_section(text)
if not approach_text.strip():
    print("> **ERROR**: No `### Approach` section found in this RDR.")
    print("> Phase-review gate requires §Approach to cross-walk against closing beads.")
    sys.exit(0)

items = parse_approach_items(approach_text)
if not items:
    print("> **ERROR**: §Approach section found but no numbered items parsed.")
    print("> Expected format: `N. **Label**: description`")
    sys.exit(0)

# === PASS 1: enumerate approach items ===
if not evidence_arg:
    print(f"### §Approach Cross-Walk — Phase {phase_arg or '?'}")
    print()
    print("Enumerate each numbered §Approach item below, then provide an evidence pointer")
    print("for each item: the closing bead ID (e.g. `nexus-abc1`) or `none` if the item")
    print("is explicitly deferred or not in scope for this phase.")
    print()
    print("| # | Label | Evidence needed |")
    print("|---|-------|-----------------|")
    for num, label, _summary in items:
        print(f"| Item{num} | **{label}** | (provide bead-id or `none`) |")
    print()
    example_parts = ','.join(f"Item{num}=nexus-xxxx" for num, _, _ in items)
    print("**Re-invoke with evidence once all items are accounted for:**")
    print()
    print(f"```")
    print(f"/conexus:phase-review-gate {rdr_id_label} --phase {phase_arg or '1'} --evidence '{example_parts}'")
    print(f"```")
    print()
    sys.exit(0)

# === PASS 2: validate evidence coverage ===
evidence = parse_evidence(evidence_arg)
failures = []
covered = []

for num, label, _summary in items:
    val = evidence.get(num, '').strip()
    if not val:
        failures.append((num, label, "no evidence pointer supplied"))
    else:
        covered.append((num, label, val))

if failures:
    print(f"> **BLOCKED** — Phase {phase_arg or '?'} cross-walk incomplete.")
    print(f"> {len(failures)} of {len(items)} approach item(s) have no evidence pointer.")
    print()
    print("### Missing Evidence")
    print()
    for num, label, reason in failures:
        print(f"- **Item{num}** ({label}): {reason}")
    print()
    print("These items must be accounted for before closing this phase.")
    print("Provide a closing bead ID or `none` with an acknowledgement for deferred items.")
    print()
    print("**Re-invoke once all items are covered:**")
    example_parts = ','.join(
        f"Item{num}={evidence.get(num, 'nexus-xxxx') or 'nexus-xxxx'}"
        for num, _, _ in items
    )
    print(f"```")
    print(f"/conexus:phase-review-gate {rdr_id_label} --phase {phase_arg or '1'} --evidence '{example_parts}'")
    print(f"```")
    sys.exit(0)

# All items covered — validation passed
print(f"### APPROACH CROSS-WALK PASSED — Phase {phase_arg or '?'}")
print()
print(f"All {len(items)} §Approach items accounted for:")
print()
for num, label, val in covered:
    print(f"- Item{num} ({label}) → `{val}`")
print()
print("> The gate verifies every §Approach item has a named evidence pointer.")
print("> It does NOT verify that the evidence is semantically complete.")
print("> Review each pointer manually before allowing the phase close to proceed.")
print()
# Write T1 scratch marker so downstream hooks can check cross-walk completion
try:
    subprocess.run(
        ['nx', 'scratch', 'put',
         f'phase-review-gate PASSED: RDR-{rdr_id_label} Phase {phase_arg}',
         '--tags', f'phase-review-passed,rdr-{rdr_id_label},phase-{phase_arg}'],
        capture_output=True, timeout=5)
except Exception:
    pass

# RDR-121 P2 co-requirement: write the PASSED sentinel that the
# phase_review_close_requires_gate PreToolUse hook reads. Sweep dead-pid
# sentinels as a side effect. Best-effort; never raises.
try:
    from nexus.phase_review_sentinel import write_sentinel
    write_sentinel(rdr_id_label, str(phase_arg or "1"))
except Exception:
    pass

