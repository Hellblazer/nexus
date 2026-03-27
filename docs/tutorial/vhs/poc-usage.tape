# Tape 2: Usage — nx already installed, show the good stuff
# Container: nexus-tutorial-ready (Dockerfile.installed, nx pre-installed)
# Same visual appearance as Tape 1 (same theme, font, width, prompt)

Output "poc-usage.mp4"

Set FontSize 16
Set Width 1200
Set Height 700
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set Shell "bash"
Env "PS1" "$ "
Env "PROMPT_COMMAND" ""

# Enter container
Type "docker exec -it nexus-tutorial-ready bash"
Enter
Sleep 3s

# Health check
Type "NX_LOCAL=1 nx doctor"
Enter
Sleep 10s

# Store a memory
Type@60ms 'nx memory put "Auth uses JWT tokens with 24h expiry" --project demo --title auth-notes --ttl permanent'
Enter
Sleep 6s

# Search memory
Type 'nx memory search "JWT"'
Enter
Sleep 6s

# Index the demo repo
Type "NX_LOCAL=1 nx index repo ."
Enter
Sleep 45s

# Semantic search
Type 'NX_LOCAL=1 nx search "how does authentication work"'
Enter
Sleep 10s

Type 'NX_LOCAL=1 nx search "error handling" --corpus code'
Enter
Sleep 10s

# Show help
Type "nx --help"
Enter
Sleep 6s
