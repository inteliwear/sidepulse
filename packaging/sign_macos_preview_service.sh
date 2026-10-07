#!/bin/sh
set -eu

if [ "$(uname -s)" != Darwin ] || [ "$#" -ne 2 ]; then
    echo "Usage: sign_macos_preview_service.sh SERVICE_BINARY CODE_SIGNING_IDENTITY" >&2
    exit 2
fi

service_binary=$1
signing_identity=$2
if [ ! -f "$service_binary" ] || [ -z "$signing_identity" ]; then
    echo "Provide an existing service binary and a code signing identity." >&2
    exit 2
fi

# Keep this identifier and the certificate stable across local builds. macOS
# uses the designated requirement when retaining removable-volume consent.
/usr/bin/codesign --force --sign "$signing_identity" \
    --identifier io.sidepulse.next.service "$service_binary"
/usr/bin/codesign --verify --strict "$service_binary"
/usr/bin/codesign -d -r- "$service_binary" 2>&1
