#!/usr/bin/env python3
"""Signal the Windows VS Code piano player when a Codex turn completes."""

import json
from pathlib import Path
import sys
import tempfile
import uuid


def main():
    if len(sys.argv) != 2:
        return 0
    preview = sys.argv[1] == "--test"
    if preview:
        event = {"type": "agent-turn-complete"}
    else:
        try:
            event = json.loads(sys.argv[1])
        except (ValueError, TypeError):
            return 0
        if not isinstance(event, dict) or event.get("type") != "agent-turn-complete":
            return 0

    temporary = None
    try:
        folder = Path(__file__).resolve().parent / "completion-piano"
        folder.mkdir(parents=True, exist_ok=True)
        cwd = event.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            cwd = str(Path.cwd())
        payload = {
            "id": str(uuid.uuid4()),
            "type": "agent-turn-complete",
            "cwd": cwd,
        }
        # Only a completion ID and working directory cross to the local player.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream)
            stream.write("\n")
        temporary.replace(folder / "last-completion.json")
    except OSError as error:
        if preview:
            print(f"Could not send piano preview: {error}", file=sys.stderr)
            return 1
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
    if preview:
        print("Preview sent. The Codex Completion Piano extension must be installed locally in Windows VS Code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
