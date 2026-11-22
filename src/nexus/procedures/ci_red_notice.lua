-- procedure: ci_red_notice
-- summary: The line an aura shows for an owner row of a red develop: who owns it and what this session does.
-- args: sha:string, owner:string?, owner_alias:string?, next:string?, next_alias:string?, refresh:boolean, reason:string, stalled:boolean?, stalled_owner:string?, stalled_since:string?, stalled_sign:string?, open_minutes:integer?, previous_owner:string?, me:string
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
--   owner, after a disclaim (reason disclaimed): "handed to you: <previous_owner> said it is not theirs"
--   escalated and stalled (a red still open after the stall window; body.stalled, open_minutes, stalled_sign, stalled_owner,
--   stalled_since): the leader (owner, or no one named) is told how long it has been open and the last sign of life, "last
--   activity: <who> <acknowledged|pushed> at hh:mm UTC", or "with no acknowledgement or fix push" when there was none, and
--   to raise an ask to Sam; the session that gave the last sign gets "your last activity: ..." (no hold, no wake); everyone
--   else gets the same facts and "Hold your pushes" (no wake)
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

-- A stall escalation (reason escalated, stalled true: a red still open after the stall window) carries open_minutes, the
-- whole minutes since the red began, and the last sign of life on it: stalled_sign (ack or push), the identity that gave
-- it (stalled_owner) and when (stalled_since, an ISO-8601 UTC instant). All three are absent when there was no sign.
local stalled_owner = segment(args.stalled_owner, "stalled_owner")
-- A disclaim hand-off names the owner that said "not mine" (body.previous_owner).
local previous_owner = segment(args.previous_owner, "previous_owner")

-- "hh:mm" of an ISO-8601 UTC instant ("2026-11-22T11:14:00Z", a fraction allowed), or nil when it does not parse.
local function clock(t)
  if t == nil then
    return nil
  end
  local h, m = string.match(t, "^%d%d%d%d%-%d%d%-%d%dT(%d%d):(%d%d):%d%d%.?%d*Z$")
  if h == nil or tonumber(h) > 23 or tonumber(m) > 59 then
    return nil
  end
  return h .. ":" .. m
end

local head = "Develop is red at " .. string.sub(sha, 1, 7)
-- The owner acknowledges with `nx ci ack <sha7>`, which posts the row with the caller's own identity id in `from`
-- (nexus-igea9). The engine counts the row's sender-session stamp, not `from`, and `nx group ack-times` counts by the
-- stamp too; `from` is a label people read. The hint is a fixed short form, so it always fits whole.
local acknowledge = " Ack: nx ci ack " .. string.sub(sha, 1, 7) .. "."
local escalated = reason == "escalated"

local text
local wake = false

if escalated and args.stalled == true then
  -- A red still open after the stall window (RDR-252 audit S1): it is not handed on again, and the leader raises it with
  -- Sam. The text says what the engine knows and no more: how long it has been open, and the last sign of life if there
  -- was one (an ack or a push), never "acknowledged" for someone who only pushed. The session that gave the last sign is
  -- not told to hold its own pushes.
  local verb = nil
  if args.stalled_sign == "ack" then
    verb = "acknowledged"
  elseif args.stalled_sign == "push" then
    verb = "pushed"
  end
  local sign = verb ~= nil and stalled_owner ~= nil
  local lead = head .. ", still open"
  if args.open_minutes ~= nil and args.open_minutes >= 0 then
    lead = lead .. " after " .. args.open_minutes .. " min"
  end
  local at = clock(args.stalled_since)
  at = at ~= nil and (" at " .. at .. " UTC") or ""
  if sign and stalled_owner == me and owner ~= me then
    text = lead .. "; your last activity: " .. verb .. at .. ". The leader is raising it with Sam."
  elseif owner == me or owner == nil then
    if sign then
      text = lead .. "; last activity: " .. stalled_owner .. " " .. verb .. at .. ". Raise an ask to Sam."
    else
      text = lead .. " with no acknowledgement or fix push. Raise an ask to Sam."
    end
    wake = not args.refresh
  elseif sign then
    text = lead .. "; last activity: " .. stalled_owner .. " " .. verb .. at
      .. ". The leader is raising it with Sam. Hold your pushes."
  else
    text = lead .. " with no acknowledgement or fix push. The leader is raising it with Sam. Hold your pushes."
  end
elseif owner == me or (escalated and owner == nil) then
  if escalated then
    text = head .. "; no one acknowledged. Raise an ask to Sam."
  else
    if reason == "earliest_suspect" then
      text = head .. ", first red in your push. Diagnose it."
    elseif reason == "handoff" then
      text = head .. "; it was handed to you after no acknowledgement. Diagnose it."
    elseif reason == "disclaimed" then
      -- The previous owner posted `nx ci ack --not-mine`. A row from an engine that does not name it says "the previous
      -- owner".
      text = head .. "; handed to you: " .. (previous_owner or "the previous owner") .. " said it is not theirs. Diagnose it."
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
