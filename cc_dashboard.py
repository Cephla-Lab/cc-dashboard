#!/usr/bin/env python3
"""cc-dashboard: a live list of your Claude Code sessions, and a way to jump to each one's Terminal tab.

    cc-dashboard          live view; press a row's key to bring its Terminal tab forward
    cc-dashboard --once   print the list once and exit

Session state comes from `claude agents --json`, the supported interface for reading it.
"""
import argparse
import glob
import json
import os
import re
import select
import shutil
import string
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from notify_needs_input import transcript_title  # sibling hook script: same title as the banners

REFRESH_SECONDS = 3

# `waitingFor` values from `claude agents --json` that deserve friendlier wording.
BLOCKERS = {"permission prompt": "needs permission", "input needed": "needs input"}

# Row keys: digits, then letters. q and r are taken by quit and refresh.
HOTKEYS = "123456789" + "".join(c for c in string.ascii_lowercase if c not in "qr")

AGE_WIDTH = 6
PREFIX_WIDTH = 5 + AGE_WIDTH + 2  # " k ! " + age + gap
# Columns are packed to their content, never stretched to the window; "needs" takes what is left.
TITLE_MAX, TITLE_MIN, PROJECT_MAX, NEEDS_MIN = 44, 16, 20, 20
TITLE_SHARE = 0.4  # of the room the title and needs columns share

NEEDS_LOOKBACK = 6  # closing lines searched for a question
RULE_LINE = re.compile(r"^[`\s─━—_=*-]{6,}$")  # horizontal rules and box edges

# Colour carries state, not decoration. Chosen to stay legible on a light background
# (no yellow or cyan). Finished sessions idle longer than STALE_AFTER are dimmed.
RESET = "\033[0m"
STYLES = {
    "heading": "\033[1m",
    "key": "\033[1m",
    "blocked": "\033[1;31m",
    "blocked_title": "\033[1m",
    "finished": "\033[32m",
    "working": "\033[34m",
    "secondary": "\033[2m",
    "stale": "\033[2m",
}
STALE_AFTER = 3600

TRANSCRIPT_TAIL = 4 << 20  # bytes searched from the end of a transcript for the last message

# The tty arrives as argv, so it is never spliced into the script text.
FOCUS_SCRIPT = """
on run argv
    set target to item 1 of argv
    tell application "Terminal"
        repeat with w in windows
            repeat with t in tabs of w
                if tty of t is target then
                    set miniaturized of w to false
                    set selected of t to true
                    set index of w to 1
                    activate
                    return "found"
                end if
            end repeat
        end repeat
    end tell
    return "missing"
end run
"""


def format_age(seconds):
    """Compact duration: 0:05, 1:05, 1h01, 1d01h. Blank when unknown."""
    if seconds is None:
        return ""
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    hours, mins = divmod(minutes, 60)
    days, hrs = divmod(hours, 24)
    if days:
        return f"{days}d{hrs:02d}h"
    if hours:
        return f"{hours}h{mins:02d}"
    return f"{minutes}:{secs:02d}"


@dataclass
class Row:
    session_id: str
    group: str  # "waiting" (on you) or "working"
    title: str
    project: str
    label: str
    urgent: bool
    age: Optional[float]  # seconds since the session last did anything; None if unknown
    tty: Optional[str]
    number: int = 0
    needs: str = ""  # what the session wants from you, in a few words


def sort_key(row):
    """Display order: blocked sessions (longest-blocked on top), then finished ones, freshest first, then working."""
    if row.group == "working":
        return (2, 0)
    if row.urgent:
        return (0, -(row.age or 0))
    return (1, row.age if row.age is not None else float("inf"))


def flatten_markdown(line):
    line = re.sub(r"^(\s*([-*+>]|\d+[.)]|#{1,6})\s+)+", "", line)  # list markers, quotes, headings
    line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)  # [text](url) -> text
    line = re.sub(r"(?<!\w)\*(\S(?:.*?\S)?)\*(?!\w)", r"\1", line.replace("**", "").replace("__", ""))
    return " ".join(line.replace("`", "").split())


def summarize_needs(text):
    """What a session that stopped talking wants from you: its closing question, else its closing line.

    A heuristic, not a summary: it skips what is never the ask (code blocks, Insight boxes,
    decoration, a trailing sources list) and prefers the last question near the end.
    """
    kept, in_code, in_insight = [], False, False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
        elif in_code or not line:
            continue
        elif "★ Insight" in line:
            in_insight = True
        elif RULE_LINE.match(line):
            in_insight = False
        elif re.match(r"^\W*(sources|references)\W*$", line, re.I):
            break  # a closing link list follows
        elif not in_insight:
            kept.append(flatten_markdown(line))
    kept = [line for line in kept if line]
    for line in reversed(kept[-NEEDS_LOOKBACK:]):
        for sentence in reversed(re.split(r"(?<=[.!?])\s+", line)):
            if sentence.endswith("?"):
                return sentence
    return kept[-1] if kept else ""


def last_blocks(transcript_path):
    """(latest assistant text, pending tool call) from the main conversation; either may be None.

    Claude Code writes one transcript entry per content block. A tool call counts as pending only
    when it is the very last assistant block. Subagent chatter (isSidechain) is ignored.
    """
    tool, seen_last = None, False
    for line in reversed(transcript_tail(transcript_path)):
        if b'"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # the tail can start mid-line
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        for block in reversed((entry.get("message") or {}).get("content") or []):
            kind = block.get("type") if isinstance(block, dict) else None
            if kind == "text" and (block.get("text") or "").strip():
                return block["text"], tool
            if kind == "tool_use":
                if not seen_last:
                    tool = block
                seen_last = True
    return None, tool


def describe_tool(tool):
    """'Bash: Install x with Homebrew', 'Edit: settings.json': enough to decide before switching tabs."""
    name, params = tool.get("name") or "tool", tool.get("input") or {}
    command = (params.get("command") or "").strip()
    detail = (
        params.get("description")
        or (command.splitlines()[0] if command else "")
        or os.path.basename(params.get("file_path") or params.get("notebook_path") or "")
        or params.get("url")
        or params.get("query")
        or ""
    )
    return f"{name}: {detail}" if detail else name


def needs_for(status, last_text, last_tool):
    """What the session wants from you, in a few words. Blank while it is working."""
    if status == "waiting" and last_tool:
        questions = (last_tool.get("input") or {}).get("questions") or []
        if last_tool.get("name") == "AskUserQuestion" and questions:
            return str(questions[0].get("question") or "")
        return "approve " + describe_tool(last_tool)
    if status in ("waiting", "idle"):
        return summarize_needs(last_text)
    return ""


def build_rows(sessions, now, title_of, last_activity_of, tty_of, needs_of=lambda s: ""):
    """Rows for display from parsed `claude agents --json`. Lookups are injected to keep this pure."""
    rows = []
    for s in sessions:
        label, urgent = label_for(s.get("status"), s.get("waitingFor"))
        project = os.path.basename(s.get("cwd") or "") or "?"
        last_activity = last_activity_of(s)
        rows.append(Row(
            session_id=s.get("sessionId") or "",
            group="working" if s.get("status") == "busy" else "waiting",
            title=title_of(s) or s.get("name") or project,
            project=project,
            label=label,
            urgent=urgent,
            age=None if last_activity is None else now - last_activity,
            tty=tty_of(s),
            needs=needs_of(s) or "",
        ))
    rows.sort(key=sort_key)
    for number, row in enumerate(rows, start=1):
        row.number = number
    return rows


def last_message_time(transcript_path):
    """Epoch time of the last user/assistant message, or None.

    Not the file's mtime: Claude Code appends metadata (titles, mode changes, settings reloads)
    to a transcript long after the conversation went quiet.
    """
    for line in reversed(transcript_tail(transcript_path)):
        if b'"user"' not in line and b'"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("type") in ("user", "assistant") and entry.get("timestamp"):
                return datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            continue  # a torn or foreign line must not hide the messages before it
    return None


def transcript_tail(transcript_path):
    """Lines from the end of a transcript (the first may be cut mid-line); empty if unreadable."""
    try:
        with open(transcript_path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - TRANSCRIPT_TAIL))
            return f.read().splitlines()
    except OSError:
        return []


def hotkey_for(number):
    """Key that jumps to row `number`: 1-9, then letters. Blank once the keys run out."""
    return HOTKEYS[number - 1] if 1 <= number <= len(HOTKEYS) else ""


def row_for_key(rows, key):
    return next((r for r in rows if key and hotkey_for(r.number) == key), None)


def wants_color(isatty, env):
    """Colour only for a person at a terminal, and never when NO_COLOR is set (no-color.org)."""
    return bool(isatty) and not env.get("NO_COLOR")


def clip(text, width):
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def column_widths(rows, width):
    """(title, project, label, needs) widths. Needs is 0 when no row has any or there is no room."""
    title = min(TITLE_MAX, max(len(r.title) for r in rows))
    project = min(PROJECT_MAX, max(len(r.project) for r in rows))
    label = max(len(r.label) for r in rows)

    def room_for_needs(title_width):
        return width - (PREFIX_WIDTH + title_width + 2 + project + 2 + label + 2)

    needs = 0
    if any(r.needs for r in rows):
        # What a session wants matters more than its name: the title takes at most TITLE_SHARE of
        # the room the two columns share (never below TITLE_MIN); needs get the rest, or are dropped.
        shared = room_for_needs(0)
        squeezed = min(title, max(TITLE_MIN, int(shared * TITLE_SHARE)))
        if room_for_needs(squeezed) >= NEEDS_MIN:
            title, needs = squeezed, room_for_needs(squeezed)
    overflow = PREFIX_WIDTH + title + 2 + project + 2 + label - width
    if not needs and overflow > 0:
        title = max(10, title - overflow)
    return title, project, label, needs


def render(rows, width, color=False):
    """The list as text lines, none wider than `width`. Colour adds escape codes but never moves text."""
    if not rows:
        return ["No Claude Code sessions are running."]

    def paint(text, style):
        return f"{STYLES[style]}{text}{RESET}" if color and style else text

    title_width, project_width, label_width, needs_width = column_widths(rows, width)
    lines = []
    for group, heading in (("waiting", "WAITING ON YOU"), ("working", "WORKING")):
        members = [r for r in rows if r.group == group]
        if not members:
            continue
        if lines:
            lines.append("")
        lines.append(paint(heading, "heading"))
        for r in members:
            state = "blocked" if r.urgent else "working" if group == "working" else "finished"
            stale = state == "finished" and r.age is not None and r.age > STALE_AFTER
            needs = clip(r.needs, needs_width) if needs_width else ""
            emphasis = "blocked_title" if r.urgent else None
            # (text, style): every cell but the last is padded, so styling can never shift a column.
            cells = [
                (f" {hotkey_for(r.number):1} ", "key"),
                (f"{'!' if r.urgent else ' '} ", state),
                (f"{format_age(r.age) if group == 'waiting' else '':>{AGE_WIDTH}}  ", None),
                (f"{clip(r.title, title_width):<{title_width}}  ", emphasis),
                (f"{clip(r.project, project_width):<{project_width}}  ", None),
            ]
            if needs:
                cells += [(f"{r.label:<{label_width}}  ", state), (needs, emphasis)]
            else:
                cells += [(r.label, state)]
            plain = "".join(text for text, _ in cells)
            if len(plain) > width:  # too narrow for the columns: cut the line, uncoloured
                lines.append(plain[:width].rstrip())
            elif stale:
                lines.append(paint(plain, "stale"))
            else:
                lines.append("".join(paint(text, style) for text, style in cells))
    return lines


def focus_command(tty):
    """osascript argv that selects the Terminal tab on `tty` and brings its window forward."""
    return ["osascript", "-e", FOCUS_SCRIPT, "/dev/" + tty]


def label_for(status, waiting_for):
    """(label, urgent) for a session. Urgent = blocked mid-task, as opposed to finished."""
    if status == "waiting":
        return BLOCKERS.get(waiting_for, waiting_for or "needs you"), True
    if status == "idle":
        return "finished", False
    return "working", False


# --- Wiring the banner hook into Claude Code's settings.json ---------------------------------

HOOK_SCRIPT = "notify_needs_input.py"  # any Notification hook running this script counts as ours
BACKUP_SUFFIX = ".bak-cc-dashboard"


def _is_ours(hook):
    return HOOK_SCRIPT in str(hook.get("command", ""))


def with_hook(settings, command):
    """A copy of `settings` with the Notification hook added; unchanged if one is already wired."""
    groups = settings.get("hooks", {}).get("Notification", [])
    if any(_is_ours(hook) for group in groups for hook in group.get("hooks", [])):
        return settings
    ours = {"matcher": "", "hooks": [{"type": "command", "command": command, "async": True, "timeout": 20}]}
    return {**settings, "hooks": {**settings.get("hooks", {}), "Notification": [*groups, ours]}}


def without_hook(settings):
    """A copy of `settings` without our Notification hook; every other hook and setting is kept."""
    hooks = settings.get("hooks", {})
    groups = hooks.get("Notification", [])
    kept = [
        {**group, "hooks": [h for h in group.get("hooks", []) if not _is_ours(h)]}
        for group in groups
    ]
    kept = [group for group in kept if group["hooks"]]
    if kept == groups:
        return settings
    hooks = {**hooks, "Notification": kept} if kept else {k: v for k, v in hooks.items() if k != "Notification"}
    rest = {k: v for k, v in settings.items() if k != "hooks"}
    return {**rest, "hooks": hooks} if hooks else rest


def update_settings_file(path, transform):
    """Apply `transform` to a settings file. Returns "updated", "unchanged" or "invalid".

    The original is copied to <path>.bak-cc-dashboard before any write, a file that is not valid
    JSON is never touched, and nothing is written when the transform changes nothing.
    """
    try:
        with open(path, encoding="utf-8") as f:
            original = f.read()
    except FileNotFoundError:
        original = None
    try:
        settings = json.loads(original) if original and original.strip() else {}
    except ValueError:
        return "invalid"
    if not isinstance(settings, dict):
        return "invalid"
    updated = transform(settings)
    if updated == settings:
        return "unchanged"
    if original is not None:
        with open(path + BACKUP_SUFFIX, "w", encoding="utf-8") as f:
            f.write(original)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(updated, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return "updated"


# --- I/O shell: everything below talks to the outside world ---------------------------------


def load_sessions():
    """Parsed `claude agents --json`; empty when it can't be read."""
    claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    try:
        result = subprocess.run([claude, "agents", "--json"], capture_output=True, text=True, timeout=10)
        sessions = json.loads(result.stdout)
        return sessions if isinstance(sessions, list) else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def ttys_by_pid(pids):
    """{pid: "ttys085"} from a single `ps` call. Processes without a terminal are left out."""
    if not pids:
        return {}
    result = subprocess.run(
        ["ps", "-o", "pid=,tty=", "-p", ",".join(str(p) for p in pids)], capture_output=True, text=True
    )
    pairs = (line.split() for line in result.stdout.splitlines())
    return {int(pid): dev for pid, dev in (p for p in pairs if len(p) == 2) if dev.startswith("tty")}


_transcript_paths = {}  # session id -> path
_transcript_info = {}  # path -> ((mtime_ns, size), (title, last_message_time, last_text, pending_tool))


def transcript_info(session):
    """(title, last message time, last text, pending tool) for a session; re-read only when its transcript changes."""
    session_id = session.get("sessionId") or ""
    path = _transcript_paths.get(session_id)
    if not path or not os.path.exists(path):
        matches = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{glob.escape(session_id)}.jsonl"))
        path = _transcript_paths[session_id] = matches[0] if session_id and matches else None
    if not path:
        return None, None, None, None
    stat = os.stat(path)
    version = (stat.st_mtime_ns, stat.st_size)
    cached = _transcript_info.get(path)
    if not cached or cached[0] != version:
        info = (transcript_title(path), last_message_time(path), *last_blocks(path))
        cached = _transcript_info[path] = (version, info)
    return cached[1]


def snapshot():
    sessions = load_sessions()
    ttys = ttys_by_pid([s["pid"] for s in sessions if s.get("pid")])
    return build_rows(
        sessions,
        now=time.time(),
        title_of=lambda s: transcript_info(s)[0],
        last_activity_of=lambda s: transcript_info(s)[1],
        tty_of=lambda s: ttys.get(s.get("pid")),
        needs_of=lambda s: needs_for(s.get("status"), *transcript_info(s)[2:]),
    )


def focus(row):
    """Bring the row's Terminal tab forward; returns a one-line status message."""
    if not row.tty:
        return f"{row.title}: no terminal tab (background session?)"
    result = subprocess.run(focus_command(row.tty), capture_output=True, text=True, timeout=10)
    if result.stdout.strip() == "found":
        return f"-> {row.title}"
    return f"{row.title}: no Terminal tab on /dev/{row.tty}" + (f" ({result.stderr.strip()})" if result.stderr.strip() else "")


def draw(rows, message, width, color):
    footer = clip(f"[1-9 a-z] jump to tab   [r] refresh   [q] quit      {time.strftime('%H:%M:%S')}", width)
    if color:
        footer = f"{STYLES['secondary']}{footer}{RESET}"
    frame = render(rows, width, color) + ["", footer, clip(message, width)]
    sys.stdout.write("\033[H\033[J" + "\n".join(frame))
    sys.stdout.flush()


def run(color):
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    sys.stdout.write("\033[?1049h\033[?25l")  # alternate screen, cursor hidden
    try:
        tty.setcbreak(fd)
        message = ""
        while True:
            rows = snapshot()
            draw(rows, message, shutil.get_terminal_size().columns, color)
            message = ""
            if not select.select([sys.stdin], [], [], REFRESH_SECONDS)[0]:
                continue
            key = sys.stdin.read(1)
            if key == "q":
                break
            row = row_for_key(rows, key)
            if row:
                message = focus(row)
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()


def configure_hook(install):
    """Add or remove the banner hook in ~/.claude/settings.json; returns a process exit code."""
    path = os.path.expanduser("~/.claude/settings.json")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), HOOK_SCRIPT)
    command = f'python3 "{script}" || true'
    outcome = update_settings_file(path, (lambda s: with_hook(s, command)) if install else without_hook)
    if outcome == "invalid":
        print(f"{path} is not valid JSON, so it was left untouched.", file=sys.stderr)
        if install:
            print(f"Add this Notification hook command by hand:\n  {command}", file=sys.stderr)
        return 1
    verb = "added to" if install else "removed from"
    if outcome == "updated":
        print(f"Banner hook {verb} {path} (previous version saved as {os.path.basename(path)}{BACKUP_SUFFIX}).")
    else:
        print(f"Banner hook already {'present in' if install else 'absent from'} {path}; nothing changed.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="cc-dashboard",
        description="Live list of your Claude Code sessions: who is waiting on you, for what, "
        "and one key to jump to that session's Terminal tab.",
    )
    parser.add_argument("--once", action="store_true", help="print the list once and exit")
    parser.add_argument("--install-hook", action="store_true",
                        help="add the macOS banner hook to ~/.claude/settings.json (backs the file up first)")
    parser.add_argument("--remove-hook", action="store_true", help="remove that hook again")
    args = parser.parse_args()
    if args.install_hook or args.remove_hook:
        sys.exit(configure_hook(install=args.install_hook))

    color = wants_color(sys.stdout.isatty(), os.environ)
    if args.once or not sys.stdout.isatty():
        print("\n".join(render(snapshot(), shutil.get_terminal_size().columns, color)))
    else:
        run(color)


if __name__ == "__main__":
    main()
