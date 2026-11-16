-- procedure: ci_red_notice
-- summary: The line an aura shows for a red develop sha: diagnose it before the next push.
-- args: sha:string, outcome:string
-- grants: none

-- RDR-249 Technical Design 7 (nexus-mdbds.12.1), the procedure the shipped ci_develop_red
-- aura wraps. It reads nothing and writes nothing: the aura maps the CI post's sha and
-- outcome dimensions to the two arguments, and shows the returned text in the session's
-- threads entry when show is true. The aura's trigger matches outcome=failure, so every
-- call is a red post and the procedure always shows; outcome is declared because the aura
-- maps it, and the procedure does not test it again.

local args = ...

local sha = args.sha
if not string.match(sha, "^[0-9a-f]+$") or #sha < 7 or #sha > 64 then
  error("sha must be 7 to 64 lower-case hex digits; got " .. sha, 0)
end

return {
  show = true,
  text = "develop red at " .. string.sub(sha, 1, 7) .. ": diagnose before the next push (worktrees rule 7)",
}
