#!/usr/bin/env bash
# Builds dist/NovelBang.app and dist/NovelBang-<version>-<arch>.dmg for this Mac's architecture.
#   packaging/build-macos.sh
#
# Unsigned by default: the app is ad-hoc signed, so it runs on this Mac, but other Macs block it.
# To build one any Mac opens, sign it with a Developer ID and notarize it (see README):
#   MACOS_SIGN_IDENTITY   "Developer ID Application: Name (TEAMID)", in your keychain
# and, to notarize, either
#   NOTARY_PROFILE        a profile saved with `xcrun notarytool store-credentials`
# or an App Store Connect API key (what CI uses):
#   APPLE_API_KEY_PATH    the AuthKey_<id>.p8 file
#   APPLE_API_KEY_ID, APPLE_API_ISSUER
set -euo pipefail
cd "$(dirname "$0")/.."

identity=${MACOS_SIGN_IDENTITY:-}
notary=()
if [[ -n "${NOTARY_PROFILE:-}" ]]; then
  notary=(--keychain-profile "$NOTARY_PROFILE")
elif [[ -n "${APPLE_API_KEY_PATH:-}" ]]; then
  notary=(--key "$APPLE_API_KEY_PATH" --key-id "$APPLE_API_KEY_ID" --issuer "$APPLE_API_ISSUER")
fi
if [[ ${#notary[@]} -gt 0 && -z "$identity" ]]; then
  echo "Notarizing needs a signed app: set MACOS_SIGN_IDENTITY too" >&2
  exit 1
fi

uv sync --extra desktop --group build
rm -rf build/NovelBang dist/NovelBang dist/NovelBang.app
# With MACOS_SIGN_IDENTITY set, PyInstaller signs every binary and the app (see NovelBang.spec).
uv run --extra desktop --group build pyinstaller --noconfirm --clean packaging/NovelBang.spec
if [[ -n "$identity" ]]; then
  codesign --verify --deep --strict dist/NovelBang.app
fi

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

if [[ -n "$identity" ]]; then
  codesign --sign "$identity" --timestamp "$dmg"
fi
if [[ ${#notary[@]} -gt 0 ]]; then
  # Apple checks the app inside the DMG too; this usually takes a few minutes.
  echo "Notarizing $dmg…"
  if ! xcrun notarytool submit "$dmg" "${notary[@]}" --wait --timeout 30m --output-format json > "$staging/notary.json"; then
    cat "$staging/notary.json" >&2
    id=$(plutil -extract id raw "$staging/notary.json" 2>/dev/null || true)
    [[ -n "$id" ]] && xcrun notarytool log "$id" "${notary[@]}" >&2
    exit 1
  fi
  status=$(plutil -extract status raw "$staging/notary.json")
  if [[ "$status" != "Accepted" ]]; then
    echo "Notarization: $status" >&2
    xcrun notarytool log "$(plutil -extract id raw "$staging/notary.json")" "${notary[@]}" >&2
    exit 1
  fi
  # Attach the ticket, so the DMG opens without a network check.
  xcrun stapler staple "$dmg"
  spctl --assess --type open --context context:primary-signature --verbose "$dmg"
fi

if [[ ${#notary[@]} -gt 0 ]]; then
  echo "Built dist/NovelBang.app and $dmg (signed and notarized)"
elif [[ -n "$identity" ]]; then
  echo "Built dist/NovelBang.app and $dmg (signed, not notarized)"
else
  echo "Built dist/NovelBang.app and $dmg (ad-hoc signed)"
fi
