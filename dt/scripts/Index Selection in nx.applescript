-- Index Selection in nx
--
-- Trigger: DEVONthink Toolbar button or Scripts menu item.
-- Action: Forwards the records currently highlighted in the front
--         viewer window to ``nx dt index --selection``. The CLI walks
--         each record into the right indexer (PDF or Markdown) and
--         stamps ``source_uri = x-devonthink-item://<UUID>`` on every
--         catalog entry it creates.
-- Logs to: ~/Library/Logs/nexus-dt-scripts.log
--
-- Install: nx dt install-scripts --target toolbar  (or --target menu)
--
-- Notes:
--   * "selection" only covers records highlighted in the item list.
--     A group selected in the Navigate sidebar is not "selection";
--     use "Index Current Group in nx" for that case.
--   * The shell call is backgrounded with a trailing "&" so DT's UI
--     does not block on Voyage embed + Chroma upsert (typically
--     1-3s per PDF).

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

	set nxPath to my findNxBinary()
	if nxPath is "" then
		my logLine("error: nx binary not found on common paths")
		display notification "nx not found. See log." with title "nx dt"
		return
	end if

	try
		do shell script ¬
			"echo \"[$(date)] Index Selection: " & selCount & " record(s)\" " ¬
			& ">> ~/Library/Logs/nexus-dt-scripts.log; " ¬
			& quoted form of nxPath & " dt index --selection " ¬
			& ">> ~/Library/Logs/nexus-dt-scripts.log 2>&1 &"
		display notification ("Indexing " & selCount & " record(s)…") with title "nx dt"
	on error errMsg
		my logLine("error: shell call failed: " & errMsg)
		display notification "Index call failed. See log." with title "nx dt"
	end try
end run

-- Probe a small set of common nx install paths. AppleScript's
-- ``do shell script`` runs with a bare PATH, so an absolute path is
-- the most portable choice. ``command -v nx`` via a login shell would
-- also work but adds latency; a static list is fine while there are
-- only a handful of install methods (uv tool, Homebrew, pipx).
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
