# rdr-audit launchd template (macOS)

Schedule the `/nx:rdr-audit <project>` skill to run periodically via macOS launchd.

Used together with the shell wrapper at `scripts/cron-rdr-audit.sh`. The plist
template fires the wrapper on a recurring interval; the wrapper invokes
`claude -p '/nx:rdr-audit <PROJECT>'` in a headless Claude Code session and
writes output to `~/.local/state/rdr-audit/<PROJECT>.log`.

## Install

1. Copy `com.nexus.rdr-audit.PROJECT.plist` to `~/Library/LaunchAgents/`, renaming
   `PROJECT` in the filename to your actual project name (e.g.
   `com.nexus.rdr-audit.ART.plist`).

2. Edit the copied file and replace every placeholder:
   - `PROJECT` → your project name (e.g. `ART`, `nexus`)
   - `/ABSOLUTE/PATH/TO/nexus` → the absolute path to this nexus repo on your machine
   - `/Users/YOURNAME` → your home directory path (use `echo $HOME`)

3. Load the trigger:

   ```bash
   launchctl load ~/Library/LaunchAgents/com.nexus.rdr-audit.<PROJECT>.plist
   ```

4. Verify it is scheduled:

   ```bash
   launchctl list | grep rdr-audit
   ```

   You should see a line with `com.nexus.rdr-audit.<PROJECT>` and a non-empty PID
   field (when running) or a `-` (when idle).

## Inspect Output

```bash
tail -f ~/.local/state/rdr-audit/<PROJECT>.log
```

Log rotation happens automatically when the file exceeds 10 MB (rotated to
`<PROJECT>.log.1`).

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.nexus.rdr-audit.<PROJECT>.plist
rm ~/Library/LaunchAgents/com.nexus.rdr-audit.<PROJECT>.plist
```

## Canonical Install Instructions from Claude Code

You can also print the exact install/uninstall commands from inside Claude Code
without leaving the session:

```
/nx:rdr-audit schedule <project>
/nx:rdr-audit unschedule <project>
```

These print the platform-specific commands to stdout for you to review and run
manually. They **do not execute** `launchctl load`, **do not write** the plist
file, and **do not modify** any system state. All installs are your explicit
manual step.

## Safety Note

**Do not run `launchctl load` automatically** — this is your explicit step. The
nx skill's `/nx:rdr-audit schedule <project>` command prints the install
instructions but never executes them. System-level installs require your review
and authorization each time.

## Schedule Cadence

The template fires on the 1st of each month at 03:00 local time (approximately
30-day cadence). launchd's `StartCalendarInterval` does not support exact 90-day
intervals natively; for a true 90-day cadence, use the Linux cron template under
`scripts/cron/rdr-audit.crontab` or accept the monthly-as-close-to-90 compromise.

## See Also

- `scripts/cron-rdr-audit.sh` — shell wrapper invoked by the plist
- `scripts/cron/README.md` — Linux cron equivalent
- `nx/skills/rdr-audit/SKILL.md` — the skill body that `claude -p` invokes
- `docs/rdr/rdr-067-cross-project-rdr-audit-loop.md` — design rationale
