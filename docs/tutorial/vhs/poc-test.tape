# POC: Prove the VHS pipeline works
# Uses only locally-available commands (nx is already installed)

Output "poc-test.mp4"

Set FontSize 16
Set Width 1200
Set Height 600
Set Theme "Catppuccin Mocha"
Set TypingSpeed 50ms
Set Shell "zsh"

# Show nx version
Type "nx --version"
Enter
Sleep 2s

# Health check
Type "nx doctor"
Enter
Sleep 4s

# Store a note
Type@80ms 'nx memory put "POC test note" --project poc-test --title test-note --ttl permanent'
Enter
Sleep 3s

# Search for it
Type 'nx memory search "POC"'
Enter
Sleep 3s

# Show help
Type "nx --help"
Enter
Sleep 4s

# Clean up
Type "nx memory delete --project poc-test --title test-note -y"
Enter
Sleep 2s
