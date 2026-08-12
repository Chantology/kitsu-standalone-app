# Kitsu Standalone App

A standalone desktop app for [Kitsu](https://kitsu.cg-wire.com/) — log in, pick one of your
assigned tasks, read its comments, and post a new one — with no DCC (Maya, Nuke, etc.) required.

Ported from the comment/login UI originally built into a set of Kitsu DCC plugins (Maya, Nuke,
Houdini, ...), reusing the same `gazu`-backed session logic. The one new piece is the task picker:
it calls `gazu.user.all_tasks_to_do`, which lists every open task assigned to the logged-in user
across all their projects.

## Run from source

```bash
python -m venv .venv
.venv/Scripts/activate   # Windows; use .venv/bin/activate on Linux
pip install -e .
python -m kitsu_standalone_app.main
```

## Build a standalone executable

Requires the `dev` extra (PyInstaller). PyInstaller does not cross-compile — run this natively on
each target OS.

```bash
pip install -e ".[dev]"
pyinstaller build/kitsu_standalone_app.spec --distpath .
```

- **Windows**: produces `KitsuApp/KitsuApp.exe` (right under `kitsu-standalone-app/`, not nested in
  a `dist/` folder) plus its supporting DLLs/resources in the same folder.
- **Linux**: produces `KitsuApp/KitsuApp` the same way. Wrap the folder into an AppImage with
  [`linuxdeploy`](https://github.com/linuxdeploy/linuxdeploy) + its Qt plugin if a single portable
  file is wanted for distribution.

This is a one-folder (`--onedir`) build, not one-file: a onefile `.exe` re-extracts its entire
bundle to a temp folder on every launch (~15-25s observed, worse over a network share) — onedir
keeps everything already unpacked, so `KitsuApp.exe` starts about as fast as any other installed
Windows app.

To share with testers: zip the `KitsuApp/` folder as-is and send that — each person unzips it on
their own machine and runs `KitsuApp.exe` from inside the extracted folder (don't pull the `.exe`
out on its own; it needs its neighboring files). No installer, no shared account — each person logs
in with their own Kitsu login on first launch.

After building, smoke-test on a clean VM with no dev Python/Qt installed — missing Qt platform
plugins (`xcb`/`wayland` on Linux) are a common PyInstaller + Qt gotcha that only show up without
a dev environment masking them.

Running straight off a network share is still slower than local disk (DLL loads go over the
network too) — copy the `KitsuApp` folder to local disk for normal use.

## Notes

- The saved login (refresh token) is stored in the OS keyring (Windows Credential Manager / Linux
  Secret Service) via the `keyring` package. If no keyring backend is available, it falls back to
  the app's own plain settings file and warns once.
- Attaching a file to a comment publishes it as a Kitsu preview/revision (via `publish_preview`),
  not a plain attachment — matching how Kitsu's own "Publish" action behaves.
