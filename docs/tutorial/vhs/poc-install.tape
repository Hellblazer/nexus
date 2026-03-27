# Tape 1: Install — uv + conexus from scratch
# Container: nexus-tutorial-bare (Dockerfile, no nx)
# Post-process: speed-map the package download at 16x

Output "poc-install.mp4"

Set FontSize 16
Set Width 1200
Set Height 700
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set Shell "bash"
Env "PS1" "$ "
Env "PROMPT_COMMAND" ""

# Enter container
Type "docker exec -it nexus-tutorial-bare bash"
Enter
Sleep 3s

# Install uv
Type "curl -LsSf https://astral.sh/uv/install.sh | sh"
Enter
Sleep 12s

Type "source $HOME/.local/bin/env"
Enter
Sleep 2s

Type "uv --version"
Enter
Sleep 4s

# Install conexus — this is the long one, 120s to be safe
Type "uv tool install conexus"
Enter
Sleep 150s

Type "nx --version"
Enter
Sleep 5s
