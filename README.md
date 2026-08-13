# Kitsu Standalone App

> ⚠️ **Heavy work in progress — treat this as an early prototype, not a released tool.**
>
> It is being built and reshaped in fast iterations against one real Kitsu instance, so:
> - Layout, wording and workflows change from one build to the next, and nothing here is a settled
>   design decision yet.
> - Whole areas are still missing or only half-covered, and there are no automated tests.
> - It writes to Kitsu for real — publishing a comment, a status change or a revision from this app
>   is exactly as permanent as doing it in the web UI. Try it on tasks you don't mind touching.
> - Expect bugs, and expect to fall back to Kitsu's web UI when something isn't supported here.
>
> Feedback on what's broken or missing is the point of it existing right now.

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

- The Assets/Shots tabs show the **whole project** — every asset and shot, with every task on it,
  whoever it belongs to — not just the logged-in user's own work. Two filters narrow it: one to
  "My tasks" (assigned to you, also shown in bold) or "My checks" (waiting on your review, marked
  `(Review)`), and one to a single task type. The task-type list holds only the types actually in
  use on the current tab, and is remembered per tab — Modeling/Shading are asset task types while
  Animation/Comp are shot ones, so a shared selection would only ever empty the other tab.
  Canceled shots/assets are listed and marked, not hidden.
- Nothing polls the task list, so use **Refresh** after changing things in the web UI. Publishing
  from this app refreshes it automatically, since that changes a status.
- The Version column fills in a moment after the trees appear: a project's preview files are by far
  the slowest thing to fetch (~13s for 7000 of them on one real project), so the trees are shown
  first and the versions land when they land.
- The saved login (refresh token) is stored in the OS keyring (Windows Credential Manager / Linux
  Secret Service) via the `keyring` package. If no keyring backend is available, it falls back to
  the app's own plain settings file and warns once.
- Attaching a file to a comment publishes it as a Kitsu preview/revision (via `publish_preview`),
  not a plain attachment — matching how Kitsu's own "Publish" action behaves.
- "Attach URL..." downloads what the address points at and publishes that file: a Kitsu comment
  carries uploaded files, not links, so there is nothing else a URL could become.
- "Paint Over..." opens a small markup editor (brush color/size, undo, clear) and attaches the
  flattened result. It shows up in two places: next to the attached file, and next to every image
  revision/attachment in the comment history — so feedback on revision N is "select it, draw on
  it, publish", without a round trip through another application.
