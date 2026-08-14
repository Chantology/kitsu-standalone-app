# Build with: pyinstaller build/kitsu_standalone_app.spec
# Run natively per target OS — PyInstaller does not cross-compile, so this
# must be run once on Windows (produces dist/KitsuApp/KitsuApp.exe) and once
# on Linux (produces dist/KitsuApp/KitsuApp; wrap with linuxdeploy for an
# AppImage).
#
# This is a one-folder (--onedir) build, not one-file: a onefile .exe has to
# re-extract its entire bundle to a temp folder on EVERY launch, which is
# what made startup take ~15-25s (worse over a network share). Onedir keeps
# everything already unpacked in dist/KitsuApp/, so KitsuApp.exe just loads
# its DLLs directly — a normal, fast launch. The trade-off is a folder to
# distribute instead of a single file — zip dist/KitsuApp/ for that.

# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ["../run.py"],
    pathex=["../"],
    binaries=[],
    # Mirrors kitsu_standalone_app/assets/ into the frozen bundle at the
    # same relative path resources.py expects (kitsu_standalone_app/assets).
    datas=[("../kitsu_standalone_app/assets", "kitsu_standalone_app/assets")],
    # keyring resolves its backend via entry points, which PyInstaller's
    # static import scan can miss — collect_all guarantees the right
    # platform backend (Windows Credential Manager / SecretService) is
    # bundled, not just whatever keyring happens to import first at
    # analysis time.
    #
    # av (PyAV, which decodes movie revisions — see movie_frames.py) is imported
    # lazily so the app still starts where it isn't available, which also hides
    # it from the import scan.
    hiddenimports=["keyring.backends", "av"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # binaries go to COLLECT() below instead of into the exe itself
    name="KitsuApp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed app — no console window on Windows
    disable_windowed_traceback=False,
    icon="../kitsu_standalone_app/assets/kitsu_icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="KitsuApp",
)
