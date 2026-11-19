"""Starts the dashboard for the tests. Every server here runs on loopback, on a mock, on fake
accounts, with its event file in a temp directory: see assert_safe_dashboard_env."""

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time

from gow_support import (
    PROJECT,
    PYTHON,
    TESTS_DIR,
    Result,
    GowTestCase,
    UnsafeRun,
    assert_safe_env,
    command_of,
    descendants,
    kill_all,
    script_env,
    start_script,
    users_text,
    wait_until,
)

SERVER = os.path.join(PROJECT, "dashboard", "server.py")
DASH_HARNESS = os.path.join(TESTS_DIR, "dash_harness.py")
HEADERS = ("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy", "Cache-Control", "Content-Security-Policy")
SIGINT = int(signal.SIGINT)
SIGTERM = int(signal.SIGTERM)
SIGKILL = int(signal.SIGKILL)


def assert_safe_dashboard_env(env):
    """The script rules, plus an event file in a temp directory"""
    assert_safe_env(env)
    events_file = env.get("GOW_EVENTS_FILE")
    if not events_file:
        raise UnsafeRun("GOW_EVENTS_FILE is not set")
    temp_root = os.path.realpath(tempfile.gettempdir())
    if not os.path.realpath(events_file).startswith(temp_root + os.sep):
        raise UnsafeRun(f"GOW_EVENTS_FILE is not in a temp directory: {events_file}")
    internet = env.get("GOW_INTERNET_CHECK_URL")
    if internet is not None and urlsplit(internet).hostname != "127.0.0.1":
        raise UnsafeRun("GOW_INTERNET_CHECK_URL must point at 127.0.0.1")


def read_events(path):
    """Every line of an event file, as parsed JSON"""
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_events(path, entries):
    """A fixture: dicts become JSON lines, and strings or bytes are written as they are"""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, "ab") as f:
        for entry in entries:
            if isinstance(entry, bytes):
                f.write(entry)
            else:
                text = entry if isinstance(entry, str) else json.dumps(entry)
                f.write(text.encode() + b"\n")


def event(name, run, ts, **fields):
    return {"v": 1, "run": run, "ts": ts, "event": name, **fields}


def run_start(run, ts, total=2, concurrency=3, source="terminal", script="claim_all_characters.py"):
    return event("run_start", run, ts, source=source, script=script, concurrency=concurrency,
                 stagger=10, total=total, lines=list(range(1, total + 1)))


def account_end(run, ts, line, label="a***@x***", claimed=1, already=0, characters=1, success=True,
                timed_out=False, interrupted=False, seconds=5):
    return event("account_end", run, ts, line=line, label=label, characters=characters, claimed=claimed,
                 already=already, success=success, timed_out=timed_out, interrupted=interrupted, seconds=seconds)


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self):
        stub = self.server.stub
        with stub.lock:
            stub.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers.items())})
            mode = "200" if stub.fail_next > 0 else stub.mode
            stub.fail_next = max(stub.fail_next - 1, 0)

        while mode == "hang" and stub.mode == "hang" and not stub.closed.is_set():
            stub.closed.wait(0.05)
        if stub.closed.is_set():
            return

        status = int(mode) if mode.isdigit() else 204
        try:
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/followed")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except OSError:
            pass

    do_GET = do_HEAD = do_POST = answer


class AttackerHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b"<!doctype html><title>another site</title><body></body>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Attacker:
    """A page on another port, standing in for a website. Loopback only"""

    def __init__(self, case):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AttackerHandler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        case.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Stub:
    """A local internet target: 204, 200, 302, refused or hang, switchable mid-test"""

    def __init__(self, case, mode="204"):
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.requests = []
        self.fail_next = 0
        self.mode = mode
        self.server = None
        self.port = None
        self.open()
        if mode == "refused":
            self.shut()
        case.addCleanup(self.close)

    def open(self):
        ThreadingHTTPServer.allow_reuse_address = True
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port or 0), StubHandler)
        self.server.daemon_threads = True
        self.server.stub = self
        self.server.handle_error = lambda *a: None
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def shut(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def set(self, mode):
        was_shut = self.server is None
        self.mode = mode
        if mode == "refused":
            self.shut()
        elif was_shut:
            self.open()

    def close(self):
        self.closed.set()
        self.shut()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/check"

    def count(self):
        with self.lock:
            return len(self.requests)


class Dash:
    """One server and its temp directory. Always under dash_harness, so a socket attempt
    that leaves loopback is blocked and recorded, and that test fails at cleanup"""

    def __init__(self, case, mock, users="", harness=None, child_harness=None, port=0, ignore_sigint=False,
                 temp_dir=None, name="data", stub=None, internet_url=None, net_interval=None, base_url=None,
                 extra_env=None, expect_audit=False):
        self.case = case
        self.mock = mock
        self.temp_dir = temp_dir or tempfile.mkdtemp(prefix="gow-dash-")
        if temp_dir is None:
            case.addCleanup(shutil.rmtree, self.temp_dir, True)
        self.events_file = os.path.join(self.temp_dir, name, "events.jsonl")
        self.users_file = os.path.join(self.temp_dir, "fake-accounts")
        self.spy_file = os.path.join(self.temp_dir, "spy.jsonl")
        self.exit_file = os.path.join(self.temp_dir, "child-exit")
        self.audit_file = os.path.join(self.temp_dir, "net-audit")
        self.thread_file = os.path.join(self.temp_dir, "net-threads")
        self.stub = stub
        self.internet_url = internet_url or (stub.url if stub else None)
        self.net_interval = net_interval
        self.base_url = base_url
        self.extra_env = extra_env or {}
        self.expect_audit = expect_audit
        self.child_spy = os.path.join(self.temp_dir, "child-spy.json")
        self.harness = harness
        self.child_harness = child_harness
        self.requested_port = port
        self.ignore_sigint = ignore_sigint
        self.process = None
        self.port = None
        self.token = None

        with open(self.users_file, "w", encoding="utf-8") as f:
            f.write(users)
        case.addCleanup(self.clean_up)
        self.start()

    def environment(self):
        env = script_env(self.mock, self.users_file)
        env["GOW_EVENTS_FILE"] = self.events_file
        env.pop("GOW_INTERNET_CHECK_URL", None)
        env.pop("GOW_NET_INTERVAL_SECONDS", None)
        if self.internet_url is not None:
            env["GOW_INTERNET_CHECK_URL"] = self.internet_url
        if self.net_interval is not None:
            env["GOW_NET_INTERVAL_SECONDS"] = str(self.net_interval)
        if self.base_url is not None:
            env["GOW_BASE_URL"] = self.base_url
        env.update(self.extra_env)
        if self.child_harness is not None:
            env["GOW_TEST_HARNESS"] = json.dumps({
                "spy_file": self.child_spy, "exit_file": self.exit_file, **self.child_harness})
        env["GOW_DASH_HARNESS"] = json.dumps({
            "spy_file": self.spy_file, "child_harness": self.child_harness is not None,
            "net_audit_file": self.audit_file, "thread_file": self.thread_file, **(self.harness or {})})
        assert_safe_dashboard_env(env)
        return env

    def start(self):
        self.output_file = os.path.join(self.temp_dir, f"server-{time.monotonic_ns()}.out")
        env = self.environment()
        preexec = (lambda: signal.signal(signal.SIGINT, signal.SIG_IGN)) if self.ignore_sigint else None
        with open(self.output_file, "ab") as output:
            self.process = subprocess.Popen(
                [PYTHON, "-B", DASH_HARNESS, "--port", str(self.requested_port)],
                cwd=PROJECT, env=env, stdout=output, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True, preexec_fn=preexec)

        def listening():
            with open(self.output_file) as f:
                found = re.findall(r"http://127\.0\.0\.1:(\d+)/", f.read())
            if found:
                self.port = int(found[-1])
            return bool(found)

        wait_until(listening, 20, "the dashboard to print its address")
        page = self.get("/")[2].decode()
        self.token = re.search(r'name="gow-token" content="([^"]+)"', page).group(1)

    def output(self):
        with open(self.output_file) as f:
            return f.read()

    def request(self, method, path, body=None, headers=()):
        """Raw HTTP, with exactly the headers given"""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                connection.putheader(name, value)
            data = body if isinstance(body, bytes) else (body.encode() if body is not None else None)
            if data is not None:
                connection.putheader("Content-Length", str(len(data)))
            connection.endheaders(data)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def host(self):
        return f"127.0.0.1:{self.port}"

    def get(self, path, host=None):
        return self.request("GET", path, headers=[("Host", host or self.host())])

    def post(self, path, payload=None, raw=None, host=None, origin="default", token="default",
             content_type="application/json"):
        headers = [("Host", host or self.host())]
        if origin is not None:
            headers.append(("Origin", f"http://{self.host()}" if origin == "default" else origin))
        if token is not None:
            headers.append(("X-Gow-Token", self.token if token == "default" else token))
        if content_type is not None:
            headers.append(("Content-Type", content_type))
        return self.request("POST", path, body=raw if raw is not None else json.dumps(payload or {}), headers=headers)

    def run(self, concurrency=3):
        return self.post("/api/run", {"concurrency": concurrency})[0]

    def retry(self, concurrency=3):
        return self.post("/api/retry", {"concurrency": concurrency})[0]

    def stop(self):
        return self.post("/api/stop", {})[0]

    def state(self):
        status, _, data = self.get("/api/state")
        self.case.assertEqual(status, 200)
        return json.loads(data)

    def wait_state(self, condition, timeout=60, message="the dashboard state"):
        found = {}

        def met():
            found["state"] = self.state()
            return condition(found["state"])

        wait_until(met, timeout, message)
        return found["state"]

    def spawns(self):
        return [entry for entry in self.spy() if entry["kind"] == "spawn"]

    def signals(self):
        return [entry for entry in self.spy() if entry["kind"] == "signal"]

    def spy(self):
        if not os.path.exists(self.spy_file):
            return []
        with open(self.spy_file) as f:
            return [json.loads(line) for line in f]

    def child_pid(self):
        """The latest child this server spawned, from the spy on its Popen, never from a run id"""
        return self.spawns()[-1]["pid"]

    def audit(self):
        if not os.path.exists(self.audit_file):
            return []
        with open(self.audit_file) as f:
            return f.read().splitlines()

    def most_check_threads(self):
        if not os.path.exists(self.thread_file):
            return 0
        with open(self.thread_file) as f:
            return int(f.read())

    def child_exit(self):
        with open(self.exit_file) as f:
            return f.read().strip()

    def log(self):
        with open(os.path.join(os.path.dirname(self.events_file), "last-run.log"), encoding="utf-8") as f:
            return f.read()

    def recorded(self):
        return read_events(self.events_file)

    def kill_server(self, number=signal.SIGKILL):
        if self.process and self.process.poll() is None:
            os.kill(self.process.pid, number)
        if self.process:
            self.process.wait(10)

    def clean_up(self):
        """Stop only what this test started: the server, and children confirmed by command line"""
        self.kill_server()
        for spawn in self.spawns():
            pid = str(spawn["pid"])
            if " ".join(spawn["argv"][1:]) in command_of(pid):
                kill_all(descendants(pid) + [pid])
        if not self.expect_audit:
            self.case.assertEqual(self.audit(), [], "a socket attempt left loopback")


def run_copy(case, script, mock, users, args=(), replace=(), drop_dashboard=False, harness=None,
             events_file=None, timeout=120, env_extra=None):
    """Run from a scratch copy of the code, so a protection can go without touching the real files.

    replace is (file, old, new) tuples. The copy holds the claim scripts, tests and dashboard only.
    """
    work = tempfile.mkdtemp(prefix="gow-copy-")
    case.addCleanup(shutil.rmtree, work, True)
    case.last_copy = work
    for name in ("claim_common.py", "claim_all_characters.py", "claim_all.py", "claim_batch.py"):
        shutil.copy(os.path.join(PROJECT, name), work)
    ignored = shutil.ignore_patterns("__pycache__", "data")
    shutil.copytree(TESTS_DIR, os.path.join(work, "tests"), ignore=ignored)
    if not drop_dashboard:
        shutil.copytree(os.path.join(PROJECT, "dashboard"), os.path.join(work, "dashboard"), ignore=ignored)

    for name, old, new in replace:
        target = os.path.join(work, name)
        with open(target, encoding="utf-8") as f:
            text = f.read()
        case.assertIn(old, text, "the control no longer matches the code")
        with open(target, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new))

    users_file = os.path.join(work, "fake-accounts")
    with open(users_file, "w", encoding="utf-8") as f:
        f.write(users)
    env = script_env(mock, users_file)
    if events_file is not None:
        env["GOW_EVENTS_FILE"] = events_file
    env.update(env_extra or {})
    env = {name: value for name, value in env.items() if value is not None}
    spy_file = os.path.join(work, "spy.json")
    if harness is not None:
        env["GOW_TEST_HARNESS"] = json.dumps({"spy_file": spy_file, **harness})
        command = [PYTHON, "-B", os.path.join(work, "tests", "script_harness.py"), script, *args]
    else:
        command = [PYTHON, "-B", os.path.join(work, script), *args]

    # The users file sits in a temp directory, so the usual rule holds for the copy too
    assert_safe_env(env)
    started = time.monotonic()
    done = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, encoding="utf-8",
                          timeout=timeout, stdin=subprocess.DEVNULL)
    return Result(done.returncode, done.stdout, done.stderr, time.monotonic() - started, {})


def wait_online(page, timeout=10):
    """Start and Retry need a second action until the indicator reads Online"""
    wait_until(lambda: "Online" in page.inner_text("#network"), timeout, "the indicator to read Online")


class DashTestCase(GowTestCase):
    def dash(self, accounts=None, users=None, mock_options=None, **kwargs):
        accounts = accounts if accounts is not None else {}
        mock = self.make_mock(accounts, **{"hydrate_ms": 100, **(mock_options or {})})
        text = users if users is not None else users_text(accounts)
        return Dash(self, mock, text, **kwargs)

    def terminal_run(self, dash, script="claim_all_characters.py", args=("--stagger", "0"), harness=None):
        """A script started by hand, recording to the same file as the server"""
        with open(dash.users_file, encoding="utf-8") as f:
            users = f.read()
        process = start_script(script, dash.mock, users, args, harness, dash.temp_dir, new_session=True,
                               env_extra={"GOW_EVENTS_FILE": dash.events_file})

        def finish_off():
            if process.poll() is None:
                kill_all(descendants(process.pid))
                process.kill()
            process.communicate()

        self.addCleanup(finish_off)
        return process

    def recording(self, script, mock, users, *args, harness=None, timeout=240, events_file=None):
        """Run a script with recording on, returning (result, event file path)"""
        temp_dir = tempfile.mkdtemp(prefix="gow-dash-")
        self.addCleanup(shutil.rmtree, temp_dir, True)
        path = events_file or os.path.join(temp_dir, "data", "events.jsonl")
        return self.run_gow(script, mock, users, *args, harness=harness, timeout=timeout,
                            env_extra={"GOW_EVENTS_FILE": path}), path

    def assert_headers(self, headers):
        for name in HEADERS:
            self.assertIn(name, headers)
        self.assertEqual([h for h in headers if h.lower().startswith("access-control-")], [])
