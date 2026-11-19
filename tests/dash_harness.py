"""Runs dashboard/server.py with protections switched off, and spies at the process boundary.

Reads GOW_DASH_HARNESS (JSON). Only tests/dash_support.py starts this.
  spy_file            every spawn and signal is appended here, one JSON object per line
  no_host_check, no_origin_check, no_token_check    REQ9
  no_live_check       RUN5: the store never reports a live run to the run control
  all_retry_lines     RETRY7: retry_lines becomes every line of the run
  no_stop_signal      STOP7: Stop sends nothing
  stop_signal_group   STOP7: Stop signals the process group instead of the pid
  no_lock_test        LIVE5: "no run_end means live", with no look at the lock
  no_validate         UI8: the reader keeps events that fail the allowlist
  stdout_pipe         ISO7: the child's stdout is a pipe to this server
  child_harness       true: the child runs under script_harness.py, configured by GOW_TEST_HARNESS
  net_audit_file      record any non-loopback socket attempt here, and block it
  net_constants       {name: value} set on dashboard.network
  net_check_hangs     every check hangs, past any socket timeout
  no_net_stale        NET5: an old result never goes stale
  net_check_in_request  NET8: the check runs inside the request
  no_net_skip         NET19: the rule that skips the internet check under a mock is gone
  net_env_proxy       NET11: the checks use the environment's proxies
  thread_file         the highest live count of check threads is kept here
  no_fetch_site_check EP5: Sec-Fetch-Site is not looked at
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

config = json.loads(os.environ["GOW_DASH_HARNESS"])
LOOPBACK = (None, "", "127.0.0.1", "localhost", "::1", b"127.0.0.1", b"localhost")


def audit(name, arguments):
    """Before anything else loads: record a non-loopback attempt, and raise before it can leave"""
    if name == "socket.connect":
        address = arguments[1]
        if not isinstance(address, tuple):
            return
        host = address[0]
    elif name == "socket.getaddrinfo":
        host = arguments[0]
    else:
        return
    if host not in LOOPBACK:
        with open(config["net_audit_file"], "a") as f:
            f.write(f"{name} {host}\n")
        raise OSError("blocked by the test audit hook")


if config.get("net_audit_file"):
    sys.addaudithook(audit)

for name in ("GOW_BASE_URL", "GOW_USERS_FILE", "GOW_EVENTS_FILE"):
    if not os.environ.get(name):
        sys.exit(f"Refusing to run: {name} is not set")
if urlsplit(os.environ["GOW_BASE_URL"]).hostname != "127.0.0.1":
    sys.exit("Refusing to run: GOW_BASE_URL is not local")

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(TESTS_DIR)
sys.dont_write_bytecode = True
sys.path.insert(0, PROJECT)

from dashboard import events, network, server, store  # noqa: E402


def spy(**fields):
    with open(config["spy_file"], "a") as f:
        f.write(json.dumps(fields) + "\n")


real_popen = subprocess.Popen
real_send_signal = subprocess.Popen.send_signal
real_killpg = os.killpg
real_single_header = server.Handler.single_header


class SpyPopen(real_popen):
    def __init__(self, arguments, **kwargs):
        details = dict(argv=list(arguments), shell=kwargs.get("shell"), new_session=kwargs.get("start_new_session"),
                       source=kwargs.get("env", {}).get("GOW_RUN_SOURCE"),
                       stdout_is_file=isinstance(kwargs.get("stdout"), int))
        if config.get("stdout_pipe"):
            kwargs["stdout"] = subprocess.PIPE
        super().__init__(arguments, **kwargs)
        spy(kind="spawn", pid=self.pid, **details)

    def send_signal(self, number):
        spy(kind="signal", to="pid", pid=self.pid, number=int(number))
        if config.get("no_stop_signal"):
            return
        if config.get("stop_signal_group"):
            real_killpg(self.pid, number)
            return
        real_send_signal(self, number)


def killpg(group, number):
    spy(kind="signal", to="group", pid=group, number=int(number))
    real_killpg(group, number)


subprocess.Popen = SpyPopen
os.killpg = killpg


def single_header(self, name):
    port = self.server.server_address[1]
    if name == "Host" and config.get("no_host_check"):
        return f"127.0.0.1:{port}"
    if name == "Origin" and config.get("no_origin_check"):
        return f"http://127.0.0.1:{port}"
    if name == "X-Gow-Token" and config.get("no_token_check"):
        return self.server.token
    return real_single_header(self, name)


server.Handler.single_header = single_header


class StoreForControl:
    """What the run control sees of the store, with one protection removed"""

    def __init__(self, real):
        self.real = real

    def __getattr__(self, name):
        return getattr(self.real, name)

    def snapshot(self):
        state = self.real.snapshot()
        if config.get("no_live_check"):
            state["live"] = False
        if config.get("all_retry_lines"):
            state["retry_lines"] = sorted(row["line"] for row in state["accounts"])
        return state


real_init = server.DashboardServer.__init__


def dashboard_init(self, port):
    real_init(self, port)
    self.control.store = StoreForControl(self.store)


server.DashboardServer.__init__ = dashboard_init

if config.get("no_fetch_site_check"):
    server.Handler.fetch_site_allowed = lambda self: True
if config.get("no_lock_test"):
    events.lock_held = lambda path: True
if config.get("no_validate"):
    events.validate_event = lambda event: type(event) is dict and "event" in event and "run" in event
    store.events.validate_event = events.validate_event

if config.get("child_harness"):
    harness = os.path.join(TESTS_DIR, "script_harness.py")
    real_command = server.RunControl.command

    def command(self, concurrency, lines):
        arguments = real_command(self, concurrency, lines)
        return [arguments[0], "-B", harness, os.path.basename(arguments[1]), *arguments[2:]]

    server.RunControl.command = command

for name, value in config.get("net_constants", {}).items():
    setattr(network, name, value)

if config.get("net_check_hangs"):
    def hanging_probe(url, method, timeout):
        threading.Event().wait()

    network.probe = hanging_probe

if config.get("no_net_stale"):
    network.NetworkMonitor.is_stale = lambda self, result, interval, timeout, now: result.at is None

if config.get("net_check_in_request"):
    real_status = network.NetworkMonitor.status

    def status_after_a_check(self):
        self.check_internet()
        return real_status(self)

    network.NetworkMonitor.status = status_after_a_check

if config.get("no_net_skip"):
    real_load_targets = network.load_targets

    def load_targets():
        return network.DEFAULT_INTERNET_URL, real_load_targets()[1]

    network.load_targets = load_targets

if config.get("net_env_proxy"):
    class EnvironmentProxy(network.urllib.request.ProxyHandler):
        def __init__(self, proxies=None):
            super().__init__()

    network.urllib.request.ProxyHandler = EnvironmentProxy

if config.get("thread_file"):
    def watch_threads():
        highest = 0
        while True:
            live = sum(thread.name == "gow-network-check" for thread in threading.enumerate())
            if live > highest:
                highest = live
                with open(config["thread_file"], "w") as f:
                    f.write(str(highest))
            time.sleep(0.02)

    threading.Thread(target=watch_threads, name="gow-test-threads", daemon=True).start()

sys.argv = [os.path.join(PROJECT, "dashboard", "server.py")] + sys.argv[1:]
server.main()
