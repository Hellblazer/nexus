# Tape: Install via expect — prompt-synchronized, no blind sleeps
# VHS handles rendering, expect handles timing

Output "poc-install.mp4"

Set FontSize 16
Set Width 1200
Set Height 700
Set Theme "Catppuccin Mocha"
Set Shell "bash"
Env "PS1" "$ "
Env "PROMPT_COMMAND" ""

# Run expect script — it waits for prompts between commands
Type "./install-demo.expect"
Enter

# Expect script runs to completion (~3-4 min)
# 300s timeout covers the full install
Sleep 300s
