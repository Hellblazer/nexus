-- Index Current Group in nx
--
-- Trigger: DEVONthink Toolbar button or Scripts menu item.
-- Action: Indexes every record under the current group (recursive)
--         by walking the children of ``current group`` and forwarding
--         each UUID to ``nx dt index --uuid``. Use this when you've
--         curated a folder of papers/notes and want the lot in
--         Nexus.
-- Logs to: ~/Library/Logs/nexus-dt-scripts.log
--
-- Install: nx dt install-scripts --target toolbar  (or --target menu)
--
-- Notes:
--   * "Current group" is whatever group is selected in the Navigate
--     sidebar of the front window. If no group is active, the script
--     falls back to the root of the front think window.
--   * Recurses into subgroups via ``children`` traversal; non-record
--     items (smart groups, replicants of out-of-scope items) are
--     skipped at the AppleScript layer.
--   * Builds one shell call per batch of UUIDs to avoid spawning a
--     separate ``nx`` process per record. The CLI accepts repeated
--     ``--uuid`` flags.

on run
	tell application id "DNtp"
		try
			set theGroup to current group
			if theGroup is missing value then
				set theGroup to root of front window
			end if
			if theGroup is missing value then
				display notification "No active group or window." with title "nx dt"
				return
			end if
			set groupName to name of theGroup
			set theRecords to my collectRecords(theGroup, {})
		on error errMsg
			my logLine("error: could not read current group: " & errMsg)
			display notification "Could not read current group. See log." with title "nx dt"
			return
		end try
	end tell

	if (count of theRecords) is 0 then
		display notification ("Group '" & groupName & "' is empty.") with title "nx dt"
		return
	end if

	set nxPath to my findNxBinary()
	if nxPath is "" then
		my logLine("error: nx binary not found on common paths")
		display notification "nx not found. See log." with title "nx dt"
		return
	end if

	-- Build a single ``--uuid <U> --uuid <V> ...`` argument string.
	-- The CLI's ``multiple=True`` accepts repeated flags, so one
	-- subprocess handles the whole group.
	set uuidArgs to ""
	tell application id "DNtp"
		repeat with r in theRecords
			set uuidArgs to uuidArgs & " --uuid " & quoted form of (uuid of r as string)
		end repeat
	end tell

	try
		do shell script ¬
			"echo \"[$(date)] Index Current Group: '" & groupName ¬
			& "' (" & (count of theRecords) & " record(s))\" " ¬
			& ">> ~/Library/Logs/nexus-dt-scripts.log; " ¬
			& quoted form of nxPath & " dt index" & uuidArgs ¬
			& " >> ~/Library/Logs/nexus-dt-scripts.log 2>&1 &"
		display notification ("Indexing " & (count of theRecords) & " record(s) from '" & groupName & "'…") with title "nx dt"
	on error errMsg
		my logLine("error: shell call failed: " & errMsg)
		display notification "Index call failed. See log." with title "nx dt"
	end try
end run

-- Walk a group recursively, returning leaf records (i.e. items whose
-- ``type`` is not "group"). Smart groups and replicants of items
-- outside the walk are filtered out at the type check.
on collectRecords(grp, acc)
	tell application id "DNtp"
		set kids to children of grp
		repeat with k in kids
			set kType to type of k
			if kType is group then
				set acc to my collectRecords(k, acc)
			else if kType is not smart group then
				set end of acc to k
			end if
		end repeat
	end tell
	return acc
end collectRecords

on findNxBinary()
	set candidates to {"/opt/homebrew/bin/nx", "/usr/local/bin/nx", (POSIX path of (path to home folder)) & ".local/bin/nx"}
	repeat with c in candidates
		set candidate to c as string
		try
			do shell script "test -x " & quoted form of candidate
			return candidate
		on error
			-- not executable here; try next candidate
		end try
	end repeat
	return ""
end findNxBinary

on logLine(msg)
	try
		do shell script "echo \"[$(date)] " & msg & "\" >> ~/Library/Logs/nexus-dt-scripts.log"
	end try
end logLine
