#!/usr/bin/env python3
"""
Game of War - Local dashboard server
Serves the page on 127.0.0.1 and starts or stops one claim run at a time.
Usage: .venv/bin/python dashboard/server.py [--port N]
       .venv/bin/python dashboard/server.py --check
"""

import argparse
import hmac
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(DASHBOARD_DIR)
sys.path.insert(0, BASE_DIR)

from dashboard import events, network, store, updater  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
CLAIM_SCRIPT = os.path.join(BASE_DIR, "claim_all_characters.py")
SERVER_SCRIPT = os.path.join(DASHBOARD_DIR, "server.py")
CONCURRENCY_CHOICES = (1, 2, 3, 5)
MAX_BODY_BYTES = 1024
REQUEST_TIMEOUT_SECONDS = 10

START_TIMEOUT_SECONDS = 60
# Above the 35s cancel grace, the 30s browser close and the 5s Playwright stop
STOP_GRACE_SECONDS = 75
STOP_KILL_SECONDS = 10
MONITOR_SECONDS = 0.2
# Long enough for the update's answer to leave before the server stops
RESTART_DELAY_SECONDS = 0.2

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

POST_ROUTES = ("/api/run", "/api/retry", "/api/stop", "/api/update", "/api/update-check")
NO_FIELD_ROUTES = ("/api/stop", "/api/update", "/api/update-check")
UPDATES_OFF = "Update checks are off for this dashboard."
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}

def load_pages(token, server_id=""):
    """Read the three static files once: requests choose from this map, never from a path"""
    pages = {}
    for route, (name, content_type) in STATIC_FILES.items():
        with open(os.path.join(DASHBOARD_DIR, "static", name), "rb") as f:
            pages[route] = (f.read(), content_type)

    body, content_type = pages["/"]
    body = body.replace(b"__GOW_TOKEN__", token.encode()).replace(b"__GOW_SERVER_ID__", server_id.encode())
    pages["/"] = (body, content_type)
    return pages

def run_log_path():
    """Beside the event file, so a mock run can't overwrite a real run's log"""
    path = events.events_path() or events.DEFAULT_PATH
    return os.path.join(os.path.dirname(path), "last-run.log")

def open_run_log():
    path = run_log_path()
    events.private_dir(os.path.dirname(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return fd

class RunRefused(Exception):
    """A run control request that can't be done now, with the status and message to send"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message

class RunControl:
    """The only owner of the child process: one run at a time, stopped only by this server"""

    def __init__(self, event_store):
        self.store = event_store
        self.lock = threading.Lock()
        self.child = None
        self.phase = None
        self.error = None
        self.stop_deadline = None
        self.updating = False

    def state(self):
        with self.lock:
            return {"phase": self.phase, "owned": self.child is not None, "error": self.error}

    def command(self, concurrency, lines):
        arguments = [sys.executable, CLAIM_SCRIPT, "--concurrency", str(concurrency)]
        if lines:
            arguments += ["--lines", ",".join(str(line) for line in lines)]
        return arguments

    def spawn(self, arguments):
        log_fd = open_run_log()
        try:
            # stdout is a file, not a pipe: if this server died, a pipe would make
            # the run's next print fail mid-claim. A new session keeps Ctrl+C here
            # from reaching the run.
            return subprocess.Popen(
                arguments,
                shell=False,
                cwd=BASE_DIR,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={**os.environ, "GOW_RUN_SOURCE": "dashboard"}
            )
        finally:
            os.close(log_fd)

    def start(self, concurrency, retry=False):
        with self.lock:
            if self.updating:
                raise RunRefused(409, "The dashboard is updating")

            if self.child is not None:
                raise RunRefused(409, "A run is already going")

            state = self.store.snapshot()
            if state["live"]:
                raise RunRefused(409, "A run is already going")

            lines = state["retry_lines"] if retry else None
            if retry and not lines:
                raise RunRefused(409, "Nothing to retry")

            try:
                child = self.spawn(self.command(concurrency, lines))
            except OSError:
                raise RunRefused(500, "Run could not start")

            self.child = child
            self.phase = "starting"
            self.error = None
            self.stop_deadline = None

        threading.Thread(target=self.monitor, args=(child,), name="gow-monitor", daemon=True).start()

    def begin_update(self):
        """Hold off new runs while the code is replaced. Refused while any run is live"""
        with self.lock:
            if self.child is not None or self.store.snapshot()["live"]:
                raise RunRefused(409, "A claim run is going. Update after it finishes.")
            if self.updating:
                raise RunRefused(409, "The dashboard is updating")
            self.updating = True

    def end_update(self):
        """Only after a refused update: a done one stays set until the restart"""
        with self.lock:
            self.updating = False

    def stop(self):
        with self.lock:
            if self.child is None:
                raise RunRefused(409, "No run started here to stop")

            # One signal only: a second SIGINT would interrupt the teardown
            if self.phase != "stopping":
                self.phase = "stopping"
                self.stop_deadline = time.monotonic() + STOP_GRACE_SECONDS
                # To the process, not the group: the driver and browser follow it,
                # and a group signal would cut across the run's own teardown
                self.child.send_signal(signal.SIGINT)

    def signal_group(self, child, number):
        try:
            os.killpg(child.pid, number)
        except OSError:
            pass

    def run_appeared(self, child):
        try:
            return self.store.has_run_for_pid(child.pid)
        except Exception:
            return False

    def monitor(self, child):
        """Move the run through its phases, and force it down if it won't stop"""
        started = time.monotonic()
        kill_at = None

        while True:
            # Nothing may end this loop early: with no monitor the child is never
            # cleared, and Start stays refused until the server restarts
            try:
                code = child.poll()
                with self.lock:
                    now = time.monotonic()

                    if code is not None:
                        if self.phase == "starting" and not self.run_appeared(child):
                            self.error = f"Run could not start (exit {code})"
                        self.child = None
                        self.phase = None
                        self.stop_deadline = None
                        return

                    if self.phase == "starting":
                        if self.run_appeared(child) or now - started > START_TIMEOUT_SECONDS:
                            self.phase = "running"
                    elif self.phase == "stopping":
                        # The child is unreaped here, so its group id can't have been reused
                        if kill_at is None and now >= self.stop_deadline:
                            self.signal_group(child, signal.SIGTERM)
                            kill_at = now + STOP_KILL_SECONDS
                        elif kill_at is not None and now >= kill_at:
                            self.signal_group(child, signal.SIGKILL)
            except Exception:
                pass

            time.sleep(MONITOR_SECONDS)

class Handler(BaseHTTPRequestHandler):
    """Every request passes the same checks, and no URL ever becomes a file path"""

    server_version = "gow-dashboard"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_SECONDS

    def log_message(self, format, *args):
        pass

    def respond(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def respond_json(self, status, payload):
        self.respond(status, json.dumps(payload).encode())

    def refuse(self, status, message):
        self.respond_json(status, {"error": message})

    def send_error(self, code, message=None, explain=None):
        # The default page would go out without the security headers
        self.close_connection = True
        self.refuse(code, "Request refused")

    def single_header(self, name):
        """A header's value, or None if it is missing or sent twice"""
        values = self.headers.get_all(name) or []
        return values[0] if len(values) == 1 else None

    def allowed_values(self, scheme=""):
        port = self.server.server_address[1]
        return (f"{scheme}{HOST}:{port}", f"{scheme}localhost:{port}")

    def host_allowed(self):
        # Stops a website reaching this port through a DNS name it controls
        return self.single_header("Host") in self.allowed_values()

    def origin_allowed_if_sent(self):
        # Same-origin GETs send none, so only one that is sent can be judged
        origins = self.headers.get_all("Origin") or []
        return not origins or (len(origins) == 1 and origins[0] in self.allowed_values("http://"))

    def fetch_site_allowed(self):
        # This route carries account emails, so a request a browser marks as
        # cross-site doesn't get them at all, even if the browser would hide them.
        # Absent stays allowed, because only browsers send this header
        sites = self.headers.get_all("Sec-Fetch-Site") or []
        return not sites or (len(sites) == 1 and sites[0] in ("same-origin", "none"))

    def do_GET(self):
        if not self.host_allowed():
            return self.refuse(403, "Host not allowed")
        if self.path in POST_ROUTES:
            return self.refuse(405, "Method not allowed")
        if self.path == "/api/state":
            if not self.origin_allowed_if_sent() or not self.fetch_site_allowed():
                return self.refuse(403, "Origin not allowed")
            return self.respond_json(200, self.server.state())
        if self.path in self.server.pages:
            return self.respond(200, *self.server.pages[self.path])
        self.refuse(404, "Not found")

    def refuse_method(self):
        if not self.host_allowed():
            return self.refuse(403, "Host not allowed")
        self.refuse(405, "Method not allowed")

    do_HEAD = do_OPTIONS = do_PUT = do_DELETE = do_PATCH = do_TRACE = do_CONNECT = refuse_method

    def post_allowed(self):
        """Refuse anything not sent by the page this server gave out"""
        if self.single_header("Origin") not in self.allowed_values("http://"):
            self.refuse(403, "Origin not allowed")
            return False

        token = self.single_header("X-Gow-Token") or ""
        if not hmac.compare_digest(token.encode(), self.server.token.encode()):
            self.refuse(403, "Token not accepted")
            return False

        content_type = self.single_header("Content-Type") or ""
        if content_type.split(";")[0].strip().lower() != "application/json":
            self.refuse(415, "JSON required")
            return False

        return True

    def read_body(self):
        """The request's JSON object, or None once a refusal has been sent"""
        try:
            length = int(self.single_header("Content-Length") or "")
        except ValueError:
            self.refuse(400, "Body required")
            return None

        if length > MAX_BODY_BYTES:
            self.refuse(413, "Body too large")
            return None

        if length < 0:
            self.refuse(400, "Body required")
            return None

        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, RecursionError):
            self.refuse(400, "Invalid JSON")
            return None

        if type(body) is not dict:
            self.refuse(400, "JSON object required")
            return None

        return body

    def concurrency_of(self, body):
        """The chosen concurrency, or None once a refusal has been sent"""
        if set(body) != {"concurrency"}:
            self.refuse(400, "Only concurrency is accepted")
            return None

        value = body["concurrency"]
        if type(value) is not int or value not in CONCURRENCY_CHOICES:
            self.refuse(400, "Concurrency must be 1, 2, 3 or 5")
            return None

        return value

    def do_POST(self):
        if not self.host_allowed():
            return self.refuse(403, "Host not allowed")
        if self.path not in POST_ROUTES:
            return self.refuse(404, "Not found")
        if not self.post_allowed():
            return

        body = self.read_body()
        if body is None:
            return

        if self.path in NO_FIELD_ROUTES and body:
            return self.refuse(400, "No fields are accepted")

        control = self.server.control
        try:
            if self.path == "/api/stop":
                control.stop()
            elif self.path == "/api/update-check":
                if not self.server.updater.enabled:
                    raise RunRefused(409, UPDATES_OFF)
                self.server.updater.check_soon()
            elif self.path == "/api/update":
                return self.update()
            else:
                concurrency = self.concurrency_of(body)
                if concurrency is None:
                    return
                control.start(concurrency, retry=self.path == "/api/retry")
        except (RunRefused, updater.UpdateRefused) as e:
            return self.refuse(e.status, e.message)

        self.respond_json(200, {"ok": True})

    def update(self):
        """Fast-forward the project, answer, then restart this server into the new code"""
        if not self.server.updater.enabled:
            raise RunRefused(409, UPDATES_OFF)

        control = self.server.control
        control.begin_update()
        try:
            old, new = self.server.updater.apply()
        except BaseException:
            control.end_update()
            raise

        # The files are already new: the restart must follow even if the answer
        # can't be sent, or this server would keep refusing runs as updating
        try:
            # No URL and no git output: this goes to the service's log
            print(f"Updated from {old['commit']} to {new['commit']}; restarting", flush=True)
            self.respond_json(200, {"ok": True, "restarting": True, "version": new["commit"]})
            self.wfile.flush()
        finally:
            self.server.request_restart()

class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port):
        super().__init__((HOST, port), Handler)
        self.token = secrets.token_urlsafe(32)
        # Tells an open page that the server restarted. Unrelated to the token, and
        # no use to anyone: it only ever changes the page's own behaviour
        self.server_id = secrets.token_hex(8)
        self.pages = load_pages(self.token, self.server_id)
        self.store = store.EventStore()
        self.control = RunControl(self.store)
        self.network = network.NetworkMonitor(self.run_is_live)
        self.network.start()
        self.updater = updater.Updater()
        self.updater.start()
        self.restart = False

    def run_is_live(self):
        return self.control.state()["owned"] or self.store.any_live()

    def state(self):
        self.network.viewed()
        control = self.control.state()
        state = self.store.snapshot()
        if control["phase"] in ("starting", "stopping"):
            state["status"] = control["phase"]
        state["control"] = control
        state["network"] = self.network.status()
        state["update"] = self.updater.status()
        state["server_id"] = self.server_id
        return state

    def request_restart(self):
        """Stop serving soon, so main() can start the new code in this process.
        Never called from the serve thread: shutdown() waits for it"""
        self.restart = True

        def stop():
            time.sleep(RESTART_DELAY_SECONDS)
            self.shutdown()

        threading.Thread(target=stop, name="gow-restart", daemon=True).start()

    def handle_error(self, request, client_address):
        # A dropped connection is not news, and this output must stay free of requests
        pass

def restart(port):
    """Replace this process with the updated server on the same port. Same pid, same
    stdout and stderr, so caffeinate, launchd and the launcher all keep it. The
    listening socket is not inherited, and the port can be bound again at once"""
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execv(sys.executable, [sys.executable, SERVER_SCRIPT, "--port", str(port)])
    except OSError as e:
        print(f"Error: the dashboard could not restart after the update ({type(e).__name__})", flush=True)
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Local claim dashboard", allow_abbrev=False)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    # An update runs the new code's --check before restarting into it, so every
    # future version must keep this flag, and keep it from binding, starting or writing anything
    parser.add_argument("--check", action="store_true", help="check that the server can start, then exit")
    options = parser.parse_args()

    # A SIGINT ignored at launch would pass to the child, and Stop would do nothing
    signal.signal(signal.SIGINT, signal.default_int_handler)

    try:
        network.load_targets()
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    if options.check:
        load_pages("check")
        print("ok")
        return

    try:
        server = DashboardServer(options.port)
    except (OSError, OverflowError) as e:
        print(f"Error: could not listen on port {options.port}: {e}")
        sys.exit(1)

    port = server.server_address[1]
    print(f"Dashboard: http://{HOST}:{port}/", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Any run keeps going: it has its own session and writes to a file
        server.server_close()

    if server.restart:
        restart(port)

if __name__ == "__main__":
    main()
