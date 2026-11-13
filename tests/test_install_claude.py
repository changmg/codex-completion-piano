import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
QUESTION_MATCHER = "AskUserQuestion|ExitPlanMode"
CHIME = {"hooks": [{"type": "command", "async": True,
                    "command": "powershell.exe -c \"(New-Object Media.SoundPlayer 'C:\\Windows\\Media\\chime.wav').PlaySync()\""}]}
GUARD = {"matcher": "Bash", "hooks": [{"type": "command", "command": "~/.claude/hooks/guard.sh"}]}


def snapshot(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()} if directory.exists() else {}


def load_installer():
    spec = importlib.util.spec_from_file_location("install_claude", ROOT / "install-claude.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClaudeInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="piano-claude-install-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.claude = self.root / "Claude home ' $ with spaces"
        self.codex = self.root / "Codex home ' $ with spaces"
        self.installer = load_installer()
        # Tests control the administrator policy instead of reading /etc.
        self.managed = self.root / "managed-settings.json"
        patcher = mock.patch.object(self.installer, "MANAGED_SETTINGS", self.managed)
        patcher.start()
        self.addCleanup(patcher.stop)

    def codex_piano(self):
        (self.codex / "completion-piano").mkdir(parents=True)
        shutil.copy2(ROOT / "notify-rhythm.py", self.codex / "notify-rhythm.py")

    def write_settings(self, value, mode=None):
        self.claude.mkdir(parents=True, exist_ok=True)
        path = self.claude / "settings.json"
        path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2) + "\n", encoding="utf-8")
        if mode is not None:
            path.chmod(mode)

    def settings(self):
        return json.loads((self.claude / "settings.json").read_text(encoding="utf-8"))

    def configure(self):
        return self.installer.install(self.installer.prepare(self.claude, self.codex))

    def run_main(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": ""}):
            code = self.installer.main(["--claude-dir", str(self.claude), "--codex-dir", str(self.codex)] + list(arguments))
        return code, output.getvalue()

    def is_piano(self, handler):
        return str(self.claude / "notify-claude.py") in shlex.split(handler.get("command", ""))

    def assert_piano_hooks(self, hooks, notifier):
        """Each trigger has exactly one piano handler that names this notifier."""
        expected = [sys.executable, str(self.claude / "notify-claude.py"), str(notifier)]
        found = {}
        for event, groups in hooks.items():
            for group in groups:
                for handler in group["hooks"]:
                    if self.is_piano(handler):
                        self.assertNotIn(event, found, "duplicate piano hook in " + event)
                        found[event] = (group.get("matcher"), handler)
        self.assertEqual(set(found), {"Stop", "StopFailure", "PreToolUse"})
        self.assertEqual(found["PreToolUse"][0], QUESTION_MATCHER)
        self.assertIsNone(found["Stop"][0])
        self.assertIsNone(found["StopFailure"][0])
        for _, handler in found.values():
            self.assertEqual(handler["type"], "command")
            self.assertEqual(handler["timeout"], 5)
            self.assertNotIn("async", handler)
            self.assertEqual(shlex.split(handler["command"]), expected)

    def fire(self, event_name, **fields):
        """Run the installed Stop/question command through a shell, as Claude Code does."""
        handler = next(handler for group in self.settings()["hooks"][event_name]
                       for handler in group["hooks"] if self.is_piano(handler))
        env = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_ENTRYPOINT"}
        event = dict({"hook_event_name": event_name, "cwd": "/work/project"}, **fields)
        return subprocess.run(["/bin/sh", "-c", handler["command"]], input=json.dumps(event),
                              capture_output=True, text=True, env=env, cwd=str(self.root))

    def test_shared_player_adds_hooks_without_touching_codex_or_windows(self):
        self.codex_piano()
        (self.codex / "completion-piano" / "last-completion.json").write_text('{"id":"old"}\n')
        codex_before = snapshot(self.codex)
        vsix, backup = self.configure()
        self.assertIsNone(vsix)
        self.assertIsNone(backup)
        self.assert_piano_hooks(self.settings()["hooks"], self.codex / "notify-rhythm.py")
        self.assertEqual(stat.S_IMODE((self.claude / "settings.json").stat().st_mode), 0o600)
        helper = self.claude / "notify-claude.py"
        self.assertEqual(helper.read_bytes(), (ROOT / "notify-claude.py").read_bytes())
        self.assertEqual(stat.S_IMODE(helper.stat().st_mode), 0o755)
        self.assertFalse((self.claude / "notify-rhythm.py").exists())
        self.assertFalse((self.claude / "completion-piano").exists())
        self.assertEqual(snapshot(self.codex), codex_before)
        # The configured command reaches the Codex player's marker.
        result = self.fire("Stop")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        marker = json.loads((self.codex / "completion-piano" / "last-completion.json").read_text())
        self.assertEqual((marker["type"], marker["cwd"]), ("agent-turn-complete", "/work/project"))
        self.assertNotEqual(marker["id"], "old")

    def test_standalone_player_builds_vsix_for_the_claude_marker(self):
        self.codex.mkdir()
        (self.codex / "config.toml").write_text('model = "unrelated"\n')
        codex_before = snapshot(self.codex)
        vsix, _ = self.configure()
        marker_path = self.claude / "completion-piano" / "last-completion.json"
        self.assertEqual(vsix, self.claude / "completion-piano" / "codex-completion-piano-0.1.4.vsix")
        with zipfile.ZipFile(vsix) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn(json.dumps(str(marker_path)), archive.read("extension/extension.js").decode())
            self.assertEqual(archive.read("extension/piano.wav"), (ROOT / "completion-piano/extension/piano.wav").read_bytes())
        self.assertEqual(stat.S_IMODE((self.claude / "notify-rhythm.py").stat().st_mode), 0o755)
        self.assert_piano_hooks(self.settings()["hooks"], self.claude / "notify-rhythm.py")
        self.assertEqual(snapshot(self.codex), codex_before)
        result = self.fire("PreToolUse", tool_name="AskUserQuestion", tool_input={"questions": []})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(marker_path.read_text())["cwd"], "/work/project")

    def test_reinstall_is_idempotent_and_keeps_the_last_marker(self):
        self.configure()
        marker = self.claude / "completion-piano" / "last-completion.json"
        marker.write_text('{"id":"last-marker"}\n')
        before = (self.claude / "settings.json").read_bytes()
        plan = self.installer.prepare(self.claude, self.codex)
        self.assertIsNone(plan.settings)
        _, backup = self.installer.install(plan)
        self.assertTrue(backup.is_dir())
        self.assertEqual((self.claude / "settings.json").read_bytes(), before)
        self.assertEqual(marker.read_text(), '{"id":"last-marker"}\n')

    def test_rerun_after_installing_the_codex_piano_moves_hooks_in_place(self):
        self.write_settings({"hooks": {"PreToolUse": [copy.deepcopy(GUARD)]}})
        self.configure()
        self.codex_piano()
        self.configure()
        hooks = self.settings()["hooks"]
        self.assert_piano_hooks(hooks, self.codex / "notify-rhythm.py")
        self.assertEqual(hooks["PreToolUse"][0], GUARD)
        self.assertEqual(hooks["PreToolUse"][1]["matcher"], QUESTION_MATCHER)

    def test_unrelated_settings_hooks_order_and_mode_are_preserved(self):
        self.codex_piano()
        original = {"model": "opus", "permissions": {"allow": ["Bash(ls)"]},
                    "hooks": {"Stop": [copy.deepcopy(CHIME)], "PreToolUse": [copy.deepcopy(GUARD)]},
                    "env": {"GREETING": "你好"}, "theme": "dark"}
        self.write_settings(original, mode=0o644)
        original_bytes = (self.claude / "settings.json").read_bytes()
        code, output = self.run_main()
        self.assertEqual(code, 0)
        text = (self.claude / "settings.json").read_text(encoding="utf-8")
        result = json.loads(text)
        self.assertEqual(list(result), ["model", "permissions", "hooks", "env", "theme"])
        for key in ("model", "permissions", "env", "theme"):
            self.assertEqual(result[key], original[key])
        self.assertEqual(result["hooks"]["Stop"][0], CHIME)
        self.assertEqual(result["hooks"]["PreToolUse"][0], GUARD)
        self.assert_piano_hooks(result["hooks"], self.codex / "notify-rhythm.py")
        self.assertIn('\n  "model": "opus",', text)
        self.assertIn("你好", text)
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(stat.S_IMODE((self.claude / "settings.json").stat().st_mode), 0o644)
        backups = list(self.claude.glob("completion-piano-backup-*/settings.json"))
        self.assertEqual([path.read_bytes() for path in backups], [original_bytes])
        self.assertIn("chime.wav", output)
        self.assertIn("also play a sound", output)

    def test_duplicates_and_stale_piano_hooks_are_consolidated(self):
        self.codex_piano()
        old = {"type": "command", "command": shlex.join(
            ["python3", str(self.claude / "notify-claude.py"), "/old/notify-rhythm.py"])}
        other = {"type": "command", "command": "echo other"}
        self.write_settings({"hooks": {
            "Stop": [{"hooks": [copy.deepcopy(old)]}, {"hooks": [copy.deepcopy(other), copy.deepcopy(old)]}],
            "PreToolUse": [{"matcher": "AskUserQuestion", "hooks": [copy.deepcopy(old)]}],
            "Notification": [{"matcher": "permission_prompt", "hooks": [copy.deepcopy(old)]}],
        }})
        self.configure()
        hooks = self.settings()["hooks"]
        self.assert_piano_hooks(hooks, self.codex / "notify-rhythm.py")
        self.assertEqual(hooks["Stop"][1], {"hooks": [other]})
        self.assertEqual(len(hooks["Stop"]), 2)
        self.assertEqual(len(hooks["PreToolUse"]), 1)
        self.assertEqual(hooks["Notification"], [])
        self.assertIsNone(self.installer.prepare(self.claude, self.codex).settings)

    def test_malformed_settings_never_change_the_destination(self):
        cases = ["{not json", "[]", '{"hooks": []}', '{"hooks": {"Stop": {}}}',
                 '{"hooks": {"Stop": [{"matcher": "x"}]}}', '{"hooks": {"Stop": [{"hooks": ["echo"]}]}}',
                 '{"hooks": {"Stop": ["echo"]}}']
        for text in cases:
            with self.subTest(text=text):
                if self.claude.exists():
                    shutil.rmtree(self.claude)
                self.write_settings(text)
                before = snapshot(self.claude)
                with self.assertRaises(ValueError):
                    self.configure()
                self.assertEqual(snapshot(self.claude), before)

    def test_disabled_or_managed_only_hooks_stop_installation(self):
        self.write_settings({"disableAllHooks": True})
        before = snapshot(self.claude)
        with self.assertRaisesRegex(ValueError, "disableAllHooks"):
            self.configure()
        self.assertEqual(snapshot(self.claude), before)
        self.write_settings({})
        before = snapshot(self.claude)
        for policy in ({"allowManagedHooksOnly": True}, {"disableAllHooks": True}):
            with self.subTest(policy=policy):
                self.managed.write_text(json.dumps(policy))
                with self.assertRaisesRegex(ValueError, "managed"):
                    self.configure()
                self.assertEqual(snapshot(self.claude), before)
        self.managed.write_text(json.dumps({"allowManagedHooksOnly": False}))
        self.assertIsNotNone(self.installer.prepare(self.claude, self.codex).settings)

    def test_symlinks_and_repository_destinations_are_rejected(self):
        self.claude.mkdir()
        linked = self.root / "dotfiles-settings.json"
        linked.write_text("{}\n")
        (self.claude / "settings.json").symlink_to(linked)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.installer.prepare(self.claude, self.codex)
        self.assertEqual(linked.read_text(), "{}\n")
        (self.claude / "settings.json").unlink()
        (self.claude / "notify-claude.py").symlink_to(linked)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.installer.prepare(self.claude, self.codex)
        with self.assertRaisesRegex(ValueError, "outside"):
            self.installer.prepare(ROOT / "nested-claude", self.codex)

    def test_dry_run_writes_nothing_including_bytecode(self):
        self.codex_piano()
        before = snapshot(ROOT)
        codex_before = snapshot(self.codex)
        for codex in (self.codex, self.root / "no codex"):
            with self.subTest(codex=codex):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "install-claude.py"), "--dry-run",
                     "--claude-dir", str(self.claude), "--codex-dir", str(codex)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env=dict(os.environ, WSL_DISTRO_NAME=""))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("nothing written", result.stdout)
                self.assertFalse(self.claude.exists())
        self.assertEqual(snapshot(ROOT), before)
        self.assertEqual(snapshot(self.codex), codex_before)

    def test_guidance_matches_the_player_mode_and_its_test_command_works(self):
        self.codex_piano()
        code, shared = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("Nothing to install on Windows", shared)
        self.assertNotIn(".vsix", shared)
        # Run the printed test command exactly as a user would paste it.
        command = next(line.strip() for line in shared.splitlines() if "notify-claude.py" in line and "|" in line)
        result = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True,
                                env={key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_ENTRYPOINT"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.codex / "completion-piano" / "last-completion.json").is_file())

        shutil.rmtree(self.claude)
        shutil.rmtree(self.codex)
        windows = "\\\\wsl.localhost\\Test-Distro\\Claude home\\completion-piano\\piano.vsix"
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Test-Distro"}), \
                mock.patch.object(self.installer.codex.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0, windows + "\n")):
            self.assertEqual(self.installer.main(["--claude-dir", str(self.claude), "--codex-dir", str(self.codex)]), 0)
        standalone = output.getvalue()
        self.assertIn("VSIX (Windows): " + windows, standalone)
        self.assertIn("Install from VSIX", standalone)
        self.assertIn("Test-Distro", standalone)

    def test_failed_replacement_restores_the_previous_installation(self):
        self.write_settings({"model": "previous"})
        (self.claude / "notify-claude.py").write_text("# old hook\n")
        (self.claude / "notify-rhythm.py").write_text("# old notifier\n")
        (self.claude / "completion-piano").mkdir()
        (self.claude / "completion-piano" / "old.txt").write_text("previous player\n")
        before = snapshot(self.claude)
        plan = self.installer.prepare(self.claude, self.codex)
        original_replace = Path.replace
        failed = []

        def fail_once(source, target):
            if source.name == "settings.json" and source.parent.name.startswith(".completion-piano-install-") \
                    and not failed:
                failed.append(True)
                raise OSError("simulated disk failure")
            return original_replace(source, target)

        with mock.patch.object(Path, "replace", autospec=True, side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "simulated disk failure"):
                self.installer.install(plan)
        self.assertTrue(failed)
        self.assertEqual(snapshot(self.claude), before)
        self.assertFalse(list(self.claude.glob(".completion-piano-install-*")))

    def test_settings_edited_during_installation_are_not_overwritten(self):
        self.codex_piano()
        self.write_settings({"model": "before"})
        plan = self.installer.prepare(self.claude, self.codex)
        self.write_settings({"model": "edited while installing"})
        with self.assertRaisesRegex(ValueError, "changed during"):
            self.installer.install(plan)
        self.assertEqual(self.settings(), {"model": "edited while installing"})
        self.assertFalse((self.claude / "notify-claude.py").exists())

    def test_claude_directory_selection(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "environment")}, clear=True):
            self.assertEqual(self.installer.resolve_claude_dir(), self.root / "environment")
            self.assertEqual(self.installer.resolve_claude_dir(self.claude), self.claude)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(Path, "home", return_value=self.root):
            self.assertEqual(self.installer.resolve_claude_dir(), self.root / ".claude")

# Records each notifier call so the adapter's arguments can be checked exactly.
RECORDER = """import json
import sys
from pathlib import Path
with open(Path(__file__).with_name("calls.jsonl"), "a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
"""

PRIVATE = "private prompt, answer, or plan text"
TRIGGERS = [
    {"hook_event_name": "Stop", "cwd": "/work/项目", "stop_hook_active": False,
     "last_assistant_message": PRIVATE, "background_tasks": [], "session_crons": []},
    {"hook_event_name": "StopFailure", "cwd": "/work/项目", "error": "rate_limit",
     "last_assistant_message": PRIVATE},
    {"hook_event_name": "PreToolUse", "cwd": "/work/项目", "tool_name": "AskUserQuestion",
     "tool_input": {"questions": [{"question": PRIVATE, "header": "Choice", "multiSelect": False,
                                   "options": [{"label": "A", "description": PRIVATE}]}]}},
    {"hook_event_name": "PreToolUse", "cwd": "/work/项目", "tool_name": "ExitPlanMode",
     "tool_input": {"plan": PRIVATE, "planFilePath": "/home/user/.claude/plans/plan.md"}},
]


class ClaudeHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="piano-claude-hook-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # As in shared mode, the hook and the notifier live in different homes.
        self.hook = self.root / "Claude home ' $" / "notify-claude.py"
        self.hook.parent.mkdir()
        shutil.copy2(ROOT / "notify-claude.py", self.hook)
        self.player = self.root / "Codex home ' $"
        self.player.mkdir()
        shutil.copy2(ROOT / "notify-rhythm.py", self.player / "notify-rhythm.py")
        self.recorder = self.root / "recorder" / "notify-rhythm.py"
        self.recorder.parent.mkdir()
        self.recorder.write_text(RECORDER, encoding="utf-8")

    @property
    def marker(self):
        return self.player / "completion-piano/last-completion.json"

    def run_hook(self, event, notifier=None, entrypoint="cli", arguments=None):
        env = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_ENTRYPOINT"}
        if entrypoint is not None:
            env["CLAUDE_CODE_ENTRYPOINT"] = entrypoint
        if arguments is None:
            arguments = [str(notifier or self.player / "notify-rhythm.py")]
        payload = event if isinstance(event, str) else json.dumps(event)
        return subprocess.run([sys.executable, str(self.hook)] + arguments, input=payload,
                              capture_output=True, text=True, cwd=str(self.root), env=env)

    def recorded_calls(self):
        calls = self.recorder.with_name("calls.jsonl")
        if not calls.exists():
            return []
        return [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]

    def assert_silent(self, result):
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def signals(self):
        calls = self.recorded_calls()
        self.assertTrue(all(len(call) == 1 for call in calls), calls)
        return [json.loads(call[0]) for call in calls]

    def test_triggers_send_only_completion_type_and_cwd_to_the_notifier(self):
        for event in TRIGGERS:
            with self.subTest(event=event["hook_event_name"], tool=event.get("tool_name")):
                self.assert_silent(self.run_hook(event, notifier=self.recorder))
        expected = {"type": "agent-turn-complete", "cwd": "/work/项目"}
        self.assertEqual(self.signals(), [expected] * len(TRIGGERS))

    def test_missing_cwd_falls_back_to_the_hook_directory(self):
        self.assert_silent(self.run_hook({"hook_event_name": "Stop"}, notifier=self.recorder))
        self.assertEqual(self.signals(), [{"type": "agent-turn-complete", "cwd": str(self.root)}])

    def test_each_trigger_writes_a_fresh_marker_for_the_player(self):
        identifiers = set()
        for event in TRIGGERS:
            self.assert_silent(self.run_hook(event))
            marker = json.loads(self.marker.read_text(encoding="utf-8"))
            self.assertEqual(set(marker), {"id", "type", "cwd"})
            self.assertEqual((marker["type"], marker["cwd"]), ("agent-turn-complete", "/work/项目"))
            identifiers.add(marker["id"])
        self.assertEqual(len(identifiers), len(TRIGGERS))
        self.assertNotIn(PRIVATE, self.marker.read_text(encoding="utf-8"))

    def test_other_events_tools_and_malformed_input_are_ignored(self):
        ignored = [
            {"hook_event_name": "PreToolUse", "cwd": "/p", "tool_name": "Bash", "tool_input": {"command": "ls"}},
            {"hook_event_name": "PostToolUse", "cwd": "/p", "tool_name": "AskUserQuestion"},
            {"hook_event_name": "PermissionRequest", "cwd": "/p", "tool_name": "ExitPlanMode"},
            {"hook_event_name": "PreToolUse", "cwd": "/p", "tool_name": "mcp__x__AskUserQuestion"},
            {"hook_event_name": "PreToolUse", "cwd": "/p", "tool_name": ["AskUserQuestion"]},
            {"hook_event_name": "SubagentStop", "cwd": "/p", "last_assistant_message": PRIVATE},
            {"hook_event_name": "Notification", "cwd": "/p", "notification_type": "permission_prompt"},
            {"hook_event_name": "Notification", "cwd": "/p", "notification_type": "idle_prompt"},
            {"hook_event_name": "UserPromptSubmit", "cwd": "/p", "prompt": PRIVATE},
            {"hook_event_name": "SessionEnd", "cwd": "/p"},
            "bad json", "[]", "null", "{}", '{"hook_event_name": ["Stop"]}',
        ]
        for event in ignored:
            with self.subTest(event=event):
                self.assert_silent(self.run_hook(event, notifier=self.recorder))
        self.assertEqual(self.recorded_calls(), [])

    def test_headless_and_sdk_runs_are_silent_but_interactive_surfaces_play(self):
        for entrypoint in ("sdk-cli", "sdk-ts", "sdk-py"):
            with self.subTest(entrypoint=entrypoint):
                self.assert_silent(self.run_hook(TRIGGERS[0], notifier=self.recorder, entrypoint=entrypoint))
        self.assertEqual(self.recorded_calls(), [])
        for entrypoint in ("cli", "claude-vscode", None):
            with self.subTest(entrypoint=entrypoint):
                self.assert_silent(self.run_hook(TRIGGERS[0], notifier=self.recorder, entrypoint=entrypoint))
        self.assertEqual(len(self.recorded_calls()), 3)

    def test_notifier_failures_and_missing_arguments_stay_silent(self):
        self.assert_silent(self.run_hook(TRIGGERS[0], notifier=self.root / "missing" / "notify-rhythm.py"))
        (self.player / "completion-piano").write_text("not a directory", encoding="utf-8")
        self.assert_silent(self.run_hook(TRIGGERS[0]))
        self.assert_silent(self.run_hook(TRIGGERS[0], arguments=[]))
        self.assert_silent(self.run_hook(TRIGGERS[0], arguments=[str(self.recorder), "extra"]))
        self.assertEqual(self.recorded_calls(), [])

    def test_notifier_timeout_does_not_block_claude(self):
        spec = importlib.util.spec_from_file_location("claude_hook", self.hook)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        data = json.dumps(TRIGGERS[2])
        with mock.patch.object(sys, "argv", [str(self.hook), str(self.recorder)]), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "cli"}), \
                mock.patch.object(sys, "stdin", io.StringIO(data)), \
                mock.patch.object(hook.subprocess, "run", side_effect=subprocess.TimeoutExpired("notifier", 3)) as run:
            self.assertEqual(hook.main(), 0)
        # The notifier wait must end before Claude Code's 5-second hook timeout.
        self.assertLess(run.call_args.kwargs["timeout"], 5)


if __name__ == "__main__":
    unittest.main()
