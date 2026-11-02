#!/usr/bin/env python3
"""Install Codex piano notifications on Linux and build the Windows VS Code player."""

import argparse
import copy
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

# A dry run must not create bytecode caches in the checkout either.
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor"))
import tomlkit  # noqa: E402


QUESTION_MATCHER = r"(^|\.)request_user_input(_async)?$"
EVENTS = {
    "PreToolUse", "PermissionRequest", "PostToolUse", "PreCompact",
    "PostCompact", "SessionStart", "SessionEnd", "SubagentStart",
    "SubagentStop", "UserPromptSubmit", "Stop", "Interrupt",
}
EXTENSION_FILES = ("extension.js", "package.json", "piano.wav", "LICENSE.txt", "README.txt")


@dataclass
class Installation:
    directory: Path
    original: dict
    updates: dict
    question_source: str
    question_group: dict


def resolve_codex_dir(explicit=None):
    return Path(explicit or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()


def read_optional(path):
    return path.read_bytes() if path.exists() else None


def owns_command(arguments, script):
    """Recognize the installed piano command, never arbitrary notifier names."""
    if isinstance(arguments, str):
        try:
            arguments = shlex.split(arguments)
        except ValueError:
            return False
    if not isinstance(arguments, list):
        return False
    target = str(script)
    return arguments == [target] or (
        len(arguments) == 2 and isinstance(arguments[0], str)
        and Path(arguments[0]).name.startswith("python") and arguments[1] == target
    )


def check_hook_groups(hooks, source):
    if not isinstance(hooks, dict):
        raise ValueError("{}: hooks must be a table/object".format(source))
    for event, groups in hooks.items():
        if event not in EVENTS:
            continue
        if not isinstance(groups, list):
            raise ValueError("{}: hooks.{} must be an array".format(source, event))
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("{}: invalid {} matcher group".format(source, event))
            if any(not isinstance(handler, dict) for handler in group["hooks"]):
                raise ValueError("{}: invalid {} handler".format(source, event))


def prepare(directory, replace_notify=False):
    directory = resolve_codex_dir(directory)
    if directory == ROOT or ROOT in directory.parents:
        raise ValueError("Choose a Codex directory outside this repository.")
    required = [ROOT / name for name in ("README.md", "notify-rhythm.py", "notify-question.py")]
    required += [ROOT / "completion-piano" / "build.py"]
    required += [ROOT / "completion-piano" / "extension" / name for name in EXTENSION_FILES]
    for path in required:
        if not path.is_file():
            raise ValueError("Repository is incomplete; missing {}".format(path))
    if directory.exists() and not directory.is_dir():
        raise ValueError("Codex directory is not a directory: {}".format(directory))
    for name in ("config.toml", "hooks.json", "notify-rhythm.py", "notify-question.py", "completion-piano"):
        path = directory / name
        if path.is_symlink():
            raise ValueError("Refusing to replace symlink {}; use regular files for installation.".format(path))
        if path.exists() and (path.is_dir() != (name == "completion-piano")):
            raise ValueError("Unexpected file type: {}".format(path))

    original = {name: read_optional(directory / name) for name in ("config.toml", "hooks.json")}
    try:
        document = tomlkit.parse((original["config.toml"] or b"").decode("utf-8"))
    except (ValueError, tomlkit.exceptions.ParseError) as error:
        raise ValueError("Invalid config.toml: {}".format(error)) from error
    plain = document.unwrap()
    if plain.get("allow_managed_hooks_only") is True:
        raise ValueError("allow_managed_hooks_only prevents the piano question hook; review this setting first.")
    features = plain.get("features", {})
    if not isinstance(features, dict):
        raise ValueError("config.toml: features must be a table")
    inline = plain.get("hooks", {})
    check_hook_groups(inline, "config.toml")

    existing_notify = plain.get("notify", [])
    if not isinstance(existing_notify, list) or any(not isinstance(value, str) for value in existing_notify):
        raise ValueError("config.toml: notify must be an array of strings")
    helper = directory / "notify-rhythm.py"
    if existing_notify and not owns_command(existing_notify, helper) and not replace_notify:
        raise ValueError("An unrelated notify command exists. Use --replace-notify to replace it after backup.")
    interpreter = str(Path(sys.executable).absolute())
    if existing_notify != [interpreter, str(helper)]:
        document["notify"] = [interpreter, str(helper)]
    if "features" not in document:
        document["features"] = tomlkit.table()
    document["features"]["hooks"] = True
    # The legacy alias cannot coexist with the canonical key in some Codex versions.
    document["features"].pop("codex_hooks", None)

    try:
        json_document = json.loads(original["hooks.json"].decode("utf-8")) if original["hooks.json"] is not None else {}
    except ValueError as error:
        raise ValueError("Invalid hooks.json: {}".format(error)) from error
    if not isinstance(json_document, dict):
        raise ValueError("hooks.json must be an object")
    check_hook_groups(json_document.get("hooks", {}), "hooks.json")
    old_json = copy.deepcopy(json_document)
    command = shlex.join([interpreter, str(directory / "notify-question.py")])
    handler = {"type": "command", "command": command, "timeout": 5}
    question_group = {"matcher": QUESTION_MATCHER, "hooks": [handler]}

    # Prefer the existing representation. Update an existing piano handler in
    # place so other hooks keep their order (and their Codex trust identities).
    preferred = "hooks.json" if original["hooks.json"] is not None or not EVENTS.intersection(inline) else "config.toml"
    sources = [("hooks.json", json_document.get("hooks", {})), ("config.toml", document.get("hooks", {}))]
    found = False
    source_used = preferred
    for source, hooks in sources:
        groups = hooks.get("PreToolUse", [])
        empty_groups = []
        for group_index, group in enumerate(groups):
            handlers = group["hooks"]
            duplicates = []
            for index, current in enumerate(handlers):
                if current.get("type") != "command" or not owns_command(current.get("command"), directory / "notify-question.py"):
                    continue
                if not found and group.get("matcher") == QUESTION_MATCHER:
                    for key, value in handler.items():
                        if current.get(key) != value:
                            current[key] = value
                    found = True
                    source_used = source
                else:
                    duplicates.append(index)
            for index in reversed(duplicates):
                del handlers[index]
            if duplicates and not handlers:
                empty_groups.append(group_index)
        for index in reversed(empty_groups):
            del groups[index]
    if not found:
        if preferred == "hooks.json":
            json_document.setdefault("hooks", {}).setdefault("PreToolUse", []).append(question_group)
        else:
            hooks = document["hooks"]
            if "PreToolUse" not in hooks:
                hooks["PreToolUse"] = tomlkit.aot()
            hooks["PreToolUse"].append(question_group)

    updates = {}
    new_config = tomlkit.dumps(document).encode("utf-8")
    # Parse the exact output before touching any destination file.
    tomlkit.parse(new_config.decode("utf-8"))
    if new_config != original["config.toml"]:
        updates["config.toml"] = new_config
    if json_document != old_json:
        updates["hooks.json"] = (json.dumps(json_document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return Installation(directory, original, updates, source_used, question_group)


def commit_replacements(replacements, backup):
    """Commit staged files with same-filesystem renames; restore on any failure."""
    saved = []
    installed = []
    try:
        for staged, target in replacements:
            if target.exists():
                previous = backup / target.name
                target.replace(previous)
                saved.append((previous, target))
            staged.replace(target)
            installed.append((target, staged))
    except BaseException:
        for target, staged in reversed(installed):
            target.replace(staged)
        for previous, target in reversed(saved):
            previous.replace(target)
        raise


def install(plan):
    directory = plan.directory
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    backup = None
    with tempfile.TemporaryDirectory(prefix=".completion-piano-install-", dir=str(directory)) as temporary:
        stage = Path(temporary)
        project = stage / "completion-piano"
        extension = project / "extension"
        extension.mkdir(parents=True)
        for name in EXTENSION_FILES:
            shutil.copy2(ROOT / "completion-piano" / "extension" / name, extension / name)
        shutil.copy2(ROOT / "completion-piano" / "build.py", project / "build.py")
        shutil.copy2(ROOT / "README.md", project / "README.md")
        (project / "question-hooks.json").write_text(
            json.dumps({"hooks": {"PreToolUse": [plan.question_group]}}, indent=2) + "\n", encoding="utf-8"
        )
        spec = importlib.util.spec_from_file_location("piano_build", ROOT / "completion-piano" / "build.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        vsix = builder.package(str(directory / "completion-piano" / "last-completion.json"), project, extension)

        replacements = [(project, directory / "completion-piano")]
        for name in ("notify-rhythm.py", "notify-question.py"):
            shutil.copy2(ROOT / name, stage / name)
            (stage / name).chmod(0o755)
            replacements.append((stage / name, directory / name))
        for name, content in plan.updates.items():
            staged = stage / name
            staged.write_bytes(content)
            target = directory / name
            staged.chmod(stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600)
            replacements.append((staged, target))

        for name, content in plan.original.items():
            if (directory / name).is_symlink() or read_optional(directory / name) != content:
                raise ValueError("{} changed during installation; retry when configuration editing is finished.".format(name))
        # Preserve the last marker so updating the helpers does not replay it.
        marker = directory / "completion-piano" / "last-completion.json"
        if marker.is_file():
            shutil.copy2(marker, project / marker.name)
        if any(target.exists() for _, target in replacements):
            backup = Path(tempfile.mkdtemp(prefix="completion-piano-backup-", dir=str(directory)))
        commit_replacements(replacements, backup)
    return directory / "completion-piano" / vsix.name, backup


def show_plan(plan):
    print("Codex directory: {}".format(plan.directory))
    print("Install: notification helpers, original WAV, and Windows VSIX 0.1.4")
    print("Configuration: {}".format(", ".join(plan.updates) or "already configured"))
    print("Question hook source: {}".format(plan.question_source))
    print("notify = {}".format(json.dumps([str(Path(sys.executable).absolute()), str(plan.directory / "notify-rhythm.py")])))
    print("Question command: {}".format(plan.question_group["hooks"][0]["command"]))


def show_next_steps(plan, vsix=None):
    """Print setup steps, converting only the VSIX display path for WSL."""
    if vsix is None:
        manifest = ROOT / "completion-piano" / "extension" / "package.json"
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        vsix = plan.directory / "completion-piano" / "{}-{}.vsix".format(metadata["name"], metadata["version"])
    distro = os.environ.get("WSL_DISTRO_NAME")
    print("\nPlayer connection: {}".format("WSL ({})".format(distro) if distro else "Remote SSH"))
    print("VSIX (Linux): {}".format(vsix))
    windows_path = None
    if distro:
        try:
            result = subprocess.run(
                ["wslpath", "-w", str(vsix)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, check=True, timeout=5,
            )
            windows_path = result.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            pass
        if windows_path:
            print("VSIX (Windows): {}".format(windows_path))
        else:
            print("Windows path conversion unavailable. In Windows Explorer, open:")
            print("   " + "\\\\wsl.localhost\\" + distro)
            print("   Browse from the distribution root to the Linux VSIX path above.")

    location = "WSL" if distro else "Linux"
    print("\n1. Restart Codex in {}, run /hooks, and review/trust the piano question hook.".format(location))
    if distro:
        print("2. In a LOCAL Windows VS Code window, run Extensions: Install from VSIX...")
        print("   Select the Windows VSIX path above." if windows_path else
              "   Browse to the VSIX through the WSL share above.")
        print("3. Run WSL: Connect to WSL using Distro... and select {}.".format(distro))
        print("   Reload VS Code and restart Codex in WSL.")
    else:
        print("2. Download the VSIX using VS Code Explorer's Download action or scp.")
        print("3. In a LOCAL Windows VS Code window, run Extensions: Install from VSIX...")
        print("   Install the downloaded file locally, then reconnect with Remote SSH and reload VS Code.")
    print("4. Run Codex: Preview Piano Notification in the Command Palette, then run in {}:".format(location))
    print("   {}".format(shlex.join([sys.executable, str(plan.directory / "notify-rhythm.py"), "--test"])))
    print("\nDocumentation: {}".format(plan.directory / "completion-piano" / "README.md"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-dir", type=Path, metavar="PATH", help="destination (default: CODEX_HOME, then ~/.codex)")
    parser.add_argument("--replace-notify", action="store_true", help="replace an unrelated notify command after backing it up")
    parser.add_argument("--dry-run", action="store_true", help="validate and show planned changes without writing files")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 8):
        parser.exit(1, "Python 3.8 or newer is required.\n")
    try:
        plan = prepare(resolve_codex_dir(args.codex_dir), args.replace_notify)
        show_plan(plan)
        if args.dry_run:
            show_next_steps(plan)
            print("\nDry run: nothing written. Follow the setup steps after installation.")
            return 0
        vsix, backup = install(plan)
    except Exception as error:
        parser.exit(1, "Installation failed: {}\n".format(error))
    print("\nInstalled. Existing files were backed up in: {}".format(backup or "(fresh installation)"))
    show_next_steps(plan, vsix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
