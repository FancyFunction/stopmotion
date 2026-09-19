# Autonomous Decision Log

Decisions made without the user present, during the unattended implementation run
started 2026-09-19. Each entry: what was hit, what was chosen, and why.

| # | Area | Roadblock | Decision | Rationale |
|---|---|---|---|---|
| 1 | Python tooling | `uv` is not installed, and neither is `python3-venv`/`ensurepip` — `python3 -m venv` fails and there is no system `pip`. Installing them needs `sudo`, which is not available unattended. | Created the venv with `python3 -m venv --without-pip` and bootstrapped pip from `bootstrap.pypa.io/get-pip.py`. `justfile setup` does this automatically. Plan.md's choice of `uv` was dropped. | Needs no root and no new system packages. If you later run `apt install python3.12-venv`, the workaround can be deleted — noted inline in the justfile. |
| 2 | Testing without hardware | No Android device is attached, so nothing on the desktop side could be exercised end to end. | Built `tools/fake_phone.py`, a stdlib protocol simulator that streams synthetic animated preview frames and answers captures with synthetic full-res stills. `just fake` runs the desktop app against it. | Makes the entire desktop half verifiable now, and doubles as a permanent regression harness. Real-hardware verification still has to be done by you with a phone plugged in. |
| 3 | Android package name | A package name was required and none was specified. | `de.kruse.stopmotion`, activity `.MainActivity`. | Derived from your account name. Rename with a find-and-replace across `android/` plus the `launch_app()` string in `desktop/stopmotion/device.py` if you want something else. |
| 4 | Android manifest | **The app could not talk to the desktop at all.** `ServerSocket.bind(127.0.0.1:8099)` failed with `EPERM` on the real device, retrying once a second forever. The manifest declared only `CAMERA`. | Added `android.permission.INTERNET` to the manifest. | On Android, creating *any* TCP socket — including a loopback listener reached only through `adb forward` — requires `INTERNET`, because the sandbox gates socket creation on the `AID_INET` group. It is a normal (install-time) permission and grants no actual network exposure here: the listener is bound to loopback. **The simulator could never have caught this**; only the real phone did. |
| 5 | Export frame count | The ffmpeg concat-demuxer recipe specified in Plan.md produced the wrong number of output frames. Measured on ffmpeg 6.1.1: holds `[1,3,1,2]` @ 4 fps gave 8 frames instead of 7; removing the mandatory repeated final entry instead dropped the last frame entirely (5 frames). | Kept the repeated final entry **and** pinned the output with `-frames:v <sum(holds)>`. | The concat demuxer needs the repeat or it truncates, but then over-runs by 1–2 frames depending on fps and the final hold. Pinning the count makes all tested hold/fps combinations exact, verified with `ffprobe`. |
| 6 | Hardware testing | A Galaxy S23 (SM-S911B, Android 16) turned out to be attached and authorised part-way through the run, so real verification became possible without you. | Installed the debug APK, granted `CAMERA` via `adb shell pm grant`, woke the display, and ran the full pipeline against the real camera. Left the app installed. | Hardware testing found a blocking bug (#4) that 186 green tests did not. Nothing destructive was done and the phone's lockscreen was never bypassed. Remove the app with `adb uninstall de.kruse.stopmotion` if you don't want it. |
| 7 | Per-stream decision logs | Four parallel streams each logged decisions; merging them into one table live would have caused write races. | Each stream wrote `docs/decisions-<stream>.md` (100 rows total). This file holds the cross-cutting and significant ones. | Full detail stays available per stream; this stays readable. |

## Per-stream logs

- [docs/decisions-android.md](docs/decisions-android.md) — 19 rows
- [docs/decisions-transport.md](docs/decisions-transport.md) — 32 rows
- [docs/decisions-model.md](docs/decisions-model.md) — 22 rows
- [docs/decisions-ui.md](docs/decisions-ui.md) — 27 rows

## Notable per-stream decisions worth your review

- **Focus slider direction** — `focus_diopters` is reported as `{lo: 0.0, hi: LENS_INFO_MINIMUM_FOCUS_DISTANCE}`, where 0 means infinity, so the slider runs infinity → macro.
- **`af_lock` maps to `CONTROL_AF_MODE=OFF` + an explicit focus distance** — Camera2 has no `CONTROL_AF_LOCK`.
- **Manifest gained a `next_id` key** so frame ids are never recycled after a delete-and-reopen.
- **Duplicated frames share one JPEG on disk** rather than copying bytes; `purge_unreferenced()` reference-counts.
- **`ReorderFrames.insert_at` is a pre-move drop-gap index** (the classic multi-select drag off-by-one).
- **Bandwidth readout is an estimate** (marked `≈`) because `PhoneConnection` exposes no byte counter.
- **Devices without `MANUAL_SENSOR`** fall back to `AE_MODE=ON` plus locks, and the phone screen says so.
