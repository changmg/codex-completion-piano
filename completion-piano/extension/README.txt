Codex Completion Piano 0.1.4

Play the original two-second piano phrase on Windows when Codex finishes or
opens a structured question on a connected Linux machine.

INSTALL
1. On Linux, run python3 install.py from the codex-finish-piano repository.
2. Review/trust the question hook with /hooks in the Linux Codex CLI.
3. Download the generated VSIX from the Linux machine.
4. In a LOCAL Windows VS Code window, run Extensions: Install from VSIX...
5. Install locally, reconnect with Remote SSH, reload VS Code, and restart Codex.
6. Run Codex: Preview Piano Notification from the Command Palette.

The extension runs locally in Windows and reads a small marker through VS
Code's remote filesystem. Linux needs no audio packages or additional server.
The marker contains only a unique ID, event type, and working directory.

Keep a VS Code window connected to the host. Machine scope accepts jobs from
every working directory. Workspace scope accepts only the open folders.
Windows in the same VS Code profile share receipts to prevent duplicate sounds.
Old markers at startup are ignored; closely spaced events can produce one sound.

SETTINGS
- codexCompletionPiano.enabled: enable or mute piano playback.
- codexCompletionPiano.scope: machine (default) or workspace.
- codexCompletionPiano.markerPaths: Windows User setting mapping exact remote
  authorities to absolute Linux marker paths. Overrides the installer's path,
  allowing one extension to support multiple hosts with different home paths.

Use Codex: Diagnose Piano Notification and Output > Codex Completion Piano to
inspect the authority, marker path, scope, read failures, and playback status.
Windows PowerShell plays the bundled WAV using System.Media.SoundPlayer.
The installed version updates local-tools.codex-completion-piano in place.

Full setup, testing, and removal instructions are in the repository README.md.
