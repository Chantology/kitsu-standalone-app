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
- A server URL without a scheme becomes `https://`. `http://` still works for an internal server,
  but asks for confirmation first, since the password would cross the network unencrypted.
- Downloaded media (previews, attachments, frame strips, movies) is cached under the OS temp
  directory. It's pruned after 7 days at startup and cleared completely on log out — see
  [media_cache.py](kitsu-standalone-app/kitsu_standalone_app/kitsu_core/media_cache.py). Fetching a
  frame refuses revisions over 2 GB.
- Comment text and links written by other people are shown read-only, and a clicked link is only
  opened if it's `http`/`https` — a link's label needn't match where it points.
- The saved login (refresh token) is stored in the OS keyring (Windows Credential Manager / Linux
  Secret Service) via the `keyring` package. If no keyring backend is available, it falls back to
  the app's own plain settings file and warns once.
- The composer mirrors Kitsu's own two forms. **Post Comment** carries text, a checklist, file
  attachments and URL links; **Publish Revision** carries the same plus the preview file itself, a
  version number and "set as the asset/shot thumbnail". Both can be marked *Visible to clients*
  (Kitsu's `for_client`, which only managers may set — off means internal to the production team).
- Comment text is markdown, the way Kitsu treats it: the toolbar wraps selections in `**bold**`,
  `*italic*`, `- bullets` or `` `code` ``, there's an emoji picker, and selecting a comment shows it
  rendered underneath the list. Rendering uses Qt's own `setMarkdown` — no extra dependency.
- Links are stored on the comment by Kitsu. Attaching **from a URL** is a different thing: Kitsu only
  stores uploaded files, so the file is downloaded here first and then uploaded (that is also what
  gazu's own `preview_file_url` does internally — the server never fetches the URL).
- "Paint Over..." opens a small markup editor (brush color/size, undo, clear). On a revision it
  offers two things: **Save Annotation**, which stores the markup on that revision the way Kitsu's
  own review player does (no new version), or **Attach as File**, which flattens it into an image for
  the composer. On a plain attachment or a file you picked yourself, only the flattened option
  applies.
- **Video revisions** work too: "Fetch Media & Paint Over" pulls Kitsu's frame strip (every frame of
  the clip as a 178x100 thumbnail, one request), you scrub to the frame you want, and the app then
  decodes *that* frame from the movie at full resolution to draw on. The movie is downloaded once and
  cached. A just-published revision has no frame strip until Kitsu finishes processing it, and the app
  says so rather than showing a server error.
- Annotations are written in the preview's own pixel coordinates with that size recorded as the
  fabric.js canvas size, which is how Kitsu rescales them to whatever it renders at. The frame number
  follows Kitsu's own convention, `floor(time * fps) + 1`.
- Drafts save everything the composer holds (text, status, checklist, links, attachments, flags), not
  just the text.
