---
name: cli-controller
standalone: true
description: >
  Expert guidance for controlling interactive CLI applications using tmux-cli.
  Triggers: debugging Python/Java interactively, spawning Claude Code instances,
  long-running interactive processes, web application testing with browser automation.
allowed-tools: Bash, Read, Write
# See ~/.claude/registry.yaml for standalone skill metadata
---

# CLI Controller Skill

This is a **standalone skill** that provides workflows and best practices for interactive CLI control.
It does NOT delegate to an agent.

## When This Skill Activates

- Debug Python, Java, or other code interactively
- Spawn Claude Code instances for parallel work
- Control long-running interactive processes
- Test web applications with coordinated automation
- Work with REPLs or interactive CLIs
- Monitor and interact with build processes

For simple one-off commands, use the regular Bash tool instead.

## Core Principles

### 1. Always Launch Shell First
**CRITICAL**: Never launch commands directly - always launch a shell first.

```bash
# CORRECT - Launch shell first
pane=$(tmux-cli launch "zsh")
tmux-cli send "python script.py" --pane=$pane

# WRONG - Direct command launch (if it errors, pane closes and you lose output!)
pane=$(tmux-cli launch "python script.py")
```

### 2. Use wait_idle Instead of Polling
```bash
# CORRECT
tmux-cli send "long_running_command" --pane=2
tmux-cli wait_idle --pane=2 --idle-time=2.0
output=$(tmux-cli capture --pane=2)

# WRONG - Arbitrary wait
tmux-cli send "long_running_command" --pane=2
sleep 5
```

### 3. Always Capture Before Killing
```bash
output=$(tmux-cli capture --pane=2)
tmux-cli kill --pane=2
```

## Quick Reference Workflows

### Python Debugging
```bash
pane=$(tmux-cli launch "zsh")
tmux-cli send "python -m pdb script.py" --pane=$pane
tmux-cli wait_idle --pane=$pane
tmux-cli send "b function_name" --pane=$pane  # breakpoint
tmux-cli send "c" --pane=$pane                 # continue
tmux-cli send "p variable" --pane=$pane        # print
output=$(tmux-cli capture --pane=$pane)
tmux-cli send "q" --pane=$pane
tmux-cli kill --pane=$pane
```

### Spawning Claude Code
```bash
pane=$(tmux-cli launch "zsh")
tmux-cli send "cd /path/to/project && claude-code" --pane=$pane
tmux-cli wait_idle --pane=$pane --idle-time=3.0
tmux-cli send "Analyze performance in src/processor.py" --pane=$pane
tmux-cli wait_idle --pane=$pane --idle-time=5.0 --timeout=120
analysis=$(tmux-cli capture --pane=$pane)
tmux-cli escape --pane=$pane
tmux-cli kill --pane=$pane
```

### REPL Sessions
```bash
pane=$(tmux-cli launch "zsh")
tmux-cli send "python3" --pane=$pane
tmux-cli wait_idle --pane=$pane
tmux-cli send "import requests" --pane=$pane
tmux-cli send "response = requests.get('https://api.example.com')" --pane=$pane
tmux-cli wait_idle --pane=$pane
output=$(tmux-cli capture --pane=$pane)
tmux-cli send "exit()" --pane=$pane
tmux-cli kill --pane=$pane
```

### Long-Running Process Monitoring
```bash
build_pane=$(tmux-cli launch "zsh")
tmux-cli send "mvn clean install" --pane=$build_pane
for i in {1..10}; do
    sleep 10
    output=$(tmux-cli capture --pane=$build_pane)
    [[ "$output" =~ "BUILD SUCCESS" ]] && break
    [[ "$output" =~ "BUILD FAILURE" ]] && break
done
tmux-cli wait_idle --pane=$build_pane --idle-time=3.0
final=$(tmux-cli capture --pane=$build_pane)
tmux-cli kill --pane=$build_pane
```

## Error Handling Pattern
```bash
pane=$(tmux-cli launch "zsh")
[[ -z "$pane" ]] && { echo "Failed to launch"; exit 1; }
tmux-cli send "risky_command" --pane=$pane
tmux-cli wait_idle --pane=$pane --timeout=30
output=$(tmux-cli capture --pane=$pane)
[[ "$output" =~ "error" ]] && { echo "Failed: $output"; tmux-cli kill --pane=$pane; exit 1; }
tmux-cli kill --pane=$pane
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Pane closed unexpectedly | Always launch shell first |
| Missing output | Use `wait_idle` with appropriate `--idle-time` |
| Commands not executing | Add `wait_idle` between commands |
| Timeout waiting | Increase `--timeout` or check if process stuck |

## Best Practices

1. Always launch shell first (zsh or bash)
2. Use wait_idle instead of sleep for timing
3. Capture output before killing panes
4. Use appropriate idle-time (2-3 seconds for most apps)
5. Handle errors - check output for error patterns
6. Clean up panes when done
7. Use `tmux-cli status` to see all active panes
