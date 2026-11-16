#!/bin/sh
# Run as the logged-in user inside a disposable macOS VM, never with sudo.
set -eu
umask 077
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE=$(CDPATH= cd -- "$HERE/../.." && pwd)
MODE=${1:-install}
if [ "${2:-}" != --disposable-vm ]; then
    echo 'Usage: sh guest.sh install|repair|verify|live|visual-claude|visual-codex --disposable-vm' >&2
    exit 2
fi
if [ "$(uname -s)" != Darwin ] || [ "$(id -u)" = 0 ]; then
    echo 'Run as the logged-in, non-root macOS VM user.' >&2
    exit 2
fi
case "$(sysctl -n hw.model)" in
    VirtualMac*) ;;
    *) echo 'Refusing to install on a physical Mac.' >&2; exit 2 ;;
esac
# Avoid inheriting a developer shell's installation/state overrides.
unset PYTHONPATH PYTHONHOME SIDEPULSE_INSTALL_ROOT SIDEPULSE_BIN_DIR
unset XDG_STATE_HOME XDG_CONFIG_HOME XDG_DATA_HOME CODEX_HOME CLAUDE_CONFIG_DIR
export PATH="$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PYTHON="$HOME/.local/share/sidepulse/venv/bin/python"
REPORTS="$HOME/sidepulse-vm-results"
mkdir -p "$REPORTS"
case "$MODE" in
    install|repair)
        if [ "$MODE" = install ] && { [ -e "$HOME/.local/share/sidepulse" ] || [ -e "$HOME/Library/LaunchAgents/io.sidepulse.service.plist" ] || command -v sidepulse >/dev/null 2>&1; }; then
            echo 'SidePulse already exists. Restore the clean VM baseline for an install test; use verify/live to resume.' >&2
            exit 2
        fi
        if [ "$MODE" = repair ]; then
            if [ ! -x "$PYTHON" ]; then
                echo 'No existing SidePulse environment to repair; use install.' >&2
                exit 2
            fi
            echo 'Repairing an existing installation; this is not a clean-install test.'
        fi
        # Exercise the real bootstrap, including private Python on stock macOS.
        export SIDEPULSE_INSTALL_SPEC="$SOURCE"
        export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SIDEPULSE=0+vmtest
        if ! sh "$SOURCE/scripts/setup.sh" >"$REPORTS/$MODE.log" 2>&1; then
            printf '{"scope":"%s","live_agents":"not_run","failures":1,"results":[{"name":"installer","status":"failed","detail":"See %s.log"}]}\n' "$MODE" "$MODE" >"$REPORTS/$MODE-report.json"
            printf '<testsuite name="sidepulse-macos-%s" tests="1" failures="1"><testcase name="installer"><failure message="See %s.log"/></testcase></testsuite>\n' "$MODE" "$MODE" >"$REPORTS/$MODE-junit.xml"
            echo "Installer failed. See $REPORTS/$MODE.log" >&2
            exit 1
        fi
        printf '{"scope":"%s","live_agents":"not_run","failures":0,"results":[{"name":"installer","status":"passed","detail":"See %s.log; agent checks are reported separately"}]}\n' "$MODE" "$MODE" >"$REPORTS/$MODE-report.json"
        printf '<testsuite name="sidepulse-macos-%s" tests="1" failures="0"><testcase name="installer"/></testsuite>\n' "$MODE" >"$REPORTS/$MODE-junit.xml"
        "$PYTHON" "$HERE/check.py" --install-agents --output "$REPORTS"
        ;;
    verify) "$PYTHON" "$HERE/check.py" --output "$REPORTS" ;;
    live) "$PYTHON" "$HERE/check.py" --live --output "$REPORTS" ;;
    visual-claude) "$PYTHON" "$HERE/visual.py" record claude --output "$REPORTS" ;;
    visual-codex) "$PYTHON" "$HERE/visual.py" record codex --output "$REPORTS" ;;
    *) echo "Unknown mode: $MODE" >&2; exit 2 ;;
esac
