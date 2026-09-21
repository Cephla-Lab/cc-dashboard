"""Tests for cc_dashboard.py. Run from the repo root: python3 -m unittest discover -s tests -t ."""
import json
import os
import re
import shutil
import tempfile
import unittest

import cc_dashboard as cw


class FormatAge(unittest.TestCase):
    def test_seconds_only(self):
        self.assertEqual(cw.format_age(5), "0:05")

    def test_under_an_hour_shows_minutes_and_seconds(self):
        self.assertEqual(cw.format_age(65), "1:05")

    def test_hours(self):
        self.assertEqual(cw.format_age(3700), "1h01")

    def test_days(self):
        self.assertEqual(cw.format_age(90000), "1d01h")

    def test_unknown_age_is_blank(self):
        self.assertEqual(cw.format_age(None), "")

    def test_clock_skew_clamps_to_zero(self):
        self.assertEqual(cw.format_age(-3), "0:00")


class LabelFor(unittest.TestCase):
    def test_permission_prompt_is_urgent(self):
        self.assertEqual(cw.label_for("waiting", "permission prompt"), ("needs permission", True))

    def test_input_needed_is_urgent(self):
        self.assertEqual(cw.label_for("waiting", "input needed"), ("needs input", True))

    def test_other_blockers_are_shown_as_reported(self):
        self.assertEqual(cw.label_for("waiting", "dialog open"), ("dialog open", True))

    def test_waiting_without_a_reason(self):
        self.assertEqual(cw.label_for("waiting", None), ("needs you", True))

    def test_idle_means_claude_finished_its_turn(self):
        self.assertEqual(cw.label_for("idle", None), ("finished", False))

    def test_busy(self):
        self.assertEqual(cw.label_for("busy", None), ("working", False))


NOW = 1_000_000.0


def session(sid, status, cwd="/work/web-app", waiting_for=None, name=None):
    entry = {"sessionId": sid, "status": status, "cwd": cwd, "pid": 100, "kind": "interactive"}
    if waiting_for:
        entry["waitingFor"] = waiting_for
    if name:
        entry["name"] = name
    return entry


def rows_for(sessions, titles=None, last_activity=None, ttys=None):
    titles, last_activity, ttys = titles or {}, last_activity or {}, ttys or {}
    return cw.build_rows(
        sessions,
        now=NOW,
        title_of=lambda s: titles.get(s["sessionId"]),
        last_activity_of=lambda s: last_activity.get(s["sessionId"]),
        tty_of=lambda s: ttys.get(s["sessionId"]),
    )


class BuildRows(unittest.TestCase):
    def test_idle_and_blocked_sessions_are_waiting_on_you(self):
        rows = rows_for([session("a", "idle"), session("b", "waiting", waiting_for="permission prompt")])
        self.assertEqual({r.group for r in rows}, {"waiting"})

    def test_busy_sessions_are_working(self):
        self.assertEqual(rows_for([session("a", "busy")])[0].group, "working")

    def test_title_comes_from_the_transcript(self):
        rows = rows_for([session("a", "idle", name="web-app-fc")], titles={"a": "Fix the flaky checkout test"})
        self.assertEqual(rows[0].title, "Fix the flaky checkout test")

    def test_title_falls_back_to_session_name_then_project(self):
        named, bare = rows_for([session("a", "busy", name="web-app-fc"), session("b", "busy")])
        self.assertEqual((named.title, bare.title), ("web-app-fc", "web-app"))

    def test_project_is_the_folder_name(self):
        self.assertEqual(rows_for([session("a", "idle", cwd="/x/y/firmware")])[0].project, "firmware")

    def test_age_counts_from_last_activity(self):
        self.assertEqual(rows_for([session("a", "idle")], last_activity={"a": NOW - 432})[0].age, 432)

    def test_age_is_unknown_without_last_activity(self):
        self.assertIsNone(rows_for([session("a", "idle")])[0].age)

    def test_tty_is_carried_for_tab_switching(self):
        self.assertEqual(rows_for([session("a", "idle")], ttys={"a": "ttys085"})[0].tty, "ttys085")

    def test_order_is_blocked_then_freshly_finished_then_working(self):
        rows = rows_for(
            [session("old", "idle"), session("busy", "busy"), session("fresh", "idle"),
             session("blocked", "waiting", waiting_for="permission prompt")],
            last_activity={"old": NOW - 600, "fresh": NOW - 30, "blocked": NOW - 10},
        )
        self.assertEqual([r.session_id for r in rows], ["blocked", "fresh", "old", "busy"])

    def test_longest_blocked_session_comes_first(self):
        rows = rows_for(
            [session("b10", "waiting", waiting_for="input needed"), session("b100", "waiting", waiting_for="input needed")],
            last_activity={"b10": NOW - 10, "b100": NOW - 100},
        )
        self.assertEqual([r.session_id for r in rows], ["b100", "b10"])

    def test_finished_sessions_of_unknown_age_sink_below_known_ones(self):
        rows = rows_for([session("unknown", "idle"), session("known", "idle")], last_activity={"known": NOW - 5000})
        self.assertEqual([r.session_id for r in rows], ["known", "unknown"])

    def test_rows_are_numbered_in_display_order(self):
        rows = rows_for([session("busy", "busy"), session("idle", "idle")])
        self.assertEqual([(r.number, r.session_id) for r in rows], [(1, "idle"), (2, "busy")])


def row(number, title="Some task", group="waiting", label="finished", urgent=False, age=65, project="web-app", needs=""):
    return cw.Row(session_id=f"s{number}", group=group, title=title, project=project, label=label,
                  urgent=urgent, age=age, tty="ttys001", number=number, needs=needs)


class Hotkeys(unittest.TestCase):
    def test_first_nine_rows_use_digits(self):
        self.assertEqual([cw.hotkey_for(n) for n in (1, 9)], ["1", "9"])

    def test_later_rows_use_letters(self):
        self.assertEqual([cw.hotkey_for(n) for n in (10, 11)], ["a", "b"])

    def test_quit_and_refresh_keys_are_never_hotkeys(self):
        keys = [cw.hotkey_for(n) for n in range(1, 25)]
        self.assertEqual(len(set(keys)), 24, "every one of the first 24 rows needs its own key")
        self.assertFalse({"q", "r"} & set(keys))

    def test_rows_beyond_the_available_keys_get_none(self):
        self.assertEqual(cw.hotkey_for(500), "")

    def test_key_press_finds_its_row(self):
        rows = [row(n) for n in range(1, 12)]
        self.assertEqual(cw.row_for_key(rows, "a").number, 10)

    def test_unassigned_key_finds_nothing(self):
        self.assertIsNone(cw.row_for_key([row(1)], "z"))


class Render(unittest.TestCase):
    def test_sessions_are_listed_under_their_group_heading(self):
        lines = cw.render([row(1, "Review PR"), row(2, "Port driver", group="working", label="working", age=None)], width=100)
        text = "\n".join(lines)
        self.assertLess(text.index("WAITING ON YOU"), text.index("Review PR"))
        self.assertLess(text.index("Review PR"), text.index("WORKING"))
        self.assertLess(text.index("WORKING"), text.index("Port driver"))

    def test_heading_is_omitted_for_an_empty_group(self):
        text = "\n".join(cw.render([row(1)], width=100))
        self.assertIn("WAITING ON YOU", text)
        self.assertNotIn("WORKING", text)

    def test_blocked_rows_are_flagged(self):
        blocked, finished = cw.render([row(1, urgent=True, label="needs permission"), row(2)], width=100)[1:3]
        self.assertIn("!", blocked)
        self.assertNotIn("!", finished)

    def test_row_shows_hotkey_title_project_label_and_age(self):
        line = cw.render([row(1, "Fix the flaky checkout test", label="needs permission", urgent=True, age=40)], width=100)[1]
        for part in ("1", "Fix the flaky checkout test", "web-app", "needs permission", "0:40"):
            self.assertIn(part, line)

    def test_working_rows_show_no_age(self):
        line = cw.render([row(1, group="working", label="working", age=12)], width=100)[1]
        self.assertNotIn("0:12", line)

    def test_long_titles_are_cut_to_fit_the_terminal(self):
        lines = cw.render([row(1, "An extremely long session title " * 5)], width=70)
        self.assertTrue(all(len(line) <= 70 for line in lines))
        self.assertIn("…", lines[1])

    def test_no_sessions(self):
        self.assertEqual(cw.render([], width=80), ["No Claude Code sessions are running."])


class SummarizeNeeds(unittest.TestCase):
    def test_closing_question_is_what_is_needed(self):
        text = "I rebased the branch.\n\nShall I open the pull request now and ask for a review?"
        self.assertEqual(cw.summarize_needs(text), "Shall I open the pull request now and ask for a review?")

    def test_without_a_question_the_closing_line_is_used(self):
        text = "Details first.\n\nThe migration stays on hold until the review is done."
        self.assertEqual(cw.summarize_needs(text), "The migration stays on hold until the review is done.")

    def test_a_question_beats_a_later_statement(self):
        text = "Want me to run the migration?\n\nI have not touched the database yet."
        self.assertEqual(cw.summarize_needs(text), "Want me to run the migration?")

    def test_only_the_question_sentence_is_kept(self):
        text = "The tests pass. Shall I push now?"
        self.assertEqual(cw.summarize_needs(text), "Shall I push now?")

    def test_insight_boxes_are_skipped(self):
        text = "The doc is updated.\n\n`★ Insight ─────────────`\nIs this a teaching point?\n`───────────────────────`"
        self.assertEqual(cw.summarize_needs(text), "The doc is updated.")

    def test_code_blocks_are_skipped(self):
        text = "Run this when ready:\n\n```\nmake deploy  # ok?\n```"
        self.assertEqual(cw.summarize_needs(text), "Run this when ready:")

    def test_source_lists_are_skipped(self):
        text = "Allow the app in System Settings.\n\nSources:\n- [issue](https://github.com/x/y/issues/1)\n- [repo](https://github.com/x/y)"
        self.assertEqual(cw.summarize_needs(text), "Allow the app in System Settings.")

    def test_markdown_is_flattened(self):
        self.assertEqual(cw.summarize_needs("- **Next:** run `make test` and [tell me](http://x.y)"), "Next: run make test and tell me")

    def test_nothing_said(self):
        self.assertEqual(cw.summarize_needs(None), "")

    def test_only_whitespace_said(self):
        self.assertEqual(cw.summarize_needs("  \n\n"), "")


def transcript_file(test, *entries):
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    test.addCleanup(os.unlink, f.name)
    f.write("".join(json.dumps(e) + "\n" for e in entries))
    f.close()
    return f.name


def said(block, sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain, "timestamp": "2026-09-21T07:00:00.000Z",
            "message": {"role": "assistant", "content": [block]}}


BASH = {"type": "tool_use", "name": "Bash", "input": {"command": "brew install x", "description": "Install x with Homebrew"}}


class LastBlocks(unittest.TestCase):
    def test_turn_that_ended_with_text_has_no_pending_tool(self):
        path = transcript_file(self, said(BASH), said({"type": "text", "text": "Done."}))
        self.assertEqual(cw.last_blocks(path), ("Done.", None))

    def test_a_tool_call_as_the_last_block_is_the_pending_one(self):
        path = transcript_file(self, said({"type": "text", "text": "Installing."}), said(BASH))
        self.assertEqual(cw.last_blocks(path), ("Installing.", BASH))

    def test_subagent_chatter_is_ignored(self):
        path = transcript_file(self, said({"type": "text", "text": "Main says hi."}),
                               said({"type": "text", "text": "subagent"}, sidechain=True))
        self.assertEqual(cw.last_blocks(path), ("Main says hi.", None))

    def test_missing_transcript(self):
        self.assertEqual(cw.last_blocks("/nonexistent/transcript.jsonl"), (None, None))


class NeedsFor(unittest.TestCase):
    def test_permission_prompt_names_the_tool_and_what_it_does(self):
        self.assertEqual(cw.needs_for("waiting", "Installing.", BASH), "approve Bash: Install x with Homebrew")

    def test_bash_without_a_description_shows_the_first_command_line(self):
        tool = {"type": "tool_use", "name": "Bash", "input": {"command": "git push origin main\necho done"}}
        self.assertEqual(cw.needs_for("waiting", None, tool), "approve Bash: git push origin main")

    def test_permission_for_a_file_edit_names_the_file(self):
        tool = {"type": "tool_use", "name": "Edit", "input": {"file_path": "/a/b/settings.json", "old_string": "x", "new_string": "y"}}
        self.assertEqual(cw.needs_for("waiting", None, tool), "approve Edit: settings.json")

    def test_a_question_dialog_shows_the_question(self):
        tool = {"type": "tool_use", "name": "AskUserQuestion", "input": {"questions": [{"question": "Which fix do you want?"}]}}
        self.assertEqual(cw.needs_for("waiting", None, tool), "Which fix do you want?")

    def test_blocked_without_a_tool_falls_back_to_what_claude_said(self):
        self.assertEqual(cw.needs_for("waiting", "Pick one. Which do you prefer?", None), "Which do you prefer?")

    def test_finished_sessions_show_the_closing_ask(self):
        self.assertEqual(cw.needs_for("idle", "All done.\n\nShall I push now?", None), "Shall I push now?")

    def test_working_sessions_need_nothing(self):
        self.assertEqual(cw.needs_for("busy", "Shall I push now?", None), "")


class Layout(unittest.TestCase):
    def test_needs_are_carried_into_the_row(self):
        rows = cw.build_rows([session("a", "idle")], now=NOW, title_of=lambda s: None, last_activity_of=lambda s: None,
                             tty_of=lambda s: None, needs_of=lambda s: "Shall I push now?")
        self.assertEqual(rows[0].needs, "Shall I push now?")

    def test_columns_pack_to_their_content_instead_of_stretching_to_the_window(self):
        lines = cw.render([row(1, "Review PR"), row(2, "Port the payment client")], width=250)
        self.assertRegex(lines[2], r"Port the payment client {2}web-app")
        self.assertLess(max(len(line) for line in lines), 80)

    def test_very_long_titles_are_capped(self):
        line = cw.render([row(1, "An extremely long session title " * 5)], width=250)[1]
        self.assertIn("…", line)
        self.assertLess(line.index("web-app"), 70)

    def test_needs_column_comes_last(self):
        line = cw.render([row(1, "Review PR", needs="Shall I push now?")], width=250)[1]
        self.assertLess(line.index("finished"), line.index("Shall I push now?"))

    def test_needs_are_clipped_to_the_window(self):
        line = cw.render([row(1, "Review PR", needs="Shall I push " + "every single commit " * 20)], width=120)[1]
        self.assertLessEqual(len(line), 120)
        self.assertTrue(line.endswith("…"), line)

    def test_long_titles_give_way_so_needs_still_fit(self):
        line = cw.render([row(1, "A fairly long session title that would use the room", needs="Shall I push now?")], width=90)[1]
        self.assertIn("Shall I push", line)
        self.assertLessEqual(len(line), 90)

    def test_needs_get_more_room_than_the_title_in_a_modest_window(self):
        title, needs = "A session title long enough to want all of its room", "Shall I push " + "every single commit " * 20
        line = cw.render([row(1, title, needs=needs)], width=110)[1]
        shown_needs = line.split("finished  ", 1)[1]
        shown_title = line[cw.PREFIX_WIDTH:line.index("web-app")].strip()
        self.assertGreater(len(shown_needs), len(shown_title))
        self.assertGreaterEqual(len(shown_title), cw.TITLE_MIN)

    def test_needs_are_dropped_when_there_is_no_room_at_all(self):
        line = cw.render([row(1, "Review PR", needs="Shall I push now?")], width=50)[1]
        self.assertNotIn("Shall", line)
        self.assertLessEqual(len(line), 50)


ANSI = re.compile(r"\x1b\[[0-9;]*m")


class ColumnOrder(unittest.TestCase):
    def test_age_comes_right_after_the_hotkey_before_the_title(self):
        line = cw.render([row(1, "Fix the flaky checkout test", age=40)], width=100)[1]
        self.assertLess(line.index("0:40"), line.index("Fix the flaky"))


class Color(unittest.TestCase):
    ROWS = [
        row(1, "Blocked task", urgent=True, label="needs permission", age=40, needs="approve Bash: Install x"),
        row(2, "Fresh result", age=300, needs="Shall I push now?"),
        row(3, "Stale result", age=7200, needs="Merge stays on hold."),
        row(4, "Busy task", group="working", label="working", age=None),
    ]

    def line_with(self, text):
        return next(line for line in cw.render(self.ROWS, width=100, color=True) if text in line)

    def test_color_never_changes_the_layout(self):
        colored = cw.render(self.ROWS, width=100, color=True)
        self.assertIn("\x1b[", "\n".join(colored))
        self.assertEqual([ANSI.sub("", line) for line in colored], cw.render(self.ROWS, width=100))

    def test_plain_output_has_no_escape_codes(self):
        self.assertNotIn("\x1b", "\n".join(cw.render(self.ROWS, width=100)))

    def test_blocked_rows_stand_out(self):
        self.assertIn(cw.STYLES["blocked"], self.line_with("Blocked task"))
        self.assertNotIn(cw.STYLES["blocked"], self.line_with("Fresh result"))

    def test_stale_results_are_dimmed_and_fresh_ones_are_not(self):
        self.assertIn(cw.STYLES["stale"], self.line_with("Stale result"))
        self.assertNotIn(cw.STYLES["stale"], self.line_with("Fresh result"))

    def test_styles_never_leak_into_the_next_line(self):
        styled = [line for line in cw.render(self.ROWS, width=100, color=True) if ANSI.search(line)]
        self.assertTrue(styled, "expected styled lines")
        for line in styled:
            self.assertEqual(ANSI.findall(line)[-1], cw.RESET, line)

    def test_narrow_terminals_still_fit(self):
        lines = cw.render(self.ROWS, width=50, color=True)
        self.assertTrue(all(len(ANSI.sub("", line)) <= 50 for line in lines))


class WantsColor(unittest.TestCase):
    def test_color_on_an_interactive_terminal(self):
        self.assertIs(cw.wants_color(isatty=True, env={}), True)

    def test_no_color_when_output_is_piped(self):
        self.assertIs(cw.wants_color(isatty=False, env={}), False)

    def test_no_color_convention_is_respected(self):
        self.assertIs(cw.wants_color(isatty=True, env={"NO_COLOR": "1"}), False)


class LastMessageTime(unittest.TestCase):
    """Transcripts get metadata appended long after the last message, so file mtime can't be used."""

    def transcript(self, *entries):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        self.addCleanup(os.unlink, f.name)
        f.write("".join(json.dumps(e) + "\n" for e in entries))
        f.close()
        return f.name

    def test_time_of_the_last_conversation_message(self):
        path = self.transcript(
            {"type": "user", "timestamp": "2026-09-21T07:00:00.000Z"},
            {"type": "assistant", "timestamp": "2026-09-21T07:05:30.000Z"},
        )
        self.assertEqual(cw.last_message_time(path), 1789974330.0)

    def test_metadata_written_after_the_last_message_is_ignored(self):
        path = self.transcript(
            {"type": "assistant", "timestamp": "2026-09-21T07:05:30.000Z"},
            {"type": "ai-title", "aiTitle": "x", "timestamp": "2026-09-21T09:00:00.000Z"},
            {"type": "mode", "timestamp": "2026-09-21T09:00:01.000Z"},
        )
        self.assertEqual(cw.last_message_time(path), 1789974330.0)

    def test_message_lookalikes_nested_in_other_entries_are_ignored(self):
        path = self.transcript(
            {"type": "assistant", "timestamp": "2026-09-21T07:05:30.000Z"},
            {"type": "attachment", "timestamp": "2026-09-21T08:00:00.000Z", "item": {"type": "user"}},
        )
        self.assertEqual(cw.last_message_time(path), 1789974330.0)

    def test_a_corrupt_line_does_not_hide_earlier_messages(self):
        path = self.transcript({"type": "assistant", "timestamp": "2026-09-21T07:05:30.000Z"})
        with open(path, "a") as f:
            f.write('{"type":"assistant","timestamp": truncated mid-wri\n')
        self.assertEqual(cw.last_message_time(path), 1789974330.0)

    def test_no_messages_yet(self):
        self.assertIsNone(cw.last_message_time(self.transcript({"type": "mode"})))

    def test_missing_transcript(self):
        self.assertIsNone(cw.last_message_time("/nonexistent/transcript.jsonl"))


class FocusCommand(unittest.TestCase):
    def test_tty_is_passed_as_an_argument_not_spliced_into_the_script(self):
        command = cw.focus_command("ttys085")
        self.assertEqual(command[0], "osascript")
        self.assertEqual(command[-1], "/dev/ttys085")
        self.assertNotIn("ttys085", " ".join(command[:-1]))



OURS = 'python3 "/home/me/.claude/hooks/notify_needs_input.py" || true'
THEIRS = {"matcher": "", "hooks": [{"type": "command", "command": "say done"}]}


def commands(settings, event="Notification"):
    return [h["command"] for group in settings.get("hooks", {}).get(event, []) for h in group["hooks"]]


class WithHook(unittest.TestCase):
    def test_hook_is_added_to_empty_settings(self):
        self.assertEqual(commands(cw.with_hook({}, OURS)), [OURS])

    def test_hook_runs_in_the_background_so_it_never_blocks_a_session(self):
        hook = cw.with_hook({}, OURS)["hooks"]["Notification"][0]["hooks"][0]
        self.assertIs(hook.get("async"), True)

    def test_existing_notification_hooks_are_kept(self):
        settings = {"hooks": {"Notification": [THEIRS]}}
        self.assertEqual(commands(cw.with_hook(settings, OURS)), ["say done", OURS])

    def test_other_events_and_settings_are_untouched(self):
        settings = {"model": "opus", "hooks": {"Stop": [THEIRS]}}
        result = cw.with_hook(settings, OURS)
        self.assertEqual((result["model"], commands(result, "Stop")), ("opus", ["say done"]))

    def test_installing_twice_adds_it_once(self):
        once = cw.with_hook({}, OURS)
        self.assertEqual(cw.with_hook(once, OURS), once)

    def test_a_copy_installed_at_another_path_counts_as_present(self):
        elsewhere = cw.with_hook({}, 'python3 "/opt/tools/notify_needs_input.py"')
        self.assertEqual(cw.with_hook(elsewhere, OURS), elsewhere)

    def test_the_input_is_not_modified(self):
        settings = {"hooks": {"Notification": [THEIRS]}}
        cw.with_hook(settings, OURS)
        self.assertEqual(commands(settings), ["say done"])


class WithoutHook(unittest.TestCase):
    def test_only_our_hook_is_removed(self):
        settings = cw.with_hook({"hooks": {"Notification": [THEIRS], "Stop": [THEIRS]}}, OURS)
        result = cw.without_hook(settings)
        self.assertEqual((commands(result), commands(result, "Stop")), (["say done"], ["say done"]))

    def test_a_hook_sharing_our_group_survives(self):
        shared = {"hooks": {"Notification": [{"matcher": "", "hooks": [
            {"type": "command", "command": OURS}, {"type": "command", "command": "say done"}]}]}}
        self.assertEqual(commands(cw.without_hook(shared)), ["say done"])

    def test_nothing_is_left_behind_when_ours_was_the_only_hook(self):
        self.assertEqual(cw.without_hook(cw.with_hook({"model": "opus"}, OURS)), {"model": "opus"})

    def test_removing_when_absent_changes_nothing(self):
        settings = {"hooks": {"Notification": [THEIRS]}}
        self.assertEqual(cw.without_hook(settings), settings)


class UpdateSettingsFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "settings.json")

    def write(self, text):
        with open(self.path, "w") as f:
            f.write(text)

    def test_change_is_written_and_the_original_is_backed_up(self):
        self.write('{"model": "opus"}')
        self.assertEqual(cw.update_settings_file(self.path, lambda s: cw.with_hook(s, OURS)), "updated")
        with open(self.path) as f:
            self.assertEqual(commands(json.load(f)), [OURS])
        with open(self.path + cw.BACKUP_SUFFIX) as f:
            self.assertEqual(f.read(), '{"model": "opus"}')

    def test_no_change_means_no_write_and_no_backup(self):
        self.write('{"model": "opus"}')
        self.assertEqual(cw.update_settings_file(self.path, lambda s: s), "unchanged")
        self.assertFalse(os.path.exists(self.path + cw.BACKUP_SUFFIX))

    def test_invalid_json_is_left_alone(self):
        self.write('{"model": "opus",}')
        self.assertEqual(cw.update_settings_file(self.path, lambda s: cw.with_hook(s, OURS)), "invalid")
        with open(self.path) as f:
            self.assertEqual(f.read(), '{"model": "opus",}')

    def test_missing_settings_file_is_created(self):
        self.assertEqual(cw.update_settings_file(self.path, lambda s: cw.with_hook(s, OURS)), "updated")
        with open(self.path) as f:
            self.assertEqual(commands(json.load(f)), [OURS])

    def test_non_ascii_text_survives_unescaped(self):
        self.write('{"note": "café — ok"}')
        cw.update_settings_file(self.path, lambda s: cw.with_hook(s, OURS))
        with open(self.path, encoding="utf-8") as f:
            self.assertIn("café — ok", f.read())


if __name__ == "__main__":
    unittest.main()
