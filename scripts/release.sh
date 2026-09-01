#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
OUTPUT_DIR="$ROOT_DIR/dist"
RUN_TESTS=1
RELEASE_VERSION=""

usage() {
    cat <<'EOF'
Build a verified SidePulse Python release.

Usage: ./scripts/release.sh [options]

Options:
  --output-dir DIR  Write the wheel, source archive, and checksums to DIR.
                    Default: ./dist
  --version VALUE   Use VALUE instead of generating a calendar version.
                    VALUE must have the form 1.YYYYMMDD.SECONDS.
  --skip-tests      Skip the repository test suite. Artifact checks still run.
  -h, --help        Show this help.

Environment:
  PYTHON_BIN         Python 3.10+ interpreter to use. Default: python3

This command builds release files only. It does not tag, publish, or push.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --output-dir)
            if [ "$#" -lt 2 ]; then
                echo "release: --output-dir requires a directory" >&2
                exit 2
            fi
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --skip-tests)
            RUN_TESTS=0
            shift
            ;;
        --version)
            if [ "$#" -lt 2 ]; then
                echo "release: --version requires 1.YYYYMMDD.SECONDS" >&2
                exit 2
            fi
            RELEASE_VERSION="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "release: unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    echo "release: SidePulse requires Python 3.10 or newer" >&2
    exit 1
fi

if [ -z "$RELEASE_VERSION" ]; then
    case "${GITHUB_REF_NAME:-}" in
        v*) RELEASE_VERSION="${GITHUB_REF_NAME#v}" ;;
        *)
            RELEASE_VERSION="$("$PYTHON_BIN" <<'PY'
from datetime import datetime, timezone

now = datetime.now(timezone.utc)
seconds = now.hour * 3600 + now.minute * 60 + now.second
print(f"1.{now:%Y%m%d}.{seconds}")
PY
)"
            ;;
    esac
fi
if ! "$PYTHON_BIN" - "$RELEASE_VERSION" <<'PY'
from datetime import datetime
import sys

try:
    major, date, seconds = sys.argv[1].split(".")
    if major != "1" or len(date) != 8 or not date.isdigit():
        raise ValueError
    datetime.strptime(date, "%Y%m%d")
    if not seconds.isdigit() or str(int(seconds)) != seconds:
        raise ValueError
    if not 0 <= int(seconds) < 86400:
        raise ValueError
except (TypeError, ValueError):
    raise SystemExit(1)
PY
then
    echo "release: version must have the form 1.YYYYMMDD.SECONDS with a real UTC date" >&2
    exit 2
fi

# setuptools-scm normally reads this version from a Git tag. Providing the
# same value here lets a local preflight build happen before the tag exists.
export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SIDEPULSE="$RELEASE_VERSION"

temporary_dir="$(mktemp -d "${TMPDIR:-/tmp}/sidepulse-release.XXXXXX")"
trap 'rm -rf "$temporary_dir"' EXIT
build_venv="$temporary_dir/build-venv"
build_output="$temporary_dir/artifacts"
smoke_venv="$temporary_dir/smoke-venv"
mkdir -p "$build_output"

echo "SidePulse $RELEASE_VERSION release"
echo "Creating an isolated build environment..."
"$PYTHON_BIN" -m venv "$build_venv"
"$build_venv/bin/python" -m pip install --quiet --upgrade pip build twine

if [ "$RUN_TESTS" -eq 1 ]; then
    echo "Installing test dependencies..."
    "$build_venv/bin/python" -m pip install --quiet -e "$ROOT_DIR[test]"
    echo "Running tests..."
    "$build_venv/bin/python" -m pytest "$ROOT_DIR/tests" -q
else
    echo "Skipping repository tests."
fi

echo "Building wheel and source archive..."
"$build_venv/bin/python" -m build "$ROOT_DIR" --outdir "$build_output"

echo "Checking package metadata..."
"$build_venv/bin/python" -m twine check "$build_output"/*

wheel_path="$(find "$build_output" -maxdepth 1 -type f -name '*.whl' -print -quit)"
source_path="$(find "$build_output" -maxdepth 1 -type f -name '*.tar.gz' -print -quit)"
if [ -z "$wheel_path" ] || [ -z "$source_path" ]; then
    echo "release: the build did not produce both a wheel and source archive" >&2
    exit 1
fi

echo "Smoke-testing the built wheel..."
"$PYTHON_BIN" -m venv "$smoke_venv"
"$smoke_venv/bin/python" -m pip install --quiet --upgrade pip
"$smoke_venv/bin/python" -m pip install --quiet "$wheel_path"
(
    cd "$temporary_dir"
    "$smoke_venv/bin/python" -c \
        "import sidepulse; assert sidepulse.__version__ == '$RELEASE_VERSION'"
    "$smoke_venv/bin/sidepulse" --help >/dev/null
)

mkdir -p "$OUTPUT_DIR"
for artifact in "$wheel_path" "$source_path"; do
    destination="$OUTPUT_DIR/$(basename "$artifact")"
    if [ -e "$destination" ]; then
        echo "release: refusing to overwrite existing artifact: $destination" >&2
        exit 1
    fi
done

checksum_name="sidepulse-$RELEASE_VERSION-SHA256SUMS.txt"
checksum_path="$OUTPUT_DIR/$checksum_name"
if [ -e "$checksum_path" ]; then
    echo "release: refusing to overwrite existing checksums: $checksum_path" >&2
    exit 1
fi

cp "$wheel_path" "$source_path" "$OUTPUT_DIR/"
(
    cd "$OUTPUT_DIR"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$(basename "$wheel_path")" "$(basename "$source_path")"
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$(basename "$wheel_path")" "$(basename "$source_path")"
    else
        echo "release: need sha256sum or shasum to create checksums" >&2
        exit 1
    fi > "$checksum_name"
)

echo
echo "Release artifacts are ready:"
echo "  $OUTPUT_DIR/$(basename "$wheel_path")"
echo "  $OUTPUT_DIR/$(basename "$source_path")"
echo "  $checksum_path"
echo
echo "Nothing was published. To release these sources as this exact version:"
echo "  git tag v$RELEASE_VERSION"
echo "  git push origin v$RELEASE_VERSION"
