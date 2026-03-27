# POC: Prove the VHS pipeline works
# Uses only locally-available commands (nx is already installed)
# Forces local mode so nx doctor output is short (no cloud collections)

Output "poc-test.mp4"

Set FontSize 16
Set Width 1200
Set Height 600
Set Theme "Catppuccin Mocha"
Set TypingSpeed 50ms
Set Shell "bash"

# Clean shell — bash avoids zsh artifacts
Env "PS1" "$ "
Env "PROMPT_COMMAND" ""
Env "NX_LOCAL" "1"

# Let shell initialize
Sleep 2s

# Show nx version (~1.7s to run + buffer)
Type "nx --version"
Enter
Sleep 5s

# Health check — local mode keeps output short (~4s + buffer)
Type "nx doctor"
Enter
Sleep 10s

# Store a note (~1.5s + buffer)
Type@80ms 'nx memory put "POC test note" --project poc-test --title test-note --ttl permanent'
Enter
Sleep 5s

# Search for it (~1.5s + buffer)
Type 'nx memory search "POC"'
Enter
Sleep 5s

# Show help (~1.5s + buffer)
Type "nx --help"
Enter
Sleep 8s

# Clean up
Type "nx memory delete --project poc-test --title test-note -y"
Enter
Sleep 5s
