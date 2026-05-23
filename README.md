# Scribe

Push-to-talk dictation for Linux (Wayland or X11). Hold **Right Ctrl**, speak,
release -- Scribe transcribes your speech with Groq's Whisper Large v3 Turbo,
copies it to the clipboard, and auto-pastes into the focused window. No GUI or
tray icon.

## Quick install

```sh
git clone https://github.com/cszach/scribe.git && cd scribe && ./install.sh
```

Or see below for manual setup and configuration.

## Prereqs

- Linux with a working microphone
- Python 3.11+
- `wl-clipboard`
- `ydotool` for auto-paste (optional if you set `SCRIBE_NO_AUTO_PASTE=1`)
- A [Groq API key](https://console.groq.com) (free tier is sufficient for
  personal use)

```sh
# Debian/Ubuntu
sudo apt install wl-clipboard ydotool

# Fedora
sudo dnf install wl-clipboard ydotool
```

## One-time setup

Listening to a global hotkey under Wayland requires read access to
`/dev/input/event*`, which is gated on the `input` group:

```bash
sudo usermod -aG input $USER
```

**Log out and back in** (or reboot) for the group change to take effect. Verify
with `groups | grep input`.

For auto-paste, start the `ydotoold` daemon so `ydotool` has a socket to talk
to:

```bash
sudo systemctl enable --now ydotool
```

**Fedora (and any distro shipping the upstream unit):** the default
`ydotool.service` writes its socket to `/tmp/.ydotool_socket` owned by root, so
non-root `ydotool` calls fail with `exit status 2`. Change the socket ownership
(not the path — `/run/user/$UID/` doesn't exist at boot, so pointing the system
unit there makes `ydotoold` crash before you log in):

```bash
sudo mkdir -p /etc/systemd/system/ydotool.service.d
sudo tee /etc/systemd/system/ydotool.service.d/override.conf >/dev/null <<EOF
[Service]
ExecStart=
ExecStart=/usr/bin/ydotoold --socket-path=/tmp/.ydotool_socket --socket-own=$(id -u):$(id -g)
EOF
sudo systemctl daemon-reload
sudo systemctl restart ydotool
```

Scribe sets `YDOTOOL_SOCKET=/tmp/.ydotool_socket` when invoking `ydotool`, so no
shell-rc export is needed.

`ydotoold` runs as root and injects synthetic keyboard events into
`/dev/uinput`. If you don't want this, set `SCRIBE_NO_AUTO_PASTE=1` and Scribe
will only copy to clipboard (you'll paste manually).

## Install

```bash
git clone https://github.com/cszach/scribe.git && cd scribe
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If `evdev` or `sounddevice` build from source (no prebuilt wheel for your Python
version), install the native deps first:

```sh
# Debian/Ubuntu
sudo apt install libevdev-dev portaudio19-dev

# Fedora
sudo dnf install libevdev-devel portaudio-devel
```

## Configure

```bash
cp .env.example .env
```

Then paste your key into `.env`:

```
GROQ_API_KEY=gsk_...
```

## Run

```bash
python scribe.py
```

You should see something like:

```
14:22:01 INFO ready. watching 2 device(s): 'AT Translated Set 2 keyboard' (/dev/input/event3), 'Logitech USB Receiver' (/dev/input/event8)
```

## Use

1. Focus the window you want to dictate into.
2. Hold **Right Ctrl**.
3. Speak.
4. Release. The transcript is pasted at your cursor.

Quick taps (under 300 ms) are ignored. Recordings auto-stop at 60 seconds. To
disable auto-paste and just use the clipboard, run with
`SCRIBE_NO_AUTO_PASTE=1`.

## Auto-paste modes

Different apps bind paste to different keystrokes. Set `SCRIBE_PASTE_MODE` to
pick which one Scribe synthesizes:

| Mode                 | Keystroke                    | Use it for                                                         | Avoid                            |
| -------------------- | ---------------------------- | ------------------------------------------------------------------ | -------------------------------- |
| `shortcut` (default) | Ctrl+Shift+V                 | Terminals (Claude Code, gnome-terminal, kitty), most Electron apps | Native GTK/GNOME apps (no-op)    |
| `ctrl_v`             | Ctrl+V                       | GTK/GNOME apps (gnome-text-editor), most browsers                  | Terminals (inserts literal `^V`) |
| `type`               | Types character-by-character | Anywhere the shortcut modes don't work                             | Long transcripts (~10ms/char)    |

The text is on the clipboard regardless of mode, so a manual paste always works
as a fallback.

## Troubleshooting

| Symptom                                                                     | Fix                                                                                                                                                                                                                                                                                                                                                                        |
| --------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `no keyboards with KEY_RIGHTCTRL found`                                     | You're not in the `input` group, or haven't logged out since adding it. See One-time setup.                                                                                                                                                                                                                                                                                |
| `permission denied on /dev/input/eventN`                                    | Same as above.                                                                                                                                                                                                                                                                                                                                                             |
| Hotkey suddenly stops working after sleep/wake, dock unplug, or USB re-plug | Kernel re-enumerated the keyboard and scribe's evdev fd died. Confirm with `journalctl --user -u scribe \| grep disconnected` — look for `device /dev/input/eventN disconnected`. Recover with `systemctl --user restart scribe` (or `kill <pid>` and relaunch if running ad-hoc). Known limitation — scribe scans devices once at startup and doesn't re-bind on hotplug. |
| `GROQ_API_KEY not set`                                                      | `.env` is missing or empty. See Configure.                                                                                                                                                                                                                                                                                                                                 |
| Recording starts but transcript is empty                                    | Mic isn't capturing — check `pavucontrol` / `wpctl status` and confirm your input is unmuted.                                                                                                                                                                                                                                                                              |
| `transcription failed: ...`                                                 | Network or API issue. Daemon stays alive; just press and try again.                                                                                                                                                                                                                                                                                                        |
| `auto-paste failed: ...exit status 2`                                       | `ydotoold` is running but the socket isn't accessible to your user. Apply the Fedora override in One-time setup.                                                                                                                                                                                                                                                           |
| `auto-paste failed: ...`                                                    | `ydotoold` isn't running. `sudo systemctl start ydotool`, or set `SCRIBE_NO_AUTO_PASTE=1`.                                                                                                                                                                                                                                                                                 |
| Transcript copies but doesn't paste                                         | Wrong `SCRIBE_PASTE_MODE` for the focused app. See Auto-paste modes.                                                                                                                                                                                                                                                                                                       |
| `ydotool not installed`                                                     | Install it (see Prereqs) or set `SCRIBE_NO_AUTO_PASTE=1`. Clipboard still works regardless.                                                                                                                                                                                                                                                                                |

## Configuration

Everything tunable lives in `.env`; see `.env.example` for the full list with
documented defaults. All variables are optional except `GROQ_API_KEY`.

| Variable                | Default                  | Description                                                                          |
| ----------------------- | ------------------------ | ------------------------------------------------------------------------------------ |
| `SCRIBE_HOTKEY`         | `KEY_RIGHTCTRL`          | Push-to-talk key, any name from `evdev.ecodes`.                                      |
| `SCRIBE_MIN_DURATION_S` | `0.3`                    | Holds shorter than this are dropped as accidental taps.                              |
| `SCRIBE_MAX_DURATION_S` | _unset_                  | If set, recording auto-stops at this point. Unset = no limit.                        |
| `SCRIBE_MODEL`          | `whisper-large-v3-turbo` | Any Groq Whisper variant.                                                            |
| `SCRIBE_PROMPT`         | _empty_                  | Comma-separated vocabulary to bias Whisper toward your domain-specific proper nouns. |
| `SCRIBE_PASTE_MODE`     | `shortcut`               | Paste mechanism (see [Auto-paste modes](#auto-paste-modes)).                         |
| `SCRIBE_NO_AUTO_PASTE`  | `0`                      | Set to `1` to skip auto-paste and use the clipboard only.                            |

## The `scribe` command

`install.sh` drops a small wrapper at `~/.local/bin/scribe` that talks to the
systemd user service and edits `.env` for you.

| Command                | What it does                                                               |
| ---------------------- | -------------------------------------------------------------------------- |
| `scribe start`         | Start the systemd user service.                                            |
| `scribe stop`          | Stop the systemd user service.                                             |
| `scribe restart`       | Restart the systemd user service.                                          |
| `scribe status`        | Compact status: state, pid, uptime, autostart, key config, recent logs.    |
| `scribe test`          | Record + transcribe once to verify the pipeline. Doesn't touch the daemon. |
| `scribe add <term>...` | Append terms to `SCRIBE_PROMPT` in `.env` (dedup'd).                       |
| `scribe uninstall`     | Remove the systemd unit and the CLI wrapper (the repo stays).              |

Multi-word terms must be quoted so the shell hands them through as one arg:

```sh
scribe add "Claude Code" Anthropic OAuth Kubernetes
```

If `~/.local/bin` isn't on your `PATH`, install.sh will tell you to add it to
your shell rc.

## Run on login

`install.sh` sets this up for you if you answer **Yes** to the autostart prompt.
To do it manually, create `~/.config/systemd/user/scribe.service`:

```ini
[Unit]
Description=Scribe push-to-talk dictation
After=graphical-session.target sound.target

[Service]
Type=simple
WorkingDirectory=%h/Code/scribe
ExecStart=%h/Code/scribe/.venv/bin/python %h/Code/scribe/scribe.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

Replace the paths with your own. `WorkingDirectory` is required so `.env` is
found. Then run:

```sh
systemctl --user daemon-reload
systemctl --user enable --now scribe
```
