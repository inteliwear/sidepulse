#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ARCH="$(uname -m)"
BUILD_DIR="$ROOT_DIR/build/macos-pkg"
DIST_DIR="$ROOT_DIR/dist"
VENV_DIR="$BUILD_DIR/venv"
APP_PATH="$BUILD_DIR/pyinstaller/SidePulse.app"
COMPONENT_PKG="$BUILD_DIR/SidePulse-component.pkg"
APP_ID="${APP_ID:-io.sidepulse.cli}"

APP_SIGN_IDENTITY="${APP_SIGN_IDENTITY:-}"
INSTALLER_SIGN_IDENTITY="${INSTALLER_SIGN_IDENTITY:-}"
NOTARY_PROFILE="${NOTARY_PROFILE:-}"
ALLOW_UNSIGNED="${ALLOW_UNSIGNED:-0}"
SIDEPULSE_VERSION="${SIDEPULSE_VERSION:-}"

if { [ -z "$APP_SIGN_IDENTITY" ] || [ -z "$INSTALLER_SIGN_IDENTITY" ]; } && [ "$ALLOW_UNSIGNED" != "1" ]; then
    echo "Set APP_SIGN_IDENTITY to a Developer ID Application identity and" >&2
    echo "INSTALLER_SIGN_IDENTITY to a Developer ID Installer identity." >&2
    exit 2
fi

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR" "$DIST_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
if [ -n "$SIDEPULSE_VERSION" ]; then
    export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SIDEPULSE="$SIDEPULSE_VERSION"
fi
"$VENV_DIR/bin/python" -m pip install 'pyinstaller>=6.10' "$ROOT_DIR"
VERSION="$("$VENV_DIR/bin/python" -c 'from importlib.metadata import version; print(version("sidepulse"))')"
OUTPUT_PKG="$DIST_DIR/SidePulse-${VERSION}-${ARCH}.pkg"

"$VENV_DIR/bin/pyinstaller" \
    --noconfirm --clean --windowed \
    --name SidePulse \
    --osx-bundle-identifier "$APP_ID" \
    --distpath "$BUILD_DIR/pyinstaller" \
    --workpath "$BUILD_DIR/work" \
    --specpath "$BUILD_DIR" \
    --collect-submodules Cocoa \
    --collect-data sidepulse.resources \
    "$ROOT_DIR/packaging/sidepulse_entry.py"

/usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$APP_PATH/Contents/Info.plist" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP_PATH/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$APP_PATH/Contents/Info.plist" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$APP_PATH/Contents/Info.plist"

if [ -n "$APP_SIGN_IDENTITY" ]; then
    codesign --force --deep --options runtime --timestamp \
        --entitlements "$ROOT_DIR/packaging/entitlements.plist" \
        --sign "$APP_SIGN_IDENTITY" "$APP_PATH"
    codesign --verify --deep --strict --verbose=2 "$APP_PATH"
else
    codesign --force --deep --sign - "$APP_PATH"
fi

chmod 755 "$ROOT_DIR/packaging/scripts/postinstall"
export COPYFILE_DISABLE=1
pkgbuild \
    --component "$APP_PATH" \
    --install-location /Applications \
    --identifier "$APP_ID" \
    --version "$VERSION" \
    --scripts "$ROOT_DIR/packaging/scripts" \
    "$COMPONENT_PKG"
if [ -n "$INSTALLER_SIGN_IDENTITY" ]; then
    productbuild --package "$COMPONENT_PKG" \
        --sign "$INSTALLER_SIGN_IDENTITY" \
        --timestamp "$OUTPUT_PKG"
    pkgutil --check-signature "$OUTPUT_PKG"
else
    productbuild --package "$COMPONENT_PKG" "$OUTPUT_PKG"
fi

if [ -n "$NOTARY_PROFILE" ] && [ -n "$INSTALLER_SIGN_IDENTITY" ]; then
    xcrun notarytool submit "$OUTPUT_PKG" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$OUTPUT_PKG"
    xcrun stapler validate "$OUTPUT_PKG"
else
    echo "Built package: $OUTPUT_PKG"
    if [ -n "$INSTALLER_SIGN_IDENTITY" ]; then
        echo "Set NOTARY_PROFILE to submit and staple it automatically."
    else
        echo "Local verification build only: this package is not Developer ID signed or notarized."
    fi
fi
