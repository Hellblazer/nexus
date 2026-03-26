# Section 1: Prerequisites
# Checks git, python, installs uv

Output 01-prerequisites.mp4

Set FontSize 16
Set Width 1200
Set Height 600
Set Theme "Catppuccin Mocha"
Set TypingSpeed 50ms
Set Shell "zsh"

# Check git
Type "git --version"
Enter
Sleep 2s

# Check Python
Type "python3 --version"
Enter
Sleep 2s

# --- PAUSE for voice: "We need Python 3.12 or 3.13..." ---
Sleep 3s

# Install uv
Type "curl -LsSf https://astral.sh/uv/install.sh | sh"
Enter
Sleep 8s

# Verify uv
Type "uv --version"
Enter
Sleep 2s

# --- PAUSE for voice: "If you need a specific Python version..." ---
Sleep 2s

# Show Python management (don't actually install — just show the command)
Type "# If you need Python 3.13:"
Enter
Type "# uv python install 3.13"
Enter
Sleep 3s

# --- PAUSE for voice: "Last thing — Claude Code..." ---
Sleep 2s

# Verify Claude Code (assume already installed)
Type "claude --version"
Enter
Sleep 3s
