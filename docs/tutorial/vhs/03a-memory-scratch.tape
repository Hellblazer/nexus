# Section 3a: Memory and Scratch
# nx memory put/list/get/search, nx scratch put/list/search

Output 03a-memory-scratch.mp4

Set FontSize 16
Set Width 1200
Set Height 600
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set Shell "zsh"

# Store a permanent note
Type@100ms "nx memory put \"Auth uses JWT tokens with 24-hour expiry\" --project demo-repo --title auth-notes --ttl permanent"
Enter
Sleep 3s

# List
Type "nx memory list --project demo-repo"
Enter
Sleep 3s

# Retrieve
Type "nx memory get --project demo-repo --title auth-notes"
Enter
Sleep 3s

# Search
Type "nx memory search \"JWT\""
Enter
Sleep 3s

# --- PAUSE for voice: "Store, list, retrieve, search..." ---
Sleep 2s

# Scratch
Type@100ms "nx scratch put \"hypothesis: the bug is in the retry logic\""
Enter
Sleep 2s

Type "nx scratch list"
Enter
Sleep 2s

Type "nx scratch search \"retry\""
Enter
Sleep 3s

# --- PAUSE for voice: "When the session ends, scratch is gone." ---
Sleep 2s
