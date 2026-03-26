# VHS Tape Files

Terminal recording scripts for the nexus tutorial video. Each `.tape` file
produces one MP4 clip via [VHS](https://github.com/charmbracelet/vhs).

## Usage

```bash
# Install VHS
brew install charmbracelet/tap/vhs   # macOS
# or: go install github.com/charmbracelet/vhs@latest

# Record a single section
vhs 01-prerequisites.tape

# Record all sections
for f in *.tape; do vhs "$f"; done
```

## Sections

| File | Tutorial Section | Notes |
|------|-----------------|-------|
| `01-prerequisites.tape` | 1. Prerequisites | Checks git, python, installs uv |
| `02-install-nexus.tape` | 2. Install Nexus | uv tool install, nx doctor, nx help |
| `03a-memory-scratch.tape` | 3. First Use (memory + scratch) | nx memory, nx scratch |
| `03b-index-search.tape` | 3. First Use (index + search) | nx index repo, nx search |
| `04-install-plugin.tape` | 4. Install Plugin | Claude Code plugin install |

Sections 5-7 require pre-recorded Claude Code sessions (LLM responses
are non-deterministic). Use screen capture (OBS or macOS) for those.

## Customization

Edit the `Set` directives at the top of each tape file to match your
terminal preferences (font size, dimensions, theme).
