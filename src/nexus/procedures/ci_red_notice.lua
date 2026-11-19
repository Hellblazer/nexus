-- procedure: ci_red_notice
-- summary: The line an aura shows for an owner row of a red develop: who owns it and what this session does.
-- args: sha:string, owner:string?, next:string?, refresh:boolean, reason:string, me:string
-- grants: none

-- RDR-252 Step 4 (nexus-igea9.7), the procedure the shipped ci_develop_red aura wraps. It reads nothing and
-- writes nothing: the aura maps the owner row's dims.sha and body.owner, body.next, body.refresh and
-- body.reason, and the session's own identity (self.identity) as me, and shows the returned text in the
-- session's threads entry. The row carries identity ids, not aliases, and this procedure has no read grant to
-- resolve one, so a name shown for someone else is the id.
--
-- The displays, by state of the row:
--   owner, the pusher's own red (reason earliest_suspect): "first red in your push"
--   owner, a red the leader takes (any other reason but handoff and escalated): the reason is named
--   owner, after a hand-off (reason handoff): "handed to you after no acknowledgement"
--   escalated, naming me or naming no one (a member sees it): raise an ask to Sam
--   next up: "<owner> owns it. You are next up."
--   everyone else: "<owner> owns it. Hold your pushes." No wake.
--
-- wake is true only on a row that is not a refresh (refresh false), when me is the owner or next up, or when the
-- row is escalated and names me or names no one (the load gate has made me a member of the serving group).
-- A refresh row rewrites the block and does not wake again.
--
-- Values a tuple's writer controls (sha, owner, next, reason, me) are checked against their grammar and never
-- echoed in an error: the error text is logged.

local args = ...

local sha = args.sha
if not string.match(sha, "^[0-9a-f]+$") or #sha < 7 or #sha > 64 then
  error("sha must be 7 to 64 lower-case hex digits; got " .. #sha .. " characters", 0)
end

local function segment(value, name)
  if value ~= nil and not string.match(value, "^[A-Za-z0-9][A-Za-z0-9._-]*$") then
    error(name .. " must be an identity id of letters, digits, dot, dash and underscore", 0)
  end
  return value
end

local me = segment(args.me, "me")
local owner = segment(args.owner, "owner")
local next_up = segment(args.next, "next")

local reason = args.reason
if not string.match(reason, "^[a-z][a-z_]*$") or #reason > 32 then
  error("reason must be 1 to 32 lower-case letters and underscores; got " .. #reason .. " characters", 0)
end

local head = "Develop is red at " .. string.sub(sha, 1, 7)
local acknowledge = " Ack: post kind=ack sha=" .. sha .. " to board/ci/nexus-develop--owner."
local escalated = reason == "escalated"

local text
local wake = false

if owner == me or (escalated and owner == nil) then
  if escalated then
    text = head .. "; no one acknowledged. Raise an ask to Sam."
  else
    if reason == "earliest_suspect" then
      text = head .. ", first red in your push. Diagnose it."
    elseif reason == "handoff" then
      text = head .. "; it was handed to you after no acknowledgement. Diagnose it."
    else
      text = head .. "; the leader takes it (" .. reason .. "). Diagnose it."
    end
    -- The state line holds 200 characters: the acknowledgement hint goes on only whole.
    if #text + #acknowledge <= 200 then
      text = text .. acknowledge
    end
  end
  wake = not args.refresh
elseif owner == nil then
  text = head .. "; no one is named as owner. Hold your pushes."
elseif next_up == me then
  text = head .. "; " .. owner .. " owns it. You are next up."
  wake = not args.refresh
else
  text = head .. "; " .. owner .. " owns it. Hold your pushes."
end

return { show = true, text = text, wake = wake }
