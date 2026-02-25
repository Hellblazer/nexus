#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PreCompact hook: detect and save active sequential thinking chains to T2.

Receives the conversation transcript path via stdin JSON, parses the JSONL
transcript to find in-progress sequential thinking chains (from the
nx:sequential-thinking skill pattern), and saves them to nx memory before
context is compacted.

Recovery is handled by the post-compact SessionStart hook entry, which
reads the saved chain from T2 and injects it back into Claude's context.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

SENTINEL = Path.home() / '.config' / 'nexus' / 'thought-chain-pending'

# Matches: **Thought N of ~T** or **Thought N of ~T [flags]**
THOUGHT_HEADER = re.compile(r'\*\*Thought (\d+) of ~(\d+)\*\*')
DONE_SIGNAL = re.compile(r'nextThoughtNeeded:\s*false', re.IGNORECASE)


def _extract_text(content: object) -> str:
    """Extract text from message content (string or list of content blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            match block.get('type', ''):
                case 'text':
                    parts.append(block.get('text', ''))
                case 'thinking':
                    parts.append(block.get('thinking', ''))
        return '\n'.join(parts)
    return ''


def _find_thought_chain(transcript_path: Path) -> list[dict] | None:
    """Parse JSONL transcript, return last active thought chain or None."""
    thoughts: list[dict] = []

    try:
        with open(transcript_path) as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                role = entry.get('role') or entry.get('type', '')
                if role not in ('assistant', 'assistant_message'):
                    continue

                text = _extract_text(entry.get('content', ''))
                if not text:
                    continue

                for m in THOUGHT_HEADER.finditer(text):
                    thought_num = int(m.group(1))
                    total_est = int(m.group(2))
                    start = m.start()
                    nxt = THOUGHT_HEADER.search(text, m.end())
                    end = nxt.start() if nxt else len(text)
                    body = text[start:end].strip()
                    thoughts.append({
                        'number': thought_num,
                        'total': total_est,
                        'text': body,
                        'done': bool(DONE_SIGNAL.search(body)),
                    })
    except OSError:
        return None

    if not thoughts:
        return None

    # Chain is complete — nothing to preserve
    if thoughts[-1]['done']:
        return None

    return thoughts


def _project_name() -> str:
    try:
        r = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return Path(r.stdout.strip()).name
    except Exception:
        pass
    return 'project'


def _save_chain(thoughts: list[dict], project: str) -> bool:
    last = thoughts[-1]
    lines = [
        '# Active Sequential Thinking Chain',
        '',
        f'**Progress:** Thought {last["number"]} of ~{last["total"]} — INCOMPLETE',
        f'**Next:** Resume at Thought {last["number"] + 1} of ~{last["total"]}',
        '',
        '---',
        '',
    ]
    for t in thoughts:
        lines.append(t['text'])
        lines.append('')
    lines += [
        '---',
        '',
        '**Recovery instruction:** This chain was active when the context was compacted.',
        'Continue using the `nx:sequential-thinking` skill from where it left off.',
    ]
    content = '\n'.join(lines)

    try:
        r = subprocess.run(
            ['nx', 'memory', 'put', content,
             '--project', f'{project}_active',
             '--title', 'sequential-thinking-chain.md',
             '--ttl', '7d'],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        sys.exit(0)

    transcript_path = Path(data.get('transcript_path', ''))
    if not transcript_path.exists():
        sys.exit(0)

    thoughts = _find_thought_chain(transcript_path)
    if not thoughts:
        sys.exit(0)

    project = _project_name()
    last = thoughts[-1]
    saved = _save_chain(thoughts, project)

    if saved:
        # Write sentinel so the UserPromptSubmit hook knows to re-inject.
        try:
            SENTINEL.parent.mkdir(parents=True, exist_ok=True)
            SENTINEL.touch()
        except OSError:
            pass
        print(
            f'[nx] Sequential thinking chain saved: '
            f'Thought {last["number"]} of ~{last["total"]} — will be re-injected on next prompt.',
            file=sys.stderr,
        )
    else:
        print(
            f'[nx] WARNING: Active thought chain (Thought {last["number"]} of ~{last["total"]}) '
            f'detected but could not be saved to T2 — nx may not be installed.',
            file=sys.stderr,
        )


if __name__ == '__main__':
    main()
