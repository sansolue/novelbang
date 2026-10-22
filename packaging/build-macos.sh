#!/usr/bin/env bash
# Builds dist/NovelBang.app and dist/NovelBang-<version>.dmg for this Mac's architecture.
#   packaging/build-macos.sh
# The app is ad-hoc signed only: it runs on this Mac, but other Macs will block it
# until it's signed with a Developer ID and notarized (see README).
set -euo pipefail
cd "$(dirname "$0")/.."

uv sync --extra desktop --group build
rm -rf build/NovelBang dist/NovelBang dist/NovelBang.app
uv run --extra desktop --group build pyinstaller --noconfirm --clean packaging/NovelBang.spec

version=$(uv run python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
dmg="dist/NovelBang-$version-$(uname -m).dmg"
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
cp -R dist/NovelBang.app "$staging/"
ln -s /Applications "$staging/Applications"
rm -f "$dmg"
# hdiutil now and then fails with "Resource busy" (often on CI machines); a retry clears it.
for attempt in 1 2 3 4 5; do
  hdiutil create -quiet -volname NovelBang -srcfolder "$staging" -fs HFS+ -format UDZO "$dmg" && break
  [[ $attempt == 5 ]] && exit 1
  echo "hdiutil failed, retrying ($attempt)…" >&2
  sleep $((attempt * 3))
done
echo "Built dist/NovelBang.app and $dmg"
