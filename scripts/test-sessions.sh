#!/bin/sh
# Local session flows only. No production endpoints, push credentials, or real-device writes.
set -eu
if [ -z "${JAVA_HOME:-}" ] && [ -d /opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home ]; then
  JAVA_HOME=/opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home
  export JAVA_HOME
fi
cd "$(dirname "$0")/.."
cargo test --manifest-path ../sidepulse-bridge/Cargo.toml
cargo build --manifest-path ../sidepulse-bridge/Cargo.toml
SIDEPULSE_BRIDGE_BINARY="$PWD/../sidepulse-bridge/target/debug/sidepulse-bridge" \
  .venv/bin/python -m pytest -q tests/test_session_delivery_flow.py tests/test_program_status_bridge.py tests/test_program_hook_parity.py tests/test_codex_questions.py tests/test_daemon_runtime.py
swift test --package-path ../sidepulse-ios/SidePulse --filter ProgramStatusTests
xcodebuild -quiet -project ../sidepulse-ios/SidePulse/SidePulse.xcodeproj -scheme SidePulse \
  -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath /tmp/sidepulse-session-checks CODE_SIGNING_ALLOWED=NO build
(cd ../sidepulse-android && ./gradlew testDebugUnitTest --tests 'io.sidepulse.android.Program*Tests')
