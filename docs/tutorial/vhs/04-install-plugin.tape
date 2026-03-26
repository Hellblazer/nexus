# Section 4: Install the Plugin
# Claude Code marketplace + plugin install + preflight
# NOTE: This runs inside Claude Code — VHS can script this but
# the /plugin and /nx: commands require Claude Code to be running.
# If Claude Code is not available, use screen capture instead.

Output 04-install-plugin.mp4

Set FontSize 16
Set Width 1200
Set Height 600
Set Theme "Catppuccin Mocha"
Set TypingSpeed 50ms
Set Shell "zsh"

# Start Claude Code
Type "claude"
Enter
Sleep 5s

# Add marketplace
Type "/plugin marketplace add Hellblazer/nexus"
Enter
Sleep 5s

# Install plugin
Type "/plugin install nx@nexus-plugins"
Enter
Sleep 8s

# --- PAUSE for voice: "If you see a not found error..." ---
Sleep 2s

# Preflight
Type "/nx:nx-preflight"
Enter
Sleep 8s

# --- PAUSE for voice: "Green means good..." ---
Sleep 3s

# Exit and restart to show session hooks
Type "exit"
Enter
Sleep 2s

Type "claude"
Enter
Sleep 8s

# --- PAUSE for voice: "See the context at the top..." ---
Sleep 5s

Type "exit"
Enter
Sleep 1s
