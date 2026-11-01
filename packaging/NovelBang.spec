# PyInstaller spec for the NovelBang desktop app.
# Build with packaging/build-macos.sh, packaging/build-windows.ps1 or packaging/build-linux.sh.
import os
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
# macOS: a Developer ID ("Developer ID Application: Name (TEAMID)") signs the app with the
# hardened runtime, ready for notarization; the app needs no entitlements (runtime exceptions).
# Without one the app is only ad-hoc signed.
SIGN_IDENTITY = os.environ.get("MACOS_SIGN_IDENTITY") or None
LINUX = sys.platform.startswith("linux")

# uvicorn picks its protocol and loop implementations at runtime.
hiddenimports = collect_submodules("uvicorn")
excludes = ["tkinter", "pytest", "PyInstaller"]  # not PIL: the PDF export (fpdf2) uses it
if LINUX:
    # The window is pywebview's GTK one, reached through PyGObject (gi). GTK, WebKitGTK and
    # GLib come from the system, as they're found at runtime by introspection; leaving their
    # gi.repository modules out keeps PyInstaller from copying the libraries in.
    hiddenimports += ["webview.platforms.gtk", *collect_submodules("gi")]
    excludes += [f"gi.repository.{m}" for m in (
        "Gtk", "Gdk", "GdkPixbuf", "Gio", "GLib", "GObject", "Pango", "PangoCairo", "cairo",
        "Soup", "WebKit2", "JavaScriptCore", "AyatanaAppIndicator3", "AppIndicator3",
    )]

a = Analysis(
    [str(ROOT / "packaging" / "novelbang_desktop.py")],
    pathex=[str(ROOT / "src")],
    datas=collect_data_files("novelbang"),  # static/ (the UI)
    hiddenimports=hiddenimports,
    excludes=excludes,
)
if LINUX:
    # Keep only what comes with Python and the app's own packages. A copy of a system library
    # (GLib, cairo, libffi…) would clash with the GTK and WebKitGTK the system loads beside it.
    a.binaries = [b for b in a.binaries if not b[1].startswith(("/usr/", "/lib"))]
    a.datas = [d for d in a.datas if not d[0].startswith(("gi_typelibs", "share/", "lib/gdk-pixbuf"))]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="NovelBang",
    console=False, argv_emulation=False, target_arch=None,
    icon=str(ROOT / "packaging" / "icon.png"),  # converted to .ico on Windows (needs Pillow)
    codesign_identity=SIGN_IDENTITY,
)
coll = COLLECT(exe, a.binaries, a.datas, name="NovelBang")
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="NovelBang.app",
        icon=str(ROOT / "packaging" / "icon.png"),
        bundle_identifier="com.sansolue.novelbang",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": "NovelBang",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSApplicationCategoryType": "public.app-category.productivity",
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "© SanSolue",
        },
    )
