#!/bin/sh
set -eu

HEADLESS=${SIDEPULSE_SETUP_NONINTERACTIVE:-0}
SELECT_EJECT_GUARD=1
for option do
    case "$option" in
        --headless) HEADLESS=1 ;;
        --no-sd-eject-guard) SELECT_EJECT_GUARD=0 ;;
        -h|--help)
            printf 'Usage: sh setup.sh [--headless] [--no-sd-eject-guard]\n'
            printf '  --headless           Install the selected components without prompting.\n'
            printf '  --no-sd-eject-guard  Skip system-wide SD card ejection prevention.\n'
            exit 0
            ;;
        *)
            printf 'Unknown setup option: %s\n' "$option" >&2
            exit 2
            ;;
    esac
done

PYTHON_BIN=${PYTHON_BIN:-python3}
INSTALL_SPEC=${SIDEPULSE_INSTALL_SPEC:-"git+https://github.com/inteliwear/sidepulse.git"}
INSTALL_ROOT=${SIDEPULSE_INSTALL_ROOT:-"$HOME/.local/share/sidepulse"}
VENV=$INSTALL_ROOT/venv
BIN_DIR=${SIDEPULSE_BIN_DIR:-"$HOME/.local/bin"}
PLATFORM=$(uname -s)
hook_agents='codex claude grok cursor junie opencode'
default_agents="$hook_agents t3code"
AGENTS="$hook_agents herdr t3code"
MAC_COMPONENTS='sd-eject-guard status-bar'
if [ -n "${HERDR_BIN_PATH:-}" ] || command -v herdr >/dev/null 2>&1 ||
    [ -x "$HOME/.local/bin/herdr" ] || [ -x /opt/homebrew/bin/herdr ] || [ -x /usr/local/bin/herdr ]; then
    default_agents=$AGENTS
fi
selected_components=" $default_agents "
if [ "$PLATFORM" = Darwin ]; then
    selected_components="${selected_components}status-bar "
    if [ "$SELECT_EJECT_GUARD" -eq 1 ]; then
        selected_components="${selected_components}sd-eject-guard "
    fi
fi
terminal_settings=

component_selected() {
    case "$selected_components" in
        *" $1 "*) return 0 ;;
        *) return 1 ;;
    esac
}

draw_setup_title() {
    menu_focus=
    menu_checked=
    menu_heading=
    menu_reset=
    if [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != dumb ]; then
        menu_heading=$(printf '\033[1m')
        menu_reset=$(printf '\033[0m')
        case "${TERM:-}:${COLORTERM:-}" in
            *256color:*|*:truecolor|*:24bit)
                menu_focus=$(printf '\033[1;38;5;110m')
                menu_checked=$(printf '\033[38;5;108m')
                printf '\n\033[1mSide\033[38;5;174mP\033[38;5;179mu\033[38;5;108ml'
                printf '\033[38;5;110ms\033[38;5;140me%s\n' "$menu_reset"
                ;;
            *)
                menu_focus=$(printf '\033[1;36m')
                menu_checked=$(printf '\033[32m')
                printf '\n\033[1mSidePulse%s\n' "$menu_reset"
                ;;
        esac
    else
        printf '\nSidePulse\n'
    fi
}

draw_group() {
    printf '\n%s%.*s%s\n' "$menu_heading" "$menu_width" "$1" "$menu_reset"
    shift
    menu_lines=$((menu_lines + 2 + $#))
    for component do
        row_count=$((row_count + 1))
        pointer=' '
        checked=' '
        row_color=
        if [ "$row_count" -eq "$focused_row" ]; then
            pointer='>'
            focused_component=$component
            row_color=$menu_focus
        fi
        checkbox_color=$row_color
        if component_selected "$component"; then
            checked=x
            checkbox_color=${row_color:-$menu_checked}
        fi
        case "$component" in
            sd-eject-guard) label='SD card ejection prevention (system-wide, important for deep sleep)' ;;
            codex) label='Codex' ;;
            claude) label='Claude Code' ;;
            grok) label='Grok' ;;
            cursor) label='Cursor' ;;
            junie) label='Junie' ;;
            opencode) label='OpenCode' ;;
            herdr) label='Herdr' ;;
            t3code) label='T3 Code' ;;
            status-bar) label='Status-bar indicator' ;;
        esac
        prefix="  $pointer [$checked]"
        printf '%s%.*s%s' "$checkbox_color" "$menu_width" "$prefix" "$menu_reset"
        if [ "$menu_width" -gt "${#prefix}" ]; then
            printf '%s%.*s%s' "$row_color" "$((menu_width - ${#prefix}))" " $label" "$menu_reset"
        fi
        printf '\n'
    done
}

draw_checklist() {
    row_count=0
    menu_lines=4 # The Install row, key hints, and the blank lines above them.
    if [ "$PLATFORM" = Darwin ]; then
        draw_group 'macOS integrations' $MAC_COMPONENTS
    fi
    draw_group 'Agent hooks' $AGENTS
    row_count=$((row_count + 1))
    pointer=' '
    row_color=$menu_heading
    if [ "$focused_row" -eq 0 ] || [ "$focused_row" -eq "$row_count" ]; then
        focused_row=$row_count
        focused_component=install
        pointer='>'
        row_color=$menu_focus
    fi
    printf '\n%s%.*s%s\n' "$row_color" "$menu_width" "  $pointer Install" "$menu_reset"
    printf '\n%.*s\n' "$menu_width" 'Up/Down move  Space toggles  Enter on Install  q cancels'
}

menu_stty() {
    stty "$@" </dev/tty 2>/dev/null
}

restore_menu_terminal() {
    menu_stty "$terminal_settings"
}

select_components() {
    focused_row=0

    # Keep each row on one line so the checklist can redraw in place.
    terminal_size=$(menu_stty size) || terminal_size='0 80'
    terminal_columns=${terminal_size#* }
    [ "$terminal_columns" -gt 1 ] || terminal_columns=80
    menu_width=$((terminal_columns - 1))

    terminal_settings=$(menu_stty -g) || return 1
    trap restore_menu_terminal EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    menu_stty -echo -icanon icrnl min 1 time 0 || return 1

    escape=$(printf '\033')
    enter=$(printf '\n.')
    result=1
    draw_setup_title
    draw_checklist
    while :; do
        # The suffix preserves Enter and distinguishes it from terminal EOF.
        key=$(dd bs=1 count=1 2>/dev/null; printf .)
        case "$key" in
            "$escape".)
                # Arrow keys send Escape plus two bytes. Time out on a lone Escape.
                menu_stty min 0 time 2 || break
                arrow=$(dd bs=1 count=2 2>/dev/null)
                menu_stty min 1 time 0 || break
                case "$arrow" in
                    '[A'|OA) focused_row=$(( (focused_row + row_count - 2) % row_count + 1 )) ;;
                    '[B'|OB) focused_row=$(( focused_row % row_count + 1 )) ;;
                    *) continue ;;
                esac
                ;;
            ' .')
                [ "$focused_component" != install ] || continue
                if component_selected "$focused_component"; then
                    selected_components="${selected_components%%" $focused_component "*} ${selected_components#*" $focused_component "}"
                else
                    selected_components="$selected_components$focused_component "
                fi
                ;;
            "$enter")
                [ "$focused_component" = install ] || continue
                result=0
                break
                ;;
            q.|Q.|.) break ;;
            *) continue ;;
        esac
        printf '\033[%dA\r\033[J' "$menu_lines"
        draw_checklist
    done
    restore_menu_terminal || return 1
    trap - EXIT HUP INT TERM
    return "$result"
}

# In curl | sh, stdin holds the script. Read the choices from the terminal.
if [ "$HEADLESS" = 1 ]; then
    export PIP_NO_INPUT=1 GIT_TERMINAL_PROMPT=0
elif ( : </dev/tty ) 2>/dev/null; then
    if ! select_components </dev/tty >/dev/tty; then
        printf '\nSidePulse setup cancelled.\n'
        exit 0
    fi
fi

LOG_DIR=${XDG_STATE_HOME:-"$HOME/.local/state"}/sidepulse/setup
mkdir -p "$LOG_DIR"
SETUP_LOG=$(mktemp "$LOG_DIR/install.XXXXXX")
exec 3>&1 4>&2
exec >>"$SETUP_LOG" 2>&1

WHEEL_DIR=
SERVICE_STOPPED=0
STATUS_BAR_STOPPED=0
STATUS_BAR_PLIST="$HOME/Library/LaunchAgents/io.sidepulse.agentstatus.plist"
LAUNCH_DOMAIN=
INSTALL_TTY_MUTED=0

progress() {
    printf '[%s/5] %s…\n' "$1" "$2" >&3
    printf '\n[%s/5] %s\n' "$1" "$2"
}

print_shell_command() {
    separator='      '
    for argument do
        printf '%s' "$separator"
        case "$argument" in
            ''|*[!a-zA-Z0-9_./-]*)
                printf "'%s'" "$(printf '%s' "$argument" | sed "s/'/'\\\\''/g")"
                ;;
            *) printf '%s' "$argument" ;;
        esac
        separator=' '
    done
    printf '\n'
}

system_guard_sudo() {
    set -- "$VENV/bin/sidepulse" sdejectguard start --scope system
    if [ -n "$terminal_settings" ]; then
        printf '      Administrator access is required for system-wide SD card ejection prevention.\n' >&3
        set -- sudo -H -p '[SidePulse] Administrator password for %p: ' "$@"
        sudo_input=/dev/tty
    else
        set -- sudo -n -H "$@"
        sudo_input=/dev/null
    fi
    print_shell_command "$@" >&3
    "$@" <"$sudo_input"
}

resume_status_bar() {
    if ! launchctl print "$LAUNCH_DOMAIN/io.sidepulse.agentstatus" >/dev/null 2>&1; then
        launchctl bootstrap "$LAUNCH_DOMAIN" "$STATUS_BAR_PLIST" || return 1
    fi
    STATUS_BAR_STOPPED=0
}

cleanup() {
    result=$?
    trap - EXIT
    if [ "$INSTALL_TTY_MUTED" -eq 1 ]; then
        # Discard keys pressed during setup before returning control to the shell.
        if menu_stty -icanon min 0 time 0; then
            cat </dev/tty >/dev/null 2>&1 || :
        fi
        restore_menu_terminal || :
    fi
    if [ "$result" -ne 0 ]; then
        if [ "$SERVICE_STOPPED" -eq 1 ]; then
            "$VENV/bin/sidepulse" service start || printf 'Could not restart the background service.\n' >&2
        fi
        if [ "$STATUS_BAR_STOPPED" -eq 1 ]; then
            resume_status_bar || printf 'Could not restart the menu-bar app.\n' >&2
        fi
        printf '\nSidePulse setup failed. Recent output:\n' >&4
        tail -n 30 "$SETUP_LOG" >&4
        printf '\nFull log: %s\n' "$SETUP_LOG" >&4
    fi
    if [ -n "$WHEEL_DIR" ]; then
        rm -rf "$WHEEL_DIR" || :
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [ -n "$terminal_settings" ]; then
    INSTALL_TTY_MUTED=1
    menu_stty -echo || INSTALL_TTY_MUTED=0
fi

printf '\n' >&3
progress 1 'Checking system requirements'

need_apple_tools() {
    cat >&2 <<'EOF'
SidePulse setup needs Apple Command Line Tools.

1. Run: xcode-select --install
2. Complete Apple's installer and wait for installation to finish.
3. Run this SidePulse installer again.

If the tools are already installed, select them with:
  sudo xcode-select --switch /Library/Developer/CommandLineTools
For a full Xcode installation, select its Contents/Developer directory instead.
EOF
    exit 1
}

APPLE_TOOLS=1
if [ "$PLATFORM" = Darwin ]; then
    # Check before calling Apple shims, which can launch the tools installer.
    if ! xcode-select -p; then
        APPLE_TOOLS=0
        case "$INSTALL_SPEC" in
            git+*) [ "$(command -v git || :)" != /usr/bin/git ] || need_apple_tools ;;
        esac
    fi
    if component_selected sd-eject-guard; then
        [ "$APPLE_TOOLS" -eq 1 ] && xcrun --find clang || need_apple_tools
    fi
fi

usable_python() {
    python_path=$(command -v "$1") || return 1
    if [ "$APPLE_TOOLS" -eq 0 ] && [ "$python_path" = /usr/bin/python3 ]; then
        return 1
    fi
    "$python_path" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null
}

install_macos_python() (
    # Keep uv and its Python private to SidePulse. The unmanaged installer does
    # not edit shell profiles, and --no-bin avoids global Python symlinks.
    uv_bin=$INSTALL_ROOT/uv/uv
    if [ ! -x "$uv_bin" ]; then
        bootstrap_dir=$(mktemp -d "${TMPDIR:-/tmp}/sidepulse-python.XXXXXX")
        trap 'rm -rf "$bootstrap_dir"' EXIT
        curl -fsSL https://astral.sh/uv/install.sh -o "$bootstrap_dir/install-uv.sh" || exit 1
        UV_UNMANAGED_INSTALL="$INSTALL_ROOT/uv" sh "$bootstrap_dir/install-uv.sh" >&2 || exit 1
    fi
    printf 'Installing Python 3.13 for SidePulse…\n' >&2
    UV_PYTHON_INSTALL_DIR="$INSTALL_ROOT/python" "$uv_bin" python install \
        --no-config --no-bin 3.13 >&2 || exit 1
    UV_PYTHON_INSTALL_DIR="$INSTALL_ROOT/python" "$uv_bin" python find \
        --no-config --no-project --managed-python 3.13
)

progress 2 'Preparing the Python environment'

# Updates use the existing interpreter even if the shell's python3 is older.
if [ -x "$VENV/bin/python" ]; then
    if ! usable_python "$VENV/bin/python"; then
        printf 'The existing SidePulse environment needs Python 3.10 or newer: %s\n' "$VENV" >&2
        exit 1
    fi
    PYTHON_BIN=$VENV/bin/python
elif ! usable_python "$PYTHON_BIN"; then
    found_python=
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
        if usable_python "$candidate"; then
            found_python=$(command -v "$candidate")
            break
        fi
    done
    if [ -n "$found_python" ]; then
        PYTHON_BIN=$found_python
    elif [ "$PLATFORM" = Darwin ]; then
        if ! PYTHON_BIN=$(install_macos_python) || ! usable_python "$PYTHON_BIN"; then
            printf 'Could not install Python for SidePulse. Check your internet connection and try again.\n' >&2
            exit 1
        fi
    else
        printf 'SidePulse requires Python 3.10 or newer. Install it and run setup again.\n' >&2
        exit 1
    fi
fi

# Reuse a working environment on updates; recreating it can change the Python
# interpreter underneath an existing installation.
if [ ! -x "$VENV/bin/python" ]; then
    if ! "$PYTHON_BIN" -m venv "$VENV"; then
        printf 'Could not create a Python virtual environment.\n' >&2
        printf 'On Debian or Ubuntu, install python3-venv and run this command again.\n' >&2
        exit 1
    fi
fi

progress 3 'Downloading and building packages'
WHEEL_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sidepulse-setup.XXXXXX")

# Fetch and build everything before pausing services. GitHub/network/build
# failures leave the installed code and running services alone.
"$VENV/bin/python" -m pip wheel --wheel-dir "$WHEEL_DIR" "$INSTALL_SPEC"
set -- "$WHEEL_DIR"/sidepulse-*.whl
if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
    printf 'The build did not produce exactly one SidePulse wheel.\n' >&2
    exit 1
fi
SIDEPULSE_WHEEL=$1

progress 4 'Installing SidePulse and command links'

if [ -x "$VENV/bin/sidepulse" ] && "$VENV/bin/sidepulse" service status >/dev/null 2>&1; then
    "$VENV/bin/sidepulse" service stop
    SERVICE_STOPPED=1
fi
if [ "$PLATFORM" = Darwin ] && [ -f "$STATUS_BAR_PLIST" ]; then
    LAUNCH_DOMAIN="gui/$(id -u)"
    if launchctl print "$LAUNCH_DOMAIN/io.sidepulse.agentstatus" >/dev/null 2>&1; then
        launchctl bootout "$LAUNCH_DOMAIN" "$STATUS_BAR_PLIST"
        STATUS_BAR_STOPPED=1
    fi
fi

# --upgrade alone can retain old code when GitHub commits share a version.
# Force replacement, using only the wheels already downloaded above.
"$VENV/bin/python" -m pip install --upgrade --force-reinstall \
    --no-index --find-links "$WHEEL_DIR" "$SIDEPULSE_WHEEL"

mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/sidepulse" "$BIN_DIR/sidepulse"
ln -sf "$VENV/bin/sidepulse" "$BIN_DIR/sp"
# Remove only aliases owned by this installation, including broken symlinks.
for legacy in agent-monitor agent-status-bar; do
    if [ -L "$BIN_DIR/$legacy" ] && [ "$(readlink "$BIN_DIR/$legacy")" = "$VENV/bin/$legacy" ]; then
        rm "$BIN_DIR/$legacy"
    fi
done

progress 5 'Configuring the background service and selected components'
set -- setup
selected_agents=
for agent in $AGENTS; do
    if component_selected "$agent"; then
        selected_agents="$selected_agents $agent"
    fi
done
case "$selected_agents" in
    '') set -- "$@" --no-agent-monitor ;;
    " $default_agents") ;;
    *)
        for agent in $selected_agents; do
            set -- "$@" --agent "$agent"
        done
        ;;
esac
if [ "$PLATFORM" = Darwin ]; then
    # Run the rest of setup as the login user. Only the guard needs root.
    set -- "$@" --no-sd-eject-guard
    if component_selected sd-eject-guard; then
        printf '      Installing system-wide SD card ejection prevention…\n' >&3
        if [ "$(id -u)" -eq 0 ]; then
            "$VENV/bin/sidepulse" sdejectguard start --scope system
        else
            if ! system_guard_sudo; then
                printf 'System-wide SD card ejection prevention could not be installed.\n' >&2
                if [ -z "$terminal_settings" ]; then
                    printf 'Headless setup requires non-interactive sudo for the eject guard. Rerun in a terminal to authorize it.\n' >&2
                fi
                exit 1
            fi
            # Retire the old per-user guard only after the system job starts.
            "$VENV/bin/sidepulse" sdejectguard uninstall --scope user
        fi
    fi
    if ! component_selected status-bar; then
        set -- "$@" --no-status-bar
    fi
fi
"$VENV/bin/sidepulse" "$@"
if [ "$STATUS_BAR_STOPPED" -eq 1 ] && ! component_selected status-bar; then
    resume_status_bar
fi
"$VENV/bin/sidepulse" --version

printf '\nSidePulse is installed.\n' >&3
printf 'Command: %s\n' "$BIN_DIR/sidepulse" >&3
case ":${PATH:-}:" in
    *":$BIN_DIR:"*) ;;
    *) printf 'Add %s to PATH to run sidepulse directly.\n' "$BIN_DIR" >&3 ;;
esac
