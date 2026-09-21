# cc-dashboard

A live list of your [Claude Code](https://claude.com/claude-code) sessions: which ones are waiting
on you, what each one wants, and one key to jump to its terminal tab.

If you run many Claude Code sessions at once, they end up scattered across terminal tabs and you
lose track of which one finished, which one is blocked on a permission prompt, and what it asked.
`cc-dashboard` answers that from a single tab.

```
WAITING ON YOU
 1 !   0:40  Fix the flaky checkout test    web-app    needs permission  approve Bash: Run the checkout test suite
 2     3:12  Port the payment client        web-app    finished          Shall I open the pull request now?
 3    48:39  Bump the motor driver timeout  firmware   finished          Does this look right?
 4     5h21  Draft the release notes        docs       finished          The draft is in RELEASE.md.

WORKING
 5           Migrate the settings schema    web-app    working

[1-9 a-z] jump to tab   [r] refresh   [q] quit      14:02:11
```

- **Blocked sessions first** (`!`, bold red): permission prompts and questions, with what is being
  asked: `approve Bash: …`, `approve Edit: settings.json`, or the text of the question.
- **Finished sessions next**, freshest first, with how long ago Claude stopped and its closing
  question (or closing line). Sessions idle for over an hour are dimmed.
- **Press a row's key** and that session's Terminal tab comes to the front.
- **Titles are the task**, not `web-app-3f`: the AI-written session title, or the name you gave the
  session with `claude -n` / `/rename`.
- Optional **macOS banner** when a session starts waiting, titled with the same task name.

Unofficial; not affiliated with Anthropic.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/Cephla-Lab/cc-dashboard/main/install.sh | sh
```

or from a clone: `./install.sh`. Then run `cc-dashboard` in any terminal tab.

The installer copies two Python files into `~/.claude/hooks`, puts a `cc-dashboard` launcher in
`~/.local/bin`, and adds one `Notification` hook to `~/.claude/settings.json` for the banners. The
settings file is backed up to `settings.json.bak-cc-dashboard` first, is never touched if it is not
valid JSON, and an already-wired hook is left alone.

| | |
|---|---|
| `./install.sh --no-hook` | dashboard only; leave `settings.json` alone |
| `./install.sh --uninstall` | remove the files, the launcher and the hook |
| `cc-dashboard --install-hook` / `--remove-hook` | wire or unwire the banner hook later |

Requirements: Python 3.9+ and a Claude Code version that has `claude agents --json` (tested with
2.1.278). The list works anywhere; **jump-to-tab needs Apple Terminal** and the banners need macOS.

### Banners piling up?

Banners are posted with `osascript`, which can post a notification but can neither replace nor
retract one. Turn off **System Settings → Notifications → Script Editor → Show in Notification
Center** so they pop up without accumulating. The dashboard, not Notification Center, is the list.

## Usage

```
cc-dashboard          live view, refreshed every 3 s
cc-dashboard --once   print the list once (plain text when piped)
```

Keys: `1`-`9` then `a`-`z` jump to a row's tab, `r` refreshes, `q` quits. Colour follows the state
and is off when output is piped or `NO_COLOR` is set. The palette was chosen on a light background
(no yellow or cyan); if blue or dim text is hard to read on yours, change `STYLES`.

## How it works

- Session state comes from **`claude agents --json`**, the interface Claude Code documents for
  reading session state from scripts: `status` (`busy` / `waiting` / `idle`), `waitingFor`, `pid`,
  `cwd`. Local background sessions appear too; cloud sessions are not listed by that command.
- Title, idle time and the "needs" text are read from the tail of each session's **local
  transcript** under `~/.claude/projects/`. Nothing is sent anywhere and no model is called.
  The transcript format is internal to Claude Code and can change between versions; every lookup
  degrades to a blank or to the `claude agents` name rather than failing.
- Idle time is measured from the last user/assistant message. The transcript's modification time
  is not usable: Claude Code appends metadata to transcripts long after a conversation goes quiet.
- "Needs" is a heuristic, not a summary: the last question near the end of Claude's final message,
  else its last line, skipping code blocks, boxed asides, rules and a trailing sources list. For a
  blocked session it is the pending tool call.
- Jump-to-tab maps the session's `pid` to its tty and asks Terminal to select the tab on that tty.

## Tuning

All in `cc_dashboard.py`: `sort_key()` (row order), `STALE_AFTER` (when finished rows dim),
`STYLES` (colours), `TITLE_SHARE` (how the title and needs columns split the width),
`REFRESH_SECONDS`. Which events raise a banner, and which play a sound, is the `ALERTS` table in
`notify_needs_input.py`. Re-run `./install.sh` after editing.

## Development

```sh
python3 -m unittest discover -s tests -t .
```

The logic (row building, ordering, layout, colour, transcript parsing, the `settings.json` merge)
is pure and covered by the tests; the terminal loop and the subprocess calls are a thin shell
around it.

## License

MIT
