"""The always-on dashboard service: dashboard/service.sh and dashboard/service_helper.py.

Every case runs a copy of the script inside its own temp tree T, never the real one:
T/proj is a copy of the project, T/home is HOME, T/LaunchAgents is GOW_LAUNCH_AGENTS_DIR and
T/fake-launchctl stands in for launchctl, so the real launchctl and ~/Library/LaunchAgents are
never reached. service_env() refuses to build an environment that breaks any of that. Every
port is a free one the test picked, never the real dashboard's, and every data and log write
lands under T. At cleanup, only processes whose command line contains T are killed."""

import ast
import http.client
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock as unittest_mock

from dash_support import Stub
from gow_support import (
    PROJECT,
    PYTHON,
    Account,
    Mock,
    UnsafeRun,
    assert_safe_env,
    command_of,
    descendants,
    is_real_dashboard_port,
    users_text,
    wait_until,
)

from dashboard import launcher

LABEL = "com.godofwar.dashboard"
UID = os.getuid()
DOMAIN = f"gui/{UID}"
SERVICE = f"{DOMAIN}/{LABEL}"
SERVICE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
REAL_PORT = 8765
# The user's own dashboard: never signalled, whatever its command line
PROTECTED_PIDS = ("29443",)
TEMP_ROOT = os.path.realpath(tempfile.gettempdir())
REAL_HOME = os.path.expanduser("~")
REAL_AGENTS = os.path.realpath(os.path.join(REAL_HOME, "Library", "LaunchAgents"))
CHANGE_VERBS = ("enable", "bootstrap", "load", "bootout", "unload")
SCRIPT = os.path.join(PROJECT, "dashboard", "service.sh")
HELPER = os.path.join(PROJECT, "dashboard", "service_helper.py")
USAGE_START = "Usage: bash dashboard/service.sh install"
NOT_LOCAL = "http://127.0.0.1:9/en-us"
EMAIL = "service@x.test"
LOG_NAME = "dashboard-service.log"
MIB = 1048576
WAIT_LOOP = "    while :; do"

PYTHON_WRAPPER = f'#!/bin/sh\nexec {PYTHON} "$@"\n'
PYTHON_37 = "#!/bin/sh\necho 3.7\nexit 3\n"
PYTHON_BROKEN = "#!/bin/sh\necho 'dyld: Library not loaded' >&2\nexit 1\n"
NO_PLAYWRIGHT = ("#!/bin/sh\ncase \"$*\" in\n  *'import playwright'*)\n"
                 "    echo \"ModuleNotFoundError: No module named 'playwright'\" >&2\n    exit 1 ;;\nesac\n"
                 f'exec {PYTHON} "$@"\n')


PYTHON_SILENT = "#!/bin/sh\nexit 0\n"
PROBE_FAILS = ("#!/bin/sh\ncase \"$*\" in\n  *'service_helper.py probe'*)\n"
               "    echo 'Traceback: broken' >&2\n    exit 1 ;;\nesac\n"
               f'exec {PYTHON} "$@"\n')
CANT_RUN = "can't run this project's Python at"
IN_USE = "in use by another program"


def version_answer(version, code):
    """A python that gives this answer to the version check only, and passes all else to the real one"""
    return (f"#!/bin/sh\ncase \"$*\" in\n  *version_info*)\n    echo {version}\n    exit {code} ;;\nesac\n"
            f'exec {PYTHON} "$@"\n')


FAKE_LAUNCHCTL = r'''#!PYTHON -I
"""A stand-in for launchctl, confined to one test's temp tree"""
import json
import os
import plistlib
import signal
import subprocess
import sys
import time

T = TEMP
RECORD = os.path.join(T, "launchctl.jsonl")
STATE = os.path.join(T, "launchctl-state.json")
CONFIG = os.path.join(T, "launchctl-config.json")
SERVICE = "SERVICE_NAME"


def read(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(state):
    with open(STATE + ".tmp", "w") as f:
        json.dump(state, f)
    os.replace(STATE + ".tmp", STATE)


def command_of(pid):
    return subprocess.run(["/bin/ps", "-ww", "-o", "command=", "-p", str(pid)],
                          capture_output=True, text=True).stdout


def tree(pid):
    found, pending = [], [str(pid)]
    while pending:
        children = subprocess.run(["/usr/bin/pgrep", "-P", pending.pop()],
                                  capture_output=True, text=True).stdout.split()
        found += children
        pending += children
    return found


def stop(pid):
    """SIGTERM the job's tree, only processes whose command line shows this temp tree"""
    if not pid or int(pid) <= 1:
        return
    pids = [p for p in [str(pid)] + tree(pid) if T in command_of(p)]
    for p in pids:
        try:
            os.kill(int(p), signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(command_of(p).strip() for p in pids):
        time.sleep(0.05)


def spawn(path, config):
    with open(path, "rb") as f:
        plist = plistlib.load(f)
    arguments = plist["ProgramArguments"]
    if not arguments[1].startswith(T + "/") or "8765" in arguments:
        sys.exit(99)
    env = dict(plist.get("EnvironmentVariables", {}))
    env.update(config["spawn_env"])
    return subprocess.Popen(arguments, cwd=plist["WorkingDirectory"], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True).pid


args = sys.argv[1:]
with open(RECORD, "a") as f:
    f.write(json.dumps(args) + "\n")
config = read(CONFIG, {})
codes = config.get("codes", {})
state = read(STATE, {"loaded": False, "pid": None})
verb = args[0] if args else ""

if verb == "print":
    if not state["loaded"]:
        print(f'Could not find service "{SERVICE}" in domain', file=sys.stderr)
        sys.exit(113)
    print(f"{SERVICE} = {{")
    print("\tactive count = 1")
    if state["pid"] and command_of(state["pid"]).strip():
        print(f"\tpid = {state['pid']}")
    print("}")
    sys.exit(0)
if verb == "enable":
    sys.exit(codes.get("enable", 0))
if verb in ("bootstrap", "load"):
    if codes.get(verb, 0):
        sys.exit(codes[verb])
    if verb == "bootstrap" and state["loaded"]:
        # launchd's own answer to a second bootstrap of a loaded job
        sys.exit(5)
    pid = spawn(args[-1], config) if config.get("spawn") else None
    save({"loaded": True, "pid": pid})
    sys.exit(0)
if verb in ("bootout", "unload"):
    if codes.get(verb, 0):
        sys.exit(codes[verb])
    stop(state.get("pid"))
    save({"loaded": False, "pid": None})
    sys.exit(0)
sys.exit(64)
'''


# Every port a test here picked, so the end of the module can check nothing still serves on one
USED_PORTS = set()


def free_port():
    """A port nothing listens on, never the real dashboard's"""
    for _ in range(20):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        if port != REAL_PORT:
            USED_PORTS.add(port)
            return port
    raise AssertionError("no free port")


def probe(port):
    if int(port) == REAL_PORT:
        raise UnsafeRun("never the real dashboard's port")
    return launcher.probe(int(port))


def under(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def check_service_env(env, script, temp):
    """Refuse an install, uninstall or status run that could reach anything real"""
    if not under(temp, TEMP_ROOT):
        raise UnsafeRun(f"not a temp tree: {temp}")
    for name in ("GOW_LAUNCH_AGENTS_DIR", "HOME"):
        value = env.get(name)
        if not value:
            raise UnsafeRun(f"{name} is not set")
        if not under(value, temp):
            raise UnsafeRun(f"{name} is not under the temp tree: {value}")
    if under(env["GOW_LAUNCH_AGENTS_DIR"], REAL_AGENTS):
        raise UnsafeRun("GOW_LAUNCH_AGENTS_DIR is the real LaunchAgents")
    launchctl = env.get("GOW_LAUNCHCTL")
    if not launchctl or os.path.realpath(launchctl) != os.path.realpath(os.path.join(temp, "fake-launchctl")):
        raise UnsafeRun(f"GOW_LAUNCHCTL is not this test's fake: {launchctl}")
    plutil = env.get("GOW_PLUTIL")
    if plutil and plutil != "/usr/bin/plutil" and not under(plutil, temp):
        raise UnsafeRun(f"GOW_PLUTIL is neither plutil nor a fake: {plutil}")
    # Unset would mean the script's default, the real dashboard's port
    if "GOW_DASHBOARD_PORT" not in env:
        raise UnsafeRun("GOW_DASHBOARD_PORT is not set")
    if is_real_dashboard_port(env["GOW_DASHBOARD_PORT"]):
        raise UnsafeRun("GOW_DASHBOARD_PORT is the real dashboard's port")
    check_script(script, temp)
    events_file = env.get("GOW_EVENTS_FILE")
    if events_file and not under(events_file, temp):
        raise UnsafeRun(f"GOW_EVENTS_FILE is not under the temp tree: {events_file}")
    internet = env.get("GOW_INTERNET_CHECK_URL")
    if internet and not internet.startswith("http://127.0.0.1:"):
        raise UnsafeRun("GOW_INTERNET_CHECK_URL is not loopback")
    assert_safe_env(env)


def check_script(script, temp):
    if under(script, PROJECT):
        raise UnsafeRun(f"the script is the real one: {script}")
    if not under(script, temp):
        raise UnsafeRun(f"the script is not under the temp tree: {script}")


def check_run_env(env, script, temp, port):
    """Refuse a `run` that could serve on the real port or reach the real site"""
    if is_real_dashboard_port(str(port)):
        raise UnsafeRun("run on the real dashboard's port")
    check_script(script, temp)
    if not env.get("GOW_INTERNET_CHECK_URL", "").startswith("http://127.0.0.1:"):
        raise UnsafeRun("GOW_INTERNET_CHECK_URL is not loopback")
    events_file = env.get("GOW_EVENTS_FILE")
    if events_file and not under(events_file, temp):
        raise UnsafeRun(f"GOW_EVENTS_FILE is not under the temp tree: {events_file}")
    if "GOW_DASHBOARD_PORT" not in env:
        raise UnsafeRun("GOW_DASHBOARD_PORT is not set")
    assert_safe_env(env)


def service_env(svc, script=None, **changes):
    """The environment for S install, uninstall or status. A value of None unsets that name"""
    env = dict(os.environ)
    for name in ("GOW_EVENTS_FILE", "GOW_TEST_HARNESS", "GOW_RUN_SOURCE", "PLAYWRIGHT_BROWSERS_PATH", "GOW_PLUTIL",
                 "GOW_SERVICE_WAIT_SECONDS", "GOW_SERVICE_POLL_SECONDS", "GOW_NET_INTERVAL_SECONDS",
                 "GOW_SERVICE_UID"):
        env.pop(name, None)
    env.update(
        HOME=svc.home,
        GOW_LAUNCH_AGENTS_DIR=svc.agents,
        GOW_LAUNCHCTL=svc.launchctl,
        GOW_DASHBOARD_PORT=str(svc.port),
        GOW_BASE_URL=NOT_LOCAL,
        GOW_USERS_FILE=svc.users_file,
        GOW_INTERNET_CHECK_URL=svc.stub.url,
        PYTHONDONTWRITEBYTECODE="1",
    )
    for name, value in changes.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    check_service_env(env, script or svc.script, svc.temp)
    return env


def listener_pid(port):
    """The listener on 127.0.0.1:port, the address the dashboard uses"""
    out = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP@127.0.0.1:{port}", "-sTCP:LISTEN", "-t"],
                         capture_output=True, text=True).stdout.split()
    return out[0] if out else None


def tree(pid):
    return [str(pid)] + descendants(pid)


def all_processes():
    out = subprocess.run(["/bin/ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        found.append((pid, command.strip()))
    return found


def mode_of(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


class Listener:
    """A plain TCP listener the test owns: accepts nothing, sends no header"""

    def __init__(self, case, port):
        if int(port) == REAL_PORT:
            raise UnsafeRun("never the real dashboard's port")
        self.port = port
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", port))
        self.socket.listen(64)
        case.addCleanup(self.close)

    def is_open(self):
        if self.socket.fileno() == -1:
            return False
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=2).close()
            return True
        except OSError:
            return False

    def close(self):
        self.socket.close()


class V6Listener:
    """A plain listener on [::1]:P only, owned by the test"""

    def __init__(self, case, port):
        if int(port) == REAL_PORT:
            raise UnsafeRun("never the real dashboard's port")
        if not socket.has_ipv6:
            case.skipTest("no IPv6")
        self.socket = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            self.socket.bind(("::1", port))
            self.socket.listen(16)
        except OSError as e:
            self.socket.close()
            case.skipTest(f"can't bind IPv6 loopback: {e}")
        case.addCleanup(self.socket.close)


class LiveStubHandler(BaseHTTPRequestHandler):
    server_version = "gow-dashboard-stub"
    sys_version = ""

    def log_message(self, *args):
        pass

    def do_GET(self):
        live = self.server.live
        body = json.dumps({"live": live}).encode() if self.path == "/api/state" else b"<!doctype html>"
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if self.path == "/api/state" else "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LiveDashboardStub:
    """Answers like a dashboard with a live run, on a port the test chose"""

    def __init__(self, case, port, live=True):
        if int(port) == REAL_PORT:
            raise UnsafeRun("never the real dashboard's port")
        self.server = ThreadingHTTPServer(("127.0.0.1", port), LiveStubHandler)
        self.server.live = live
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        case.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Service:
    """One temp tree T: a project copy, a fake launchctl, a temp HOME and LaunchAgents dir"""

    def __init__(self, case, where=("proj",), python=PYTHON_WRAPPER, spawn=False, codes=None, base_url=NOT_LOCAL):
        self.case = case
        self.temp = os.path.realpath(tempfile.mkdtemp(prefix="gow-svc-"))
        case.addCleanup(self.clean_up)
        self.proj = os.path.join(self.temp, *where)
        self.script = os.path.join(self.proj, "dashboard", "service.sh")
        self.venv_python = os.path.join(self.proj, ".venv", "bin", "python")
        self.home = os.path.join(self.temp, "home")
        self.agents = os.path.join(self.temp, "LaunchAgents")
        self.plist = os.path.join(self.agents, f"{LABEL}.plist")
        self.record = os.path.join(self.temp, "launchctl.jsonl")
        self.state_file = os.path.join(self.temp, "launchctl-state.json")
        self.launchctl = os.path.join(self.temp, "fake-launchctl")
        self.data = os.path.join(self.temp, "data")
        self.events_file = os.path.join(self.data, "events.jsonl")
        self.users_file = os.path.join(self.temp, "fake-accounts")
        self.port = free_port()
        self.stub = Stub(case)
        self.outputs = 0
        self.children = []

        os.makedirs(self.home)
        with open(self.users_file, "w", encoding="utf-8") as f:
            f.write("")
        copy_project(self.proj, python)
        with open(self.launchctl, "w") as f:
            f.write(FAKE_LAUNCHCTL.replace("PYTHON", PYTHON).replace("TEMP", repr(self.temp))
                    .replace("SERVICE_NAME", SERVICE))
        os.chmod(self.launchctl, 0o755)
        self.configure(spawn=spawn, codes=codes, base_url=base_url)

    def configure(self, spawn=False, codes=None, base_url=NOT_LOCAL):
        spawn_env = {
            "HOME": self.home,
            "GOW_BASE_URL": base_url,
            "GOW_USERS_FILE": self.users_file,
            "GOW_EVENTS_FILE": self.events_file,
            "GOW_INTERNET_CHECK_URL": self.stub.url,
            "GOW_DASHBOARD_PORT": str(self.port),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        assert_safe_env(spawn_env)
        with open(os.path.join(self.temp, "launchctl-config.json"), "w") as f:
            json.dump({"spawn": spawn, "codes": codes or {}, "spawn_env": spawn_env}, f)

    def set_loaded(self, pid=None):
        with open(self.state_file, "w") as f:
            json.dump({"loaded": True, "pid": pid}, f)

    def env(self, **changes):
        return service_env(self, **changes)

    def run(self, *args, env=None, stdin=None, timeout=90, script=None, cwd=None):
        script = script or self.script
        env = env if env is not None else self.env()
        check_service_env(env, script, self.temp)
        return subprocess.run(["/bin/bash", script, *args], cwd=cwd or self.temp, env=env,
                              input=stdin if stdin is not None else "", capture_output=True, text=True,
                              timeout=timeout)

    def run_env(self, events_file="default", home=None, **changes):
        env = {
            "HOME": home or self.home,
            "PATH": SERVICE_PATH,
            "GOW_BASE_URL": NOT_LOCAL,
            "GOW_USERS_FILE": self.users_file,
            "GOW_INTERNET_CHECK_URL": self.stub.url,
            "GOW_DASHBOARD_PORT": str(self.port),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if events_file == "default":
            env["GOW_EVENTS_FILE"] = self.events_file
        elif events_file is not None:
            env["GOW_EVENTS_FILE"] = events_file
        env.update(changes)
        return env

    def start_run(self, *flags, env, port=None, umask=None, script=None):
        """S run --port P, as launchd starts it, in its own session"""
        port = port or self.port
        script = script or self.script
        check_run_env(env, script, self.temp, port)
        self.outputs += 1
        output = os.path.join(self.temp, f"run-{self.outputs}.out")
        preexec = (lambda: os.umask(umask)) if umask is not None else None
        with open(output, "wb") as out:
            process = subprocess.Popen(["/bin/bash", script, "run", "--port", str(port), *flags],
                                       cwd=self.temp, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                       stderr=subprocess.STDOUT, start_new_session=True, preexec_fn=preexec)
        process.output_file = output
        self.children.append(process)
        return process

    def start_dashboard(self, command, cwd, data_name="manual"):
        """A dashboard the test starts by hand, as the user or the launcher would"""
        env = dict(os.environ)
        for name in ("GOW_TEST_HARNESS", "GOW_RUN_SOURCE", "GOW_NET_INTERVAL_SECONDS"):
            env.pop(name, None)
        env.update(GOW_BASE_URL=NOT_LOCAL, GOW_USERS_FILE=self.users_file,
                   GOW_EVENTS_FILE=os.path.join(self.temp, data_name, "events.jsonl"),
                   GOW_INTERNET_CHECK_URL=self.stub.url, GOW_DASHBOARD_PORT=str(self.port),
                   PYTHONDONTWRITEBYTECODE="1")
        if self.port == REAL_PORT or str(self.port) not in command:
            raise UnsafeRun("a hand-started dashboard must use the test's port")
        check_run_env(env, os.path.join(cwd, "x"), self.temp, self.port)
        output = os.path.join(self.temp, f"{data_name}-server.out")
        with open(output, "wb") as out:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        self.children.append(process)
        wait_until(lambda: probe(self.port) == "dashboard", 20, "the hand-started dashboard")
        self.case.assertEqual(listener_pid(self.port), str(process.pid), "the fixture's listener is not its pid")
        return process

    def calls(self):
        if not os.path.exists(self.record):
            return []
        with open(self.record) as f:
            return [json.loads(line) for line in f if line.strip()]

    def job_pid(self):
        with open(self.state_file) as f:
            return json.load(f)["pid"]

    def load_plist(self):
        with open(self.plist, "rb") as f:
            return plistlib.load(f)

    def server_suffix(self, port=None, proj=None):
        return f"{proj or self.proj}/dashboard/server.py --port {port or self.port}"

    def server_processes(self):
        """This copy's dashboard servers on P, not counting the caffeinate that wraps one"""
        suffix = self.server_suffix()
        return [pid for pid, command in all_processes()
                if command.endswith(suffix) and not command.startswith("/usr/bin/caffeinate")]

    def assert_no_change(self, result=None):
        detail = f"\n{result.stdout}\n{result.stderr}" if result is not None else ""
        self.case.assertTrue(not os.path.exists(self.agents) or os.listdir(self.agents) == [],
                             f"LaunchAgents changed{detail}")
        changes = [call for call in self.calls() if call and call[0] in CHANGE_VERBS]
        self.case.assertEqual(changes, [], f"launchctl was asked to change something{detail}")
        self.case.assertFalse(os.path.exists(os.path.join(self.proj, "dashboard", "data")),
                              f"the project's data dir was made{detail}")

    def processes(self):
        return [pid for pid, command in all_processes()
                if self.temp in command and int(pid) != os.getpid() and pid not in PROTECTED_PIDS
                and "--port 8765" not in command]

    def clean_up(self):
        """Kill only what this test started: its own unreaped children, then every process whose
        command line shows T. A child started by relative path shows no T, so it goes by its Popen"""
        for child in self.children:
            if child.poll() is None and str(child.pid) not in PROTECTED_PIDS and child.pid > 1:
                child.kill()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            pids = self.processes()
            if not pids:
                break
            for pid in pids:
                if int(pid) > 1 and self.temp in command_of(pid):
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            time.sleep(0.1)
        for child in self.children:
            try:
                child.wait(5)
            except subprocess.TimeoutExpired:
                pass
        remaining = self.processes()
        shutil.rmtree(self.temp, ignore_errors=True)
        self.case.assertEqual(remaining, [], "processes from this test were left running")


def copy_project(proj, python):
    os.makedirs(os.path.join(proj, "dashboard"))
    for name in ("claim_all_characters.py", "claim_common.py"):
        shutil.copy(os.path.join(PROJECT, name), proj)
    for name in ("__init__.py", "events.py", "launcher.py", "network.py", "server.py", "store.py",
                 "service_helper.py", "service.sh"):
        shutil.copy(os.path.join(PROJECT, "dashboard", name), os.path.join(proj, "dashboard"))
    shutil.copytree(os.path.join(PROJECT, "dashboard", "static"), os.path.join(proj, "dashboard", "static"))
    if python is not None:
        os.makedirs(os.path.join(proj, ".venv", "bin"))
        path = os.path.join(proj, ".venv", "bin", "python")
        with open(path, "w") as f:
            f.write(python)
        os.chmod(path, 0o755)


class ServiceCase(unittest.TestCase):
    maxDiff = None

    def service(self, **kwargs):
        return Service(self, **kwargs)

    def install(self, svc, *args, **kwargs):
        result = svc.run("install", *args, **kwargs)
        return result

    def assert_ok(self, result, code=0):
        self.assertEqual(result.returncode, code, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")


class Preflight(ServiceCase):
    def assert_refused(self, svc, result, *texts):
        self.assert_ok(result, 1)
        for text in texts:
            self.assertIn(text, result.stdout)
        self.assertIn("Nothing was changed.", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("Nothing was changed."), result.stdout)
        svc.assert_no_change(result)

    def test_c1_no_python(self):
        svc = self.service(python=None)
        result = self.install(svc)
        self.assert_refused(svc, result, "Can't find this project's Python")

    def test_c1_edge_python_not_executable(self):
        svc = self.service()
        os.chmod(svc.venv_python, 0o644)
        self.assert_refused(svc, self.install(svc), "Can't find this project's Python")

    def test_c2_python_37(self):
        svc = self.service(python=PYTHON_37)
        result = self.install(svc)
        self.assert_refused(svc, result, "is 3.7", "needs Python 3.8 or newer")

    def test_c2_edge_python_that_cannot_run(self):
        svc = self.service(python=PYTHON_BROKEN)
        result = self.install(svc)
        self.assert_refused(svc, result, "Can't run this project's Python")
        self.assertNotIn("needs Python 3.8", result.stdout)

    def test_c3_no_playwright(self):
        svc = self.service(python=NO_PLAYWRIGHT)
        result = self.install(svc)
        self.assert_refused(svc, result, "Playwright isn't installed")

    def test_c4_port_setting(self):
        svc = self.service()
        result = self.install(svc, env=svc.env(GOW_DASHBOARD_PORT=""))
        self.assert_refused(svc, result, "GOW_DASHBOARD_PORT")
        for value in ("abc", "0", "70000"):
            with self.subTest(value):
                result = self.install(svc, env=svc.env(GOW_DASHBOARD_PORT=value))
                self.assert_refused(svc, result, "GOW_DASHBOARD_PORT must be a port number from 1 to 65535")

    def test_c4_edge_more_bad_ports(self):
        svc = self.service()
        for value in ("65536", "-1", " 80", "1e3", "123456", "٣٤", "80 ", "0x50"):
            with self.subTest(repr(value)):
                result = self.install(svc, env=svc.env(GOW_DASHBOARD_PORT=value))
                self.assert_refused(svc, result, "GOW_DASHBOARD_PORT must be a port number from 1 to 65535")

    def test_c5_protected_folders(self):
        for folder in ("Desktop", "Documents", "Downloads"):
            with self.subTest(folder):
                svc = self.service(where=("home", folder, "proj"))
                result = self.install(svc)
                self.assert_refused(svc, result, "Desktop, Documents or Downloads")

    def test_c5_edge_protected_folder_typed_in_another_case(self):
        """macOS folders are case-insensitive: ~/desktop is ~/Desktop, and is just as protected"""
        svc = self.service(where=("home", "Desktop", "proj"), codes={})
        lower = os.path.join(svc.home, "desktop", "proj")
        if not os.path.exists(lower):
            self.skipTest("this file system is case-sensitive")
        # As Terminal does after `cd ~/desktop/proj`: the shell exports the path as typed
        env = svc.env(GOW_SERVICE_WAIT_SECONDS="1", PWD=lower)
        result = svc.run("install", env=env, cwd=lower, script=os.path.join(lower, "dashboard", "service.sh"))
        self.assert_refused(svc, result, "Desktop, Documents or Downloads")

    def test_c6_usage(self):
        svc = self.service()
        for args in ((), ("bogus",), ("install", "--bogus"), ("status", "extra"), ("uninstall", "extra")):
            with self.subTest(args):
                result = svc.run(*args)
                self.assert_ok(result, 2)
                self.assertTrue(any(line.startswith(USAGE_START) for line in result.stdout.splitlines()),
                                result.stdout + result.stderr)
                svc.assert_no_change(result)

    def test_k5_version_check_control(self):
        """3.8 passes the version check (and the run goes on to fail at the plist check), 3.7 fails it"""
        def failing_plutil(svc):
            path = os.path.join(svc.temp, "fake-plutil")
            with open(path, "w") as f:
                f.write("#!/bin/sh\nexit 1\n")
            os.chmod(path, 0o755)
            return path

        svc = self.service(python=version_answer("3.8", 0))
        result = self.install(svc, env=svc.env(GOW_PLUTIL=failing_plutil(svc)))
        self.assert_refused(svc, result, "The service file failed its check.")
        self.assertNotIn("needs Python 3.8", result.stdout)
        self.assertNotIn("Can't run", result.stdout)

        old = self.service(python=version_answer("3.7", 3))
        result = self.install(old, env=old.env(GOW_PLUTIL=failing_plutil(old)))
        self.assert_refused(old, result, "is 3.7", "needs Python 3.8 or newer")


class Install(ServiceCase):
    def expected_plist(self, svc, extra_arguments=(), environment=None):
        return {
            "Label": LABEL,
            "ProgramArguments": ["/bin/bash", svc.proj + "/dashboard/service.sh", "run", "--port", str(svc.port),
                                 *extra_arguments],
            "WorkingDirectory": svc.proj,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "ProcessType": "Standard",
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": "/dev/null",
            "EnvironmentVariables": environment or {"PATH": SERVICE_PATH},
        }

    def installed(self, **kwargs):
        svc = self.service(spawn=True, **kwargs)
        result = self.install(svc)
        self.assert_ok(result)
        return svc, result

    def test_c7_c8_install(self):
        svc, result = self.installed()
        self.assertIn(f"http://127.0.0.1:{svc.port}/", result.stdout)
        self.assertIn("It will now start by itself when you log in.", result.stdout)
        self.assertEqual(mode_of(svc.plist), 0o644)
        lint = subprocess.run(["/usr/bin/plutil", "-lint", svc.plist], capture_output=True, text=True)
        self.assertEqual(lint.returncode, 0, lint.stdout)
        self.assertEqual(svc.load_plist(), self.expected_plist(svc))
        self.assertEqual(probe(svc.port), "dashboard")

        # C8
        calls = svc.calls()
        enable = ["enable", SERVICE]
        bootstrap = ["bootstrap", DOMAIN, svc.plist]
        self.assertIn(enable, calls)
        self.assertIn(bootstrap, calls)
        self.assertLess(calls.index(enable), calls.index(bootstrap))
        self.assertEqual([c for c in calls if c[0] == "load"], [])
        self.assertEqual(os.listdir(svc.agents), [f"{LABEL}.plist"])

    def test_c9_load_fallback(self):
        svc = self.service(spawn=True, codes={"bootstrap": 5})
        result = self.install(svc)
        self.assert_ok(result)
        calls = svc.calls()
        load = ["load", "-w", svc.plist]
        self.assertIn(load, calls)
        self.assertLess(calls.index(["bootstrap", DOMAIN, svc.plist]), calls.index(load))
        self.assertEqual(probe(svc.port), "dashboard")

    def test_c9_both_fail(self):
        svc = self.service(spawn=True, codes={"bootstrap": 5, "load": 5})
        result = self.install(svc)
        self.assert_ok(result, 1)
        self.assertIn("Nothing was installed", result.stdout)
        self.assertFalse(os.path.exists(svc.plist))
        self.assertFalse(os.path.exists(svc.plist + ".tmp"))

    def test_k2_failed_load_is_exit_1(self):
        svc = self.service(spawn=True, codes={"bootstrap": 5, "load": 5})
        result = self.install(svc)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.returncode, 1)

    def test_c10_plist_check_fails(self):
        svc = self.service(spawn=True)
        fake_plutil = os.path.join(svc.temp, "fake-plutil")
        with open(fake_plutil, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(fake_plutil, 0o755)
        result = self.install(svc, env=svc.env(GOW_PLUTIL=fake_plutil))
        self.assert_ok(result, 1)
        self.assertIn("Nothing was changed.", result.stdout)
        svc.assert_no_change(result)

    def test_k1_plutil_rejects_a_truncated_plist(self):
        svc, _ = self.installed()
        with open(svc.plist, "rb") as f:
            data = f.read()
        broken = os.path.join(svc.temp, "broken.plist")
        with open(broken, "wb") as f:
            f.write(data[:len(data) // 2])
        lint = subprocess.run(["/usr/bin/plutil", "-lint", broken], capture_output=True, text=True)
        self.assertNotEqual(lint.returncode, 0)

    def test_c11_install_twice(self):
        svc, first = self.installed()
        before = len(svc.calls())
        second = self.install(svc)
        self.assert_ok(second)
        calls = svc.calls()[before:]
        bootout = ["bootout", SERVICE]
        bootstrap = ["bootstrap", DOMAIN, svc.plist]
        self.assertIn(bootout, calls)
        self.assertIn(bootstrap, calls)
        self.assertLess(calls.index(bootout), calls.index(bootstrap))
        self.assertEqual(os.listdir(svc.agents), [f"{LABEL}.plist"])
        wait_until(lambda: len(svc.server_processes()) == 1, 10, "one server")
        time.sleep(1)
        self.assertEqual(len(svc.server_processes()), 1, svc.server_processes())
        self.assertEqual(probe(svc.port), "dashboard")
        caffeinated = [pid for pid, command in all_processes()
                       if command.startswith("/usr/bin/caffeinate") and command.endswith(svc.server_suffix())]
        self.assertEqual(len(caffeinated), 1)

    def test_c12_awkward_path(self):
        svc = self.service(spawn=True, where=("my proj & co", "godofwar"))
        result = self.install(svc)
        self.assert_ok(result)
        lint = subprocess.run(["/usr/bin/plutil", "-lint", svc.plist], capture_output=True, text=True)
        self.assertEqual(lint.returncode, 0, lint.stdout)
        plist = svc.load_plist()
        self.assertEqual(plist["ProgramArguments"][1], os.path.join(svc.temp, "my proj & co", "godofwar")
                         + "/dashboard/service.sh")
        self.assertEqual(plist["WorkingDirectory"], svc.proj)
        self.assertEqual(probe(svc.port), "dashboard")

    def test_c13_browsers_path(self):
        for name, value, expected in (("set", "/x/y", {"PATH": SERVICE_PATH, "PLAYWRIGHT_BROWSERS_PATH": "/x/y"}),
                                      ("unset", None, {"PATH": SERVICE_PATH}),
                                      ("empty", "", {"PATH": SERVICE_PATH})):
            with self.subTest(name):
                svc = self.service(spawn=True)
                result = self.install(svc, env=svc.env(PLAYWRIGHT_BROWSERS_PATH=value))
                self.assert_ok(result)
                self.assertEqual(svc.load_plist()["EnvironmentVariables"], expected)

    def test_c14_caffeinate(self):
        """macOS caffeinate execs the command under its own pid and forks a child that holds the
        no-sleep assertion on the command's behalf. So the job pid is the server, caffeinate below it"""
        svc, _ = self.installed()
        job = str(svc.job_pid())
        # (a) the job pid is the listener, and is this copy's server on P
        self.assertEqual(listener_pid(svc.port), job)
        self.assertTrue(command_of(job).strip().endswith(svc.server_suffix()), command_of(job))
        # (b) exactly one direct child is caffeinate -i
        children = subprocess.run(["pgrep", "-P", job], capture_output=True, text=True).stdout.split()
        caffeinate = [pid for pid in children if command_of(pid).startswith("/usr/bin/caffeinate -i")]
        self.assertEqual(len(caffeinate), 1, {pid: command_of(pid).strip() for pid in children})
        # (c) that child holds PreventUserIdleSystemSleep
        assertions = subprocess.run(["/usr/bin/pmset", "-g", "assertions"], capture_output=True, text=True).stdout
        self.assertRegex(assertions, rf"pid {caffeinate[0]}\(caffeinate\): .*PreventUserIdleSystemSleep")
        # (d) the assertion goes with the server
        self.assertIn(svc.temp, command_of(job))
        os.kill(int(job), signal.SIGKILL)
        wait_until(lambda: not command_of(caffeinate[0]).strip(), 5, "caffeinate to end with the server")

    def test_c14_allow_sleep(self):
        svc = self.service(spawn=True)
        result = self.install(svc, "--allow-sleep")
        self.assert_ok(result)
        plist = svc.load_plist()
        self.assertEqual(plist["ProgramArguments"][-1], "--allow-sleep")
        self.assertEqual(plist, self.expected_plist(svc, ("--allow-sleep",)))
        commands = [command_of(pid).strip() for pid in tree(svc.job_pid())]
        self.assertFalse(any("caffeinate" in command for command in commands), commands)
        self.assertTrue(any(command.endswith(svc.server_suffix()) for command in commands), commands)

    def test_c15_not_answering(self):
        svc = self.service(spawn=False)
        result = self.install(svc, env=svc.env(GOW_SERVICE_WAIT_SECONDS="2"))
        self.assert_ok(result, 1)
        self.assertIn("not answering yet", result.stdout)
        self.assertIn(os.path.join(svc.proj, "dashboard", "data", LOG_NAME), result.stdout)
        self.assertTrue(os.path.exists(svc.plist))


class PortInUse(ServiceCase):
    def test_c16_other_program(self):
        svc = self.service(spawn=True)
        listener = Listener(self, svc.port)
        result = self.install(svc)
        self.assert_ok(result, 1)
        self.assertIn("being used by another program", result.stdout)
        self.assertIn("Nothing was changed.", result.stdout)
        svc.assert_no_change(result)
        self.assertTrue(listener.is_open())

    def test_c17_foreign_dashboard_by_relative_path(self):
        svc = self.service(spawn=True)
        server = svc.start_dashboard([svc.venv_python, "dashboard/server.py", "--port", str(svc.port)], svc.proj)
        result = self.install(svc, stdin="y\n")
        self.assert_ok(result, 1)
        self.assertIn(str(server.pid), result.stdout)
        self.assertIn(f"kill {server.pid}", result.stdout)
        self.assertNotIn("Stop it so the service can take over?", result.stdout)
        self.assertIsNone(server.poll())
        self.assertEqual(probe(svc.port), "dashboard")
        self.assertEqual(listener_pid(svc.port), str(server.pid))
        svc.assert_no_change(result)

    def launcher_style(self, svc):
        return svc.start_dashboard([svc.venv_python, svc.proj + "/dashboard/server.py", "--port", str(svc.port)],
                                   svc.proj)

    def test_c18_no(self):
        for name, answer in (("n", "n\n"), ("EOF", ""), ("anything else", "maybe\n")):
            with self.subTest(name):
                svc = self.service(spawn=True)
                server = self.launcher_style(svc)
                result = self.install(svc, stdin=answer)
                self.assert_ok(result, 1)
                self.assertIn("Stop it so the service can take over? [y/N]", result.stdout)
                self.assertIn(str(server.pid), result.stdout)
                self.assertIsNone(server.poll())
                self.assertEqual(listener_pid(svc.port), str(server.pid))
                svc.assert_no_change(result)

    def test_c18_yes(self):
        svc = self.service(spawn=True)
        server = self.launcher_style(svc)
        old = str(server.pid)
        result = self.install(svc, stdin="y\n")
        self.assert_ok(result)
        self.assertEqual(server.wait(10), server.returncode)
        self.assertIsNotNone(server.poll())
        self.assertEqual(probe(svc.port), "dashboard")
        listener = listener_pid(svc.port)
        self.assertNotEqual(listener, old)
        self.assertIn(listener, tree(svc.job_pid()))

    def test_k4_other_copy_is_not_signalled(self):
        svc = self.service(spawn=True)
        other = os.path.join(svc.temp, "other")
        copy_project(other, None)
        server = svc.start_dashboard([svc.venv_python, other + "/dashboard/server.py", "--port", str(svc.port)],
                                     other, data_name="other-data")
        result = self.install(svc, stdin="y\n")
        self.assert_ok(result, 1)
        self.assertIn(f"kill {server.pid}", result.stdout)
        self.assertIsNone(server.poll())
        self.assertEqual(listener_pid(svc.port), str(server.pid))
        svc.assert_no_change(result)

    def test_c19_live_run_install_and_uninstall(self):
        svc = self.service(spawn=True)
        LiveDashboardStub(self, svc.port)
        self.assertEqual(probe(svc.port), "dashboard")
        result = self.install(svc)
        self.assert_ok(result, 1)
        self.assertIn("A claim run is going right now", result.stdout)
        svc.assert_no_change(result)

        # Uninstall, with the plist in place and the job loaded
        os.makedirs(svc.agents)
        written = subprocess.run([PYTHON, "-I", "-B", os.path.join(svc.proj, "dashboard", "service_helper.py"),
                                  "write-plist", svc.plist, svc.proj, str(svc.port)], capture_output=True, text=True)
        self.assertEqual(written.returncode, 0, written.stderr)
        svc.set_loaded()
        result = svc.run("uninstall")
        self.assert_ok(result, 1)
        self.assertIn("A claim run is going right now", result.stdout)
        self.assertEqual([c for c in svc.calls() if c[0] in ("bootout", "unload")], [])
        self.assertTrue(os.path.exists(svc.plist))


class Uninstall(ServiceCase):
    def snapshot(self, root):
        found = {}
        for folder, _, files in os.walk(root):
            for name in files:
                path = os.path.join(folder, name)
                with open(path, "rb") as f:
                    found[path] = f.read()
        return found

    def test_c20_uninstall(self):
        svc = self.service(spawn=True)
        self.assert_ok(self.install(svc))
        wait_until(lambda: os.path.exists(os.path.join(svc.data, LOG_NAME)), 10, "the service log")
        before = self.snapshot(svc.data)
        self.assertIn(os.path.join(svc.data, LOG_NAME), before)
        job = svc.job_pid()
        result = svc.run("uninstall")
        self.assert_ok(result)
        self.assertIn(["bootout", SERVICE], svc.calls())
        self.assertFalse(os.path.exists(svc.plist))
        self.assertIn("will no longer start by itself", result.stdout)
        after = self.snapshot(svc.data)
        for path, data in before.items():
            self.assertIn(path, after)
            self.assertEqual(after[path], data, path)
        self.assertEqual([pid for pid in svc.server_processes()], [])
        self.assertEqual(command_of(str(job)).strip(), "")
        wait_until(lambda: probe(svc.port) == "none", 10, "the port to be free")

    def test_c21_not_installed(self):
        svc = self.service()
        result = svc.run("uninstall")
        self.assert_ok(result)
        self.assertIn("is not installed", result.stdout)
        self.assertEqual([c for c in svc.calls() if c[0] in ("bootout", "unload")], [])

    def write_plist(self, svc):
        os.makedirs(svc.agents, exist_ok=True)
        written = subprocess.run([PYTHON, "-I", "-B", os.path.join(svc.proj, "dashboard", "service_helper.py"),
                                  "write-plist", svc.plist, svc.proj, str(svc.port)], capture_output=True, text=True)
        self.assertEqual(written.returncode, 0, written.stderr)

    def test_c22_unload_fallback(self):
        svc = self.service(codes={"bootout": 3})
        self.write_plist(svc)
        svc.set_loaded()
        result = svc.run("uninstall")
        self.assert_ok(result)
        calls = svc.calls()
        self.assertIn(["bootout", SERVICE], calls)
        self.assertIn(["unload", svc.plist], calls)
        self.assertLess(calls.index(["bootout", SERVICE]), calls.index(["unload", svc.plist]))
        self.assertFalse(os.path.exists(svc.plist))

    def test_edge_both_unloads_fail(self):
        """A coder deviation: the plist goes, the reason is explained, and the exit is 1"""
        svc = self.service(codes={"bootout": 3, "unload": 3})
        self.write_plist(svc)
        svc.set_loaded()
        result = svc.run("uninstall")
        self.assert_ok(result, 1)
        self.assertFalse(os.path.exists(svc.plist))
        self.assertIn("would not stop the running one", result.stdout)

    def test_edge_loaded_without_plist(self):
        svc = self.service()
        svc.set_loaded()
        result = svc.run("uninstall")
        self.assert_ok(result)
        self.assertIn(["bootout", SERVICE], svc.calls())
        self.assertIn("will no longer start by itself", result.stdout)


class Status(ServiceCase):
    def test_c23_nothing_installed(self):
        svc = self.service()
        result = svc.run("status")
        self.assert_ok(result, 1)
        self.assertEqual(result.stdout, "Installed: no\nLoaded: no\nAnswering: no\n")

    def test_c24_c25_installed(self):
        svc = self.service(spawn=True)
        self.assert_ok(self.install(svc))
        other = free_port()
        while other == svc.port:
            other = free_port()
        before_calls = len(svc.calls())
        listing = {name: os.stat(os.path.join(svc.agents, name)).st_mtime_ns for name in os.listdir(svc.agents)}
        result = svc.run("status", env=svc.env(GOW_DASHBOARD_PORT=str(other)))
        self.assert_ok(result)
        self.assertEqual(result.stdout.splitlines(), [f"Installed: yes ({svc.plist})", "Loaded: yes",
                                                      f"Answering: yes, http://127.0.0.1:{svc.port}/"])
        # C25
        new_calls = svc.calls()[before_calls:]
        self.assertTrue(new_calls)
        self.assertEqual({call[0] for call in new_calls}, {"print"})
        self.assertEqual({name: os.stat(os.path.join(svc.agents, name)).st_mtime_ns
                          for name in os.listdir(svc.agents)}, listing)

    def test_c25_not_installed_makes_only_print_calls(self):
        svc = self.service()
        self.assert_ok(svc.run("status"), 1)
        self.assertEqual({call[0] for call in svc.calls()}, {"print"})
        self.assertFalse(os.path.exists(svc.agents))

    def test_edge_port_taken_by_another_program(self):
        svc = self.service()
        Listener(self, svc.port)
        result = svc.run("status")
        self.assert_ok(result, 1)
        self.assertEqual(result.stdout.splitlines()[-1], f"Answering: no - port {svc.port} is used by another program")


class Run(ServiceCase):
    def log_of(self, folder):
        return os.path.join(folder, LOG_NAME)

    def wait_serving(self, svc, log, process, timeout=20):
        def serving():
            if process.poll() is not None:
                raise AssertionError(f"run exited {process.returncode}: {read_text(process.output_file)}")
            return os.path.exists(log) and f"Dashboard: http://127.0.0.1:{svc.port}/" in read_text(log)

        wait_until(serving, timeout, "run to serve")

    def test_c26_modes(self):
        svc = self.service()
        process = svc.start_run("--allow-sleep", env=svc.run_env(events_file=None), umask=0o022)
        data = os.path.join(svc.proj, "dashboard", "data")
        log = self.log_of(data)
        self.wait_serving(svc, log, process)
        self.assertEqual(mode_of(data), 0o700)
        self.assertEqual(mode_of(log), 0o600)
        self.assertIn(f"Dashboard: http://127.0.0.1:{svc.port}/", read_text(log))

    def test_k3_umask_022_makes_0644(self):
        folder = tempfile.mkdtemp(prefix="gow-svc-k3-")
        self.addCleanup(shutil.rmtree, folder, True)
        subprocess.run(["/bin/bash", "-c", "umask 022; : >> f"], cwd=folder, check=True)
        self.assertEqual(mode_of(os.path.join(folder, "f")), 0o644)

    def test_c26_symlinked_log(self):
        svc = self.service()
        data = os.path.join(svc.proj, "dashboard", "data")
        os.makedirs(data, mode=0o700)
        target = os.path.join(svc.temp, "target")
        with open(target, "w") as f:
            f.write("untouched\n")
        os.symlink(target, self.log_of(data))
        process = svc.start_run("--allow-sleep", env=svc.run_env(events_file=None), umask=0o022)
        self.assertEqual(process.wait(15), 1)
        self.assertEqual(read_text(target), "untouched\n")
        self.assertEqual(mode_of(target), 0o644)
        self.assertEqual(svc.server_processes(), [])

    def test_edge_symlinked_log_dir(self):
        svc = self.service()
        real = os.path.join(svc.temp, "real-dir")
        os.makedirs(real)
        link = os.path.join(svc.temp, "link-dir")
        os.symlink(real, link)
        process = svc.start_run("--allow-sleep", env=svc.run_env(events_file=os.path.join(link, "events.jsonl")))
        self.assertEqual(process.wait(15), 1)
        self.assertEqual(os.listdir(real), [])

    def test_c27_rotation(self):
        svc = self.service()
        os.makedirs(svc.data, mode=0o700)
        log = self.log_of(svc.data)
        old = (b"0123456789abcdef" * (MIB // 16 + 1))[:MIB + 1]
        with open(log, "wb") as f:
            f.write(old)
        process = svc.start_run("--allow-sleep", env=svc.run_env())
        self.wait_serving(svc, log, process)
        with open(log + ".1", "rb") as f:
            self.assertEqual(f.read(), old)
        self.assertLess(os.path.getsize(log), 1000)

    def test_c27_edge_exactly_1_mib_is_kept(self):
        svc = self.service()
        os.makedirs(svc.data, mode=0o700)
        log = self.log_of(svc.data)
        with open(os.path.join(svc.data, LOG_NAME + ".1"), "wb") as f:
            f.write(b"older")
        old = b"x" * MIB
        with open(log, "wb") as f:
            f.write(old)
        process = svc.start_run("--allow-sleep", env=svc.run_env())
        self.wait_serving(svc, log, process)
        with open(log, "rb") as f:
            self.assertTrue(f.read().startswith(old))
        with open(log + ".1", "rb") as f:
            self.assertEqual(f.read(), b"older")

    def test_c28_waits_for_the_port(self):
        svc = self.service()
        listener = Listener(self, svc.port)
        process = svc.start_run("--allow-sleep", env=svc.run_env(GOW_SERVICE_POLL_SECONDS="1"))
        time.sleep(4)
        self.assertIsNone(process.poll(), read_text(process.output_file))
        log = read_text(self.log_of(svc.data))
        self.assertEqual(len(re.findall(rf"port {svc.port} is in use", log)), 1, log)
        self.assertNotIn("could not listen", log)
        self.assertEqual([pid for pid, command in all_processes()
                          if f"{svc.proj}/dashboard/server.py" in command], [])
        listener.close()
        wait_until(lambda: probe(svc.port) == "dashboard", 5, "the service to take the port")

    def test_k6_without_the_wait_loop(self):
        svc = self.service()
        with open(svc.script, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(WAIT_LOOP, text, "the control no longer matches the code")
        with open(svc.script, "w", encoding="utf-8") as f:
            f.write(text.replace(WAIT_LOOP, "    while false; do"))
        Listener(self, svc.port)
        process = svc.start_run("--allow-sleep", env=svc.run_env(GOW_SERVICE_POLL_SECONDS="1"))
        self.assertEqual(process.wait(20), 1)
        self.assertIn("could not listen", read_text(self.log_of(svc.data)))

    def test_edge_run_usage(self):
        svc = self.service()
        for flags in ((), ("--port",), ("--port", "abc"), ("--port", "0"), ("--bogus",)):
            with self.subTest(flags):
                env = svc.run_env()
                check_run_env(env, svc.script, svc.temp, 1)
                done = subprocess.run(["/bin/bash", svc.script, "run", *flags], cwd=svc.temp, env=env,
                                      capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertFalse(os.path.exists(svc.data))

    def test_c29_minimal_environment_run(self):
        svc = self.service()
        mock = Mock({EMAIL: Account()}, hydrate_ms=100)
        self.addCleanup(mock.close)
        with open(svc.users_file, "w", encoding="utf-8") as f:
            f.write(users_text([EMAIL]))
        env = {
            "HOME": REAL_HOME,
            "PATH": SERVICE_PATH,
            "GOW_BASE_URL": mock.base_url,
            "GOW_USERS_FILE": svc.users_file,
            "GOW_EVENTS_FILE": svc.events_file,
            "GOW_INTERNET_CHECK_URL": svc.stub.url,
            "GOW_DASHBOARD_PORT": str(svc.port),
        }
        process = svc.start_run("--allow-sleep", env=env)
        self.wait_serving(svc, self.log_of(svc.data), process)

        host = f"127.0.0.1:{svc.port}"
        status, _, page = self.request(svc.port, "GET", "/", [("Host", host)])
        self.assertEqual(status, 200)
        token = re.search(r'name="gow-token" content="([^"]+)"', page.decode()).group(1)
        status, _, body = self.request(svc.port, "POST", "/api/run", [
            ("Host", host), ("Origin", f"http://{host}"), ("X-Gow-Token", token),
            ("Content-Type", "application/json")], json.dumps({"concurrency": 1}).encode())
        self.assertEqual(status, 200, body)

        found = {}

        def finished():
            found["state"] = json.loads(self.request(svc.port, "GET", "/api/state", [("Host", host)])[2])
            return found["state"]["status"] == "finished" and not found["state"]["control"]["owned"]

        try:
            wait_until(finished, 120, "the run to finish")
        except AssertionError:
            log = os.path.join(svc.data, "last-run.log")
            raise AssertionError(f"{found.get('state', {}).get('status')}\n"
                                 f"{read_text(log)[-3000:] if os.path.exists(log) else ''}")
        totals = found["state"]["totals"]
        self.assertEqual((totals["claimed"], totals["failed"]), (1, 0), totals)
        self.assertEqual(found["state"]["run"]["source"], "dashboard")
        self.assertTrue(os.path.exists(os.path.join(svc.data, "last-run.log")))
        self.assertTrue(os.path.exists(os.path.join(svc.data, LOG_NAME)))
        self.assertEqual([(email, character) for email, character in mock.ledger_pairs()], [(EMAIL, "only")])

    def request(self, port, method, path, headers, body=None):
        if int(port) == REAL_PORT:
            raise UnsafeRun("never the real dashboard's port")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                connection.putheader(name, value)
            if body is not None:
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_c30_launcher_finds_the_service(self):
        svc = self.service()
        process = svc.start_run("--allow-sleep", env=svc.run_env())
        self.wait_serving(svc, self.log_of(svc.data), process)
        urls = []

        def opener(url):
            urls.append(url)
            return True

        log = []
        spy = unittest_mock.Mock(side_effect=AssertionError("start_server must not be called"))
        with unittest_mock.patch.object(launcher, "start_server", spy):
            outcome = launcher.open_dashboard(lambda msg, level="INFO": log.append((msg, level)), svc.port, opener)
        self.assertEqual(outcome, "opened", log)
        self.assertEqual(urls, [f"http://127.0.0.1:{svc.port}/"])
        spy.assert_not_called()


def write_service_plist(case, svc, port):
    """An installed plist for port, written by the copy's own helper"""
    os.makedirs(svc.agents, exist_ok=True)
    written = subprocess.run([PYTHON, "-I", "-B", os.path.join(svc.proj, "dashboard", "service_helper.py"),
                              "write-plist", svc.plist, svc.proj, str(port)], capture_output=True, text=True)
    case.assertEqual(written.returncode, 0, written.stderr)
    with open(svc.plist, "rb") as f:
        return f.read()


def other_free_port(*taken):
    for _ in range(20):
        port = free_port()
        if port not in taken:
            return port
    raise AssertionError("no second free port")


class ReviewFixes(ServiceCase):
    def run_log(self, svc):
        path = os.path.join(svc.data, LOG_NAME)
        return read_text(path) if os.path.exists(path) else ""

    def test_c31_root_is_refused(self):
        svc = self.service(spawn=True)
        env = svc.env(GOW_SERVICE_UID="0")
        for command in ("install", "uninstall", "status"):
            with self.subTest(command):
                result = svc.run(command, env=env)
                self.assert_ok(result, 1)
                self.assertIn("sudo", result.stdout)
                self.assertIn("Nothing was changed.", result.stdout)
                self.assertEqual(svc.calls(), [])
                self.assertFalse(os.path.exists(svc.agents))
                self.assertFalse(os.path.exists(os.path.join(svc.proj, "dashboard", "data")))

        with self.subTest("uninstall with a plist"):
            before = write_service_plist(self, svc, svc.port)
            result = svc.run("uninstall", env=env)
            self.assert_ok(result, 1)
            self.assertIn("sudo", result.stdout)
            self.assertIn("Nothing was changed.", result.stdout)
            self.assertEqual(svc.calls(), [])
            with open(svc.plist, "rb") as f:
                self.assertEqual(f.read(), before)
            self.assertEqual(os.listdir(svc.agents), [f"{LABEL}.plist"])
            self.assertFalse(os.path.exists(os.path.join(svc.proj, "dashboard", "data")))

    def test_k7_own_uid_installs(self):
        self.assertNotEqual(os.getuid(), 0)
        svc = self.service(spawn=True)
        result = svc.run("install", env=svc.env(GOW_SERVICE_UID=str(os.getuid())))
        self.assert_ok(result)
        self.assertEqual(probe(svc.port), "dashboard")

    def test_c32_broken_python_in_run(self):
        svc = self.service()
        os.remove(svc.venv_python)
        os.symlink(os.path.join(svc.temp, "missing"), svc.venv_python)
        process = svc.start_run("--allow-sleep", env=svc.run_env(GOW_SERVICE_POLL_SECONDS="1"))
        time.sleep(4)
        self.assertIsNone(process.poll(), read_text(process.output_file))
        log = self.run_log(svc)
        wanted = f"{CANT_RUN} {svc.venv_python}"
        self.assertEqual(len([line for line in log.splitlines() if wanted in line]), 1, log)
        self.assertEqual([line for line in log.splitlines() if IN_USE in line], [], log)
        self.assertEqual([pid for pid, command in all_processes()
                          if f"{svc.proj}/dashboard/server.py" in command], [])

        # The python comes back, and the waiting run becomes the server itself
        fixed = svc.venv_python + ".new"
        with open(fixed, "w") as f:
            f.write(PYTHON_WRAPPER)
        os.chmod(fixed, 0o755)
        os.replace(fixed, svc.venv_python)
        wait_until(lambda: probe(svc.port) == "dashboard", 5, "the service to serve")
        self.assertEqual(listener_pid(svc.port), str(process.pid))
        self.assertIsNone(process.poll())

    def test_k8_silent_python_is_cant_run(self):
        svc = self.service(python=PYTHON_SILENT)
        process = svc.start_run("--allow-sleep", env=svc.run_env(GOW_SERVICE_POLL_SECONDS="1"))
        wait_until(lambda: CANT_RUN in self.run_log(svc), 10, "the can't-run line")
        time.sleep(2)
        self.assertIsNone(process.poll())
        log = self.run_log(svc)
        self.assertEqual(len([line for line in log.splitlines() if CANT_RUN in line]), 1, log)
        self.assertNotIn(IN_USE, log)

    def test_k8_port_in_use_is_not_cant_run(self):
        svc = self.service()
        Listener(self, svc.port)
        process = svc.start_run("--allow-sleep", env=svc.run_env(GOW_SERVICE_POLL_SECONDS="1"))
        wait_until(lambda: IN_USE in self.run_log(svc), 10, "the in-use line")
        time.sleep(2)
        self.assertIsNone(process.poll())
        log = self.run_log(svc)
        self.assertIn(IN_USE, log)
        self.assertNotIn(CANT_RUN, log)

    def test_edge_install_refuses_when_the_probe_cannot_run(self):
        svc = self.service(spawn=True, python=PROBE_FAILS)
        result = svc.run("install")
        self.assert_ok(result, 1)
        self.assertIn(f"Couldn't check port {svc.port}", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("Nothing was changed."), result.stdout)
        svc.assert_no_change(result)

    def installed_elsewhere(self, live):
        """C33's setup: the service is installed and loaded on P, and the new setting is Q"""
        svc = self.service()
        before = write_service_plist(self, svc, svc.port)
        svc.set_loaded()
        LiveDashboardStub(self, svc.port, live=live)
        self.assertEqual(probe(svc.port), "dashboard")
        other = other_free_port(svc.port, REAL_PORT)
        return svc, other, before

    def test_c33_live_guard_on_the_installed_port(self):
        svc, other, before = self.installed_elsewhere(live=True)
        env = svc.env(GOW_DASHBOARD_PORT=str(other))
        for command in ("install", "uninstall"):
            with self.subTest(command):
                result = svc.run(command, env=env)
                self.assert_ok(result, 1)
                self.assertIn("A claim run is going right now", result.stdout)
                self.assertTrue(result.stdout.rstrip().endswith("Nothing was changed."), result.stdout)
                self.assertEqual([c for c in svc.calls() if c[0] in CHANGE_VERBS], [])
                with open(svc.plist, "rb") as f:
                    self.assertEqual(f.read(), before)

    def test_k9_not_live_goes_on_to_bootout(self):
        svc, other, _ = self.installed_elsewhere(live=False)
        result = svc.run("install", env=svc.env(GOW_DASHBOARD_PORT=str(other), GOW_SERVICE_WAIT_SECONDS="2"))
        calls = svc.calls()
        bootout = ["bootout", SERVICE]
        self.assertIn(bootout, calls, result.stdout)
        bootstraps = [i for i, call in enumerate(calls) if call[0] == "bootstrap"]
        self.assertTrue(bootstraps, calls)
        self.assertLess(calls.index(bootout), bootstraps[0])

    def dual_stack(self):
        """C34's setup: a test-owned listener on [::1]:P and a hand-started dashboard on 127.0.0.1:P"""
        svc = self.service(spawn=True)
        V6Listener(self, svc.port)
        server = svc.start_dashboard([svc.venv_python, "dashboard/server.py", "--port", str(svc.port)], svc.proj)
        return svc, server

    def test_c34_listener_lookup_is_ipv4_loopback(self):
        svc, server = self.dual_stack()
        result = svc.run("install", stdin="y\n")
        self.assert_ok(result, 1)
        self.assertIn(str(server.pid), result.stdout)
        self.assertIn(f"kill {server.pid}", result.stdout)
        self.assertNotIn(f"(process {os.getpid()})", result.stdout)
        self.assertNotIn(f"kill {os.getpid()}", result.stdout)
        self.assertIsNone(server.poll())
        svc.assert_no_change(result)

    def test_k10_unfiltered_lookup_sees_both(self):
        svc, server = self.dual_stack()
        listed = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{svc.port}", "-sTCP:LISTEN", "-t"],
                                capture_output=True, text=True).stdout.split()
        self.assertIn(str(os.getpid()), listed)
        self.assertIn(str(server.pid), listed)


class Static(unittest.TestCase):
    def source(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_s1_bash_32(self):
        done = subprocess.run(["/bin/bash", "-n", SCRIPT], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        text = self.source(SCRIPT)
        for pattern in ("declare -A", "mapfile", "readarray", ",,}", "^^}", "|&", "coproc", "[[ -v", "local -n"):
            with self.subTest(pattern):
                self.assertNotIn(pattern, text)

    def test_s1_mode_0755(self):
        self.assertEqual(mode_of(SCRIPT) & 0o777, 0o755)

    def test_s2_installs_and_opens_nothing(self):
        for path in (SCRIPT, HELPER):
            with self.subTest(os.path.basename(path)):
                self.assertNotRegex(self.source(path),
                                    r"pip|playwright install|ensurepip|brew |-m venv|webbrowser|/usr/bin/open")

    def test_s3_parses_as_python_38(self):
        ast.parse(self.source(HELPER), feature_version=(3, 8))

    def test_s3_imports_only_launcher_and_stdlib(self):
        tree = ast.parse(self.source(HELPER))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(f"{node.module}:{','.join(a.name for a in node.names)}")
        self.assertEqual(imported, {"json", "os", "plistlib", "re", "sys", "urllib", "dashboard:launcher"})

    def test_s4_launchctl_and_plutil_only_through_variables(self):
        lines = [line for line in self.source(SCRIPT).splitlines() if not line.strip().startswith("#")]
        launchctl = [line for line in lines if "/bin/launchctl" in line]
        self.assertEqual(launchctl, ['LAUNCHCTL="${GOW_LAUNCHCTL:-/bin/launchctl}"'])
        plutil = [line for line in lines if "/usr/bin/plutil" in line]
        self.assertEqual(plutil, ['PLUTIL="${GOW_PLUTIL:-/usr/bin/plutil}"'])
        for line in lines:
            rest = line.replace("$LAUNCHCTL", "").replace("GOW_LAUNCHCTL", "").replace("LAUNCHCTL=", "")
            rest = rest.replace("/bin/launchctl", "")
            self.assertNotRegex(rest, r"\blaunchctl\b", line)
            rest = line.replace("$PLUTIL", "").replace("GOW_PLUTIL", "").replace("PLUTIL=", "")
            rest = rest.replace("/usr/bin/plutil", "")
            self.assertNotRegex(rest, r"\bplutil\b", line)
        self.assertGreater(sum('"$LAUNCHCTL"' in line for line in lines), 3)
        self.assertGreater(sum('"$PLUTIL"' in line for line in lines), 0)


class Guard(unittest.TestCase):
    def test_s6_service_env_refuses(self):
        svc = Service(self)
        service_env(svc)
        cases = {
            "agents unset": dict(GOW_LAUNCH_AGENTS_DIR=None),
            "agents real": dict(GOW_LAUNCH_AGENTS_DIR=os.path.join(REAL_HOME, "Library", "LaunchAgents")),
            "launchctl unset": dict(GOW_LAUNCHCTL=None),
            "launchctl real": dict(GOW_LAUNCHCTL="/bin/launchctl"),
            "port 8765": dict(GOW_DASHBOARD_PORT="8765"),
            "port 08765": dict(GOW_DASHBOARD_PORT="08765"),
            "port unset": dict(GOW_DASHBOARD_PORT=None),
            "home real": dict(HOME=REAL_HOME),
        }
        for name, changes in cases.items():
            with self.subTest(name):
                with self.assertRaises(UnsafeRun):
                    service_env(svc, **changes)
        with self.subTest("script under PROJECT"):
            with self.assertRaises(UnsafeRun):
                service_env(svc, script=SCRIPT)
            with self.assertRaises(UnsafeRun):
                svc.run("status", script=SCRIPT)
        with self.subTest("run on 8765"):
            with self.assertRaises(UnsafeRun):
                svc.start_run("--allow-sleep", env=svc.run_env(), port=REAL_PORT)


def tearDownModule():
    left = [(pid, command) for pid, command in all_processes() if int(pid) != os.getpid() and (
            "/gow-svc-" in command or any(command.endswith(f"server.py --port {port}") for port in USED_PORTS))]
    if left:
        raise AssertionError(f"processes left running: {left}")


if __name__ == "__main__":
    unittest.main()
