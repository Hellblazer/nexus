#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discovery header for the /conexus:rdr-audit slash command.

Extracted from conexus/commands/rdr-audit.md so the !{ } command block
invokes a script by path instead of wrapping a Python heredoc. Claude
Code's command runner emits a heredoc-bearing !{ } block as raw source
instead of executing it (nexus-t1b1k); a plain `python3 <path>` call
matches the working echo/-c form. ARGUMENTS arrive via NEXUS_RDR_ARGS.
"""
import os, subprocess
from pathlib import Path

args = os.environ.get('NEXUS_RDR_ARGS', '').strip()

# Derive current project name (precedence: git remote → pwd basename).
# Note: the skill documents a third fallback of prompting the user when the
# derivation is ambiguous — that interactive step is handled in the skill body,
# not in this Python preamble (which must be safe for headless `claude -p` use
# where no TTY is available for prompts).
def derive_project_name():
    try:
        url = subprocess.check_output(
            ['git', 'remote', 'get-url', 'origin'],
            stderr=subprocess.DEVNULL, text=True).strip()
        if url:
            name = url.rsplit('/', 1)[-1]
            if name.endswith('.git'):
                name = name[:-4]
            if name:
                return name
    except Exception:
        pass
    try:
        root = subprocess.check_output(
            ['git', 'rev-parse', '--show-toplevel'],
            stderr=subprocess.DEVNULL, text=True).strip()
        if root:
            return os.path.basename(root)
    except Exception:
        pass
    return os.path.basename(os.getcwd())

current_project = derive_project_name()

READONLY_SUBCOMMANDS = {'list', 'status', 'history'}
PRINTONLY_SUBCOMMANDS = {'schedule', 'unschedule'}
SUBCOMMANDS = READONLY_SUBCOMMANDS | PRINTONLY_SUBCOMMANDS

first_token = args.split()[0] if args else ''
if first_token in SUBCOMMANDS:
    mode = 'management'
    subcommand = first_token
    target = args[len(first_token):].strip() or (current_project if subcommand != 'list' else '')
    safety_class = 'read-only' if subcommand in READONLY_SUBCOMMANDS else 'print-only'
    print(f"**Mode:** management subcommand `{subcommand}` ({safety_class})")
    if target:
        print(f"**Target project:** `{target}`")
    else:
        print(f"**Scope:** all scheduled audits on this machine")
    print()
    if safety_class == 'read-only':
        print(f"> `{subcommand}` is read-only — no OS state mutation, no T2 state mutation.")
        print(f"> The skill body shells out to `launchctl list` / `crontab -l` / `memory_search` / `memory_get` only.")
    else:
        print(f"> `{subcommand}` is print-only — prints install/uninstall instructions for user review.")
        print(f"> The skill body does NOT run `launchctl load/unload`, does NOT write plist files, does NOT edit crontab.")
        print(f"> All system-level installs are the user's explicit manual step.")
    print()
else:
    mode = 'audit'
    target = first_token or current_project
    print(f"**Mode:** audit dispatch (default)")
    print(f"**Target project:** `{target}`" + (" (derived from current repo)" if not first_token else ""))
    print()

    # Probe for the target project's worktree on the local machine.
    # Precedence:
    #   1. NEXUS_PROJECT_ROOTS env var — colon-separated list of directories under which
    #      the user keeps project worktrees (e.g. "$HOME/src:$HOME/work")
    #   2. Default candidate roots — common conventions, none assumed authoritative
    home = Path.home()
    roots_env = os.environ.get('NEXUS_PROJECT_ROOTS', '').strip()
    if roots_env:
        roots = [Path(os.path.expanduser(r)) for r in roots_env.split(':') if r]
        roots_source = 'NEXUS_PROJECT_ROOTS'
    else:
        roots = [
            home / 'git',
            home / 'src',
            home / 'projects',
            home / 'code',
            home / 'work',
            home / 'dev',
            home / 'Documents' / 'git',
        ]
        roots_source = 'default candidates (set NEXUS_PROJECT_ROOTS to override)'
    candidate_paths = [r / target for r in roots if r.is_dir()]
    found_path = next((p for p in candidate_paths if p.exists() and p.is_dir()), None)
    if found_path:
        print(f"**Worktree found:** `{found_path}`")
        postmortem_dir = found_path / 'docs' / 'rdr' / 'post-mortem'
        if postmortem_dir.exists():
            count = len(list(postmortem_dir.glob('*.md')))
            print(f"**Post-mortems available:** {count} files in `{postmortem_dir}`")
        else:
            print(f"> No `docs/rdr/post-mortem/` directory found at `{found_path}`. The audit will fall back to alternate paths per the canonical prompt.")
    else:
        probed = ', '.join(str(r) for r in roots if r.is_dir()) or '(no existing roots)'
        print(f"> No local worktree found for `{target}`. Probed roots ({roots_source}): {probed}.")
        print(f"> Set `NEXUS_PROJECT_ROOTS` (colon-separated) to the directory(ies) where you keep project worktrees, or pass an explicit absolute path as the project argument.")
        print(f"> The audit will proceed with T2 `rdr_process` evidence only.")

    # Check for session transcripts
    claude_projects = home / '.claude' / 'projects'
    if claude_projects.exists():
        # The directory-mangled naming scheme varies; glob for any matching token
        match_candidates = list(claude_projects.glob(f'*{target}*'))
        if match_candidates:
            print(f"**Session transcripts available:** {len(match_candidates)} matching directory entries in `~/.claude/projects/`")
        else:
            print(f"> No session transcripts found for `{target}` under `~/.claude/projects/`. The main-session transcript pre-step will use the fast path (empty excerpt block).")

print()
