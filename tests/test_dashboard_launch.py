"""Opening the dashboard when a claim run starts from a terminal: dashboard/launcher.py.

Every port here is a free one the test picked, never the real dashboard's. No real browser
opens: in process, webbrowser.open is replaced for every test, and under script_harness the
harness replaces it. Every dashboard server a test starts is killed at cleanup, and only after
its command line shows dashboard/server.py and that test's own port."""

import fcntl
import glob
import http.client
import inspect
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock as unittest_mock

import gow_support
from dash_support import DashTestCase, run_copy
from gow_support import (
    PROJECT,
    PYTHON,
    SUCCESS_MESSAGE,
    TESTS_DIR,
    Account,
    UnsafeRun,
    assert_safe_env,
    command_of,
    range_args,
    users_text,
    wait_until,
)

from dashboard import events, launcher

EMAIL = "launch@x.test"
PORT_PROBLEM = "GOW_DASHBOARD_PORT must be a port number from 1 to 65535"
TERMINAL = {"dashboard_interactive": True, "default_stagger": 0}
PROTECTED_PIDS = ("24087",)


def free_port():
    """A port nothing listens on, never the real dashboard's"""
    for _ in range(20):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        if port != launcher.DEFAULT_PORT:
            return port
    raise AssertionError("no free port")


def url_of(port):
    return f"http://127.0.0.1:{port}/"


def is_test_server(pid, port):
    """Only a dashboard server on this test's port qualifies to be signalled"""
    if str(pid) in PROTECTED_PIDS or int(pid) in (os.getpid(), os.getppid()) or int(pid) <= 1:
        return False
    if int(port) == launcher.DEFAULT_PORT:
        return False
    command = command_of(str(pid))
    return "dashboard/server.py" in command and re.search(rf"--port {port}(\s|$)", command) is not None


def servers_on(port):
    found = subprocess.run(["pgrep", "-f", f"dashboard/server.py --port {port}"],
                           capture_output=True, text=True).stdout.split()
    return [pid for pid in found if is_test_server(pid, port)]


def kill_servers(port, pids=()):
    """Kill the dashboard servers this test started on its port, confirmed by command line"""
    for pid in {str(p) for p in pids} | set(servers_on(port)):
        if is_test_server(pid, port):
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
    wait_until(lambda: not servers_on(port), 10, f"the test servers on {port} to stop")


def open_paths(pid):
    """Every file path lsof shows the process holding"""
    out = subprocess.run(["lsof", "-nP", "-p", str(pid), "-Fn"], capture_output=True, text=True).stdout
    return [line[1:] for line in out.splitlines() if line.startswith("n")]


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def get_state(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", "/api/state", headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


class Log(list):
    """Stands in for claim_common.log, keeping (msg, level)"""

    def __call__(self, msg, level="INFO"):
        self.append((msg, level))


class Opener:
    """The fake browser: records URLs, then returns result or raises"""

    def __init__(self, result=True, raises=None):
        self.urls = []
        self.result = result
        self.raises = raises

    def __call__(self, url):
        self.urls.append(url)
        if self.raises is not None:
            raise self.raises
        return self.result


class PlainHandler(BaseHTTPRequestHandler):
    """Another program's web server: a normal answer, a Server header that isn't the dashboard's"""
    status = 200

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b"another program"
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LauncherCase(DashTestCase):
    """In process, on a free port, with the event file in a temp dir and no real browser"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gow-launch-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.events_file = os.path.join(self.tmp, "data", "events.jsonl")
        self.log_path = os.path.join(self.tmp, "data", "dashboard-server.log")
        self.port = free_port()
        self.assertNotEqual(self.port, launcher.DEFAULT_PORT)

        environ = unittest_mock.patch.dict(os.environ, {
            "GOW_DASHBOARD_PORT": str(self.port),
            "GOW_EVENTS_FILE": self.events_file,
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        environ.start()
        self.addCleanup(environ.stop)
        for name in ("GOW_RUN_SOURCE", "GOW_INTERNET_CHECK_URL", "http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY"):
            os.environ.pop(name, None)

        # No real browser, whatever a test does
        self.browser = []
        for name in ("open", "open_new", "open_new_tab"):
            patcher = unittest_mock.patch.object(webbrowser, name, self.fake_webbrowser)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.saved_process = launcher.server_process
        self.addCleanup(setattr, launcher, "server_process", self.saved_process)
        self.addCleanup(self.kill_started)

    def fake_webbrowser(self, url, *args, **kwargs):
        self.browser.append(url)
        return True

    def kill_started(self):
        process = launcher.server_process
        ours = process is not None and process is not self.saved_process
        kill_servers(self.port, [process.pid] if ours else [])
        if ours:
            process.wait(10)

    def plain_server(self, status=200):
        handler = type("Handler", (PlainHandler,), {"status": status})
        server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def launcher_copy(self, old, new):
        """launcher.py with one protection gone, loaded beside the real one"""
        with open(launcher.__file__, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(old, text, "the control no longer matches the code")
        module = types.ModuleType("launcher_control")
        module.__file__ = launcher.__file__
        exec(compile(text.replace(old, new), launcher.__file__, "exec"), module.__dict__)
        return module


class PortSetting(LauncherCase):
    # U1
    def test_u1_unset_is_the_default_port(self):
        os.environ.pop("GOW_DASHBOARD_PORT")
        self.assertEqual(launcher.port_setting(), (launcher.DEFAULT_PORT, None))

    def test_u1_empty_is_off_and_silent(self):
        os.environ["GOW_DASHBOARD_PORT"] = ""
        self.assertEqual(launcher.port_setting(), (None, None))

    def test_u1_a_port_number(self):
        for value, port in (("9001", 9001), ("1", 1), ("65535", 65535)):
            with self.subTest(value):
                os.environ["GOW_DASHBOARD_PORT"] = value
                self.assertEqual(launcher.port_setting(), (port, None))

    def test_u1_bad_values_name_the_rule_not_the_value(self):
        for value in ("0", "65536", "abc", " 80", "-1", "80 ", "9001\n", "+80", "1e3", "0x50", "８０", "١٢٣",
                      "00000", "99999", "9" * 40, "SECRETPASS"):
            with self.subTest(value=value):
                os.environ["GOW_DASHBOARD_PORT"] = value
                port, problem = launcher.port_setting()
                self.assertIsNone(port)
                self.assertEqual(problem, PORT_PROBLEM)
                if value.strip():
                    self.assertNotIn(value.strip(), problem)


class Probe(LauncherCase):
    def test_nothing_listening_is_none(self):
        self.assertEqual(launcher.probe(self.port), "none")

    def test_another_program_is_other(self):
        for status in (200, 404, 500):
            with self.subTest(status):
                server = self.plain_server(status)
                self.assertEqual(launcher.probe(self.port), "other")
                server.shutdown()
                server.server_close()

    def test_a_dashboard_error_page_still_counts(self):
        class Gow404(PlainHandler):
            status = 404

            def version_string(self):
                return "gow-dashboard"

        server = ThreadingHTTPServer(("127.0.0.1", self.port), Gow404)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertEqual(launcher.probe(self.port), "dashboard")

    def test_a_listener_that_never_answers_is_other_within_the_timeout(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", self.port))
        listener.listen(5)
        self.addCleanup(listener.close)
        began = time.monotonic()
        self.assertEqual(launcher.probe(self.port), "other")
        self.assertLess(time.monotonic() - began, launcher.PROBE_TIMEOUT_SECONDS + 1)

    def test_a_listener_that_hangs_up_is_other(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", self.port))
        listener.listen(5)
        self.addCleanup(listener.close)

        def hang_up():
            try:
                connection, _ = listener.accept()
                connection.close()
            except OSError:
                pass

        threading.Thread(target=hang_up, daemon=True).start()
        self.assertEqual(launcher.probe(self.port), "other")

    def test_a_dashboard_is_found_past_a_proxy_setting(self):
        dash = self.dash(port=self.port)
        self.assertEqual(dash.port, self.port)
        proxy_port = free_port()
        proxy = ThreadingHTTPServer(("127.0.0.1", proxy_port), PlainHandler)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        os.environ["http_proxy"] = os.environ["HTTP_PROXY"] = f"http://127.0.0.1:{proxy_port}"
        self.assertEqual(launcher.probe(self.port), "dashboard")


class OpenDashboard(LauncherCase):
    # U2
    def test_u2_a_running_dashboard_is_opened_not_started(self):
        dash = self.dash(port=self.port)
        self.assertEqual(dash.port, self.port)
        before = launcher.server_process
        log, opener = Log(), Opener()

        self.assertEqual(launcher.open_dashboard(log, self.port, opener), "opened")
        self.assertEqual(opener.urls, [url_of(self.port)])
        self.assertEqual(log, [(f"Dashboard: {url_of(self.port)} (opened in your browser)", "INFO")])
        self.assertIs(launcher.server_process, before)
        self.assertEqual(servers_on(self.port), [])
        self.assertEqual(self.browser, [])

    # U3
    def test_u3_nothing_listening_starts_a_detached_server(self):
        # A run's lock, held as a run holds it but inheritable, so only close_fds keeps it out of the server
        locks = self.events_file + ".locks"
        events.private_dir(os.path.dirname(self.events_file))
        events.private_dir(locks)
        lock = os.path.join(locks, "test-run.lock")
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(fd, True)

        log, opener = Log(), Opener()
        began = time.monotonic()
        outcome = launcher.open_dashboard(log, self.port, opener)
        elapsed = time.monotonic() - began

        self.assertEqual(outcome, "started", log)
        self.assertLess(elapsed, 6)
        self.assertEqual(opener.urls, [url_of(self.port)])
        self.assertEqual(log, [(f"Dashboard started: {url_of(self.port)} (opened in your browser)", "INFO")])
        self.assertEqual(launcher.probe(self.port), "dashboard")

        server = launcher.server_process
        self.assertIsNot(server, self.saved_process)
        self.assertTrue(is_test_server(server.pid, self.port), command_of(str(server.pid)))
        self.assertIsNone(server.poll())
        self.assertEqual(os.getsid(server.pid), server.pid)
        self.assertNotEqual(os.getpgid(server.pid), os.getpgid(0))

        self.assertTrue(os.path.isfile(self.log_path))
        self.assertEqual(stat.S_IMODE(os.stat(self.log_path).st_mode), 0o600)
        self.assertIn(f"Dashboard: {url_of(self.port)}", read(self.log_path))

        held = open_paths(server.pid)
        # lsof sees the server's own files, and would see the lock: this process shows it
        self.assertTrue(any(path.endswith("dashboard-server.log") for path in held), held)
        self.assertIn(os.path.realpath(lock), [os.path.realpath(p) for p in open_paths(os.getpid())])
        for prefix in (locks, os.path.realpath(locks)):
            self.assertEqual([path for path in held if path.startswith(prefix)], [])

    def test_u3_the_log_is_reset_to_private(self):
        os.makedirs(os.path.dirname(self.log_path), mode=0o700)
        with open(self.log_path, "w") as f:
            f.write("OLD CONTENT\n")
        os.chmod(self.log_path, 0o644)
        os.environ["GOW_INTERNET_CHECK_URL"] = "not-a-url"

        self.assertEqual(launcher.open_dashboard(Log(), self.port, Opener()), "start_failed")
        self.assertEqual(stat.S_IMODE(os.stat(self.log_path).st_mode), 0o600)
        self.assertNotIn("OLD CONTENT", read(self.log_path))

    def test_a_symlinked_log_is_refused_and_its_target_untouched(self):
        os.makedirs(os.path.dirname(self.log_path), mode=0o700)
        target = os.path.join(self.tmp, "elsewhere")
        with open(target, "w") as f:
            f.write("KEEP\n")
        os.symlink(target, self.log_path)
        log, opener = Log(), Opener()

        self.assertEqual(launcher.open_dashboard(log, self.port, opener), "error")
        self.assertEqual(log, [("Dashboard not opened: OSError", "WARNING")])
        self.assertEqual(read(target), "KEEP\n")
        self.assertIs(launcher.server_process, self.saved_process)
        self.assertEqual(opener.urls, [])

    def test_recording_off_gives_one_warning_not_a_crash(self):
        os.environ.pop("GOW_EVENTS_FILE")
        log, opener = Log(), Opener()
        self.assertEqual(launcher.open_dashboard(log, self.port, opener), "error")
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0][1], "WARNING")
        self.assertTrue(log[0][0].startswith("Dashboard not opened: "), log)
        self.assertIs(launcher.server_process, self.saved_process)

    # U4
    def test_u4_another_program_on_the_port(self):
        self.plain_server()
        log, opener = Log(), Opener()

        self.assertEqual(launcher.open_dashboard(log, self.port, opener), "port_taken")
        self.assertEqual(opener.urls, [])
        self.assertIs(launcher.server_process, self.saved_process)
        self.assertEqual(servers_on(self.port), [])
        self.assertEqual(log, [(f"Dashboard not opened: port {self.port} is in use by another program", "WARNING")])

    # U5
    def test_u5_a_server_that_cannot_start(self):
        os.environ["GOW_INTERNET_CHECK_URL"] = "not-a-url"
        log, opener = Log(), Opener()
        began = time.monotonic()
        outcome = launcher.open_dashboard(log, self.port, opener)
        elapsed = time.monotonic() - began

        self.assertEqual(outcome, "start_failed")
        self.assertLess(elapsed, 3)
        self.assertEqual(opener.urls, [])
        self.assertEqual(log, [(f"Dashboard could not start, see {self.log_path}. Claims continue.", "WARNING")])
        self.assertEqual(launcher.server_process.wait(5), 1)
        self.assertIn("Error: GOW_INTERNET_CHECK_URL", read(self.log_path))
        self.assertNotIn("not-a-url", read(self.log_path))

    def test_u5_a_server_that_never_answers_gives_up_at_the_ready_timeout(self):
        child = SimpleNamespace(poll=lambda: None)
        with unittest_mock.patch.object(launcher, "start_server", return_value=child), \
                unittest_mock.patch.object(launcher, "READY_TIMEOUT_SECONDS", 0.5):
            log, opener = Log(), Opener()
            began = time.monotonic()
            self.assertEqual(launcher.open_dashboard(log, self.port, opener), "start_failed")
            self.assertLess(time.monotonic() - began, 2)
        self.assertEqual(opener.urls, [])
        self.assertEqual(len(log), 1)

    def test_the_winner_of_a_race_is_reported_though_our_server_exited(self):
        # Two runs at once: ours loses the bind and exits, the other run's server answers
        answers = iter(["none", "dashboard"])
        lost = SimpleNamespace(poll=lambda: 1)
        with unittest_mock.patch.object(launcher, "start_server", return_value=lost), \
                unittest_mock.patch.object(launcher, "probe", side_effect=lambda port: next(answers)):
            log, opener = Log(), Opener()
            self.assertEqual(launcher.open_dashboard(log, self.port, opener), "started")
        self.assertEqual(opener.urls, [url_of(self.port)])
        self.assertEqual(log, [(f"Dashboard started: {url_of(self.port)} (opened in your browser)", "INFO")])

    # U6
    def test_u6_the_browser_fails(self):
        self.dash(port=self.port)
        for name, opener in (("returns False", Opener(result=False)), ("returns None", Opener(result=None)),
                             ("raises", Opener(raises=webbrowser.Error("no browser")))):
            with self.subTest(name):
                log = Log()
                self.assertEqual(launcher.open_dashboard(log, self.port, opener), "browser_failed")
                self.assertEqual(opener.urls, [url_of(self.port)])
                self.assertEqual(log, [(f"Dashboard is at {url_of(self.port)}, but the browser could not be opened",
                                        "WARNING")])

    def test_u6_the_browser_fails_after_a_start(self):
        log, opener = Log(), Opener(raises=webbrowser.Error("no browser"))
        self.assertEqual(launcher.open_dashboard(log, self.port, opener), "browser_failed")
        self.assertEqual(log, [(f"Dashboard is at {url_of(self.port)}, but the browser could not be opened",
                                "WARNING")])
        self.assertEqual(launcher.probe(self.port), "dashboard")


class Launch(LauncherCase):
    def setUp(self):
        super().setUp()
        self.started = []
        patcher = unittest_mock.patch.object(launcher, "start_server", side_effect=self.no_server)
        patcher.start()
        self.addCleanup(patcher.stop)

    def no_server(self, port):
        self.started.append(port)
        raise RuntimeError("this test starts no server")

    def gow_threads(self):
        return [t for t in threading.enumerate() if t.name == "gow-dashboard"]

    def assert_skipped(self, no_dashboard=False):
        log = Log()
        thread = launcher.launch(log, no_dashboard)
        self.assertIsNone(thread)
        self.assertEqual(self.gow_threads(), [])
        return log

    # U7
    def test_u7_skips_silently(self):
        cases = (
            ("no_dashboard", {}, True, True),
            ("GOW_RUN_SOURCE=dashboard", {"GOW_RUN_SOURCE": "dashboard"}, True, False),
            ("not interactive", {}, False, False),
            ("GOW_EVENTS_FILE empty", {"GOW_EVENTS_FILE": ""}, True, False),
            ("GOW_DASHBOARD_PORT empty", {"GOW_DASHBOARD_PORT": ""}, True, False),
            ("mock base URL, no event file", {"GOW_EVENTS_FILE": None}, True, False),
        )
        for name, env, tty, no_dashboard in cases:
            with self.subTest(name):
                values = {k: v for k, v in env.items() if v is not None}
                with unittest_mock.patch.dict(os.environ, values), \
                        unittest_mock.patch.object(launcher, "interactive", return_value=tty):
                    for k, v in env.items():
                        if v is None:
                            os.environ.pop(k, None)
                    self.assertTrue(os.environ.get("GOW_BASE_URL"))
                    log = self.assert_skipped(no_dashboard)
                self.assertEqual(log, [])
                self.assertEqual(self.browser, [])
                self.assertEqual(self.started, [])

    def test_u7_a_bad_port_warns_once(self):
        os.environ["GOW_DASHBOARD_PORT"] = "abc"
        with unittest_mock.patch.object(launcher, "interactive", return_value=True):
            log = self.assert_skipped()
        self.assertEqual(log, [(PORT_PROBLEM, "WARNING")])
        self.assertEqual(self.browser, [])

    def test_u7_partner_all_conditions_met_does_launch(self):
        log = Log()
        with unittest_mock.patch.object(launcher, "interactive", return_value=True):
            thread = launcher.launch(log)
        self.assertIsNotNone(thread)
        thread.join(5)
        self.assertEqual(self.started, [self.port])
        self.assertEqual(log, [("Dashboard not opened: RuntimeError", "WARNING")])

    def test_interactive_follows_stdin(self):
        class Raises:
            def isatty(self):
                raise ValueError("I/O operation on closed file")

        for stdin, expected in ((SimpleNamespace(isatty=lambda: True), True),
                                (SimpleNamespace(isatty=lambda: False), False),
                                (Raises(), False), (None, False)):
            with self.subTest(stdin):
                with unittest_mock.patch.object(sys, "stdin", stdin):
                    self.assertIs(launcher.interactive(), expected)

    # U8
    def test_u8_launch_never_waits_for_the_dashboard(self):
        calls = []

        def slow(log, port, opener):
            calls.append((log, port, opener))
            time.sleep(3)

        log = Log()
        with unittest_mock.patch.object(launcher, "open_dashboard", slow), \
                unittest_mock.patch.object(launcher, "interactive", return_value=True):
            began = time.monotonic()
            thread = launcher.launch(log)
            elapsed = time.monotonic() - began
            self.addCleanup(thread.join, 5)
            self.assertLess(elapsed, 0.2)
            self.assertTrue(thread.daemon)
            self.assertEqual(thread.name, "gow-dashboard")
            self.assertTrue(thread.is_alive())

            wait_until(lambda: calls, 2, "the thread to call open_dashboard")
            self.assertIs(calls[0][0], log)
            self.assertEqual(calls[0][1], self.port)

            # The opener finds webbrowser.open when it is called, so a later replacement applies
            later = []
            with unittest_mock.patch.object(webbrowser, "open", lambda url: later.append(url) or True):
                self.assertTrue(calls[0][2]("http://127.0.0.1:1/x"))
            self.assertEqual(later, ["http://127.0.0.1:1/x"])

    def test_finish_waits_a_bounded_time_and_never_raises(self):
        stop = threading.Event()
        slow = threading.Thread(target=stop.wait, args=(10,), daemon=True)
        slow.start()
        self.addCleanup(stop.set)
        with unittest_mock.patch.object(launcher, "FINISH_WAIT_SECONDS", 0.3):
            began = time.monotonic()
            launcher.finish(slow)
            self.assertLess(time.monotonic() - began, 1.5)
        launcher.finish(None)
        launcher.finish(object())
        unstarted = threading.Thread(target=lambda: None)
        launcher.finish(unstarted)
        self.assertEqual(launcher.FINISH_WAIT_SECONDS, 8)

    # U9
    def test_u9_launch_never_raises(self):
        with unittest_mock.patch.object(launcher, "interactive", return_value=True):
            with unittest_mock.patch.object(events, "events_path", side_effect=RuntimeError("boom")):
                self.assertIsNone(launcher.launch(Log()))
            with unittest_mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("no threads")):
                self.assertIsNone(launcher.launch(Log()))
            with unittest_mock.patch.object(launcher, "port_setting", side_effect=RuntimeError("boom")):
                self.assertIsNone(launcher.launch(Log()))

            def broken_log(msg, level="INFO"):
                raise OSError("stdout gone")

            os.environ["GOW_DASHBOARD_PORT"] = "abc"
            self.assertIsNone(launcher.launch(broken_log))
            os.environ["GOW_DASHBOARD_PORT"] = str(self.port)

            # The thread swallows a failing log line too
            escaped = []
            with unittest_mock.patch.object(threading, "excepthook", escaped.append):
                thread = launcher.launch(broken_log)
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(escaped, [])


class Controls(LauncherCase):
    """Each asserts the broken behaviour, so its partner test is shown able to fail"""

    def test_k1_without_the_source_check_a_dashboard_run_opens_the_browser(self):
        copy = self.launcher_copy('os.environ.get("GOW_RUN_SOURCE") == "dashboard"', "False")
        copy.interactive = lambda: True
        copy.open_dashboard = lambda log, port, opener: opener(url_of(port))
        os.environ["GOW_RUN_SOURCE"] = "dashboard"

        thread = copy.launch(Log())
        self.assertIsNotNone(thread)
        thread.join(5)
        self.assertEqual(self.browser, [url_of(self.port)])

        # The real launcher, the same way, opens nothing
        with unittest_mock.patch.object(launcher, "interactive", return_value=True):
            self.assertIsNone(launcher.launch(Log()))

    def test_k4_run_inline_launch_waits_for_the_dashboard(self):
        copy = self.launcher_copy("thread.start()", "thread.run()")
        copy.interactive = lambda: True
        copy.open_dashboard = lambda log, port, opener: time.sleep(3)
        began = time.monotonic()
        copy.launch(Log())
        self.assertGreaterEqual(time.monotonic() - began, 3)

    def test_k5_a_script_env_that_sets_no_port_is_refused(self):
        source = inspect.getsource(gow_support.script_env)
        line = '        GOW_DASHBOARD_PORT="",\n'
        self.assertIn(line, source, "the control no longer matches the code")
        namespace = dict(vars(gow_support))
        exec(source.replace(line, ""), namespace)
        mock = SimpleNamespace(base_url="http://127.0.0.1:1/en-us")
        os.environ.pop("GOW_DASHBOARD_PORT")

        with self.assertRaises(UnsafeRun):
            assert_safe_env(namespace["script_env"](mock, "/tmp/gow/fake-accounts"))
        assert_safe_env(gow_support.script_env(mock, "/tmp/gow/fake-accounts"))


class EndToEnd(DashTestCase):
    """The real scripts under script_harness, on the mock, with one account"""

    def terminal(self, script="claim_all_characters.py", extra_args=(), harness=None, env_extra=None, port=None,
                 account=None):
        port = port or free_port()
        self.assertNotEqual(port, launcher.DEFAULT_PORT)
        temp = tempfile.mkdtemp(prefix="gow-launch-")
        self.addCleanup(shutil.rmtree, temp, True)
        events_file = os.path.join(temp, "data", "events.jsonl")
        self.addCleanup(kill_servers, port)
        mock = self.make_mock({EMAIL: account or Account()})
        result = self.run_gow(script, mock, users_text([EMAIL]), *range_args(script, 1), *extra_args,
                              harness=harness or TERMINAL, timeout=120,
                              env_extra={"GOW_EVENTS_FILE": events_file, "GOW_DASHBOARD_PORT": str(port),
                                         **(env_extra or {})})
        self.addCleanup(kill_servers, port, [s["pid"] for s in result.spy.get("dashboard_servers", [])])
        return result, port, events_file

    def dashboard_lines(self, result):
        return [line for line in result.stdout.splitlines() if "Dashboard" in line]

    def assert_claimed(self, script, result):
        self.assertEqual(result.returncode, 0, result.output[-2000:])
        self.assertIn(SUCCESS_MESSAGE[script], result.stdout)
        self.assertIn("SUMMARY", result.stdout)

    def assert_started_line(self, result, port):
        lines = self.dashboard_lines(result)
        self.assertEqual(len(lines), 1, result.stdout[-3000:])
        self.assertRegex(lines[0], rf"\[INFO\] Dashboard started: http://127\.0\.0\.1:{port}/ "
                                   rf"\(opened in your browser\)$")

    # E1
    def test_e1_a_terminal_run_starts_and_opens_the_dashboard(self):
        script = "claim_all_characters.py"
        result, port, events_file = self.terminal(script)
        self.assert_claimed(script, result)
        self.assert_started_line(result, port)
        self.assertNotRegex(result.stdout, r"(?m)^Dashboard: http")
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.spy["browser_opens"], [url_of(port)])
        self.assertEqual(len(result.spy["dashboard_servers"]), 1)
        server = result.spy["dashboard_servers"][0]
        self.assertIn(str(port), server["args"])
        self.assertTrue(is_test_server(server["pid"], port))

        # After the script has gone, the server stays, and shows the run as finished
        self.assertEqual(launcher.probe(port), "dashboard")
        status, state = get_state(port)
        self.assertEqual(status, 200)
        self.assertEqual(state["run"]["source"], "terminal")
        self.assertEqual(state["status"], "finished")
        locks = events_file + ".locks"
        held = open_paths(server["pid"])
        self.assertEqual([p for p in held if p.startswith(locks) or p.startswith(os.path.realpath(locks))], [])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(os.path.dirname(events_file),
                                                           "dashboard-server.log")).st_mode), 0o600)

    # E2
    def test_e2_a_running_dashboard_is_only_opened(self):
        port = free_port()
        dash = self.dash(port=port)
        self.assertEqual(dash.port, port)
        script = "claim_all_characters.py"
        result, _, _ = self.terminal(script, port=port)
        self.assert_claimed(script, result)
        lines = self.dashboard_lines(result)
        self.assertEqual(len(lines), 1, result.stdout[-3000:])
        self.assertRegex(lines[0], rf"\[INFO\] Dashboard: http://127\.0\.0\.1:{port}/ \(opened in your browser\)$")
        self.assertEqual(result.spy["dashboard_servers"], [])
        self.assertEqual(result.spy["browser_opens"], [url_of(port)])

    def assert_untouched(self, script, result, port):
        self.assert_claimed(script, result)
        self.assertEqual(self.dashboard_lines(result), [])
        self.assertEqual(result.spy["browser_opens"], [])
        self.assertEqual(result.spy["dashboard_servers"], [])
        self.assertEqual(launcher.probe(port), "none")

    # E3
    def test_e3_no_dashboard_flag(self):
        result, port, _ = self.terminal(extra_args=("--no-dashboard",))
        self.assert_untouched("claim_all_characters.py", result, port)

    # E4
    def test_e4_a_dashboard_started_run(self):
        result, port, _ = self.terminal(env_extra={"GOW_RUN_SOURCE": "dashboard"})
        self.assert_untouched("claim_all_characters.py", result, port)

    # E5
    def test_e5_no_terminal(self):
        result, port, _ = self.terminal(harness={"default_stagger": 0})
        self.assert_untouched("claim_all_characters.py", result, port)

    # E6
    def test_e6_the_other_two_scripts(self):
        for script in ("claim_batch.py", "claim_all.py"):
            with self.subTest(script):
                result, port, _ = self.terminal(script)
                self.assert_claimed(script, result)
                self.assert_started_line(result, port)
                self.assertEqual(result.spy["browser_opens"], [url_of(port)])
                self.assertEqual(len(result.spy["dashboard_servers"]), 1)

    # E7
    def test_e7_ctrl_c_leaves_the_dashboard_running(self):
        script = "claim_all_characters.py"
        port = free_port()
        temp = tempfile.mkdtemp(prefix="gow-launch-")
        self.addCleanup(shutil.rmtree, temp, True)
        self.addCleanup(kill_servers, port)
        mock = self.make_mock({EMAIL: Account(vault_delay_ms=25000)})
        result, _ = self.interrupted_run(
            script, mock, users_text([EMAIL]), (*range_args(script, 1),), trigger="Dashboard started",
            harness=TERMINAL, group=True, limit=30,
            env_extra={"GOW_EVENTS_FILE": os.path.join(temp, "data", "events.jsonl"), "GOW_DASHBOARD_PORT": str(port)})

        self.assertEqual(result.returncode, 1, result.output[-2000:])
        self.assertIn("Interrupted by user", result.stdout)
        self.assertNotIn("SUMMARY", result.stdout)
        self.assertLess(result.seconds, 30)
        time.sleep(1)
        self.assertEqual(launcher.probe(port), "dashboard")
        self.assertEqual(len(servers_on(port)), 1)
        self.assertEqual(result.spy["browser_opens"], [url_of(port)])

    # A2
    def test_a2_help_lists_the_flag(self):
        mock = self.make_mock()
        for script in ("claim_all_characters.py", "claim_all.py", "claim_batch.py"):
            with self.subTest(script):
                result = self.run_gow(script, mock, "", "--help", timeout=60)
                self.assertEqual(result.returncode, 0, result.output)
                self.assertIn("[--no-dashboard]", result.stdout)
                self.assertNotIn("Dashboard", result.stdout.replace("--no-dashboard", ""))


class CopyControls(DashTestCase):
    """K2 and K3: the real script from a scratch copy with one protection gone"""

    def copy_run(self, replace, during=None):
        port = free_port()
        temp = tempfile.mkdtemp(prefix="gow-launch-")
        self.addCleanup(shutil.rmtree, temp, True)
        self.addCleanup(kill_servers, port)
        mock = self.make_mock({EMAIL: Account()})
        self.last_copy = None
        if during:
            threading.Thread(target=during, args=(port,), daemon=True).start()
        result = run_copy(self, "claim_all_characters.py", mock, users_text([EMAIL]), ("1", "1"),
                          replace=[("dashboard/launcher.py", *replace)], harness=TERMINAL,
                          events_file=os.path.join(temp, "data", "events.jsonl"),
                          env_extra={"GOW_DASHBOARD_PORT": str(port)}, timeout=120)
        with open(os.path.join(self.last_copy, "spy.json")) as f:
            spy = json.load(f)
        self.addCleanup(kill_servers, port, [s["pid"] for s in spy["dashboard_servers"]])
        return result, spy, port

    def test_k2_without_a_new_session_the_server_shares_the_run_session(self):
        result, spy, port = self.copy_run(("start_new_session=True", "start_new_session=False"))
        self.assertEqual(result.returncode, 0, result.output[-2000:])
        self.assertEqual(len(spy["dashboard_servers"]), 1)
        pid = spy["dashboard_servers"][0]["pid"]
        self.assertTrue(is_test_server(pid, port))
        self.assertNotEqual(os.getsid(pid), pid)

    def test_k3_without_the_log_file_the_server_line_leaks_into_the_run(self):
        def stop_server_once_the_run_exits(port):
            # The server holds the run's stdout pipe open, so the run can't be read to the end until it goes
            wait_until(lambda: self.last_copy and os.path.exists(os.path.join(self.last_copy, "spy.json")),
                       100, "the run to exit")
            time.sleep(1)
            kill_servers(port)

        result, spy, port = self.copy_run(("stdout=log_fd", "stdout=None"), during=stop_server_once_the_run_exits)
        self.assertRegex(result.stdout, rf"(?m)^Dashboard: http://127\.0\.0\.1:{port}/$")


class Static(unittest.TestCase):
    # S3
    def test_s3_launcher_matches_the_server(self):
        from dashboard import server
        self.assertEqual(launcher.HOST, server.HOST)
        self.assertEqual(launcher.DEFAULT_PORT, server.DEFAULT_PORT)
        self.assertEqual(server.DEFAULT_PORT, int("87" + "65"))
        self.assertEqual(launcher.SERVER_SCRIPT, os.path.join(PROJECT, "dashboard", "server.py"))
        self.assertEqual((launcher.PROBE_TIMEOUT_SECONDS, launcher.READY_TIMEOUT_SECONDS,
                          launcher.READY_POLL_SECONDS, launcher.FINISH_WAIT_SECONDS, launcher.LOG_NAME),
                         (1, 5, 0.1, 8, "dashboard-server.log"))

    def test_s3_launcher_imports_only_events_and_the_stdlib(self):
        with open(launcher.__file__, encoding="utf-8") as f:
            text = f.read()
        self.assertNotRegex(text, r"(?m)^\s*(from|import)\s+.*\b(network|server|store)\b")
        imported = re.findall(r"(?m)^\s*(?:from\s+(\S+)\s+import|import\s+(\S+))", text)
        modules = sorted({a or b for a, b in imported})
        self.assertEqual(modules, sorted(["os", "re", "subprocess", "sys", "threading", "time", "urllib.error",
                                          "urllib.request", "webbrowser", "dashboard"]))
        self.assertIn("from dashboard import events\n", text)

    def test_s3_importing_the_launcher_loads_no_other_dashboard_module(self):
        program = ("import sys; sys.path.insert(0, sys.argv[1]); from dashboard import launcher; "
                   "print(sorted(m for m in sys.modules if m.startswith('dashboard')))")
        done = subprocess.run([PYTHON, "-B", "-c", program, PROJECT], capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "['dashboard', 'dashboard.events', 'dashboard.launcher']")

    def test_claim_common_falls_back_when_the_launcher_is_broken(self):
        with open(os.path.join(PROJECT, "claim_common.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertRegex(text, r"try:\n\s+from dashboard import launcher\nexcept Exception:\n\s+launcher = NoEvents\(\)")

    # S4
    def test_s4_no_test_uses_the_real_port(self):
        real = "87" + "65"
        bare = re.compile(rf"(?<![\w\"'.]){real}(?![\w\"'])|(?<![\w\"'.])0+{real}(?![\w\"'])")
        offenders = []
        for path in sorted(glob.glob(os.path.join(TESTS_DIR, "*.py"))):
            with open(path, encoding="utf-8") as f:
                for number, line in enumerate(f, 1):
                    if real not in line:
                        continue
                    as_port = re.search(rf"--port\W+0*{real}\b|:0*{real}\b|port\s*=\s*0*{real}\b|env_extra.*{real}"
                                        rf"|\(\s*[\"']127\.0\.0\.1[\"']\s*,\s*0*{real}\b", line)
                    if as_port or (bare.search(line) and "==" not in line):
                        offenders.append(f"{os.path.basename(path)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [])


class Docs(unittest.TestCase):
    def test_readme_documents_the_feature(self):
        with open(os.path.join(PROJECT, "README.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertRegex(text, r"\| `--no-dashboard` \| off \| Don't open or start the dashboard \|")
        for needed in ("GOW_DASHBOARD_PORT", "dashboard/data/dashboard-server.log", "lsof -i :" + "87" + "65"):
            self.assertIn(needed, text)
        self.assertFalse(os.path.exists(os.path.join(PROJECT, "dashboard", "README.md")))


if __name__ == "__main__":
    unittest.main()
