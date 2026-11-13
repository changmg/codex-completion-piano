#!/usr/bin/env python3
"""Install Claude Code piano notifications that reuse the Windows VS Code piano player."""

import argparse
import copy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile
from typing import List, Optional

# A dry run must not create bytecode caches in the checkout either.
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import install as codex  # noqa: E402


QUESTION_MATCHER = "AskUserQuestion|ExitPlanMode"
# Hook groups the piano needs; None means the group has no matcher.
TRIGGERS = {"Stop": None, "StopFailure": None, "PreToolUse": QUESTION_MATCHER}
MANAGED_SETTINGS = Path("/etc/claude-code/managed-settings.json")


@dataclass
class Installation:
    directory: Path
    player: Path
    shared: bool
    original: Optional[bytes]
    settings: Optional[bytes]
    command: str
    other_hooks: List[str]


def resolve_claude_dir(explicit=None):
    return Path(explicit or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser().resolve()


def has_player(directory):
    """A piano player home holds the marker writer and the completion-piano directory."""
    return (directory / "notify-rhythm.py").is_file() and (directory / "completion-piano").is_dir()


def owns_hook(command, script):
    """Recognize the piano hook by its script, whichever interpreter and notifier it names."""
    if not isinstance(command, str):
        return False
    try:
        arguments = shlex.split(command)
    except ValueError:
        return False
    if arguments and Path(arguments[0]).name.startswith("python"):
        arguments = arguments[1:]
    return bool(arguments) and arguments[0] == str(script)


def check_hooks(hooks):
    if not isinstance(hooks, dict):
        raise ValueError("settings.json: hooks must be an object")
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise ValueError("settings.json: hooks.{} must be an array".format(event))
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("settings.json: invalid {} matcher group".format(event))
            if any(not isinstance(handler, dict) for handler in group["hooks"]):
                raise ValueError("settings.json: invalid {} handler".format(event))


def check_policy(settings):
    if settings.get("disableAllHooks") is True:
        raise ValueError("disableAllHooks is true in settings.json; the piano hooks would never run.")
    try:
        policy = json.loads(MANAGED_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(policy, dict):
        for key in ("allowManagedHooksOnly", "disableAllHooks"):
            if policy.get(key) is True:
                raise ValueError("{} sets {}; managed settings block the piano hooks.".format(MANAGED_SETTINGS, key))


def merge_hooks(hooks, handler, script):
    """Update piano handlers in place, drop duplicates, and append missing trigger groups."""
    found = set()
    for event, groups in hooks.items():
        empty_groups = []
        for group_index, group in enumerate(groups):
            handlers = group["hooks"]
            duplicates = []
            for index, current in enumerate(handlers):
                if current.get("type") != "command" or not owns_hook(current.get("command"), script):
                    continue
                if event in TRIGGERS and event not in found and (group.get("matcher") or None) == TRIGGERS[event]:
                    current.update(handler)
                    found.add(event)
                else:
                    duplicates.append(index)
            for index in reversed(duplicates):
                del handlers[index]
            if duplicates and not handlers:
                empty_groups.append(group_index)
        for index in reversed(empty_groups):
            del groups[index]
    for event, matcher in TRIGGERS.items():
        if event not in found:
            group = {"matcher": matcher} if matcher else {}
            group["hooks"] = [dict(handler)]
            hooks.setdefault(event, []).append(group)


def prepare(directory, codex_dir=None):
    directory = resolve_claude_dir(directory)
    codex_dir = codex.resolve_codex_dir(codex_dir)
    if directory == ROOT or ROOT in directory.parents:
        raise ValueError("Choose a Claude directory outside this repository.")
    required = [ROOT / name for name in ("README.md", "notify-rhythm.py", "notify-claude.py")]
    required += [ROOT / "completion-piano" / "build.py"]
    required += [ROOT / "completion-piano" / "extension" / name for name in codex.EXTENSION_FILES]
    for path in required:
        if not path.is_file():
            raise ValueError("Repository is incomplete; missing {}".format(path))
    if directory.exists() and not directory.is_dir():
        raise ValueError("Claude directory is not a directory: {}".format(directory))

    # Share an installed Codex piano so its existing Windows player hears Claude too.
    shared = codex_dir != directory and has_player(codex_dir)
    player = codex_dir if shared else directory
    managed = ["settings.json", "notify-claude.py"] + ([] if shared else ["notify-rhythm.py", "completion-piano"])
    for name in managed:
        path = directory / name
        if path.is_symlink():
            raise ValueError("Refusing to replace symlink {}; use regular files for installation.".format(path))
        if path.exists() and (path.is_dir() != (name == "completion-piano")):
            raise ValueError("Unexpected file type: {}".format(path))

    original = codex.read_optional(directory / "settings.json")
    try:
        settings = json.loads(original.decode("utf-8")) if original is not None else {}
    except ValueError as error:
        raise ValueError("Invalid settings.json: {}".format(error)) from error
    if not isinstance(settings, dict):
        raise ValueError("settings.json must be an object")
    check_policy(settings)
    check_hooks(settings.get("hooks", {}))

    before = copy.deepcopy(settings)
    script = directory / "notify-claude.py"
    command = shlex.join([str(Path(sys.executable).absolute()), str(script), str(player / "notify-rhythm.py")])
    hooks = settings.setdefault("hooks", {})
    merge_hooks(hooks, {"type": "command", "command": command, "timeout": 5}, script)
    other_hooks = ["{}: {}".format(event, handler.get("command") or handler.get("type"))
                   for event in ("Stop", "StopFailure") for group in hooks.get(event, [])
                   for handler in group["hooks"] if not owns_hook(handler.get("command"), script)]
    updated = None
    if settings != before:
        # Claude Code writes two-space JSON; matching it keeps unrelated lines unchanged.
        updated = (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return Installation(directory, player, shared, original, updated, command, other_hooks)


def install(plan):
    directory = plan.directory
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    backup = vsix = None
    with tempfile.TemporaryDirectory(prefix=".completion-piano-install-", dir=str(directory)) as temporary:
        stage = Path(temporary)
        replacements = []
        if not plan.shared:
            replacements, vsix = codex.stage_player(stage, directory)
        replacements.append(codex.stage_helper(stage, directory, "notify-claude.py"))
        target = directory / "settings.json"
        if plan.settings is not None:
            staged = stage / "settings.json"
            staged.write_bytes(plan.settings)
            staged.chmod(stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600)
            replacements.append((staged, target))
        if target.is_symlink() or codex.read_optional(target) != plan.original:
            raise ValueError("settings.json changed during installation; retry when settings editing is finished.")
        if not plan.shared:
            codex.keep_marker(stage, directory)
        if any(path.exists() for _, path in replacements):
            backup = Path(tempfile.mkdtemp(prefix="completion-piano-backup-", dir=str(directory)))
        codex.commit_replacements(replacements, backup)
    return vsix, backup


def show_plan(plan):
    print("Claude Code directory: {}".format(plan.directory))
    if plan.shared:
        print("Player: shared with the Codex piano in {}".format(plan.player))
    else:
        print("Player: standalone in {} (builds the Windows VSIX)".format(plan.player / "completion-piano"))
    print("Marker: {}".format(plan.player / "completion-piano" / "last-completion.json"))
    print("Settings: {}".format("settings.json" if plan.settings is not None else "already configured"))
    print("Hooks: Stop, StopFailure, PreToolUse ({})".format(QUESTION_MATCHER))
    print("Hook command: {}".format(plan.command))
    if plan.shared and has_player(plan.directory):
        print("Note: an unused standalone player remains in {}.".format(plan.directory / "completion-piano"))
    if plan.other_hooks:
        print("\nOther Stop hooks stay in place; remove any that also play a sound:")
        for hook in plan.other_hooks:
            print("   " + hook)


def show_next_steps(plan, vsix=None):
    hook = [str(Path(sys.executable).absolute()), str(plan.directory / "notify-claude.py"),
            str(plan.player / "notify-rhythm.py")]
    test = "printf '%s' {} | {}".format(shlex.quote('{"hook_event_name": "Stop"}'), shlex.join(hook))
    if plan.shared:
        print("\nNothing to install on Windows: the Codex Completion Piano player already reads this marker.")
        print("\n1. Keep a VS Code window connected to this machine. Claude Code reloads its settings")
        print("   automatically; restart open sessions if the piano stays silent.")
        print("2. Test the hook; the piano plays in the connected Windows VS Code window:")
        print("   " + test)
        return
    distro = os.environ.get("WSL_DISTRO_NAME")
    print()
    converted = codex.show_vsix_location(vsix or codex.default_vsix(plan.directory))
    print("\n1. In a LOCAL Windows VS Code window, run Extensions: Install from VSIX...")
    if distro:
        print("   Select the Windows VSIX path above." if converted else
              "   Browse to the VSIX through the WSL share above.")
        print("2. Run WSL: Connect to WSL using Distro... and select {}, then reload VS Code.".format(distro))
    else:
        print("   Copy the VSIX to Windows first.")
        print("2. Connect VS Code to this machine and reload it.")
    print("3. Run Codex: Preview Piano Notification in the Command Palette, then test the hook here:")
    print("   " + test)
    print("\nDocumentation: {}".format(plan.directory / "completion-piano" / "README.md"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-dir", type=Path, metavar="PATH",
                        help="Claude Code directory (default: CLAUDE_CONFIG_DIR, then ~/.claude)")
    parser.add_argument("--codex-dir", type=Path, metavar="PATH",
                        help="Codex directory whose installed piano is shared (default: CODEX_HOME, then ~/.codex)")
    parser.add_argument("--dry-run", action="store_true", help="validate and show planned changes without writing files")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 8):
        parser.exit(1, "Python 3.8 or newer is required.\n")
    try:
        plan = prepare(args.claude_dir, args.codex_dir)
        show_plan(plan)
        if args.dry_run:
            show_next_steps(plan)
            print("\nDry run: nothing written. Follow the steps above after installation.")
            return 0
        vsix, backup = install(plan)
    except Exception as error:
        parser.exit(1, "Installation failed: {}\n".format(error))
    print("\nInstalled. Existing files were backed up in: {}".format(backup or "(fresh installation)"))
    show_next_steps(plan, vsix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
