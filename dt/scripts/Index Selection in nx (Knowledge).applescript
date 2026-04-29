-- Index Selection in nx (Knowledge)
--
-- Trigger: DEVONthink Scripts menu item.
-- Action: Prompts for a knowledge collection name, then forwards the
--         current selection to ``nx dt index --selection
--         --collection knowledge__<name>``. Use this when the
--         selection should land in a named external-reference
--         corpus (e.g. ``knowledge__delos``,
--         ``knowledge__agentic-scholar``) instead of
--         ``docs__default``.
-- Logs to: ~/Library/Logs/nexus-dt-scripts.log
--
-- Install: nx dt install-scripts --target menu
--
-- Notes:
--   * The leading ``knowledge__`` prefix is added automatically; just
--     type the bare name (e.g. ``papers`` becomes
--     ``knowledge__papers``).
--   * Cancel the dialog to abort with no side effects.

on run
	tell application id "DNtp"
		try
			set theSelection to the selection
			if theSelection is missing value or (count of theSelection) is 0 then
				display notification "No records selected." with title "nx dt"
				return
			end if
			set selCount to count of theSelection
		on error errMsg
			my logLine("error: could not read selection: " & errMsg)
			display notification "Could not read selection. See log." with title "nx dt"
			return
		end try
	end tell

	try
		set dialogResult to display dialog ¬
			"Knowledge collection name (the ``knowledge__`` prefix is added automatically):" ¬
			default answer "" ¬
			with title "nx dt: Index Selection (Knowledge)" ¬
			buttons {"Cancel", "Index"} default button "Index"
		set rawName to text returned of dialogResult
	on error number -128
		-- User cancelled: silent abort.
		return
	end try

	set rawName to my trimWhitespace(rawName)
	if rawName is "" then
		display notification "Empty collection name; aborting." with title "nx dt"
		return
	end if
	set collectionName to "knowledge__" & rawName

	set nxPath to my findNxBinary()
	if nxPath is "" then
		my logLine("error: nx binary not found on common paths")
		display notification "nx not found. See log." with title "nx dt"
		return
	end if

	try
		do shell script ¬
			"echo \"[$(date)] Index Selection (Knowledge): " & selCount ¬
			& " record(s) -> " & collectionName & "\" " ¬
			& ">> ~/Library/Logs/nexus-dt-scripts.log; " ¬
			& quoted form of nxPath & " dt index --selection --collection " ¬
			& quoted form of collectionName ¬
			& " >> ~/Library/Logs/nexus-dt-scripts.log 2>&1 &"
		display notification ("Indexing " & selCount & " record(s) -> " & collectionName) with title "nx dt"
	on error errMsg
		my logLine("error: shell call failed: " & errMsg)
		display notification "Index call failed. See log." with title "nx dt"
	end try
end run

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

on trimWhitespace(s)
	set s to s as string
	set trimChars to {space, tab, return, linefeed}
	repeat while (length of s) > 0 and (character 1 of s) is in trimChars
		set s to text 2 thru -1 of s
	end repeat
	repeat while (length of s) > 0 and (character -1 of s) is in trimChars
		set s to text 1 thru -2 of s
	end repeat
	return s
end trimWhitespace

on logLine(msg)
	try
		do shell script "echo \"[$(date)] " & msg & "\" >> ~/Library/Logs/nexus-dt-scripts.log"
	end try
end logLine
