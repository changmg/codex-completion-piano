#!/usr/bin/env python3
"""Send queued questions to the existing Windows piano notifier."""

import json
from pathlib import Path
import subprocess
import sys


QUESTION_TOOLS = {"request_user_input", "request_user_input_async"}


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(event, dict):
        return 0
    if event.get("hook_event_name") != "PreToolUse":
        return 0
    tool_name = str(event.get("tool_name", "")).rsplit(".", 1)[-1]
    if tool_name not in QUESTION_TOOLS:
        return 0

    try:
        # Reuse the player's marker format without forwarding question text.
        cwd = event.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            cwd = str(Path.cwd())
        signal = {"type": "agent-turn-complete", "cwd": cwd}
        notifier = Path(__file__).with_name("notify-rhythm.py")
        subprocess.run(
            [sys.executable, str(notifier), json.dumps(signal)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Notification failures must never interfere with asking a question.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
