Codex Completion Piano 0.1.4

Play the original two-second piano phrase on Windows when Codex finishes or
opens a structured question in a connected WSL distribution or on a Linux
SSH host.

INSTALL WITH WSL
1. In WSL, run python3 install.py from the codex-completion-piano repository.
2. Review/trust the question hook with /hooks in the WSL Codex CLI.
3. Copy the Windows VSIX path printed by the installer. It uses wslpath -w;
   if conversion is unavailable, browse \\wsl.localhost\<distro> in Windows
   Explorer and follow the Linux path from the distribution root to the VSIX.
4. In a LOCAL Windows VS Code window, run Extensions: Install from VSIX...
   Select the Windows VSIX path and verify the player under Local - Installed.
5. Run WSL: Connect to WSL using Distro... and select the distribution printed
   by the installer. Reload VS Code and restart Codex in WSL.
6. Run Codex: Preview Piano Notification from the Command Palette.

REMOTE SSH ALTERNATIVE
Run the installer and review /hooks on the Linux host. Download the generated
VSIX using VS Code Explorer or scp, install it in local Windows VS Code, then
reconnect with Remote SSH, reload VS Code, and restart Codex on Linux.

The extension runs locally in Windows and reads a small marker through VS
Code's remote filesystem. WSL needs no WSLg audio, audio packages, or additional
server. The marker contains only a unique ID, event type, and working directory.

Keep a VS Code window connected to the WSL distribution or SSH host. Machine
scope accepts jobs from every working directory. Workspace scope accepts only
the open folders.
Windows in the same VS Code profile share receipts to prevent duplicate sounds.
Old markers at startup are ignored; closely spaced events can produce one sound.

SETTINGS
- codexCompletionPiano.enabled: enable or mute piano playback.
- codexCompletionPiano.scope: machine (default) or workspace.
- codexCompletionPiano.markerPaths: Windows User setting mapping exact remote
  authorities to absolute Linux marker paths. Overrides the installer's path,
  allowing one extension to support multiple WSL distributions and SSH hosts.
  Keep these as Linux paths; Windows path conversion is only for locating
  the VSIX.

Use Codex: Diagnose Piano Notification and Output > Codex Completion Piano to
inspect the authority, marker path, scope, read failures, and playback status.
For WSL, Diagnose should show platform "win32" and remoteName "wsl". Copy its
exact authority for markerPaths mappings; do not assume the distribution label.
Windows PowerShell plays the bundled WAV using System.Media.SoundPlayer.
The installed version updates local-tools.codex-completion-piano in place.

Full setup, testing, and removal instructions are in the repository README.md.
