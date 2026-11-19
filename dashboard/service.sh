#!/bin/bash
# Game of War - Always-on dashboard
# Installs a LaunchAgent for this user that starts the dashboard at login and
# restarts it if it stops. Works with the bash that ships with macOS (3.2).
#
# Usage: bash dashboard/service.sh install [--allow-sleep] | uninstall | status
#   install        Start the dashboard now and at every login. It also keeps
#                  the Mac from idle-sleeping; --allow-sleep turns that off.
#   uninstall      Stop it and remove the service. Data and logs are kept.
#   status         Show whether it is installed, loaded and answering.
#   run --port N [--allow-sleep]
#                  Internal: what launchd starts. Logs to dashboard-service.log,
#                  waits while another program holds the port, then starts
#                  the server.
#
# GOW_DASHBOARD_PORT sets the port, as for the launcher (8765 when unset).
#
# Test-only overrides:
#   GOW_LAUNCH_AGENTS_DIR     Where the plist goes (default ~/Library/LaunchAgents)
#   GOW_LAUNCHCTL             The launchctl to call (default: the system's, see LAUNCHCTL)
#   GOW_PLUTIL                The plutil to check the plist with (default: the system's, see PLUTIL)
#   GOW_SERVICE_WAIT_SECONDS  How long install waits for the dashboard to answer (default 20)
#   GOW_SERVICE_POLL_SECONDS  How often run checks a taken port again (default 30)
#   GOW_SERVICE_UID           The user id checked for root (default: id -u)

set -u
unset CDPATH

LABEL="com.godofwar.dashboard"
DEFAULT_PORT=8765
LOG_NAME="dashboard-service.log"
MAX_LOG_BYTES=1048576
USAGE="Usage: bash dashboard/service.sh install [--allow-sleep] | uninstall | status"
PORT_PROBLEM="GOW_DASHBOARD_PORT must be a port number from 1 to 65535"

# launchd sets HOME for its agents, but a bare environment may not have it
if [ -z "${HOME:-}" ]; then
    HOME=~
    export HOME
fi

PROJECT="$(cd "$(dirname "$0")/.." && pwd -P)" || exit 1
PY="$PROJECT/.venv/bin/python"
HELPER="$PROJECT/dashboard/service_helper.py"
SERVER="$PROJECT/dashboard/server.py"

LAUNCHCTL="${GOW_LAUNCHCTL:-/bin/launchctl}"
PLUTIL="${GOW_PLUTIL:-/usr/bin/plutil}"
AGENTS_DIR="${GOW_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
PLIST="$AGENTS_DIR/$LABEL.plist"
DOMAIN="gui/$UID"
SERVICE="$DOMAIN/$LABEL"

# The service is given no GOW_EVENTS_FILE, so its data stays in the default place
SERVICE_DATA_DIR="$PROJECT/dashboard/data"
SERVICE_LOG="$SERVICE_DATA_DIR/$LOG_NAME"

WORK=""
trap 'if [ -n "$WORK" ]; then rm -rf "$WORK"; fi' EXIT

usage() {
    echo "$USAGE"
    exit 2
}

# Every refusal comes before any change
refuse() {
    echo "$1"
    echo "Nothing was changed."
    exit 1
}

seconds_setting() {
    # $1 is the value, $2 the default: a whole number of seconds, or the default
    case "$1" in
        ''|*[!0-9]*) echo "$2" ;;
        *) echo "$((10#$1))" ;;
    esac
}

valid_port() {
    # Prints the port as a number if $1 is one from 1 to 65535, as the launcher reads it
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
    esac
    if [ "${#1}" -gt 5 ]; then
        return 1
    fi
    local value=$((10#$1))
    if [ "$value" -lt 1 ] || [ "$value" -gt 65535 ]; then
        return 1
    fi
    echo "$value"
}

port_setting() {
    # Sets PORT from GOW_DASHBOARD_PORT, or sets PORT empty and PORT_ERROR and returns 1.
    # Unlike the launcher's, an empty value is a problem here, not "off"
    PORT_ERROR=""
    if [ -z "${GOW_DASHBOARD_PORT+set}" ]; then
        PORT=$DEFAULT_PORT
        return 0
    fi
    if [ -z "$GOW_DASHBOARD_PORT" ]; then
        PORT=""
        PORT_ERROR="GOW_DASHBOARD_PORT is set but empty. Unset it to use port $DEFAULT_PORT, or set it to a port number."
        return 1
    fi
    if ! PORT="$(valid_port "$GOW_DASHBOARD_PORT")"; then
        PORT=""
        PORT_ERROR="$PORT_PROBLEM"
        return 1
    fi
}

helper() {
    "$PY" -I -B "$HELPER" "$@"
}

probe() {
    # dashboard, none or other, or nothing when the helper can't run
    local state
    state="$(helper probe "$1" 2>/dev/null)"
    case "$state" in
        dashboard|none|other) echo "$state" ;;
    esac
}

live() {
    helper live "$1" 2>/dev/null
}

is_loaded() {
    "$LAUNCHCTL" print "$SERVICE" >/dev/null 2>&1
}

not_loaded() {
    ! is_loaded
}

job_pid() {
    "$LAUNCHCTL" print "$SERVICE" 2>/dev/null | sed -n 's/^[[:space:]]*pid = \([0-9][0-9]*\)[[:space:]]*$/\1/p' | head -n 1
}

listener_pid() {
    # Only the address the dashboard uses, so a program on [::1] isn't named
    /usr/sbin/lsof -nP -iTCP@127.0.0.1:"$1" -sTCP:LISTEN -t 2>/dev/null | head -n 1
}

command_of() {
    /bin/ps -o command= -p "$1" 2>/dev/null
}

parent_of() {
    /bin/ps -o ppid= -p "$1" 2>/dev/null | tr -d ' '
}

port_free() {
    [ "$(probe "$PORT")" = "none" ]
}

wait_for() {
    # Runs the command in $2... once a second until it succeeds, for up to $1 seconds
    local limit=$1
    local waited=0
    shift
    while ! "$@"; do
        if [ "$waited" -ge "$limit" ]; then
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
}

is_ours() {
    # True if listener $1 is the service's job $2 or one of its descendants
    local pid=$1
    local job=$2
    local level=0
    if [ -z "$pid" ] || [ -z "$job" ]; then
        return 1
    fi
    while [ "$level" -le 10 ]; do
        if [ "$pid" = "$job" ]; then
            return 0
        fi
        pid="$(parent_of "$pid")"
        case "$pid" in
            ''|0|1|*[!0-9]*) return 1 ;;
        esac
        level=$((level + 1))
    done
    return 1
}

is_project_server() {
    # True if command line $1 is this project's dashboard server on PORT, run by python
    local command=$1
    local suffix=" $PROJECT/dashboard/server.py --port $PORT"
    case "$command" in
        *"$suffix") ;;
        *) return 1 ;;
    esac
    local program="${command%% *}"
    case "${program##*/}" in
        python*|Python*) return 0 ;;
    esac
    return 1
}

under() {
    # True if path $1 is folder $2 or inside it
    case "$1/" in
        "$2"/*) return 0 ;;
    esac
    return 1
}

lower() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

in_protected_folder() {
    # Compared in lower case: macOS folder names ignore case, and pwd -P keeps
    # the case that was typed, so ~/desktop/proj is still on the Desktop
    local project
    local raw_home
    local home
    local folder
    project="$(lower "$PROJECT")"
    raw_home="$(lower "$HOME")"
    home="$(cd "$HOME" 2>/dev/null && pwd -P)" || home="$HOME"
    home="$(lower "$home")"
    for folder in desktop documents downloads; do
        if under "$project" "$raw_home/$folder" || under "$project" "$home/$folder"; then
            return 0
        fi
    done
    return 1
}

refuse_live() {
    refuse "A claim run is going right now, and stopping the dashboard would take it off the Stop button. Wait for it to finish, then try again."
}

refuse_if_live() {
    # Refuses if a dashboard answers on port $1 and reports a claim run going
    if [ -n "$1" ] && [ "$(probe "$1")" = "dashboard" ] && [ "$(live "$1")" = "yes" ]; then
        refuse_live
    fi
}

installed_port() {
    # The port in the installed plist, or nothing
    if [ -f "$PLIST" ]; then
        helper plist-port "$PLIST" 2>/dev/null
    fi
}

refuse_root() {
    # Under sudo the service would go to root's launchd, not this user's login,
    # and its files would belong to root
    local uid="${GOW_SERVICE_UID:-}"
    if [ -z "$uid" ]; then
        uid="$(/usr/bin/id -u)"
    fi
    if [ "$uid" = "0" ]; then
        refuse "Don't use sudo for this, and don't run it as root. Run it again as yourself, without sudo."
    fi
}

bootout_job() {
    if "$LAUNCHCTL" bootout "$SERVICE" >/dev/null 2>&1; then
        return 0
    fi
    "$LAUNCHCTL" unload "$PLIST" >/dev/null 2>&1
}

write_plist() {
    # Writes the plist for this install to $1
    set -- write-plist "$1" "$PROJECT" "$PORT"
    if [ "$ALLOW_SLEEP" = "yes" ]; then
        set -- "$@" --allow-sleep
    fi
    if [ -n "${PLAYWRIGHT_BROWSERS_PATH:-}" ]; then
        set -- "$@" --browsers-path "$PLAYWRIGHT_BROWSERS_PATH"
    fi
    helper "$@" >/dev/null 2>&1
}

check_python() {
    if [ ! -x "$PY" ]; then
        refuse "Can't find this project's Python at $PY. Set up the project on this Mac first (see README)."
    fi

    # This check runs even under Python 2, before the helper can be trusted
    local version
    local status
    version="$("$PY" -c 'import sys; v=sys.version_info[:2]; print("%d.%d" % v); sys.exit(0 if v >= (3, 8) else 3)' 2>/dev/null)"
    status=$?
    if [ "$status" -eq 3 ]; then
        refuse "This project's Python is $version, but the dashboard needs Python 3.8 or newer."
    fi
    if [ "$status" -ne 0 ]; then
        refuse "Can't run this project's Python at $PY. Set up the project on this Mac first (see README)."
    fi

    if ! "$PY" -I -c 'import playwright' >/dev/null 2>&1; then
        refuse "Playwright isn't installed in $PROJECT/.venv, so runs started from the dashboard would fail. Set up the project on this Mac first (see README)."
    fi
}

stop_foreign() {
    # Stops the project's own dashboard $1, which the user agreed to, or exits
    local pid=$1
    if ! is_project_server "$(command_of "$pid")"; then
        echo "The program on port $PORT changed, so it was not stopped. Run install again."
        exit 1
    fi
    kill -INT "$pid" 2>/dev/null
    if wait_for 10 port_free; then
        return 0
    fi
    if is_project_server "$(command_of "$pid")"; then
        kill -TERM "$pid" 2>/dev/null
        if wait_for 5 port_free; then
            return 0
        fi
    fi
    echo "Tried to stop the dashboard on port $PORT (process $pid), but could not stop it. Stop it yourself (kill $pid), then run install again."
    exit 1
}

cmd_install() {
    ALLOW_SLEEP="no"
    while [ $# -gt 0 ]; do
        case "$1" in
            --allow-sleep) ALLOW_SLEEP="yes" ;;
            *) usage ;;
        esac
        shift
    done

    refuse_root
    if [ ! -f "$SERVER" ]; then
        refuse "Can't find the dashboard at $SERVER."
    fi
    if in_protected_folder; then
        refuse "This project is in your Desktop, Documents or Downloads folder, where macOS stops services from reading files. Move the project folder into your home folder (for example ~/Apps/godofwar), then run install again from there."
    fi
    if ! port_setting; then
        refuse "$PORT_ERROR"
    fi
    check_python

    WORK="$(mktemp -d "${TMPDIR:-/tmp}/gow-service.XXXXXX")" || refuse "Can't make a temporary folder."
    if ! write_plist "$WORK/$LABEL.plist" || ! "$PLUTIL" -lint "$WORK/$LABEL.plist" >/dev/null 2>&1; then
        refuse "The service file failed its check."
    fi

    # Replacing the service stops its dashboard, which may be on another port
    if is_loaded; then
        local old_port
        old_port="$(installed_port)"
        if [ -n "$old_port" ] && [ "$old_port" != "$PORT" ]; then
            refuse_if_live "$old_port"
        fi
    fi

    # Who has the port now
    local stop_pid=""
    local state
    state="$(probe "$PORT")"
    case "$state" in
        '')
            refuse "Couldn't check port $PORT, because this project's Python couldn't run $HELPER."
            ;;
        none) ;;
        dashboard)
            if [ "$(live "$PORT")" = "yes" ]; then
                refuse_live
            fi
            local listener
            local job
            listener="$(listener_pid "$PORT")"
            job="$(job_pid)"
            if ! is_ours "$listener" "$job"; then
                local command=""
                if [ -n "$listener" ]; then
                    command="$(command_of "$listener")"
                fi
                if [ -n "$listener" ] && is_project_server "$command"; then
                    echo "This project's dashboard is already running on port $PORT, but not as the service:"
                    echo "  $listener $command"
                    echo "Stopping it stops only the dashboard. A claim run in progress keeps going."
                    printf "Stop it so the service can take over? [y/N] "
                    local answer=""
                    if ! read -r answer; then
                        answer=""
                        echo
                    fi
                    case "$answer" in
                        y|Y|yes|Yes|YES) stop_pid=$listener ;;
                        *) refuse "The dashboard was left running." ;;
                    esac
                elif [ -n "$listener" ]; then
                    refuse "A dashboard is already running on port $PORT (process $listener), but it wasn't started by this service. Stop it first (press Ctrl+C in its window, or run: kill $listener), then run install again."
                else
                    refuse "A dashboard is already running on port $PORT, but it wasn't started by this service. Stop it first (press Ctrl+C in its window), then run install again."
                fi
            fi
            ;;
        *)
            echo "Port $PORT is being used by another program, so the dashboard can't use it."
            local holder
            holder="$(listener_pid "$PORT")"
            if [ -n "$holder" ]; then
                echo "That program is: $(command_of "$holder")"
            fi
            refuse "Choose another port with GOW_DASHBOARD_PORT, or stop that program, then run install again."
            ;;
    esac

    # Changes start here
    if is_loaded; then
        bootout_job
        # launchd refuses a bootstrap while the old job is still going away
        wait_for 10 not_loaded
        wait_for 10 port_free
    fi
    if [ -n "$stop_pid" ]; then
        stop_foreign "$stop_pid"
    fi

    if ! mkdir -p "$AGENTS_DIR" ||
       ! cp "$WORK/$LABEL.plist" "$PLIST.tmp" ||
       ! chmod 0644 "$PLIST.tmp" ||
       ! mv -f "$PLIST.tmp" "$PLIST"; then
        rm -f "$PLIST.tmp"
        echo "Could not write the service file $PLIST."
        exit 1
    fi

    # enable clears a "disabled" mark an old unload -w may have left
    "$LAUNCHCTL" enable "$SERVICE" >/dev/null 2>&1
    if ! "$LAUNCHCTL" bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1 &&
       ! "$LAUNCHCTL" load -w "$PLIST" >/dev/null 2>&1; then
        rm -f "$PLIST"
        echo "macOS would not start the service. Nothing was installed."
        exit 1
    fi

    local limit
    local waited=0
    limit="$(seconds_setting "${GOW_SERVICE_WAIT_SECONDS:-}" 20)"
    while [ "$waited" -lt "$limit" ]; do
        sleep 1
        waited=$((waited + 1))
        if [ "$(probe "$PORT")" = "dashboard" ]; then
            echo "The dashboard is running: http://127.0.0.1:$PORT/"
            echo "It will now start by itself when you log in. Open that address in your browser to use it."
            exit 0
        fi
    done

    echo "The service is installed, but the dashboard is not answering yet."
    echo "Its log is $SERVICE_LOG."
    echo "To remove it: bash dashboard/service.sh uninstall"
    exit 1
}

cmd_uninstall() {
    if [ $# -gt 0 ]; then
        usage
    fi
    refuse_root

    local installed="no"
    local loaded="no"
    if [ -f "$PLIST" ]; then
        installed="yes"
    fi
    if is_loaded; then
        loaded="yes"
    fi
    if [ "$installed" = "no" ] && [ "$loaded" = "no" ]; then
        echo "The dashboard service is not installed."
        exit 0
    fi

    # The installed port, not the setting, is the one the service is on
    PORT="$(installed_port)" || PORT=""
    if [ -z "$PORT" ]; then
        port_setting || PORT=""
    fi
    refuse_if_live "$PORT"

    local stopped="yes"
    if [ "$loaded" = "yes" ]; then
        bootout_job || stopped="no"
    fi
    rm -f "$PLIST"

    if [ "$stopped" = "no" ]; then
        echo "The service file was removed, so the dashboard won't start at the next login, but macOS would not stop the running one. It stops when you log out or restart the Mac."
        exit 1
    fi
    echo "Removed. The dashboard will no longer start by itself. Your data and logs were kept in $SERVICE_DATA_DIR."
    exit 0
}

cmd_status() {
    if [ $# -gt 0 ]; then
        usage
    fi
    refuse_root

    local all="yes"
    PORT=""
    if [ -f "$PLIST" ]; then
        echo "Installed: yes ($PLIST)"
        PORT="$(helper plist-port "$PLIST" 2>/dev/null)" || PORT=""
    else
        echo "Installed: no"
        all="no"
    fi
    if [ -z "$PORT" ]; then
        port_setting || PORT=""
    fi

    if is_loaded; then
        echo "Loaded: yes"
    else
        echo "Loaded: no"
        all="no"
    fi

    local state=""
    if [ -n "$PORT" ]; then
        state="$(probe "$PORT")"
    fi
    case "$state" in
        dashboard) echo "Answering: yes, http://127.0.0.1:$PORT/" ;;
        other)
            echo "Answering: no - port $PORT is used by another program"
            all="no"
            ;;
        *)
            echo "Answering: no"
            all="no"
            ;;
    esac

    if [ "$all" = "yes" ]; then
        exit 0
    fi
    exit 1
}

cmd_run() {
    local allow_sleep="no"
    PORT=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --port)
                [ $# -ge 2 ] || usage
                PORT="$(valid_port "$2")" || usage
                shift
                ;;
            --allow-sleep) allow_sleep="yes" ;;
            *) usage ;;
        esac
        shift
    done
    [ -n "$PORT" ] || usage

    # The log and its folder are this user's only, whatever launchd's umask
    umask 077

    # Beside the event file, as the server keeps last-run.log
    local dir="$SERVICE_DATA_DIR"
    if [ -n "${GOW_EVENTS_FILE:-}" ]; then
        dir="$(dirname "$GOW_EVENTS_FILE")"
    fi
    local log="$dir/$LOG_NAME"
    if [ -L "$dir" ]; then
        echo "Error: $dir is a symlink" >&2
        exit 1
    fi
    if ! mkdir -p -m 0700 "$dir"; then
        echo "Error: could not make $dir" >&2
        exit 1
    fi
    if [ -L "$log" ]; then
        echo "Error: $log is a symlink" >&2
        exit 1
    fi
    if [ -f "$log" ] && [ "$(wc -c <"$log" | tr -d ' ')" -gt "$MAX_LOG_BYTES" ]; then
        mv -f "$log" "$log.1"
    fi
    exec >>"$log" 2>&1
    chmod 0600 "$log"

    # Exiting here while another program has the port, or while Python can't
    # run, would only restart this every ThrottleInterval, logging each time.
    # Each reason is logged once, when it starts
    local poll
    local state
    local reason
    local waiting_for=""
    poll="$(seconds_setting "${GOW_SERVICE_POLL_SECONDS:-}" 30)"
    while :; do
        state="$(probe "$PORT")"
        if [ "$state" = "none" ]; then
            break
        fi
        if [ -z "$state" ]; then
            reason="can't run this project's Python at $PY"
        else
            reason="port $PORT is in use by another program"
        fi
        if [ "$reason" != "$waiting_for" ]; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') $reason; waiting"
            waiting_for=$reason
        fi
        sleep "$poll"
    done

    if [ "$allow_sleep" = "yes" ]; then
        exec "$PY" "$SERVER" --port "$PORT"
    fi
    # caffeinate -i stops idle system sleep for as long as the server runs
    exec /usr/bin/caffeinate -i "$PY" "$SERVER" --port "$PORT"
}

if [ $# -eq 0 ]; then
    usage
fi
COMMAND=$1
shift
case "$COMMAND" in
    install) cmd_install "$@" ;;
    uninstall) cmd_uninstall "$@" ;;
    status) cmd_status "$@" ;;
    run) cmd_run "$@" ;;
    *) usage ;;
esac
