# 1. Prerequisites

> **Time**: 3–5 minutes
> **Goal**: Viewer has uv, Python 3.12–3.13, git, and Claude Code installed

---

## VOICE

We need four things: uv, Python, git, and Claude Code. If you have all of these, skip to section two.

Let's check.

## SCREEN [5s]

```bash
git --version
python3 --version
```

## VOICE

We need Python 3.12 or 3.13. If you're on 3.14 or newer, nexus won't install yet. If you're on 3.11 or older, you'll need to upgrade.

Don't worry — uv handles Python versions for you.

[PAUSE 1s]

Now let's install uv.

## SCREEN [8s]

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Verify
uv --version
```

## OVERLAY

> **What is uv?** A fast Python package manager. Replaces pip, virtualenv, and pyenv in one tool.

## OVERLAY (Windows)

> **Windows:** `winget install astral-sh.uv` or PowerShell: `irm https://astral.sh/uv/install.ps1 | iex`

## VOICE

If you need a specific Python version, uv can install it.

## SCREEN [5s]

```bash
# Only if needed
uv python install 3.13
```

## VOICE

Last thing — Claude Code. You probably already have it. If not:

## SCREEN [5s]

```bash
npm install -g @anthropic-ai/claude-code
claude --version
```

## OVERLAY

> **Checklist**
> - `uv --version` — any recent version
> - `python3 --version` — 3.12.x or 3.13.x
> - `git --version` — any recent version
> - `claude --version` — any recent version
