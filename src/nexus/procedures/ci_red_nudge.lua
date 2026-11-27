-- procedure: ci_red_nudge
-- summary: The line and wake for next up when the owner of a red develop has not acknowledged it.
-- args: sha:string, branch:string?, next:string?, next_alias:string?, me:string
-- grants: none

-- RDR-252 post-close audit S2 (nexus-igea9), the procedure the shipped ci_develop_nudge aura wraps. It reads nothing
-- and writes nothing. The engine posts a kind=nudge row on the owner post when half the acknowledgement deadline has
-- passed with no ack, naming next up in body.next (and its alias in body.next_alias); the aura maps the row's dims.sha,
-- body.next and body.next_alias, and the session's own identity (self.identity) as me.
--
-- Next up is shown the red by ci_red_notice and not woken there, because the owner nearly always acknowledges within
-- two minutes. This is the one place it is woken: a nudge naming me shows the line and wakes; a nudge naming anyone else,
-- or no one, shows nothing and wakes nothing (show false leaves the block as it was). Who owns the red is not this
-- procedure's business, and an alias is a name to show, never a way to decide who this session is.
--
-- The branch named in the text is the aura's own (a template aura maps branch = "self.branch", see ci_red_notice); with
-- none mapped it is develop, as before the template.
--
-- Values a tuple's writer controls (sha, next, next_alias, me) are checked against their grammar and never echoed in
-- an error: the error text is logged.

local args = ...

local sha = args.sha
if not string.match(sha, "^[0-9a-f]+$") or #sha < 7 or #sha > 64 then
  error("sha must be 7 to 64 lower-case hex digits; got " .. #sha .. " characters", 0)
end

local function segment(value, name, what)
  if value ~= nil and not string.match(value, "^[A-Za-z0-9][A-Za-z0-9._-]*$") then
    error(name .. " must be " .. (what or "an identity id") .. " of letters, digits, dot, dash and underscore", 0)
  end
  return value
end

local me = segment(args.me, "me")
local branch = segment(args.branch, "branch", "a branch") or "develop"
if #branch > 32 then
  error("branch must be a branch of at most 32 letters, digits, dot, dash and underscore; got " .. #branch .. " characters", 0)
end
local next_up = segment(args.next, "next")
segment(args.next_alias, "next_alias", "an alias")

if next_up == nil or next_up ~= me then
  return { show = false, text = "", wake = false }
end

local text = string.upper(string.sub(branch, 1, 1)) .. string.sub(branch, 2) .. " is red at " .. string.sub(sha, 1, 7) .. "; no one has acknowledged it yet. You are next up."
return { show = true, text = text, wake = true }
