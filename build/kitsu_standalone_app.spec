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
    # This is a pure QtWidgets app: it imports QtCore/QtGui/QtWidgets and
    # nothing else (checked against the source). PySide6's hook otherwise
    # collects QML, Quick, Pdf, OpenGL and Network anyway, which is ~23 MB of
    # DLLs that are never loaded. QtNetwork is worth naming specially — it is
    # what drags in Qt's own OpenSSL build (libcrypto/libssl-3-x64.dll). The
    # app's HTTPS goes through requests/gazu, i.e. Python's _ssl and its
    # separate libcrypto-3.dll, which stays.
    excludes=[
        "PySide6.QtNetwork",
        "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuickWidgets",
    ],
    cipher=block_cipher,
    noarchive=False,
)
# Naming a module in `excludes` keeps its Python binding out, but PyInstaller
# still copies the Qt DLLs themselves when another collected module merely sits
# next to them — so they are dropped here by name as well. Nothing in this list
# is a load-time dependency of Qt6Core/Gui/Widgets: if that were wrong the app
# would fail to start outright, which is exactly what the post-build smoke test
# checks.
_UNUSED_BINARIES = (
    "qt6qml",
    "qt6quick",
    "qt6pdf",
    "qt6opengl",
    "qt6network",
    "libcrypto-3-x64.dll",
    "libssl-3-x64.dll",
)


def _is_unused(entry):
    name = entry[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    return any(name.startswith(prefix) or name == prefix for prefix in _UNUSED_BINARIES)


a.binaries = TOC([entry for entry in a.binaries if not _is_unused(entry)])
a.datas = TOC([entry for entry in a.datas if not _is_unused(entry)])

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
