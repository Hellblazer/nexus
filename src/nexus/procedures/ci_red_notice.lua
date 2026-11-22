-- procedure: ci_red_notice
-- summary: The line an aura shows for an owner row of a red develop: who owns it and what this session does.
-- args: sha:string, owner:string?, owner_alias:string?, next:string?, next_alias:string?, refresh:boolean, reason:string, me:string
-- grants: none

-- RDR-252 Step 4 (nexus-igea9.7), the procedure the shipped ci_develop_red aura wraps. It reads nothing and
-- writes nothing: the aura maps the owner row's dims.sha and body.owner, body.next, body.refresh and
-- body.reason, and the session's own identity (self.identity) as me, and shows the returned text in the
-- session's threads entry. From engine 0.1.198 the row also carries body.owner_alias and body.next_alias, the live
-- alias of each at stamp time (absent for an identity with none, and on a row an older engine stamped); a name
-- shown for someone else is that alias, else the id. This procedure has no read grant to resolve an alias itself,
-- and who owns the red is always decided on the ids: an alias is a name to show and nothing more.
--
-- The displays, by state of the row:
--   owner, the pusher's own red (reason earliest_suspect): "first red in your push"
--   owner, a red the leader takes (any other reason but handoff and escalated): the reason is named
--   owner, after a hand-off (reason handoff): "handed to you after no acknowledgement"
--   escalated, naming me or naming no one (a member sees it): raise an ask to Sam
--   next up: "<owner> owns it. You are next up." No wake: a nudge row wakes it (ci_red_nudge), and a hand-off that
--     seats it as owner wakes it through the owner display.
--   everyone else: "<owner> owns it. Hold your pushes." No wake.
--
-- wake is true only on a row that is not a refresh (refresh false), when me is the owner, or when the row is
-- escalated and names me or names no one (the load gate has made me a member of the serving group). Next up is shown
-- and never woken here (RDR-252 audit S2). A refresh row rewrites the block and does not wake again.
--
-- Values a tuple's writer controls (sha, owner, owner_alias, next, next_alias, reason, me) are checked against their grammar and never
-- echoed in an error: the error text is logged.

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
local owner = segment(args.owner, "owner")
local next_up = segment(args.next, "next")
segment(args.next_alias, "next_alias", "an alias")
-- The name shown for the owner: its alias when the row has one, else its id.
local owner_name = segment(args.owner_alias, "owner_alias", "an alias") or owner

local reason = args.reason
if not string.match(reason, "^[a-z][a-z_]*$") or #reason > 32 then
  error("reason must be 1 to 32 lower-case letters and underscores; got " .. #reason .. " characters", 0)
end

local head = "Develop is red at " .. string.sub(sha, 1, 7)
-- The owner acknowledges with `nx ci ack <sha7>`, which posts the row with the caller's own identity id in `from`
-- (nexus-igea9). The engine counts the row's sender-session stamp, not `from`, and `nx group ack-times` counts by the
-- stamp too; `from` is a label people read. The hint is a fixed short form, so it always fits whole.
local acknowledge = " Ack: nx ci ack " .. string.sub(sha, 1, 7) .. "."
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
    -- The longest owner text with the hint is under 120 characters, inside the state line's 200 (a test pins it).
    text = text .. acknowledge
  end
  wake = not args.refresh
elseif owner == nil then
  text = head .. "; no one is named as owner. Hold your pushes."
elseif next_up == me then
  -- Shown, not woken (RDR-252 audit S2): the owner usually acknowledges within two minutes, so a wake here cost the
  -- conductor most of its sessions' interruptions. Next up is woken by a nudge row (ci_red_nudge) when half the
  -- deadline has passed with no ack, and a hand-off that seats it as owner wakes it through the owner branch above.
  text = head .. "; " .. owner_name .. " owns it. You are next up."
else
  text = head .. "; " .. owner_name .. " owns it. Hold your pushes."
end

return { show = true, text = text, wake = wake }
