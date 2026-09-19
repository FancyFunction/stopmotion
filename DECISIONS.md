# Autonomous Decision Log

Decisions made without the user present, during the unattended implementation run
started 2026-09-19. Each entry: what was hit, what was chosen, and why.

| # | Area | Roadblock | Decision | Rationale |
|---|---|---|---|---|
| 1 | Python tooling | `uv` is not installed, and neither is `python3-venv`/`ensurepip` — `python3 -m venv` fails and there is no system `pip`. Installing them needs `sudo`, which is not available unattended. | Created the venv with `python3 -m venv --without-pip` and bootstrapped pip from `bootstrap.pypa.io/get-pip.py`. `justfile setup` does this automatically. Plan.md's choice of `uv` was dropped. | Needs no root and no new system packages. If you later run `apt install python3.12-venv`, the workaround can be deleted — noted inline in the justfile. |
| 2 | Testing without hardware | No Android device is attached, so nothing on the desktop side could be exercised end to end. | Built `tools/fake_phone.py`, a stdlib protocol simulator that streams synthetic animated preview frames and answers captures with synthetic full-res stills. `just fake` runs the desktop app against it. | Makes the entire desktop half verifiable now, and doubles as a permanent regression harness. Real-hardware verification still has to be done by you with a phone plugged in. |
| 3 | Android package name | A package name was required and none was specified. | `de.kruse.stopmotion`, activity `.MainActivity`. | Derived from your account name. Rename with a find-and-replace across `android/` plus the `launch_app()` string in `desktop/stopmotion/device.py` if you want something else. |
