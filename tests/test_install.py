import copy
import hashlib
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
import wave
import zipfile
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install
import tomlkit


def snapshot(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()} if directory.exists() else {}


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="piano-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "Codex home ' $ with spaces"

    def write(self, name, content):
        target = self.directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def configure(self):
        return install.install(install.prepare(self.directory))

    def read_config(self):
        return tomlkit.parse((self.directory / "config.toml").read_text()).unwrap()

    def test_fresh_install_builds_complete_local_windows_vsix(self):
        vsix, backup = self.configure()
        self.assertIsNone(backup)
        config = self.read_config()
        self.assertEqual(config["notify"], [sys.executable, str(self.directory / "notify-rhythm.py")])
        self.assertTrue(config["features"]["hooks"])
        hooks = json.loads((self.directory / "hooks.json").read_text())
        handler = hooks["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(shlex.split(handler["command"]), [sys.executable, str(self.directory / "notify-question.py")])
        self.assertEqual(stat.S_IMODE((self.directory / "config.toml").stat().st_mode), 0o600)
        with zipfile.ZipFile(vsix) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(set(archive.namelist()), {
                "[Content_Types].xml", "extension.vsixmanifest", "extension/package.json",
                "extension/extension.js", "extension/piano.wav", "extension/README.txt", "extension/LICENSE.txt",
            })
            script = archive.read("extension/extension.js").decode()
            self.assertIn(json.dumps(str(self.directory / "completion-piano/last-completion.json")), script)
            self.assertNotIn("const EVENT_FILE = null", script)
            metadata = json.loads(archive.read("extension/package.json"))
            self.assertEqual(metadata["extensionKind"], ["ui"])
            self.assertEqual(metadata["version"], "0.1.4")
            self.assertEqual(metadata["publisher"] + "." + metadata["name"], "local-tools.codex-completion-piano")
            self.assertEqual(archive.read("extension/piano.wav"), (ROOT / "completion-piano/extension/piano.wav").read_bytes())
            manifest = ET.fromstring(archive.read("extension.vsixmanifest"))
            ns = {"v": "http://schemas.microsoft.com/developer/vsx-schema/2011"}
            self.assertEqual(manifest.find("v:Metadata/v:Identity", ns).get("Version"), metadata["version"])
            ET.fromstring(archive.read("[Content_Types].xml"))

    def test_reinstall_is_idempotent_and_preserves_last_marker(self):
        self.configure()
        marker = self.directory / "completion-piano/last-completion.json"
        marker.write_text('{"id":"last-marker"}\n')
        old_config = (self.directory / "config.toml").read_bytes()
        old_hooks = (self.directory / "hooks.json").read_bytes()
        plan = install.prepare(self.directory)
        self.assertEqual(plan.updates, {})
        _, backup = install.install(plan)
        self.assertTrue(backup.is_dir())
        self.assertEqual((self.directory / "config.toml").read_bytes(), old_config)
        self.assertEqual((self.directory / "hooks.json").read_bytes(), old_hooks)
        self.assertEqual(marker.read_text(), '{"id":"last-marker"}\n')
        self.assertEqual(len(json.loads(old_hooks)["hooks"]["PreToolUse"]), 1)

    def test_upgrade_preserves_comments_modes_settings_and_trust(self):
        self.write("config.toml", '# user comment\nmodel = "example" # keep model\n'
                   'notify = ["python3", ' + json.dumps(str(self.directory / "notify-rhythm.py")) + '] # piano\n'
                   '[features]\ncodex_hooks = false # old alias\nmemories = false # keep feature\n'
                   '[projects."/work/project"]\ntrust_level = "trusted"\n'
                   '[hooks.state."example-hook"]\ntrusted_hash = "keep-hash"\nenabled = true\n')
        self.write("notify-rhythm.py", "# previous helper\n")
        (self.directory / "config.toml").chmod(0o640)
        before = (self.directory / "config.toml").read_bytes()
        _, backup = self.configure()
        self.assertEqual((backup / "config.toml").read_bytes(), before)
        self.assertEqual((backup / "notify-rhythm.py").read_text(), "# previous helper\n")
        text = (self.directory / "config.toml").read_text()
        for comment in ("# user comment", "# keep model", "# keep feature", "# piano"):
            self.assertIn(comment, text)
        config = self.read_config()
        self.assertEqual(config["hooks"]["state"]["example-hook"]["trusted_hash"], "keep-hash")
        self.assertEqual(config["projects"]["/work/project"]["trust_level"], "trusted")
        self.assertNotIn("codex_hooks", config["features"])
        self.assertFalse(config["features"]["memories"])
        self.assertTrue(config["features"]["hooks"])
        self.assertEqual(stat.S_IMODE((self.directory / "config.toml").stat().st_mode), 0o640)

    def test_unrelated_notifier_requires_explicit_replacement(self):
        self.write("config.toml", 'notify = ["other-notifier", "--argument"]\n')
        before = snapshot(self.directory)
        with self.assertRaisesRegex(ValueError, "--replace-notify"):
            install.prepare(self.directory)
        self.assertEqual(snapshot(self.directory), before)
        _, backup = install.install(install.prepare(self.directory, replace_notify=True))
        self.assertEqual((backup / "config.toml").read_bytes(), before["config.toml"])

    def test_same_basename_on_another_path_is_unrelated(self):
        self.write("config.toml", 'notify = ["python3", "/another/notify-rhythm.py"]\n')
        with self.assertRaisesRegex(ValueError, "--replace-notify"):
            install.prepare(self.directory)

    def test_json_merge_preserves_unrelated_hooks_and_order(self):
        other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo existing"}]}
        existing = {"description": "keep this", "hooks": {"PreToolUse": [other], "Stop": [copy.deepcopy(other)]}}
        self.write("hooks.json", json.dumps(existing))
        self.configure()
        result = json.loads((self.directory / "hooks.json").read_text())
        self.assertEqual(result["description"], existing["description"])
        self.assertEqual(result["hooks"]["PreToolUse"][0], other)
        self.assertEqual(result["hooks"]["Stop"], existing["hooks"]["Stop"])
        self.assertEqual(len(result["hooks"]["PreToolUse"]), 2)

    def test_inline_toml_merge_preserves_existing_hook_comments(self):
        self.write("config.toml", '# keep\n[[hooks.PreToolUse]]\nmatcher = "Bash" # matcher comment\n'
                   '[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = "echo existing" # command comment\n')
        self.configure()
        self.assertFalse((self.directory / "hooks.json").exists())
        config = self.read_config()
        self.assertEqual(config["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "echo existing")
        self.assertEqual(config["hooks"]["PreToolUse"][1]["matcher"], install.QUESTION_MATCHER)
        text = (self.directory / "config.toml").read_text()
        self.assertIn("# matcher comment", text)
        self.assertIn("# command comment", text)
        self.assertEqual(install.prepare(self.directory).updates, {})

    def test_duplicates_across_inline_and_json_are_consolidated(self):
        command = shlex.join(["python3", str(self.directory / "notify-question.py")])
        group = {"matcher": install.QUESTION_MATCHER, "hooks": [{"type": "command", "command": command}]}
        self.write("hooks.json", json.dumps({"hooks": {"PreToolUse": [copy.deepcopy(group), copy.deepcopy(group)]}}))
        document = tomlkit.document()
        document["hooks"] = {"PreToolUse": [copy.deepcopy(group)]}
        self.write("config.toml", tomlkit.dumps(document))
        self.configure()
        json_groups = json.loads((self.directory / "hooks.json").read_text())["hooks"]["PreToolUse"]
        self.assertEqual(len(json_groups), 1)
        self.assertEqual(self.read_config().get("hooks", {}).get("PreToolUse", []), [])
        self.assertEqual(install.prepare(self.directory).updates, {})

    def test_existing_inline_piano_hook_is_reused_when_json_also_exists(self):
        document = tomlkit.document()
        document["hooks"] = {"PreToolUse": [{"matcher": install.QUESTION_MATCHER, "hooks": [
            {"type": "command", "command": shlex.join(["python3", str(self.directory / "notify-question.py")])},
        ]}]}
        self.write("config.toml", tomlkit.dumps(document))
        self.write("hooks.json", '{"hooks":{"Stop":[]}}\n')
        self.configure()
        self.assertEqual((self.directory / "hooks.json").read_text(), '{"hooks":{"Stop":[]}}\n')
        self.assertEqual(len(self.read_config()["hooks"]["PreToolUse"]), 1)

    def test_malformed_configuration_never_changes_destination(self):
        cases = [("config.toml", 'model = [\n'), ("hooks.json", '{"hooks":'),
                 ("hooks.json", '[]'), ("hooks.json", '{"hooks":{"PreToolUse":{}}}'),
                 ("config.toml", 'features = "invalid"\n')]
        for name, text in cases:
            with self.subTest(name=name, text=text):
                if self.directory.exists():
                    shutil.rmtree(self.directory)
                self.write(name, text)
                before = snapshot(self.directory)
                with self.assertRaises(ValueError):
                    install.prepare(self.directory)
                self.assertEqual(snapshot(self.directory), before)

    def test_managed_only_hooks_are_reported_without_overriding_policy(self):
        self.write("config.toml", "allow_managed_hooks_only = true\n")
        before = snapshot(self.directory)
        with self.assertRaisesRegex(ValueError, "allow_managed_hooks_only"):
            install.prepare(self.directory)
        self.assertEqual(snapshot(self.directory), before)

    def test_codex_directory_selection(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "environment")}, clear=True):
            self.assertEqual(install.resolve_codex_dir(), self.root / "environment")
            self.assertEqual(install.resolve_codex_dir(self.directory), self.directory)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(Path, "home", return_value=self.root):
            self.assertEqual(install.resolve_codex_dir(), self.root / ".codex")

    def test_dry_run_does_not_create_destination_or_bytecode(self):
        before = snapshot(ROOT)
        result = subprocess.run([sys.executable, str(ROOT / "install.py"), "--dry-run", "--codex-dir", str(self.directory)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing written", result.stdout)
        self.assertFalse(self.directory.exists())
        self.assertEqual(snapshot(ROOT), before)

    def test_wsl_dry_run_shows_windows_vsix_path_without_writing(self):
        vsix = self.directory / "completion-piano/codex-completion-piano-0.1.4.vsix"
        windows_vsix = "\\\\wsl.localhost\\Test-Distro\\Codex home ' $ with spaces\\completion-piano\\piano.vsix"
        before = snapshot(ROOT)
        with mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Test-Distro"}), mock.patch.object(
                install.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, windows_vsix + "\n")) as convert, \
                mock.patch.object(sys, "stdout", io.StringIO()) as output:
            self.assertEqual(install.main(["--dry-run", "--codex-dir", str(self.directory)]), 0)
        text = output.getvalue()
        self.assertIn("Player connection: WSL (Test-Distro)", text)
        self.assertIn("VSIX (Linux): {}".format(vsix), text)
        self.assertIn("VSIX (Windows): {}".format(windows_vsix), text)
        self.assertIn("WSL: Connect to WSL using Distro...", text)
        self.assertIn("nothing written", text)
        self.assertNotIn("Remote SSH", text)
        self.assertNotIn("Download", text)
        convert.assert_called_once_with(
            ["wslpath", "-w", str(vsix)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, check=True, timeout=5,
        )
        self.assertFalse(self.directory.exists())
        self.assertEqual(snapshot(ROOT), before)

    def test_wsl_guidance_falls_back_when_windows_path_conversion_is_unavailable(self):
        outcomes = [FileNotFoundError("wslpath"), subprocess.CalledProcessError(1, "wslpath"),
                    subprocess.TimeoutExpired("wslpath", 5), None]
        for outcome in outcomes:
            with self.subTest(outcome=outcome), mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Test-Distro"}), \
                    mock.patch.object(install.subprocess, "run") as convert, \
                    mock.patch.object(sys, "stdout", io.StringIO()) as output:
                if outcome is None:
                    convert.return_value = subprocess.CompletedProcess([], 0, "\n")
                else:
                    convert.side_effect = outcome
                self.assertEqual(install.main(["--dry-run", "--codex-dir", str(self.directory)]), 0)
                text = output.getvalue()
                self.assertIn(str(self.directory / "completion-piano/codex-completion-piano-0.1.4.vsix"), text)
                self.assertIn("Windows path conversion unavailable", text)
                self.assertIn("\\\\wsl.localhost\\Test-Distro", text)
                self.assertIn("Browse from the distribution root", text)
                self.assertIn("Browse to the VSIX through the WSL share", text)
                self.assertNotIn("VSIX (Windows):", text)
                self.assertFalse(self.directory.exists())

    def test_non_wsl_guidance_keeps_ssh_flow_without_path_conversion(self):
        with mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": ""}), \
                mock.patch.object(install.subprocess, "run") as convert, \
                mock.patch.object(sys, "stdout", io.StringIO()) as output:
            self.assertEqual(install.main(["--dry-run", "--codex-dir", str(self.directory)]), 0)
        text = output.getvalue()
        self.assertIn("Player connection: Remote SSH", text)
        self.assertIn("Download the VSIX", text)
        self.assertIn("scp", text)
        self.assertIn("reconnect with Remote SSH", text)
        self.assertNotIn("WSL:", text)
        convert.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_wsl_install_keeps_linux_configuration_and_embedded_marker_path(self):
        windows_vsix = "\\\\wsl.localhost\\Test-Distro\\tmp\\piano.vsix"
        with mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Test-Distro"}), mock.patch.object(
                install.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, windows_vsix)), \
                mock.patch.object(sys, "stdout", io.StringIO()) as output:
            self.assertEqual(install.main(["--codex-dir", str(self.directory)]), 0)
        text = output.getvalue()
        self.assertIn("Installed.", text)
        self.assertIn("VSIX (Windows): {}".format(windows_vsix), text)
        config = self.read_config()
        self.assertEqual(config["notify"], [sys.executable, str(self.directory / "notify-rhythm.py")])
        hooks = json.loads((self.directory / "hooks.json").read_text())
        command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertEqual(shlex.split(command), [sys.executable, str(self.directory / "notify-question.py")])
        vsix = self.directory / "completion-piano/codex-completion-piano-0.1.4.vsix"
        with zipfile.ZipFile(vsix) as archive:
            script = archive.read("extension/extension.js").decode()
            self.assertIn(json.dumps(str(self.directory / "completion-piano/last-completion.json")), script)
            self.assertNotIn(windows_vsix, script)

    def test_symlinks_and_installing_inside_repository_are_rejected(self):
        self.directory.mkdir()
        linked = self.root / "dotfiles.toml"
        linked.write_text('model = "test"\n')
        (self.directory / "config.toml").symlink_to(linked)
        with self.assertRaisesRegex(ValueError, "symlink"):
            install.prepare(self.directory)
        self.assertEqual(linked.read_text(), 'model = "test"\n')
        with self.assertRaisesRegex(ValueError, "outside"):
            install.prepare(ROOT / "nested-install")

    def test_failed_replacement_restores_previous_installation_and_configuration(self):
        self.write("config.toml", 'model = "previous"\n')
        self.write("hooks.json", '{"hooks":{}}\n')
        self.write("notify-rhythm.py", "# old completion helper\n")
        self.write("notify-question.py", "# old question helper\n")
        self.write("completion-piano/old.txt", "previous project\n")
        before = snapshot(self.directory)
        plan = install.prepare(self.directory)
        original_replace = Path.replace
        failed = []

        def fail_once(source, target):
            if source.name == "hooks.json" and source.parent.name.startswith(".completion-piano-install-") and not failed:
                failed.append(True)
                raise OSError("simulated disk failure")
            return original_replace(source, target)

        with mock.patch.object(Path, "replace", autospec=True, side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "simulated disk failure"):
                install.install(plan)
        self.assertTrue(failed)
        self.assertEqual(snapshot(self.directory), before)
        self.assertFalse(list(self.directory.glob(".completion-piano-install-*")))

    def test_configuration_changed_during_staging_is_not_overwritten(self):
        self.write("config.toml", 'model = "before"\n')
        plan = install.prepare(self.directory)
        self.write("config.toml", 'model = "edited while installing"\n')
        with self.assertRaisesRegex(ValueError, "changed during"):
            install.install(plan)
        self.assertEqual((self.directory / "config.toml").read_text(), 'model = "edited while installing"\n')
        self.assertFalse((self.directory / "notify-rhythm.py").exists())

    def test_standalone_build_preserves_source_audio_and_is_reproducible(self):
        spec = importlib.util.spec_from_file_location("standalone_build", ROOT / "completion-piano/build.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        before = snapshot(ROOT / "completion-piano")
        first = builder.package("/remote/user/.codex/completion-piano/last-completion.json", self.root / "one")
        second = builder.package("/remote/user/.codex/completion-piano/last-completion.json", self.root / "two")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(snapshot(ROOT / "completion-piano"), before)
        with self.assertRaisesRegex(ValueError, "absolute"):
            builder.package("~/relative-marker.json", self.root / "bad")

    def test_original_audio_hash_and_format(self):
        audio = ROOT / "completion-piano/extension/piano.wav"
        self.assertEqual(hashlib.sha256(audio.read_bytes()).hexdigest(),
                         "1a4e5c1c7a497041347b074f021a872b70a4857642d5a053c68939004423aaa5")
        with wave.open(str(audio), "rb") as sound:
            self.assertEqual((sound.getnchannels(), sound.getsampwidth(), sound.getframerate()), (1, 2, 44100))
            self.assertEqual(sound.getnframes() / sound.getframerate(), 2.0)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="piano-notify-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("notify-rhythm.py", "notify-question.py"):
            shutil.copy2(ROOT / name, self.root / name)

    @property
    def marker(self):
        return self.root / "completion-piano/last-completion.json"

    def completion(self, event):
        return subprocess.run([sys.executable, str(self.root / "notify-rhythm.py"), event],
                              capture_output=True, text=True, cwd=str(self.root))

    def question(self, event):
        return subprocess.run([sys.executable, str(self.root / "notify-question.py")], input=event,
                              capture_output=True, text=True, cwd=str(self.root))

    def test_completion_forwards_only_metadata_and_generates_fresh_ids(self):
        event = json.dumps({"type": "agent-turn-complete", "cwd": "/work/项目",
                            "input-messages": ["private prompt"], "last-assistant-message": "private answer"})
        self.assertEqual(self.completion(event).returncode, 0)
        first = json.loads(self.marker.read_text())
        self.assertEqual(set(first), {"id", "type", "cwd"})
        self.assertEqual(first["cwd"], "/work/项目")
        self.assertEqual(self.completion(event).returncode, 0)
        self.assertNotEqual(first["id"], json.loads(self.marker.read_text())["id"])
        self.assertEqual([path.name for path in self.marker.parent.iterdir()], ["last-completion.json"])

    def test_both_question_tools_and_qualified_names_create_markers(self):
        previous = None
        for tool in ("request_user_input", "request_user_input_async", "functions.request_user_input",
                     "functions.request_user_input_async"):
            result = self.question(json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                                               "cwd": "/project", "tool_input": {"question": "private"}}))
            self.assertEqual(result.returncode, 0, result.stderr)
            event = json.loads(self.marker.read_text())
            self.assertEqual(set(event), {"id", "type", "cwd"})
            self.assertEqual(event["cwd"], "/project")
            self.assertNotEqual(event["id"], previous)
            previous = event["id"]

    def test_malformed_events_other_tools_and_approvals_are_ignored(self):
        for event in ("bad json", "[]", "null", '{}', '{"type":"other-event"}'):
            self.assertEqual(self.completion(event).returncode, 0)
        for event in ("bad json", "[]", "null", '{}',
                      '{"hook_event_name":"PermissionRequest","tool_name":"Bash"}',
                      '{"hook_event_name":"PostToolUse","tool_name":"request_user_input"}',
                      '{"hook_event_name":"PreToolUse","tool_name":"not_request_user_input"}'):
            self.assertEqual(self.question(event).returncode, 0)
        self.assertFalse(self.marker.exists())

    def test_failures_are_silent_except_explicit_preview(self):
        (self.root / "completion-piano").write_text("not a directory")
        result = self.completion('{"type":"agent-turn-complete"}')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        result = self.question('{"hook_event_name":"PreToolUse","tool_name":"request_user_input"}')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        preview = self.completion("--test")
        self.assertEqual(preview.returncode, 1)
        self.assertIn("Could not send piano preview", preview.stderr)

    def test_preview_uses_current_directory(self):
        result = self.completion("--test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Preview sent", result.stdout)
        self.assertEqual(json.loads(self.marker.read_text())["cwd"], str(self.root))

    def test_question_timeout_does_not_block_request(self):
        spec = importlib.util.spec_from_file_location("question_helper", self.root / "notify-question.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        import io
        data = '{"hook_event_name":"PreToolUse","tool_name":"request_user_input","cwd":"/project"}'
        with mock.patch.object(sys, "stdin", io.StringIO(data)), mock.patch.object(
                helper.subprocess, "run", side_effect=subprocess.TimeoutExpired("notifier", 3)):
            self.assertEqual(helper.main(), 0)


if __name__ == "__main__":
    unittest.main()
