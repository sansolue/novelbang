# PyInstaller spec for the NovelBang desktop app. Build with packaging/build-macos.sh.
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]

a = Analysis(
    [str(ROOT / "packaging" / "novelbang_desktop.py")],
    pathex=[str(ROOT / "src")],
    datas=collect_data_files("novelbang"),  # static/ (the UI)
    # uvicorn picks its protocol and loop implementations at runtime.
    hiddenimports=collect_submodules("uvicorn"),
    excludes=["tkinter", "pytest", "PIL", "PyInstaller"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="NovelBang",
    console=False, argv_emulation=False, target_arch=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="NovelBang")
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
