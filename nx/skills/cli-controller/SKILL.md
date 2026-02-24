---
name: cli-controller
description: Use when controlling interactive CLI applications, debugging with pdb/gdb/jshell, spawning Claude Code instances, or working with REPLs and long-running processes
---

# CLI Controller Skill

This is a **standalone skill** that provides workflows and best practices for interactive CLI control using raw tmux commands. No additional tools required — just tmux on PATH.

## When This Skill Activates

- Debug Python, Java, or other code interactively
- Spawn Claude Code instances for parallel work
- Control long-running interactive processes
- Test web applications with coordinated automation
- Work with REPLs or interactive CLIs
- Monitor and interact with build processes

For simple one-off commands, use the regular Bash tool instead.

## Core Primitives

All operations use `tmux` directly via Bash. Key commands:

| Operation | Command |
|-----------|---------|
| Launch shell in new pane | `tmux split-window -h -P -F '#{session_name}:#{window_index}.#{pane_index}' zsh` |
| Send text + Enter | `tmux send-keys -t $PANE 'command here' Enter` |
| Send text without Enter | `tmux send-keys -t $PANE 'partial text'` |
| Capture pane output | `tmux capture-pane -t $PANE -p` |
| Capture last N lines | `tmux capture-pane -t $PANE -p -S -50` |
| Kill pane | `tmux kill-pane -t $PANE` |
| List panes | `tmux list-panes -F '#{pane_index} #{pane_current_command} #{pane_id}'` |
| Send Ctrl+C | `tmux send-keys -t $PANE C-c` |
| Send Escape | `tmux send-keys -t $PANE Escape` |

### Pane Targeting

Panes are addressed as `session:window.pane`:
- `myapp:1.2` — session "myapp", window 1, pane 2
- Just use the value returned by `split-window -P -F '#{session_name}:#{window_index}.#{pane_index}'`

## Core Principles

### 1. Always Launch Shell First
**CRITICAL**: Never launch commands directly — always launch a shell first.

```bash
# CORRECT - Launch shell, then run command inside it
PANE=$(tmux split-window -h -P -F '#{session_name}:#{window_index}.#{pane_index}' zsh)
sleep 0.5
tmux send-keys -t "$PANE" 'python script.py' Enter

# WRONG - Direct command (if it errors, pane closes and you lose output!)
tmux split-window -h 'python script.py'
```

### 2. Wait for Idle Instead of Arbitrary Sleeps

Poll until pane output stabilizes (no changes for N seconds):

```bash
# Wait for pane to become idle (no output changes for 2 seconds)
wait_idle() {
    local pane="$1" idle_secs="${2:-2}" timeout="${3:-60}"
    local last_hash="" hash="" start=$(date +%s) last_change=$(date +%s)
    while true; do
        local now=$(date +%s)
        (( now - start > timeout )) && return 1
        hash=$(tmux capture-pane -t "$pane" -p | md5sum | cut -d' ' -f1)
        if [[ "$hash" != "$last_hash" ]]; then
            last_hash="$hash"; last_change=$now
        elif (( now - last_change >= idle_secs )); then
            return 0
        fi
        sleep 0.5
    done
}

# Usage:
tmux send-keys -t "$PANE" 'long_running_command' Enter
wait_idle "$PANE" 2 60
output=$(tmux capture-pane -t "$PANE" -p)
```

### 3. Execute with Exit Code Capture

For commands where you need the exit code, use marker-based execution:

```bash
# Execute and capture exit code
tmux_execute() {
    local pane="$1" cmd="$2" timeout="${3:-30}"
    local marker="__EXEC_${RANDOM}_$(date +%s)__"
    tmux send-keys -t "$pane" "echo ${marker}_START; { ${cmd}; } 2>&1; echo ${marker}_END:\$?" Enter
    local start=$(date +%s)
    while (( $(date +%s) - start < timeout )); do
        local captured=$(tmux capture-pane -t "$pane" -p -S -500)
        if [[ "$captured" == *"${marker}_END:"* ]]; then
            local exit_code=$(echo "$captured" | grep -o "${marker}_END:[0-9]*" | sed "s/${marker}_END://")
            local output=$(echo "$captured" | sed -n "/${marker}_START/,/${marker}_END/p" | sed '1d;$d')
            echo "EXIT_CODE=$exit_code"
            echo "$output"
            return "$exit_code"
        fi
        sleep 0.5
    done
    echo "EXIT_CODE=-1 (timeout)"
    return 1
}

# Usage:
tmux_execute "$PANE" "pytest tests/" 120
```

### 4. Always Capture Before Killing
```bash
output=$(tmux capture-pane -t "$PANE" -p)
tmux kill-pane -t "$PANE"
```

## Quick Reference Workflows

### Python Debugging
```bash
PANE=$(tmux split-window -h -P -F '#{session_name}:#{window_index}.#{pane_index}' zsh)
sleep 0.5
tmux send-keys -t "$PANE" 'python -m pdb script.py' Enter
sleep 1
tmux send-keys -t "$PANE" 'b function_name' Enter   # breakpoint
tmux send-keys -t "$PANE" 'c' Enter                  # continue
tmux send-keys -t "$PANE" 'p variable' Enter          # print
sleep 0.5
output=$(tmux capture-pane -t "$PANE" -p)
tmux send-keys -t "$PANE" 'q' Enter
sleep 0.3
tmux kill-pane -t "$PANE"
```

### Spawning Claude Code
```bash
PANE=$(tmux split-window -h -P -F '#{session_name}:#{window_index}.#{pane_index}' zsh)
sleep 0.5
tmux send-keys -t "$PANE" 'cd /path/to/project && claude' Enter
wait_idle "$PANE" 3 30
tmux send-keys -t "$PANE" 'Analyze performance in src/processor.py' Enter
wait_idle "$PANE" 5 120
analysis=$(tmux capture-pane -t "$PANE" -p -S -200)
tmux send-keys -t "$PANE" Escape
sleep 0.3
tmux kill-pane -t "$PANE"
```

### REPL Sessions
```bash
PANE=$(tmux split-window -h -P -F '#{session_name}:#{window_index}.#{pane_index}' zsh)
sleep 0.5
tmux send-keys -t "$PANE" 'python3' Enter
sleep 1
tmux send-keys -t "$PANE" 'import requests' Enter
tmux send-keys -t "$PANE" "response = requests.get('https://api.example.com')" Enter
wait_idle "$PANE" 2 30
output=$(tmux capture-pane -t "$PANE" -p)
tmux send-keys -t "$PANE" 'exit()' Enter
sleep 0.3
tmux kill-pane -t "$PANE"
```

### Long-Running Process Monitoring
```bash
PANE=$(tmux split-window -h -P -F '#{session_name}:#{window_index}.#{pane_index}' zsh)
sleep 0.5
tmux send-keys -t "$PANE" 'mvn clean install' Enter
for i in {1..30}; do
    sleep 10
    output=$(tmux capture-pane -t "$PANE" -p -S -50)
    [[ "$output" =~ "BUILD SUCCESS" ]] && break
    [[ "$output" =~ "BUILD FAILURE" ]] && break
done
final=$(tmux capture-pane -t "$PANE" -p)
tmux kill-pane -t "$PANE"
```

## Error Handling Pattern
```bash
PANE=$(tmux split-window -h -P -F '#{session_name}:#{window_index}.#{pane_index}' zsh)
[[ -z "$PANE" ]] && { echo "Failed to launch pane"; return 1; }
sleep 0.5
tmux send-keys -t "$PANE" 'risky_command' Enter
wait_idle "$PANE" 2 30
output=$(tmux capture-pane -t "$PANE" -p)
if [[ "$output" =~ "error" ]]; then
    echo "Failed: $output"
    tmux kill-pane -t "$PANE"
    return 1
fi
tmux kill-pane -t "$PANE"
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Pane closed unexpectedly | Always launch shell first |
| Missing output | Use `wait_idle` or longer sleep before capture |
| Commands not executing | Add sleep/wait_idle between commands |
| Timeout waiting | Increase timeout or check if process is stuck |
| Enter key not received | Add `sleep 0.3` between send-keys calls |

## Best Practices

1. Always launch shell first (zsh or bash)
2. Use `wait_idle` instead of sleep for timing
3. Capture output before killing panes
4. Use `-S -N` with capture-pane for long outputs (e.g., `-S -500` for 500 lines)
5. Handle errors — check output for error patterns
6. Clean up panes when done
7. Use `tmux list-panes` to see all active panes
8. Quote pane targets: `tmux send-keys -t "$PANE"` (targets contain colons/dots)

**Session Scratch (T1)**: Use `nx scratch put "..."` to note interactive session findings (pane IDs, interim output, working hypotheses). Flagged items auto-promote to T2 at session end.
