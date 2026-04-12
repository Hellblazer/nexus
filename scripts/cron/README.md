# rdr-audit crontab template (Linux)

Schedule the `/nx:rdr-audit <project>` skill to run periodically via Linux cron.

Used together with the shell wrapper at `scripts/cron-rdr-audit.sh`. The crontab
line fires the wrapper on a recurring interval; the wrapper invokes
`claude -p '/nx:rdr-audit <PROJECT>'` in a headless Claude Code session and
writes output to `~/.local/state/rdr-audit/<PROJECT>.log`.

## Install

1. Open your crontab for editing:

   ```bash
   crontab -e
   ```

2. Append a line from `rdr-audit.crontab`, replacing placeholders:
   - `/ABSOLUTE/PATH/TO/nexus` → the absolute path to this nexus repo
   - `ART` → your project name

   Example:
   ```
   0 3 1 */3 * PROJECT=ART /home/alice/git/nexus/scripts/cron-rdr-audit.sh
   ```

3. Save and exit. cron picks up the change automatically.

4. Verify the trigger is installed:

   ```bash
   crontab -l | grep rdr-audit
   ```

## Schedule Cadence

The template `0 3 1 */3 *` fires at 03:00 on the 1st day of every 3rd month
(approximately 90-day cadence — the 1st of January, April, July, October).

Adjust the cron expression if you want a different cadence:
- Monthly (~30 days): `0 3 1 * *`
- Every 60 days: `0 3 1 */2 *`
- Every 90 days: `0 3 1 */3 *` (default)
- Weekly: `0 3 * * 0` (Sunday 03:00)

## Inspect Output

```bash
tail -f ~/.local/state/rdr-audit/<PROJECT>.log
```

Log rotation happens automatically when the file exceeds 10 MB (rotated to
`<PROJECT>.log.1`).

## Uninstall

```bash
crontab -e
```

Remove the line matching `/nx:rdr-audit <PROJECT>`, save, and exit. Verify:

```bash
crontab -l | grep rdr-audit   # should show nothing
```

## Canonical Install Instructions from Claude Code

You can also print the exact install/uninstall commands from inside Claude Code
without leaving the session:

```
/nx:rdr-audit schedule <project>
/nx:rdr-audit unschedule <project>
```

These print the platform-specific commands to stdout for you to review and run
manually. They **do not execute** `crontab -e`, **do not write** to your crontab,
and **do not modify** any system state. All installs are your explicit manual
step.

## Safety Note

**Do not run `crontab -e` from a script automatically** — this is your explicit
step. The nx skill's `/nx:rdr-audit schedule <project>` command prints the
install instructions but never executes them. System-level installs require
your review and authorization each time.

## Anacron Caveat

If your machine is off when the cron fires, the run is **skipped** for that
interval — plain cron does not catch up missed runs. For laptops or
intermittently-online hosts, consider one of:

- **anacron** — a catch-up daemon for missed cron jobs
- **systemd timers with `Persistent=true`** — modern alternative to cron with
  built-in catch-up
- **On-boot script** — add a boot-time check that runs the audit if the last
  run is older than N days

The `/nx:rdr-audit status <project>` subcommand shows the last-run timestamp
from T2, so you can manually verify whether the schedule is keeping up.

## See Also

- `scripts/cron-rdr-audit.sh` — shell wrapper invoked by cron
- `scripts/launchd/README.md` — macOS launchd equivalent
- `nx/skills/rdr-audit/SKILL.md` — the skill body that `claude -p` invokes
- `docs/rdr/rdr-067-cross-project-rdr-audit-loop.md` — design rationale
