# Stopmotion

Shoot stopmotion with an Android phone as the camera, driven entirely from a
Linux desktop app. The phone is a dumb actuator: the desktop owns the camera
settings, triggers every capture, and receives the full-resolution stills
directly over USB. **Nothing is ever written to phone storage.**

See [Requirements.md](Requirements.md) for the original intent, [Plan.md](Plan.md)
for the design, and [DECISIONS.md](DECISIONS.md) for decisions taken during the
unattended build.

## Requirements

- Linux, Python 3.12, ffmpeg
- Android SDK platform-tools (`adb`) — the app looks on `$PATH`, then `~/Android/Sdk/platform-tools`
- An Android phone with USB debugging enabled, minSdk 26

## Setup

```bash
just setup          # creates desktop/.venv and installs PySide6 + pytest
just android-build  # builds the debug APK
```

`just setup` bootstraps pip from bootstrap.pypa.io because this machine has no
`python3-venv`/`ensurepip` and no system pip. If you ever
`apt install python3.12-venv`, the workaround can be simplified — see
[DECISIONS.md](DECISIONS.md) #1.

## Shooting

```bash
just run    # installs the phone app, launches it, starts the desktop app
```

Plug the phone in, unlock it, and leave the app in the foreground — the socket
server binds in `onStart`, so the camera only streams while the activity is
resumed.

Then: **New Project** (name, location, fps, crop), aim the rig, **lock the
camera** (on by default — without AE/AF/AWB locked, consecutive frames flicker
and the film is unusable), and shoot. Spacebar toggles Live/Review.

## Without a phone

```bash
just fake   # runs the desktop app against tools/fake_phone.py
```

`tools/fake_phone.py` is a full protocol simulator with synthetic animated
preview frames and full-resolution captures. Useful for UI work and as a
regression harness. Flags: `--drop-after N`, `--fail-capture`, `--port`,
`--sensor`, `--fps`, `--capture-delay`.

## Tests

```bash
just test   # 186 tests, headless
```

Export timing is verified with real ffmpeg runs checked by `ffprobe`, not mocks.

## Layout

| Path | What |
|---|---|
| `android/` | Kotlin app: CameraX, manual controls, socket server |
| `desktop/stopmotion/` | protocol, adb device manager, connection thread, project model, undo commands, ffmpeg export |
| `desktop/stopmotion/ui/` | main window, viewport + onion skin, filmstrip, camera controls, dialogs |
| `tools/fake_phone.py` | hardware-free protocol simulator |
| `docs/interfaces.md` | internal API contract between the modules |

## Project format

A project is a plain folder: `frames/f_0001.jpg…` plus `project.json`, written
atomically after every change. JPEGs are immutable and are never deleted by
editing — deleting a frame only removes its manifest entry. `File → Purge
unreferenced frames` is the only thing that erases pixels. If the app ever dies
mid-shoot, the folder is recoverable by hand.
