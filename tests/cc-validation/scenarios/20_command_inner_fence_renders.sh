#!/usr/bin/env bash
# Scenario 20 — nexus-61fzg: commands whose preamble previously emitted a
# literal triple-backtick must now render through real Claude Code without
# truncation. 5.1.2 broke 17/25 commands because CC closes a ```! block at the
# first inner triple-backtick (e.g. inside echo '<fence>'), leaving an unmatched
# quote. This drives a real CC on analyze-code (a previously-broken shell
# preamble with a project detector) and asserts it executes cleanly.

scenario "20 command_inner_fence_renders: previously-broken echo-fence command runs clean"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{
  "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["Bash"], "defaultMode": "bypassPermissions" }
}
EOF

# Install the real (fixed) command from the repo.
write_command "analyze-code" "$REPO_ROOT/conexus/commands/analyze-code.md"

claude_start
claude_prompt "/analyze-code"
claude_wait 90

pane="$(capture -3000)"
# The 5.1.2 regression (nexus-61fzg) manifested as a shell error from the
# truncated block ("Shell command failed ... unmatched"). Its ABSENCE is the
# regression-specific signal that the inner triple-backtick no longer closes
# the fence. (A raw heredoc/plugin-root leak would also be a failure.)
if grep -qiE 'Shell command failed|unmatched|\(eval\):' <<<"$pane"; then
    fail "block still errors — inner-fence truncation NOT fixed (nexus-61fzg)"
    tail -25 <<<"$pane" | sed 's/^/    | /'
elif grep -qE 'python3 <<|CLAUDE_PLUGIN_ROOT' <<<"$pane"; then
    fail "raw block markers leaked into the pane"
    tail -25 <<<"$pane" | sed 's/^/    | /'
else
    pass "analyze-code fenced block executed without truncation (no shell error)"
fi

claude_exit
scenario_end
