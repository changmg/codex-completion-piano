# Codex completion piano for WSL

Play the existing two-second piano phrase on your **Windows 11 computer** when
Codex running in **WSL** finishes a turn or asks a structured question. This
repository packages the working Codex Completion Piano setup as an offline
installer for Windows VS Code with the WSL extension. Remote Linux machines
connected through VS Code Remote SSH are also supported.

The Linux helpers write a small marker. A VS Code extension installed locally
on Windows reads it through the existing WSL or SSH connection and plays the
WAV with Windows PowerShell. WSL needs no audio device, WSLg audio, audio
packages, daemon, or additional network port.

## Requirements

- WSL: Python 3.8+, Codex, and a working VS Code WSL connection. Ubuntu 20.04
  with Python 3.8 is supported.
- Local Windows 11: VS Code 1.85+, the WSL extension, Windows PowerShell,
  and audio.
- For Remote SSH instead: Python 3.8+ and Codex on Linux, plus the Remote - SSH
  extension and a working SSH connection from Windows VS Code.
- For questions: Codex must support `PreToolUse` hooks and the
  `request_user_input` / `request_user_input_async` tools.

At least one Windows VS Code window must stay connected to the WSL distribution
or SSH host. Codex may run in its IDE extension or a terminal using the same
Codex home. A job can run outside the open workspace and still play the piano.

## Install in WSL or on Linux

Copy or clone this repository inside WSL or on the SSH host. Run as the same
normal Linux user who runs Codex, without `sudo`. Finish active Codex work before
installing or upgrading, and avoid editing its configuration during installation.

```bash
cd /path/to/codex-completion-piano
python3 install.py --dry-run
python3 install.py
```

The installer automatically:

1. Selects `--codex-dir`, then `CODEX_HOME` if set, then `~/.codex`.
2. Installs both Python helpers and builds a Windows VSIX with this machine's
   absolute marker path, preserving the original WAV.
3. Adds or updates the top-level `notify` command, enables lifecycle hooks,
   and merges the question hook. Existing unrelated settings and hooks remain;
   configuration comments are preserved by the bundled TOML library.
4. Backs up replaced files under `completion-piano-backup-*` in the Codex
   directory. Files are staged before replacement and restored on a failed
   replacement. A repeated installation does not add duplicate question hooks.

If existing hooks use inline TOML, the installer uses that representation;
otherwise it uses `hooks.json`. An existing piano question hook is updated in
place. The installer preserves hook trust records; it never fabricates trust.

For a custom Codex directory:

```bash
python3 install.py --codex-dir /absolute/path/to/codex-home
```

An unrelated existing `notify` command stops installation before files change.
To replace that command and retain its previous configuration in the backup:

```bash
python3 install.py --replace-notify
```

`--dry-run` validates the source/configuration and prints the proposed changes
and setup steps. In WSL, it also shows the planned Windows VSIX path; the file
is created only by an actual installation. It writes no files or directories,
including bytecode caches. Symlinks at the managed destination paths are rejected
instead of replacing linked dotfiles.
Installation and VSIX building need no pip, npm, or network access.

## Enable question hooks

Start the **Codex CLI in WSL or on the SSH host** using the same Codex home as
your IDE session. Run `/hooks`, then review, trust, and enable the piano question
hook. Restart Codex afterward. New or changed hook definitions require review in Codex;
the installer cannot complete that product-level trust step for you.

If `allow_managed_hooks_only` is enabled, the installer stops because it would
prevent the question hook from running. Administrator-enforced policies can
also disable hooks; check `/hooks` on the destination machine.

## Install the player locally on Windows

The installer prints the generated VSIX path, normally:

```text
~/.codex/completion-piano/codex-completion-piano-0.1.4.vsix
```

### WSL

When `WSL_DISTRO_NAME` is set, the installer prints the Windows path using
`wslpath -w`. For example, a VSIX inside the `Ubuntu-20.04` distribution is
accessible through a path like:

```text
\\wsl.localhost\Ubuntu-20.04\home\user\.codex\completion-piano\codex-completion-piano-0.1.4.vsix
```

Use the actual path printed by your installer. If path conversion is unavailable,
open `\\wsl.localhost\<distro>` in Windows Explorer and follow the printed Linux
path from the distribution root to the VSIX. The distribution and home path are
detected at runtime.

1. Open a **local Windows VS Code window** using **File → New Window**.
2. Run **Extensions: Install from VSIX...** from the Command Palette and
   select the printed Windows VSIX path.
3. Verify **Codex Completion Piano** appears under **Local - Installed**.
4. Run **WSL: Connect to WSL using Distro...** and select the distribution
   printed by the installer. Reload VS Code and restart Codex in WSL.

The player runs locally on Windows, while the helpers and marker stay in WSL.
Keep Linux paths in the Codex configuration and marker-path settings; Windows
path conversion is only for locating the VSIX.

### Remote SSH alternative

Download the VSIX to Windows. In the remote VS Code Explorer, open its
containing folder and right-click the VSIX → **Download**. Alternatively, from
Windows PowerShell, substitute your SSH host and the absolute path printed
by the installer:

```powershell
scp 'user@host:/home/user/.codex/completion-piano/codex-completion-piano-0.1.4.vsix' "$env:USERPROFILE\Downloads\"
```

1. Open a **local Windows VS Code window**, disconnected from the remote host.
2. Run **Extensions: Install from VSIX...** from the Command Palette and
   select the downloaded file.
3. Verify **Codex Completion Piano** is installed locally on Windows.
4. Reconnect to Linux with Remote SSH, reload VS Code, and restart Codex.

Version 0.1.4 updates the existing `local-tools.codex-completion-piano`
extension for both WSL and SSH connections. Its commands and settings retain
their existing names.

## Test the connection

After installation, use these manual acceptance steps to check real audio.
First run **Codex: Preview Piano Notification** from the Windows VS Code
Command Palette. This tests Windows playback independently of Linux.

After the connected workspace has been open for a few seconds, run in WSL
or on the SSH host:

```bash
python3 ~/.codex/notify-rhythm.py --test
```

Use the installer's printed path when using a custom Codex home. This creates
a fresh marker. `Preview sent` confirms the marker was written, not that Windows
played it. An existing marker is ignored on startup; wait and repeat if necessary.

Test the question helper separately in WSL or on the SSH host:

```bash
python3 - <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys

codex_dir = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
event = {
    "hook_event_name": "PreToolUse",
    "tool_name": "functions.request_user_input_async",
    "cwd": str(Path.cwd()),
}
subprocess.run(
    [sys.executable, str(codex_dir / "notify-question.py")],
    input=json.dumps(event), text=True, check=True,
)
PY
```

Finally, start a new Codex session and ask for a short reply to test completion.
In Plan mode, ask Codex to present a multiple-choice question using its question
tool. The question should play before you answer. Ordinary prose questions and
permission/approval prompts do not trigger the question helper.

For duplicate/reconnect acceptance, open two VS Code windows connected to the
same WSL distribution or SSH host in the same profile. A new marker should play
once. Repeated reads and reloading with an old marker should stay silent.
Disconnect and reconnect, then write a new marker; playback should resume.

## Settings, multiple distributions, and SSH hosts

- `codexCompletionPiano.enabled`: mute or enable playback (default `true`).
- `codexCompletionPiano.scope`: `machine` (default) accepts every working
  directory on the connected host; `workspace` accepts the open folders only.
- `codexCompletionPiano.markerPaths`: a **Windows User setting** mapping each
  exact remote authority to its absolute Linux marker path.

Run **Codex: Diagnose Piano Notification** and open **Output → Codex Completion
Piano** to find the authority, resolved marker path, read failures, and playback
status. In a WSL workspace, Diagnose should report `platform: "win32"` and
`remoteName: "wsl"`. Configure multiple distributions or SSH hosts in Windows
User settings, for example:

```json
{
  "codexCompletionPiano.markerPaths": {
    "wsl+Ubuntu-20.04": "/home/alice/.codex/completion-piano/last-completion.json",
    "wsl+Ubuntu-24.04": "/srv/alice/codex/completion-piano/last-completion.json",
    "ssh-remote+lab-server": "/srv/alice/codex/completion-piano/last-completion.json"
  }
}
```

Copy the exact authority from Diagnose; it can differ from a distribution name
or simple SSH alias. Marker paths must remain absolute Linux paths.
An entry overrides the path embedded by the installer. Update an existing entry
if that host's Codex home changes. Unlisted hosts use the most recently installed
VSIX's embedded path, so mappings are needed when hosts have different paths.

## Troubleshooting and behavior

- Preview silent: check Windows volume/output, local extension installation,
  and the extension's output channel.
- Diagnose reports `platform: "linux"`: install the VSIX in local Windows
  VS Code and verify it appears under **Local - Installed**.
- Preview works but Linux is silent: inspect Diagnose, the embedded/overridden
  marker path, the Codex home, and the active remote connection.
- Completion works but questions are silent: review `/hooks`, trust the current
  definition, and restart Codex. Update Codex if the required hook/tool is absent.
- Playback receipts deduplicate events across windows in the same VS Code
  profile. Separate profiles or clients have separate receipts.
- One marker is retained and playback never overlaps. Events arriving close
  together may produce one sound. Events while disconnected are not queued;
  on reconnect the last unseen marker may play once in a surviving window.
- Only a generated ID, event type, and working directory enter the marker.
  Prompts, answers, and question contents are not forwarded to Windows.
- Notification failures are ignored by the helpers during normal Codex use.
  The explicit `--test` command reports a marker-write failure.

## Build and verify

The normal installer builds the VSIX for its actual destination. To build a
VSIX without installing or changing Codex configuration:

```bash
python3 completion-piano/build.py \
  --marker-path /home/user/.codex/completion-piano/last-completion.json
```

The VSIX is written to ignored `dist/`. Source files are unchanged; no personal
path is checked in. Packaging always uses the bundled original WAV and includes
only the extension's explicit file list. Run the tests with:

```bash
python3 -m unittest discover -s tests -v
node tests/player.test.js
```

Node 18+ is needed for the development tests only. The automated tests exercise
WSL and SSH authorities, marker routing, scope, duplicate suppression, reconnects,
and installer guidance. They simulate the VS Code filesystem and Windows
playback boundary. A real audio check uses the manual acceptance steps above.

## Remove or roll back

To mute, disable `codexCompletionPiano.enabled`. To remove, uninstall the local
Windows extension, remove the piano `notify` entry and its question handler
from the active hook source, and remove the two helpers and `completion-piano/`
from the WSL or remote Linux Codex directory. Preserve unrelated hooks and
settings. Restore the previous notifier from the installer's backup if appropriate.

For a rollback, close Codex, inspect the backup directory printed by the installer,
and restore its saved files to the Codex directory. It contains only files that
were replaced; remove newly created piano files separately. Restore whole
configuration files only after checking for changes made since installation.
Backups can contain private Codex configuration and stay outside this repository.

## Sources and license

The player and WAV are derived from the existing Codex Completion Piano 0.1.3
installation. This repository retains its MIT license and extension identity.
The WAV is preserved byte for byte (mono PCM, 44.1 kHz, two seconds).
Vendored tomlkit 0.12.5 retains its upstream MIT license and download provenance
in [vendor/README.md](vendor/README.md).

- [Codex notification configuration](https://learn.chatgpt.com/docs/config-file/config-advanced#notifications)
- [Codex lifecycle hooks and trust](https://learn.chatgpt.com/docs/hooks)
- [VS Code WSL](https://code.visualstudio.com/docs/remote/wsl)
- [VS Code Remote SSH](https://code.visualstudio.com/docs/remote/ssh)
