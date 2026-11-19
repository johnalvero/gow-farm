#!/usr/bin/env python3
"""
Game of War - Dashboard service helper
The HTTP and plist work for dashboard/service.sh, which bash does badly. Each
command prints one line. Imports only dashboard.launcher and the stdlib, and
keeps to Python 3.8 syntax, the oldest Python the dashboard runs on.
Usage: .venv/bin/python dashboard/service_helper.py probe PORT
       .venv/bin/python dashboard/service_helper.py live PORT
       .venv/bin/python dashboard/service_helper.py write-plist OUT PROJECT PORT [--allow-sleep] [--browsers-path X]
       .venv/bin/python dashboard/service_helper.py plist-port PLIST
"""

import json
import os
import plistlib
import re
import sys
import urllib.request

DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(DASHBOARD_DIR)
sys.path.insert(0, BASE_DIR)

from dashboard import launcher  # noqa: E402

LABEL = "com.godofwar.dashboard"
LIVE_TIMEOUT_SECONDS = 2
# launchd's own default, so the service finds nothing a shell profile added
SERVICE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# The script logs itself, so its file is 0600, which launchd can't promise
SERVICE_OUTPUT = "/dev/null"
THROTTLE_SECONDS = 30

USAGE = ("Usage: service_helper.py probe PORT | live PORT | "
         "write-plist OUT PROJECT PORT [--allow-sleep] [--browsers-path X] | plist-port PLIST")

class UsageError(Exception):
    """The command line can't be followed"""

def port_of(text):
    """The port as an int, or None if the text isn't a port number from 1 to 65535"""
    if isinstance(text, str) and re.fullmatch(r"[0-9]{1,5}", text) and 1 <= int(text) <= 65535:
        return int(text)
    return None

def port_argument(text):
    port = port_of(text)
    if port is None:
        raise UsageError(f"not a port: {text}")
    return port

def live(port):
    """yes or no as the dashboard's /api/state reports a live run, unknown when it can't be read"""
    # No proxy, as in launcher.probe: a system proxy would answer for this port
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = launcher.dashboard_url(port) + "api/state"
        with opener.open(url, timeout=LIVE_TIMEOUT_SECONDS) as response:
            state = json.loads(response.read())
        value = state.get("live") if isinstance(state, dict) else None
    except Exception:
        return "unknown"

    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"

def service_plist(project, port, allow_sleep, browsers_path):
    """The LaunchAgent: launchd runs service.sh, which logs, waits for the port and starts the server"""
    arguments = ["/bin/bash", os.path.join(project, "dashboard", "service.sh"), "run", "--port", str(port)]
    if allow_sleep:
        arguments.append("--allow-sleep")

    environment = {"PATH": SERVICE_PATH}
    if browsers_path:
        environment["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path

    # Standard, not Background: launchd would throttle the claim run's browsers
    return {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": project,
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": THROTTLE_SECONDS,
        "ProcessType": "Standard",
        "StandardOutPath": SERVICE_OUTPUT,
        "StandardErrorPath": SERVICE_OUTPUT,
    }

def write_plist(out, plist):
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    try:
        os.fchmod(fd, 0o644)
        f = os.fdopen(fd, "wb")
    except Exception:
        os.close(fd)
        raise
    with f:
        plistlib.dump(plist, f)

def plist_port(path):
    """The port after --port in an installed plist's ProgramArguments, or None"""
    with open(path, "rb") as f:
        plist = plistlib.load(f)

    arguments = plist.get("ProgramArguments") if isinstance(plist, dict) else None
    if not isinstance(arguments, list) or "--port" not in arguments:
        return None
    position = arguments.index("--port") + 1
    if position >= len(arguments):
        return None
    return port_of(arguments[position])

def write_plist_command(args):
    if len(args) < 3:
        raise UsageError("write-plist needs OUT PROJECT PORT")
    out, project, port = args[0], args[1], port_argument(args[2])
    if not os.path.isabs(project):
        raise UsageError("PROJECT must be an absolute path")

    allow_sleep = False
    browsers_path = ""
    rest = args[3:]
    while rest:
        option = rest.pop(0)
        if option == "--allow-sleep":
            allow_sleep = True
        elif option == "--browsers-path" and rest:
            browsers_path = rest.pop(0)
        else:
            raise UsageError(f"unknown option: {option}")

    write_plist(out, service_plist(project, port, allow_sleep, browsers_path))
    return out

def run(args):
    """What to print for the command line, raising UsageError if it can't be followed"""
    if not args:
        raise UsageError("no command")
    command, args = args[0], args[1:]

    if command == "probe" and len(args) == 1:
        return launcher.probe(port_argument(args[0]))
    if command == "live" and len(args) == 1:
        return live(port_argument(args[0]))
    if command == "write-plist":
        return write_plist_command(args)
    if command == "plist-port" and len(args) == 1:
        port = plist_port(args[0])
        if port is None:
            raise ValueError("no port in the service file")
        return str(port)
    raise UsageError(f"unknown command: {command}")

def main():
    try:
        line = run(sys.argv[1:])
    except UsageError:
        print(USAGE, file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    print(line, flush=True)

if __name__ == "__main__":
    main()
