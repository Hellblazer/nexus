# 1. Prerequisites

> **Time**: 3–5 minutes
> **Goal**: Viewer has uv, Python 3.12–3.13, git, and Claude Code installed

---

## TALK

We need four things before we start: a package manager called uv, Python (the right version), git, and Claude Code itself. If you already have all of these, skip ahead to section 2.

Let's check what you have.

## DO

```bash
# Check if git is installed
git --version

# Check Python version
python3 --version
```

## TALK

We need Python 3.12 or 3.13. If you're on 3.14 or newer, nexus won't install — some upstream dependencies haven't caught up yet. If you're on 3.11 or older, you'll need to upgrade.

Don't worry about managing Python versions yourself — uv handles that for you.

## DO — Install uv

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
# powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# Or: winget install astral-sh.uv

# Verify
uv --version
```

## OVERLAY

> **What is uv?** A fast Python package manager from Astral. It replaces pip, virtualenv, and pyenv in one tool. Nexus uses it for clean, isolated installs.

## TALK

uv manages Python versions, virtual environments, and tool installations. You don't need to think about any of that — just use the commands and it handles the rest.

If you need a specific Python version, uv can install it:

## DO — Install Python (if needed)

```bash
# Only if python3 --version shows 3.14+ or < 3.12
uv python install 3.13
```

## TALK

Last prerequisite: Claude Code. If you're watching this, you probably already have it. If not:

## DO — Install Claude Code (if needed)

```bash
# Install Claude Code CLI
npm install -g @anthropic-ai/claude-code

# Verify
claude --version
```

## OVERLAY

> **Checklist**
> - [ ] `uv --version` → 0.x or newer
> - [ ] `python3 --version` → 3.12.x or 3.13.x
> - [ ] `git --version` → any recent version
> - [ ] `claude --version` → any recent version
