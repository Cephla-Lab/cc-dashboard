#!/usr/bin/env python3
"""Claude Code Notification hook: macOS banner naming the session that is waiting on you.

Reads the hook's JSON from stdin. Wired up in ~/.claude/settings.json under
hooks.Notification. Best-effort by design: on any problem it exits without a banner.

The banner is only the tap on the shoulder; `cc-dashboard` (cc_dashboard.py, next to this file)
is the live list. Banners go through osascript, which can post a banner but can neither replace
nor retract one, so turn "Show in Notification Center" off for Script Editor or they pile up.
(On macOS 14.4 we saw the notification service refuse ad-hoc-signed notifier apps outright, so a
tool that can retract banners was not an option without a code-signing identity.)
"""
import json
import os
import shutil
import subprocess
import sys

# notification_type -> (label, play_sound). Types not listed here stay silent.
# Blocked mid-task gets a sound; finished/idle is a silent banner.
ALERTS = {
    "permission_prompt": ("needs permission", True),
    "elicitation_dialog": ("needs input", True),
    "elicitation_url_dialog": ("needs input", True),
    "agent_needs_input": ("needs input", True),
    "quota_auto_resume_stale": ("press Enter to resume", True),
    "idle_prompt": ("waiting for you", False),
    "agent_completed": ("finished", False),
}
SOUND = "Glass"

# Text arrives as argv rather than being spliced into the script, so quotes and
# backslashes in a message can't break (or inject into) the AppleScript.
APPLESCRIPT = """
on run argv
    set {theBody, theTitle, theSubtitle, theSound} to argv
    if theSound is "" then
        display notification theBody with title theTitle subtitle theSubtitle
    else
        display notification theBody with title theTitle subtitle theSubtitle sound name theSound
    end if
end run
"""


def transcript_title(transcript_path):
    """Latest session title recorded in the transcript, or None.

    Claude Code re-records the title every turn: the AI-written summary of the task, or the
    name you gave the session once you set one. The transcript format is internal to Claude
    Code, so this is best-effort; callers fall back to the documented `claude agents --json`.
    """
    try:
        with open(transcript_path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            for tail in (1 << 19, size):  # the last 512 KB almost always has it; else the whole file
                f.seek(max(0, size - tail))
                for line in reversed(f.read().splitlines()):
                    if b'"ai-title"' in line:
                        return json.loads(line).get("aiTitle") or None
                if tail >= size:
                    break
    except Exception:  # the title is a nicety; never let the lookup cost us the banner
        pass
    return None


def running_session_name(session_id):
    """Display name of a running session (as shown in `claude agents`), or None."""
    claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    try:
        result = subprocess.run(
            [claude, "agents", "--json"], capture_output=True, text=True, timeout=5
        )
        for session in json.loads(result.stdout):
            if session.get("sessionId") == session_id:
                return session.get("name")
    except Exception:  # same reasoning as transcript_title
        pass
    return None


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    alert = ALERTS.get(event.get("notification_type"))
    if alert is None:
        return
    label, play_sound = alert

    project = os.path.basename(event.get("cwd") or "") or "Claude Code"
    title = (
        transcript_title(event.get("transcript_path") or "")
        or running_session_name(str(event.get("session_id") or ""))
        or project
    )
    body = event.get("message") or label
    subprocess.run(
        ["osascript", "-e", APPLESCRIPT, body, title, f"{project} · {label}",
         SOUND if play_sound else ""],
        capture_output=True,
        timeout=10,
    )


if __name__ == "__main__":
    main()
