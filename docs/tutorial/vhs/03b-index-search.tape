# Section 3b: Index and Search
# nx index repo, nx search (various), nx hooks install
# NOTE: Run this in a real git repo (demo-repo) for meaningful output

Output "03b-index-search.mp4"

Set FontSize 16
Set Width 1200
Set Height 600
Set Theme "Catppuccin Mocha"
Set TypingSpeed 50ms
Set Shell "zsh"

# Change to the demo repo
Type "cd ~/demo-repo"
Enter
Sleep 1s

# Index the repo (this will take a few seconds on a small repo)
Type "nx index repo ."
Enter
Sleep 35s

# --- PAUSE for voice: "That analyzed every file..." ---
Sleep 3s

# Search by meaning
Type@80ms 'nx search "how does authentication work"'
Enter
Sleep 5s

# Search code only
Type@80ms 'nx search "error handling" --corpus code'
Enter
Sleep 5s

# Search docs only
Type@80ms 'nx search "getting started" --corpus docs'
Enter
Sleep 5s

# Show matching text
Type 'nx search "retry logic" -c'
Enter
Sleep 5s

# --- PAUSE for voice: "That's semantic search..." ---
Sleep 3s

# Install hooks
Type "nx hooks install"
Enter
Sleep 3s

# --- PAUSE for voice: "Set it up once..." ---
Sleep 2s
