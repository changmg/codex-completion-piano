#!/usr/bin/env python3
"""Signal the Windows piano player when Claude Code finishes a turn or asks a question."""

import json
import os
from pathlib import Path
import subprocess
import sys


COMPLETION_EVENTS = {"Stop", "StopFailure"}
QUESTION_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
# Headless `claude -p` and SDK runs finish without anyone waiting at a terminal.
HEADLESS_ENTRYPOINTS = {"sdk-cli", "sdk-ts", "sdk-py"}


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if len(sys.argv) != 2 or os.environ.get("CLAUDE_CODE_ENTRYPOINT") in HEADLESS_ENTRYPOINTS:
        return 0
    if not isinstance(event, dict):
        return 0
    name = event.get("hook_event_name")
    tool = event.get("tool_name")
    question = name == "PreToolUse" and isinstance(tool, str) and tool in QUESTION_TOOLS
    if not question and not (isinstance(name, str) and name in COMPLETION_EVENTS):
        return 0

    try:
        # Reuse the player's marker format without forwarding prompts, answers, or plans.
        cwd = event.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            cwd = str(Path.cwd())
        signal = {"type": "agent-turn-complete", "cwd": cwd}
        subprocess.run(
            [sys.executable, sys.argv[1], json.dumps(signal)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Notification failures must never interfere with Claude Code.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
