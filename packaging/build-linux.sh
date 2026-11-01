#!/usr/bin/env bash
# Builds dist/NovelBang-<version>-<arch>.AppImage: the whole app in one file that runs on most
# desktop Linux distributions without installing.
#   packaging/build-linux.sh
#
# The AppImage uses the system's GTK 3 and WebKitGTK rather than carrying its own (WebKit
# can't be bundled reliably), so wherever it runs they need to be installed:
#   Ubuntu, Debian:  gir1.2-webkit2-4.1     Fedora: webkit2gtk4.1     Arch: webkit2gtk-4.1
# Building also needs what PyGObject compiles against:
#   Ubuntu, Debian:  libgirepository1.0-dev libcairo2-dev pkg-config
set -euo pipefail
cd "$(dirname "$0")/.."

arch=$(uname -m)
# appimagetool goes into every release, so it's pinned and checked.
appimagetool_version=1.9.1
declare -A appimagetool_sha256=(
  [x86_64]=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0
)
if [[ -z "${appimagetool_sha256[$arch]:-}" ]]; then
  echo "No checksum for appimagetool on $arch: add it to $0" >&2
  exit 1
fi

uv sync --extra desktop --group build
rm -rf build/NovelBang build/NovelBang.AppDir dist/NovelBang
uv run --extra desktop --group build pyinstaller --noconfirm --clean packaging/NovelBang.spec

# The AppDir: the app, a launcher (AppRun), a .desktop entry and an icon.
appdir=build/NovelBang.AppDir
mkdir -p "$appdir/usr/lib" "$appdir/usr/share/icons/hicolor/256x256/apps"
cp -a dist/NovelBang "$appdir/usr/lib/novelbang"
cat > "$appdir/AppRun" <<'EOF'
#!/bin/sh
here=$(dirname "$(readlink -f "$0")")
exec "$here/usr/lib/novelbang/NovelBang" "$@"
EOF
chmod +x "$appdir/AppRun"
cat > "$appdir/novelbang.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=NovelBang
Comment=Write novels with a local AI co-writer, and publish them as ebooks
Exec=NovelBang
Icon=novelbang
Categories=Office;WordProcessor;
Terminal=false
EOF
uv run --group build python -c 'import sys; from PIL import Image; Image.open("packaging/icon.png").resize((256, 256), Image.LANCZOS).save(sys.argv[1])' \
  "$appdir/usr/share/icons/hicolor/256x256/apps/novelbang.png"
cp "$appdir/usr/share/icons/hicolor/256x256/apps/novelbang.png" "$appdir/novelbang.png"
ln -sf novelbang.png "$appdir/.DirIcon"

tool="build/appimagetool-$appimagetool_version-$arch.AppImage"
if [[ ! -x "$tool" ]]; then
  curl -fsSL -o "$tool.part" \
    "https://github.com/AppImage/appimagetool/releases/download/$appimagetool_version/appimagetool-$arch.AppImage"
  echo "${appimagetool_sha256[$arch]}  $tool.part" | sha256sum -c --quiet -
  chmod +x "$tool.part"
  mv "$tool.part" "$tool"
fi

version=$(uv run python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
out="dist/NovelBang-$version-$arch.AppImage"
rm -f "$out"
# Extract-and-run: the tool itself runs without FUSE, which build machines often lack.
ARCH=$arch "$tool" --appimage-extract-and-run "$appdir" "$out"
echo "Built $out"
