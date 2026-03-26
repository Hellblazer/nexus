# Tutorial Recording Scripts

Automated terminal recordings for the nexus tutorial video.

## Two Recording Methods

### Sections 1-4: VHS (fully automated)

[VHS](https://github.com/charmbracelet/vhs) scripts terminal interactions
and renders to MP4. No human input needed.

```bash
brew install charmbracelet/tap/vhs   # macOS
vhs 01-prerequisites.tape            # renders to MP4
```

### Sections 5-7: tmux + Claude Code (one manual step)

Shell scripts drive Claude Code via tmux `send-keys`. The only manual
step is logging into Claude Code.

```bash
# 1. Set up the container
./container-setup.sh

# 2. Start tmux and Claude Code
cd ~/demo-repo
tmux new-session -s tutorial
claude                          # LOG IN (the one manual step)

# 3. From a second terminal, run the demo scripts
./05-nexus-in-claude.sh
./06-agents-demo.sh
./07-rdr-demo.sh
```

Record the tmux session with asciinema or screen capture.

## File Index

| File | Section | Method |
|------|---------|--------|
| `container-setup.sh` | — | Sets up demo repo, installs nx, populates memory |
| `tmux-helpers.sh` | — | Shared functions: send, wait_for_prompt, pause |
| `01-prerequisites.tape` | 1. Prerequisites | VHS |
| `02-install-nexus.tape` | 2. Install Nexus | VHS |
| `03a-memory-scratch.tape` | 3. First Use (memory + scratch) | VHS |
| `03b-index-search.tape` | 3. First Use (index + search) | VHS |
| `04-install-plugin.tape` | 4. Install Plugin | VHS |
| `05-nexus-in-claude.sh` | 5. Nexus Inside Claude | tmux driver |
| `06-agents-demo.sh` | 6. Agents and Skills | tmux driver |
| `07-rdr-demo.sh` | 7. The RDR Process | tmux driver |

## How It Works

The tmux driver scripts use `tmux send-keys` to type commands into a
Claude Code session and `tmux capture-pane` to detect when the prompt
returns (Claude finished responding). This is the same pattern as the
`/nx:cli-controller` skill.

The `wait_for_prompt` function polls for the `❯` prompt character. The
`MAX_WAIT` timeout (300s) prevents infinite loops if Claude hangs.

## Customization

- **VHS theme**: Edit `Set Theme` in `.tape` files (default: Catppuccin Mocha)
- **Font size**: Edit `Set FontSize` (default: 16)
- **Timing**: Adjust `Sleep` durations in `.tape` files and `pause` calls in `.sh` scripts
- **Prompt character**: Edit `PROMPT_CHAR` in `tmux-helpers.sh` if your prompt differs
