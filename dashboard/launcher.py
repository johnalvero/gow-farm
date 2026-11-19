#!/usr/bin/env python3
"""
Game of War - Dashboard launcher
Makes sure the local dashboard answers when a claim run starts from a terminal,
starting it as a detached process if needed, then opens it in the browser.
Nothing here may fail, slow or change a claim: launch() never raises, and all
of the waiting happens on a daemon thread. Imports only events and the stdlib,
so the claim process never loads the dashboard's network checks.
"""

import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from dashboard import events

DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(DASHBOARD_DIR)

# The same values as server.py's, which this module must not import
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SERVER_SCRIPT = os.path.join(BASE_DIR, "dashboard", "server.py")
PROBE_TIMEOUT_SECONDS = 1
READY_TIMEOUT_SECONDS = 5
READY_POLL_SECONDS = 0.1
FINISH_WAIT_SECONDS = 8
LOG_NAME = "dashboard-server.log"

PORT_PROBLEM = "GOW_DASHBOARD_PORT must be a port number from 1 to 65535"
SERVER_NAME = "gow-dashboard"

# The last server this process started. Kept so it is never collected while
# running; it is never waited on or signalled
server_process = None

def port_setting():
    """The dashboard port as (port, problem): (None, None) when turned off with an empty value"""
    configured = os.environ.get("GOW_DASHBOARD_PORT")
    if configured is None:
        return DEFAULT_PORT, None
    if configured == "":
        return None, None

    # The problem never repeats the value, in case it was pasted from somewhere private
    if re.fullmatch(r"[0-9]{1,5}", configured) and 1 <= int(configured) <= 65535:
        return int(configured), None
    return None, PORT_PROBLEM

def dashboard_url(port):
    return f"http://{HOST}:{port}/"

def is_dashboard(headers):
    return (headers.get("Server") or "").startswith(SERVER_NAME)

def probe(port):
    """What answers on the port: dashboard, none (refused) or other"""
    # No proxy: a system proxy would answer for a port nothing is listening on.
    # GET / starts none of the dashboard's internet checks
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(dashboard_url(port), timeout=PROBE_TIMEOUT_SECONDS) as response:
            return "dashboard" if is_dashboard(response.headers) else "other"
    except urllib.error.HTTPError as e:
        try:
            answer = "dashboard" if is_dashboard(e.headers) else "other"
            e.close()
            return answer
        except Exception:
            return "other"
    except urllib.error.URLError as e:
        return "none" if isinstance(e.reason, ConnectionRefusedError) else "other"
    except ConnectionRefusedError:
        return "none"
    except Exception:
        return "other"

def server_log_path():
    """Beside the event file, as server.py keeps last-run.log"""
    return os.path.join(os.path.dirname(events.events_path()), LOG_NAME)

def open_server_log():
    path = server_log_path()
    events.private_dir(os.path.dirname(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return fd

def start_server(port):
    """Start the dashboard detached from this run, so it outlives it and Ctrl+C here can't reach it"""
    global server_process
    log_fd = open_server_log()
    try:
        # stdout is a file, not this terminal: the server's own lines stay out of
        # the claim output. close_fds keeps the run's event lock out of the server,
        # or the run would look live for as long as the server runs
        server_process = subprocess.Popen(
            [sys.executable, SERVER_SCRIPT, "--port", str(port)],
            shell=False,
            cwd=BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=dict(os.environ)
        )
    finally:
        os.close(log_fd)
    return server_process

def wait_until_ready(port, child):
    """True once the dashboard answers, False if it doesn't in time or its process ends first"""
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(READY_POLL_SECONDS)
        # The probe first: when two runs race, the loser's server exits but the
        # winner's answers, and both runs should report it
        if probe(port) == "dashboard":
            return True
        if child.poll() is not None:
            return False
    return False

def open_browser(opener, url):
    try:
        return bool(opener(url))
    except Exception:
        return False

def open_dashboard(log, port, opener):
    """Find or start the dashboard and open it, logging exactly one line. Returns the outcome"""
    url = dashboard_url(port)
    try:
        state = probe(port)
        if state == "other":
            log(f"Dashboard not opened: port {port} is in use by another program", "WARNING")
            return "port_taken"

        started = False
        if state == "none":
            child = start_server(port)
            if not wait_until_ready(port, child):
                log(f"Dashboard could not start, see {server_log_path()}. Claims continue.", "WARNING")
                return "start_failed"
            started = True

        if not open_browser(opener, url):
            log(f"Dashboard is at {url}, but the browser could not be opened", "WARNING")
            return "browser_failed"

        if started:
            log(f"Dashboard started: {url} (opened in your browser)")
            return "started"
        log(f"Dashboard: {url} (opened in your browser)")
        return "opened"
    except Exception as e:
        log(f"Dashboard not opened: {type(e).__name__}", "WARNING")
        return "error"

def interactive():
    """True when the run was started from a terminal, not cron or the dashboard"""
    try:
        return sys.stdin.isatty()
    except Exception:
        return False

def open_dashboard_quietly(log, port):
    try:
        # Looked up at call time, so a replaced webbrowser.open applies
        open_dashboard(log, port, lambda url: webbrowser.open(url))
    except Exception:
        pass

def launch(log, no_dashboard=False):
    """Open the dashboard on a daemon thread, returning the thread, or None when skipped"""
    try:
        if no_dashboard or os.environ.get("GOW_RUN_SOURCE") == "dashboard":
            return None
        # Cron runs would open a tab each, and with recording off there is nothing to show
        if not interactive() or events.events_path() is None:
            return None

        port, problem = port_setting()
        if problem:
            log(problem, "WARNING")
            return None
        if port is None:
            return None

        thread = threading.Thread(target=open_dashboard_quietly, args=(log, port), name="gow-dashboard", daemon=True)
        thread.start()
        return thread
    except Exception:
        return None

def finish(thread):
    """Give the dashboard a bounded time to open, so a short run still shows it"""
    try:
        if thread is not None:
            thread.join(FINISH_WAIT_SECONDS)
    except Exception:
        pass
