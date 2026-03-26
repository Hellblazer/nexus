# Tutorial Recording Scripts

Automated terminal recordings for the nexus tutorial video.

## Two Recording Methods

### Sections 1-3: VHS (fully automated)

[VHS](https://github.com/charmbracelet/vhs) scripts terminal interactions
and renders to MP4. No human input needed.

```bash
brew install charmbracelet/tap/vhs   # macOS
vhs 01-prerequisites.tape            # renders to MP4
```

### Sections 4-7: tmux + Claude Code (one manual step)

Shell scripts drive Claude Code via tmux `send-keys` with hash-based
idle detection. The only manual step is logging into Claude Code.

```bash
# 1. Set up the container (idempotent — safe to re-run)
./container-setup.sh

# 2. Start tmux and Claude Code
cd ~/demo-repo
tmux new-session -s tutorial
claude                          # LOG IN (the one manual step)

# 3. Start recording (from a second terminal)
asciinema rec -c 'bash' tutorial.cast
# Or use OBS / macOS screen capture

# 4. Run the demo scripts in sequence
./05-nexus-in-claude.sh
./06-agents-demo.sh
./07-rdr-demo.sh
```

Section 4 (plugin install) needs screen capture — it requires
interactive Claude Code auth that can't be automated.

## File Index

| File | Section | Method |
|------|---------|--------|
| `container-setup.sh` | — | Stages demo repo, installs nx, populates memory |
| `tmux-helpers.sh` | — | Shared: send, wait_idle, pause (hash-based detection) |
| `01-prerequisites.tape` | 1. Prerequisites | VHS |
| `02-install-nexus.tape` | 2. Install Nexus | VHS |
| `03a-memory-scratch.tape` | 3. First Use (memory + scratch) | VHS |
| `03b-index-search.tape` | 3. First Use (index + search) | VHS |
| `04-install-plugin.tape.disabled` | 4. Install Plugin | Screen capture (needs auth) |
| `05-nexus-in-claude.sh` | 5. Nexus Inside Claude | tmux driver |
| `06-agents-demo.sh` | 6. Agents and Skills | tmux driver |
| `07-rdr-demo.sh` | 7. The RDR Process | tmux driver |

## How It Works

The tmux driver scripts use `tmux send-keys` to type commands into a
Claude Code session and **hash-based idle detection** (`tmux capture-pane`
+ `md5sum`) to know when Claude has finished responding. This is the
pattern from the `/nx:cli-controller` skill — prompt-char detection
fails because Claude Code's persistent footer line always contains `❯`.

The `wait_idle` function polls the pane content hash every 0.5s. When
the hash is stable for 4 seconds (configurable), Claude is done. The
`MAX_WAIT` timeout (300s) prevents infinite loops.

The first command in each session uses `send_first` which handles Claude
Code's splash-screen keystroke swallow (type text, pause, then Enter).

## Known Limitations

- **`bd` not installed**: RDR commands show "Beads not available" in output.
  This is expected and handled gracefully — not a recording error.
- **Re-runs**: `container-setup.sh` resets T2 memory but does not delete
  `docs/rdr/` files. For a clean section 7 recording, use a fresh container.
- **Section 4**: VHS cannot automate Claude Code login. Use screen capture.

## Customization

- **VHS theme**: Edit `Set Theme` in `.tape` files (default: Catppuccin Mocha)
- **Font size**: Edit `Set FontSize` (default: 16)
- **Idle detection**: Edit `IDLE_SECONDS` in `tmux-helpers.sh` (default: 4s)
- **Timeout**: Edit `MAX_WAIT` in `tmux-helpers.sh` (default: 300s)
- **tmux target**: Edit `TMUX_TARGET` in `tmux-helpers.sh` (default: `tutorial:0.0`)
