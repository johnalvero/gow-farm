"""In-app update (plan: in-app-update). Every server here runs from a TEMP PROJECT whose origin is a
local bare repo: nothing contacts GitHub, nothing touches the real checkout's git state, and nothing
listens on the real dashboard's port. A guard refuses update checks for any server under PROJECT."""

import http.client
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import dash_support
import test_dashboard_page as page_tests
from dash_support import Dash, DashTestCase, assert_safe_dashboard_env, event, run_start, write_events
from gow_support import PROJECT, PYTHON, TESTS_DIR, GowTestCase, command_of, kill_all, wait_until

from dashboard import events

GIT = "/usr/bin/git"
UPDATE_HARNESS = os.path.join(TESTS_DIR, "update_harness.py")
REAL_PORT = 8765
KEYS = {"enabled", "current", "available", "behind", "checking", "checked_seconds_ago", "problem"}
FIRST_DATE = "2026-11-18T12:00:00+00:00"
LATER_DATE = "2026-11-19T12:00:00+00:00"

UPDATES_OFF = {"error": "Update checks are off for this dashboard."}
LIVE_RUN = {"error": "A claim run is going. Update after it finishes."}
LOCAL_CHANGES = "This folder has local changes to project files, so it can't update. Nothing was changed."
DIVERGED = "This copy has its own changes that GitHub doesn't have, so it can't update here. Nothing was changed."
WOULD_REPLACE = "The update would replace files kept only on this Mac, so nothing was changed."
NEW_VERSION_ERROR = "The new version has an error, so nothing was changed."
UNDONE = "The new version failed its startup check, so the update was undone."
UP_TO_DATE = "Already up to date."
UPDATING_TEXT = "Updating… the page will reload"
LOST_TEXT = ("The dashboard didn't come back after updating. "
             "See dashboard/data/dashboard-service.log, or double-click update.command.")

COPIED = ("dashboard/__init__.py", "dashboard/service.sh")


def git_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", LC_ALL="C",
               GIT_AUTHOR_NAME="Tester", GIT_AUTHOR_EMAIL="tester@x.test",
               GIT_COMMITTER_NAME="Tester", GIT_COMMITTER_EMAIL="tester@x.test",
               GIT_AUTHOR_DATE=LATER_DATE, GIT_COMMITTER_DATE=LATER_DATE)
    return env


def git(cwd, *args, check=True, env=None):
    done = subprocess.run([GIT, *args], cwd=cwd, env=env or git_env(), capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    if check and done.returncode != 0:
        raise AssertionError(f"git {args} failed in {cwd}: {done.stderr}")
    return done.stdout.strip()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class Project:
    """A copy of the dashboard code in a temp folder, a git repo with a local bare origin, and a second
    clone of that origin that stands in for 'someone pushed to GitHub'"""

    def __init__(self, case, with_git=True, parent_repo=False):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="gow-upd-"))
        case.addCleanup(shutil.rmtree, self.root, True)
        self.parent = self.root
        if parent_repo:
            git(self.root, "init", "-q", "-b", "main")
            write(os.path.join(self.root, "parent.txt"), "parent")
            git(self.root, "add", ".")
            git(self.root, "commit", "-q", "-m", "parent")
            self.dir = os.path.join(self.root, "proj")
        else:
            self.dir = os.path.join(self.root, "proj")
        self.bare = os.path.join(self.root, "origin.git")
        self.other = os.path.join(self.root, "other")
        self.events_file = os.path.join(self.root, "events", "events.jsonl")
        self.server_path = os.path.join(self.dir, "dashboard", "server.py")

        os.makedirs(os.path.join(self.dir, "dashboard", "static"))
        for name in sorted(os.listdir(os.path.join(PROJECT, "dashboard"))):
            source = os.path.join(PROJECT, "dashboard", name)
            if name.endswith(".py") or name in ("service.sh",):
                shutil.copy(source, os.path.join(self.dir, "dashboard", name))
        for name in os.listdir(os.path.join(PROJECT, "dashboard", "static")):
            shutil.copy(os.path.join(PROJECT, "dashboard", "static", name),
                        os.path.join(self.dir, "dashboard", "static", name))
        write(os.path.join(self.dir, ".gitignore"), "users.txt\n.venv/\ndashboard/data/\n__pycache__/\n")
        write(os.path.join(self.dir, "VERSION.txt"), "1\n")
        write(os.path.join(self.dir, "NOTES.md"), "notes\n")
        if not with_git:
            return

        env = {**git_env(), "GIT_AUTHOR_DATE": FIRST_DATE, "GIT_COMMITTER_DATE": FIRST_DATE}
        git(self.dir, "init", "-q", "-b", "main")
        git(self.dir, "add", ".")
        git(self.dir, "commit", "-q", "-m", "first", env=env)
        git(self.root, "init", "-q", "--bare", "-b", "main", self.bare)
        git(self.dir, "remote", "add", "origin", self.bare)
        git(self.dir, "push", "-q", "-u", "origin", "main")
        git(self.root, "clone", "-q", self.bare, self.other)

    def g(self, *args, **kw):
        return git(self.dir, *args, **kw)

    def head(self):
        return self.g("rev-parse", "HEAD")

    def short(self, rev="HEAD"):
        return self.g("rev-parse", "--short=7", rev)

    def origin_head(self):
        return git(self.bare, "rev-parse", "main")

    def origin_short(self):
        return git(self.bare, "rev-parse", "--short=7", "main")

    def push(self, files, message="change", force_add=False):
        """Commit these {path: text} files in the other clone and push them to the bare origin"""
        for path, text in files.items():
            write(os.path.join(self.other, path), text)
        git(self.other, "add", "-f" if force_add else "-A", *([] if not force_add else list(files)))
        if force_add:
            git(self.other, "add", "-A", "--", ".gitignore")
        git(self.other, "commit", "-q", "-m", message)
        git(self.other, "push", "-q", "origin", "main")
        return git(self.other, "rev-parse", "HEAD")

    def edit_server(self, old, new):
        """The other clone's dashboard/server.py with one replacement, pushed"""
        path = os.path.join(self.other, "dashboard", "server.py")
        text = read(path)
        assert old in text
        return self.push({"dashboard/server.py": text.replace(old, new, 1)})

    def status(self):
        return self.g("status", "--porcelain", "--ignored")

    def files(self):
        """{relative path: bytes} of everything outside .git"""
        found = {}
        for folder, names, files in os.walk(self.dir):
            names[:] = [n for n in names if n != ".git"]
            for name in files:
                path = os.path.join(folder, name)
                with open(path, "rb") as f:
                    found[os.path.relpath(path, self.dir)] = f.read()
        return found

    def mtimes(self, paths):
        return {p: os.stat(os.path.join(self.dir, p)).st_mtime_ns for p in paths}


class TempServer:
    """One dashboard server started from a Project, on port 0"""

    def __init__(self, case, project, checks="on", env_extra=None, harness=None, mode="direct", relative=False,
                 start=True, python=None):
        if os.path.realpath(project.server_path).startswith(os.path.realpath(PROJECT) + os.sep):
            raise AssertionError("refusing: this server is under the real checkout")
        self.case = case
        self.project = project
        self.checks = checks
        self.env_extra = env_extra or {}
        self.harness = harness
        self.mode = mode
        self.relative = relative
        self.python = python or PYTHON
        self.process = None
        self.port = None
        self.token = None
        self.server_id = None
        self.child_pid = None
        self.output_file = os.path.join(project.root, f"server-{time.monotonic_ns()}.out")
        case.addCleanup(self.clean_up)
        if start:
            self.start()

    def environment(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "GOW_UPDATE_CHECKS"}
        env.update(GOW_USERS_FILE=os.path.join(self.project.root, "fake-accounts"),
                   GOW_EVENTS_FILE=self.project.events_file, GOW_DASHBOARD_PORT="",
                   PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        env.pop("GOW_INTERNET_CHECK_URL", None)
        if self.checks is not None:
            env["GOW_UPDATE_CHECKS"] = self.checks
        env.update(self.env_extra)
        if self.harness is not None:
            env["GOW_UPDATE_HARNESS"] = json.dumps({"project": self.project.dir, **self.harness})
        assert_safe_dashboard_env(env)
        return env

    def command(self):
        if self.harness is not None:
            command = [self.python, "-B", UPDATE_HARNESS, "--port", "0"]
        elif self.relative:
            command = [self.python, "-B", os.path.join("dashboard", "server.py"), "--port", "0"]
        else:
            command = [self.python, "-B", self.project.server_path, "--port", "0"]
        if self.mode == "caffeinate":
            command = ["/usr/bin/caffeinate", "-i", *command]
        return command

    def start(self):
        with open(self.output_file, "ab") as output:
            self.process = subprocess.Popen(self.command(), cwd=self.project.dir, env=self.environment(),
                                            stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                            start_new_session=self.mode != "direct")

        def listening():
            found = re.findall(r"http://127\.0\.0\.1:(\d+)/", self.output())
            if found:
                self.port = int(found[-1])
            return bool(found)

        wait_until(listening, 20, "the dashboard to print its address")
        self.case.assertNotEqual(self.port, REAL_PORT)

    def output(self):
        with open(self.output_file) as f:
            return f.read()

    def request(self, method, path, body=None, headers=()):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                connection.putheader(name, value)
            data = body.encode() if isinstance(body, str) else body
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

    def load_page(self):
        """GET /, remembering this server's token and id"""
        page = self.get("/")[2].decode()
        self.token = re.search(r'name="gow-token" content="([^"]+)"', page).group(1)
        self.server_id = re.search(r'name="gow-server" content="([^"]+)"', page).group(1)
        return page

    def post(self, path, payload=None, raw=None, host=None, origin="default", token="default",
             content_type="application/json"):
        if self.token is None:
            self.load_page()
        headers = [("Host", host or self.host())]
        if origin is not None:
            headers.append(("Origin", f"http://{self.host()}" if origin == "default" else origin))
        if token is not None:
            headers.append(("X-Gow-Token", self.token if token == "default" else token))
        if content_type is not None:
            headers.append(("Content-Type", content_type))
        status, response_headers, data = self.request(
            "POST", path, body=raw if raw is not None else json.dumps({} if payload is None else payload),
            headers=headers)
        return status, json.loads(data) if data else None

    def state(self):
        status, _, data = self.get("/api/state")
        self.case.assertEqual(status, 200)
        return json.loads(data)

    def wait_state(self, condition, timeout=15, message="the dashboard state"):
        found = {}

        def met():
            try:
                found["state"] = self.state()
            except (OSError, http.client.HTTPException):
                return False
            return condition(found["state"])

        try:
            wait_until(met, timeout, message)
        except AssertionError as e:
            raise AssertionError(f"{e}; last state {found.get('state', {}).get('update')}") from None
        return found["state"]

    def settle(self, timeout=20):
        """A first check, or one asked for, has finished"""
        return self.wait_state(lambda s: not s["update"]["checking"], timeout, "the update check to finish")["update"]

    def wait_new_server(self, old_id, timeout=15):
        return self.wait_state(lambda s: s["server_id"] != old_id, timeout, "the restarted server")

    def alive(self):
        return self.process.poll() is None

    def listener_pids(self):
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{self.port}", "-sTCP:LISTEN", "-t"], capture_output=True,
                             text=True).stdout
        return [int(p) for p in out.split()]

    def kill(self):
        if self.process and self.process.poll() is None:
            os.kill(self.process.pid, signal.SIGKILL)
        if self.process:
            self.process.wait(10)

    def clean_up(self):
        """Only what this test started, and only if its command line names this temp project"""
        for pid in (self.child_pid, self.process.pid if self.process else None):
            if pid and self.project.root in command_of(str(pid)) + " " + self.cwd_hint(pid):
                kill_all([str(pid)])
        if self.process:
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                pass

    def cwd_hint(self, pid):
        out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True).stdout
        return out


def forbidden_in(text):
    return [word for word in ("github.com", "git@", "fatal:", "error:", "://") if word in text.lower()]


class UpdateCase(GowTestCase):
    def setUp(self):
        self.project_ = None

    def project(self, **kw):
        project = Project(self, **kw)
        self.last_project = project
        return project

    def server(self, project, **kw):
        server = TempServer(self, project, **kw)
        self.last_server = server
        return server

    def started(self, project, **kw):
        """A server whose first check has finished"""
        server = self.server(project, **kw)
        server.settle()
        return server

    def assert_unchanged_after(self, project, server, action):
        head, status, files = project.head(), project.status(), project.files()
        pid, server_id = server.process.pid, server.state()["server_id"]
        result = action()
        self.assertEqual(project.head(), head)
        self.assertEqual(project.status(), status)
        self.assertEqual(project.files(), files)
        self.assertTrue(server.alive())
        self.assertEqual(server.process.pid, pid)
        self.assertEqual(server.state()["server_id"], server_id)
        return result

    def refused(self, server, project, status, message):
        def act():
            code, body = server.post("/api/update", {})
            self.assertEqual((code, body), (status, {"error": message}))
        self.assert_unchanged_after(project, server, act)


def get_json(response):
    return json.loads(response[2])


# ---------------------------------------------------------------- A: state and version


class StateAndVersion(UpdateCase):
    def test_a1_state_keys_and_current_version(self):
        project = self.project()
        server = self.started(project)
        update = server.state()["update"]
        self.assertEqual(set(update), KEYS)
        self.assertEqual(update["current"]["commit"], project.short())
        self.assertEqual(update["current"]["date"], "2026-11-18")
        self.assertRegex(update["current"]["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(update["enabled"])
        self.assertIsNone(update["problem"])
        self.assertEqual((update["behind"], update["available"]), (0, None))
        self.assertIsInstance(update["checked_seconds_ago"], int)

    def scenario_a2(self, checks="on"):
        project = self.project()
        project.push({"VERSION.txt": "2\n"})
        server = self.server(project, checks=checks)
        state = server.wait_state(lambda s: s["update"]["behind"] == 1 and not s["update"]["checking"], 15,
                                  "the origin's change to show")
        update = state["update"]
        self.assertEqual(update["available"]["commit"], project.origin_short())
        self.assertIsNone(update["problem"])
        self.assertNotEqual(update["available"]["commit"], project.short())
        return project, server

    def test_a2_origin_ahead_is_found_without_a_request(self):
        self.scenario_a2()

    def test_a3_check_now_finds_a_second_commit(self):
        project, server = self.scenario_a2()
        project.push({"VERSION.txt": "3\n"}, "second")
        began = time.monotonic()
        code, body = server.post("/api/update-check", {})
        self.assertLess(time.monotonic() - began, 1.0)
        self.assertEqual((code, body), (200, {"ok": True}))
        state = server.wait_state(lambda s: s["update"]["behind"] == 2 and not s["update"]["checking"], 15,
                                  "two commits behind")
        self.assertEqual(state["update"]["available"]["commit"], project.origin_short())

    def test_a4_state_answers_while_a_fetch_hangs_then_fetch_failed_and_group_gone(self):
        project = self.project()
        project.push({"VERSION.txt": "2\n"})
        marks = os.path.join(project.root, "marks")
        os.makedirs(marks)
        script = os.path.join(project.root, "hang-git.sh")
        write(script, f'''#!/bin/bash
if [ "$1" = fetch ]; then
  echo $$ > "{marks}/bash"
  sleep 300 &
  echo $! > "{marks}/sleep"
  wait
fi
exec {GIT} "$@"
''')
        os.chmod(script, 0o755)
        server = self.server(project, env_extra={"GOW_GIT": script}, harness={"fetch_timeout": 3})
        wait_until(lambda: os.path.exists(os.path.join(marks, "sleep")), 15, "the fetch to hang")
        pids = [int(read(os.path.join(marks, name))) for name in ("bash", "sleep")]
        for pid in pids:
            self.assertTrue(command_of(str(pid)).strip(), "the hung process should be alive during the hang")
        slowest = 0
        for _ in range(5):
            began = time.monotonic()
            state = server.state()
            slowest = max(slowest, time.monotonic() - began)
        self.assertLess(slowest, 1.0)
        self.assertTrue(state["update"]["checking"])
        state = server.wait_state(lambda s: s["update"]["problem"] == "fetch_failed", 15, "fetch_failed")
        self.assertFalse(state["update"]["checking"])
        self.assertIsNone(state["update"]["available"])
        wait_until(lambda: not any(command_of(str(pid)).strip() for pid in pids), 10, "the hung group to be gone")

    def check_problem(self, project, problem, available_null=True, **server_kw):
        server = self.server(project, **server_kw)
        update = server.wait_state(lambda s: not s["update"]["checking"] and s["update"]["problem"] is not None,
                                   20, f"problem {problem}")["update"]
        self.assertEqual(update["problem"], problem)
        if available_null:
            self.assertIsNone(update["available"])
        # A6: nothing that looks like a remote, a path or git's own words, in anything this server says
        texts = [json.dumps(update)]
        code, body = server.post("/api/update", {})
        self.assertEqual(code, 409)
        texts.append(json.dumps(body))
        code, body = server.post("/api/update-check", {})
        texts.append(json.dumps(body))
        server.settle()
        texts.append(json.dumps(server.state()))
        log = "\n".join(line for line in server.output().splitlines() if not line.startswith("Dashboard:"))
        texts.append(log)
        everything = "\n".join(texts)
        self.assertEqual(forbidden_in(everything), [], everything)
        self.assertNotIn(project.bare, everything)
        self.assertNotIn(project.root, everything)
        return server

    def test_a5_not_git_even_with_a_repo_in_the_parent(self):
        project = self.project(with_git=False, parent_repo=True)
        # The control: without the ceiling, git would find the parent's repo from the project folder
        found = git(project.dir, "rev-parse", "--show-toplevel", env={**git_env(), "GIT_CEILING_DIRECTORIES": ""})
        self.assertEqual(os.path.realpath(found), project.root)
        self.check_problem(project, "not_git")

    def test_a5_detached_head_is_no_upstream(self):
        project = self.project()
        project.g("checkout", "-q", "--detach")
        self.check_problem(project, "no_upstream")

    def test_a5_branch_without_upstream_is_no_upstream(self):
        project = self.project()
        project.g("branch", "--unset-upstream")
        self.check_problem(project, "no_upstream")

    def test_a5_origin_at_a_missing_path_is_fetch_failed(self):
        project = self.project()
        project.g("remote", "set-url", "origin", os.path.join(project.root, "no-such-repo.git"))
        self.check_problem(project, "fetch_failed")

    def test_a5_edited_tracked_file_is_local_changes(self):
        project = self.project()
        write(os.path.join(project.dir, "VERSION.txt"), "edited\n")
        self.check_problem(project, "local_changes", available_null=False)

    def test_a5_local_commit_plus_origin_commit_is_diverged(self):
        project = self.project()
        write(os.path.join(project.dir, "local.txt"), "local\n")
        project.g("add", "local.txt")
        project.g("commit", "-q", "-m", "local")
        project.push({"VERSION.txt": "2\n"})
        server = self.check_problem(project, "diverged")
        self.assertEqual(server.state()["update"]["behind"], 0)

    def test_a5_missing_git_is_no_git(self):
        project = self.project()
        self.check_problem(project, "no_git", env_extra={"GOW_GIT": "/nonexistent/git"})

    def test_a7_real_checkout_default_is_off_and_untouched(self):
        saved = os.environ.pop("GOW_UPDATE_CHECKS", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "GOW_UPDATE_CHECKS", saved)
        index = os.path.join(PROJECT, ".git", "FETCH_HEAD")

        def mark():
            return (git(PROJECT, "rev-parse", "origin/main", env=os.environ.copy()),
                    os.stat(index).st_mtime_ns if os.path.exists(index) else None)

        before = mark()
        dash = Off.make(self)
        state = dash.state()
        self.assertFalse(state["update"]["enabled"])
        self.assertEqual(state["update"]["problem"], "off")
        self.assertEqual(set(state["update"]), KEYS)
        time.sleep(20)
        for path in ("/api/update-check", "/api/update"):
            status, _, data = dash.request(
                "POST", path, body="{}", headers=[("Host", dash.host()), ("Origin", f"http://{dash.host()}"),
                                                  ("X-Gow-Token", dash.token), ("Content-Type", "application/json")])
            self.assertEqual((status, json.loads(data)), (409, UPDATES_OFF))
        self.assertEqual(dash.state()["update"]["problem"], "off")
        self.assertEqual(mark(), before)


class Off:
    @staticmethod
    def make(case):
        mock = case.make_mock({}, hydrate_ms=100)
        return Dash(case, mock, "", harness={})


# ---------------------------------------------------------------- B: gating


class Gating(UpdateCase):
    def test_b1_gating_for_both_routes(self):
        project = self.project()
        project.push({"VERSION.txt": "2\n"})
        server = self.started(project)
        server.load_page()
        head, server_id = project.head(), server.state()["server_id"]
        for path in ("/api/update", "/api/update-check"):
            cases = [
                ("wrong token", dict(token="wrong"), 403, "Token not accepted"),
                ("no origin", dict(origin=None), 403, "Origin not allowed"),
                ("foreign origin", dict(origin="http://evil.test"), 403, "Origin not allowed"),
                ("bad host", dict(host="evil.test"), 403, "Host not allowed"),
                ("not json", dict(content_type="text/plain"), 415, None),
                ("fields", dict(payload={"x": 1}), 400, "No fields are accepted"),
            ]
            for name, options, status, message in cases:
                with self.subTest(path=path, case=name):
                    code, body = server.post(path, **{"payload": {}, **options})
                    self.assertEqual(code, status)
                    if message:
                        self.assertEqual(body, {"error": message})
            with self.subTest(path=path, case="GET"):
                self.assertEqual(server.get(path)[0], 405)
            with self.subTest(path=path, case="effects"):
                self.assertEqual(project.head(), head)
                self.assertEqual(server.state()["server_id"], server_id)
                self.assertTrue(server.alive())

    def test_b2_live_run_refuses_update(self):
        project = self.project()
        project.push({"VERSION.txt": "2\n"})
        server = self.started(project)
        run = "20261101T000000Z-1"
        lock = events.lock_path(project.events_file, run)
        os.makedirs(os.path.dirname(lock), mode=0o700, exist_ok=True)
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        write_events(project.events_file, [run_start(run, time.time(), total=2)])
        server.wait_state(lambda s: s["live"], 15, "the live run")
        head, pid, sid = project.head(), server.process.pid, server.state()["server_id"]
        code, body = server.post("/api/update", {})
        self.assertEqual((code, body), (409, LIVE_RUN))
        self.assertEqual(project.head(), head)
        self.assertTrue(server.alive())
        self.assertEqual(server.process.pid, pid)
        self.assertEqual(server.state()["server_id"], sid)

    def test_b3_run_is_refused_while_updating(self):
        project = self.project()
        project.push({"VERSION.txt": "2\n"})
        marks = os.path.join(project.root, "merging")
        script = os.path.join(project.root, "slow-merge-git.sh")
        write(script, f'''#!/bin/bash
if [ "$1" = merge ]; then
  touch "{marks}"
  sleep 4
fi
exec {GIT} "$@"
''')
        os.chmod(script, 0o755)
        server = self.started(project, env_extra={"GOW_GIT": script})
        server.load_page()
        outcome = {}

        def update():
            outcome["update"] = server.post("/api/update", {})

        thread = threading.Thread(target=update)
        thread.start()
        try:
            wait_until(lambda: os.path.exists(marks), 15, "the merge to start")
            code, body = server.post("/api/run", {"concurrency": 1})
            self.assertEqual((code, body), (409, {"error": "The dashboard is updating"}))
        finally:
            thread.join(60)
        self.assertEqual(outcome["update"][0], 200)


# ---------------------------------------------------------------- C: refusals change nothing


class Refusals(UpdateCase):
    def test_c1_edited_tracked_file(self):
        project = self.project()
        project.push({"VERSION.txt": "2\n"})
        write(os.path.join(project.dir, "VERSION.txt"), "edited\n")
        server = self.started(project)
        self.refused(server, project, 409, LOCAL_CHANGES)

    def test_c2_diverged(self):
        project = self.project()
        write(os.path.join(project.dir, "local.txt"), "local\n")
        project.g("add", "local.txt")
        project.g("commit", "-q", "-m", "local")
        project.push({"VERSION.txt": "2\n"})
        server = self.started(project)
        self.refused(server, project, 409, DIVERGED)

    def scenario_c3(self, harness=None, env_extra=None):
        project = self.project()
        write(os.path.join(project.dir, "users.txt"), "LOCAL")
        os.makedirs(os.path.join(project.dir, "dashboard", "data"))
        write(os.path.join(project.dir, "dashboard", "data", "marker"), "keep")
        project.push({"users.txt": "FROM ORIGIN", "dashboard/data/x": "FROM ORIGIN"}, force_add=True)
        server = self.started(project, harness=harness, env_extra=env_extra)
        self.refused(server, project, 409, WOULD_REPLACE)
        self.assertEqual(read(os.path.join(project.dir, "users.txt")), "LOCAL")
        self.assertFalse(os.path.exists(os.path.join(project.dir, "dashboard", "data", "x")))
        return project, server

    def test_c3_ignored_local_files_are_never_replaced(self):
        self.scenario_c3()

    def test_c3_only_a_data_path_that_is_ignored_but_absent(self):
        """Nothing at the path on disk: only the ignore check can protect it"""
        project = self.project()
        os.makedirs(os.path.join(project.dir, "dashboard", "data"))
        write(os.path.join(project.dir, "dashboard", "data", "marker"), "keep")
        project.push({"dashboard/data/x": "FROM ORIGIN"}, force_add=True)
        server = self.started(project)
        self.refused(server, project, 409, WOULD_REPLACE)

    def test_c3_a_parent_that_is_a_file(self):
        project = self.project()
        write(os.path.join(project.dir, "sub"), "I am a file")
        project.push({"sub/inner.txt": "x"})
        server = self.started(project)
        self.refused(server, project, 409, WOULD_REPLACE)
        self.assertEqual(read(os.path.join(project.dir, "sub")), "I am a file")

    def test_c3_an_untracked_file_at_a_new_path(self):
        project = self.project()
        write(os.path.join(project.dir, "notes.txt"), "mine")
        project.push({"notes.txt": "theirs"})
        server = self.started(project)
        self.refused(server, project, 409, WOULD_REPLACE)
        self.assertEqual(read(os.path.join(project.dir, "notes.txt")), "mine")

    def test_c4_syntax_error_is_refused(self):
        project = self.project()
        server_text = read(os.path.join(project.dir, "dashboard", "server.py"))
        project.push({"dashboard/server.py": server_text + "\ndef (\n"})
        server = self.started(project)
        self.refused(server, project, 409, NEW_VERSION_ERROR)

    def test_c4_match_statement_follows_the_interpreter(self):
        """3.10 syntax: refused below 3.10, accepted (and restarted into) on 3.10 and later"""
        version = subprocess.run([PYTHON, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
                                 capture_output=True, text=True).stdout.split()
        modern = (int(version[0]), int(version[1])) >= (3, 10)
        project = self.project()
        text = read(os.path.join(project.dir, "dashboard", "server.py"))
        project.push({"dashboard/server.py": text.replace(
            "def main():", "def _unused(x):\n    match x:\n        case 1:\n            return 1\n\ndef main():", 1)})
        server = self.started(project)
        if modern:
            old_id = server.state()["server_id"]
            code, body = server.post("/api/update", {})
            self.assertEqual((code, body["ok"]), (200, True))
            server.wait_new_server(old_id)
            self.assertEqual(project.head(), project.origin_head())
        else:
            self.refused(server, project, 409, NEW_VERSION_ERROR)

    def test_c5_already_up_to_date_and_the_updating_flag_is_cleared(self):
        project = self.project()
        server = self.started(project)
        self.refused(server, project, 409, UP_TO_DATE)
        # A flag left set by a refusal would turn this into "The dashboard is updating"
        self.refused(server, project, 409, UP_TO_DATE)

    def scenario_c6(self, harness=None):
        project = self.project()
        old = project.head()
        project.edit_server("import argparse\n", "import argparse\nimport no_such_module_gow\n")
        server = self.started(project, harness=harness)
        pid, sid = server.process.pid, server.state()["server_id"]
        code, body = server.post("/api/update", {})
        self.assertEqual((code, body), (500, {"error": UNDONE}))
        self.assertEqual(project.head(), old)
        self.assertEqual(project.g("status", "--porcelain"), "")
        self.assertTrue(server.alive())
        self.assertEqual(server.process.pid, pid)
        self.assertEqual(server.state()["server_id"], sid)
        return project, server

    def test_c6_a_new_version_that_cannot_start_is_undone(self):
        project, server = self.scenario_c6()
        self.assertNotIn("Updated from", server.output())

    def test_c7_untracked_and_ignored_files_survive_an_update(self):
        project = self.project()
        write(os.path.join(project.dir, "notes.txt"), "untracked")
        write(os.path.join(project.dir, ".venv", "marker"), "venv")
        write(os.path.join(project.dir, "dashboard", "data", "marker"), "data")
        write(os.path.join(project.dir, "users.txt"), "LOCAL")
        project.push({"VERSION.txt": "2\n"})
        server = self.started(project)
        kept = ["notes.txt", ".venv/marker", "dashboard/data/marker", "users.txt"]
        before_bytes = {p: read(os.path.join(project.dir, p)) for p in kept}
        before_times = project.mtimes(kept)
        old_id = server.state()["server_id"]
        code, body = server.post("/api/update", {})
        self.assertEqual(code, 200, body)
        server.wait_new_server(old_id)
        self.assertEqual({p: read(os.path.join(project.dir, p)) for p in kept}, before_bytes)
        self.assertEqual(project.mtimes(kept), before_times)
        self.assertEqual(read(os.path.join(project.dir, "VERSION.txt")), "2\n")
        self.assertEqual(project.head(), project.origin_head())


    def test_c8_case_only_rename_is_not_refused(self):
        probe = tempfile.mkdtemp(prefix="gow-case-")
        self.addCleanup(shutil.rmtree, probe, True)
        write(os.path.join(probe, "Case"), "x")
        if os.path.exists(os.path.join(probe, "case")):
            pass
        else:
            self.skipTest("the temp disk is case-sensitive, so a case-only rename is not an alias here")
        project = self.project()
        git(project.other, "mv", "NOTES.md", "Notes.md")
        git(project.other, "commit", "-q", "-m", "rename")
        git(project.other, "push", "-q", "origin", "main")
        server = self.started(project)
        old_id = server.state()["server_id"]
        code, body = server.post("/api/update", {})
        self.assertEqual(code, 200, body)
        self.assertEqual((body["ok"], body["restarting"]), (True, True))
        server.wait_new_server(old_id)
        self.assertEqual(project.head(), project.origin_head())
        listed = project.g("ls-files").splitlines()
        self.assertIn("Notes.md", listed)
        self.assertNotIn("NOTES.md", listed)
        self.assertIn("Notes.md", os.listdir(project.dir))
        self.assertEqual(project.g("status", "--porcelain"), "")


class StaticServed(UpdateCase):
    def test_a8_no_slash_check_in_the_served_page(self):
        project = self.project()
        server = self.server(project)
        for path in ("/", "/app.js", "/app.css"):
            with self.subTest(path):
                self.assertNotIn("/check", server.get(path)[2].decode())


# ---------------------------------------------------------------- D: update and restart


class UpdateAndRestart(UpdateCase):
    def update_and_check(self, project, server, relative_note=""):
        project.push({"VERSION.txt": "2\n"})
        server.post("/api/update-check", {})
        server.wait_state(lambda s: s["update"]["behind"] == 1 and not s["update"]["checking"], 15, "an update")
        old_short = project.short()
        new_short = project.origin_short()
        old_token, old_id, port, pid = server.token, server.state()["server_id"], server.port, server.process.pid

        status, _, data = server.request("POST", "/api/update", body="{}", headers=[
            ("Host", server.host()), ("Origin", f"http://{server.host()}"), ("X-Gow-Token", server.token),
            ("Content-Type", "application/json")])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"ok": True, "restarting": True, "version": new_short})

        server.wait_new_server(old_id)
        state = server.wait_state(lambda s: s["update"]["current"] is not None and not s["update"]["checking"],
                                  15, "the new version to be read")
        self.assertEqual(server.port, port)
        self.assertEqual(state["update"]["current"]["commit"], new_short)
        self.assertEqual(state["update"]["behind"], 0)
        self.assertEqual(project.head(), project.origin_head())
        self.assertTrue(server.alive())
        self.assertEqual(server.process.pid, pid)
        return old_short, new_short, old_token

    def check_log_and_tokens(self, server, old_short, new_short, old_token, port_pid):
        self.assertEqual(server.listener_pids(), [port_pid])
        code, body = server.post("/api/update-check", {}, token=old_token)
        self.assertEqual((code, body), (403, {"error": "Token not accepted"}))
        server.load_page()
        self.assertNotEqual(server.token, old_token)
        self.assertEqual(server.post("/api/update-check", {}), (200, {"ok": True}))
        lines = server.output().splitlines()
        updated = [i for i, line in enumerate(lines) if line.startswith("Updated from")]
        self.assertEqual(len(updated), 1, lines)
        self.assertEqual(lines[updated[0]], f"Updated from {old_short} to {new_short}; restarting")
        self.assertTrue(lines[updated[0] + 1].startswith("Dashboard: http://127.0.0.1:"), lines)
        self.assertEqual(len([l for l in lines if l.startswith("Dashboard:")]), 2)

    def test_d1_direct_with_a_relative_argv(self):
        project = self.project()
        server = self.started(project, relative=True)
        server.load_page()
        pid = server.process.pid
        old_short, new_short, old_token = self.update_and_check(project, server)
        self.check_log_and_tokens(server, old_short, new_short, old_token, pid)

    def test_d2_the_launchers_way(self):
        project = self.project()
        server = self.started(project, mode="session")
        server.load_page()
        pid = server.process.pid
        old_short, new_short, old_token = self.update_and_check(project, server)
        self.check_log_and_tokens(server, old_short, new_short, old_token, pid)

    def test_d2_under_caffeinate(self):
        """On this Mac caffeinate execs the server (same pid) and leaves a helper child that holds the
        assertion, so the helper is what must survive the restart"""
        project = self.project()
        server = self.started(project, mode="caffeinate")
        server.load_page()
        pid = server.process.pid

        def helpers():
            out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
            return [p for p in out if "caffeinate" in command_of(p)]

        before = helpers()
        self.assertEqual(len(before), 1, "the caffeinate helper")
        self.assertEqual(server.listener_pids(), [pid])
        old_short, new_short, old_token = self.update_and_check(project, server)
        self.assertEqual(helpers(), before)
        self.check_log_and_tokens(server, old_short, new_short, old_token, pid)

    def test_d3_a_restart_that_fails_is_visible(self):
        project = self.project()
        project.push({"VERSION.txt": "2\n"})
        server = self.started(project, harness={"exec_fails": True})
        code, body = server.post("/api/update", {})
        self.assertEqual(code, 200)
        self.assertNotEqual(server.process.wait(15), 0)
        self.assertIn("Error: the dashboard could not restart after the update (PermissionError)", server.output())

    def d5(self, python):
        project = self.project()
        project.push({"VERSION.txt": "2\n"})
        server = self.started(project, python=python)
        server.load_page()
        old_id, old_short, new_short = server.state()["server_id"], project.short(), project.origin_short()
        body = b"{}"
        request = (f"POST /api/update HTTP/1.1\r\nHost: {server.host()}\r\nOrigin: http://{server.host()}\r\n"
                   f"X-Gow-Token: {server.token}\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode() + body
        sock = socket.create_connection(("127.0.0.1", server.port), timeout=10)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.sendall(request)
        sock.close()  # a reset: the reply can't be written
        state = server.wait_new_server(old_id, 30)
        state = server.wait_state(lambda s: s["update"]["current"] is not None and not s["update"]["checking"],
                                  15, "the new version to be read")
        self.assertEqual(state["update"]["current"]["commit"], new_short)
        lines = server.output().splitlines()
        updated = [i for i, line in enumerate(lines) if line.startswith("Updated from")]
        self.assertEqual(len(updated), 1, lines)
        self.assertEqual(lines[updated[0]], f"Updated from {old_short} to {new_short}; restarting")
        self.assertTrue(lines[updated[0] + 1].startswith("Dashboard: http://127.0.0.1:"), lines)
        server.token = None
        server.load_page()
        code, body = server.post("/api/run", {"concurrency": 1})
        self.assertNotEqual((body or {}).get("error"), "The dashboard is updating")
        return server

    def test_d5_restart_follows_an_update_whose_reply_is_reset_venv_python(self):
        self.d5(None)

    @unittest.skipUnless(os.path.exists("/usr/bin/python3"), "no /usr/bin/python3")
    def test_d5_restart_follows_an_update_whose_reply_is_reset_system_python(self):
        version = subprocess.run(["/usr/bin/python3", "-V"], capture_output=True, text=True).stdout
        print(f"\nD5 system python: {version.strip()}")
        self.d5("/usr/bin/python3")

    def test_d4_check_flag_binds_nothing_and_writes_nothing(self):
        project = self.project()
        occupied = socket.socket()
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        self.addCleanup(occupied.close)
        taken = occupied.getsockname()[1]
        self.assertNotEqual(taken, REAL_PORT)
        server = TempServer(self, project, start=False)
        env = server.environment()
        before = project.files()
        for port in (taken, "0"):
            began = time.monotonic()
            done = subprocess.run([PYTHON, "-B", project.server_path, "--check", "--port", str(port)],
                                  cwd=project.dir, env=env, capture_output=True, text=True, timeout=30,
                                  stdin=subprocess.DEVNULL)
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "ok\n", ""))
            self.assertLess(time.monotonic() - began, 20)
        self.assertFalse(os.path.exists(os.path.dirname(project.events_file)))
        self.assertEqual(project.files(), before)

    def test_d4_check_flag_from_the_real_checkout_writes_nothing(self):
        with_env = {k: v for k, v in os.environ.items()}
        events_dir = tempfile.mkdtemp(prefix="gow-chk-")
        self.addCleanup(shutil.rmtree, events_dir, True)
        with_env["GOW_EVENTS_FILE"] = os.path.join(events_dir, "events.jsonl")
        with_env["PYTHONDONTWRITEBYTECODE"] = "1"
        done = subprocess.run([PYTHON, "-B", os.path.join(PROJECT, "dashboard", "server.py"), "--check"],
                              cwd=events_dir, env=with_env, capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL)
        self.assertEqual((done.returncode, done.stdout), (0, "ok\n"))
        self.assertEqual(os.listdir(events_dir), [])


# ---------------------------------------------------------------- E: the page


class UpdatePage(page_tests.PageCase):
    """PageCase's browser plumbing, over a temp project's server"""

    def project(self, **kw):
        return Project(self, **kw)

    def server(self, project, **kw):
        server = TempServer(self, project, **kw)
        server.settle()
        return server

    def test_e1_version_and_available(self):
        project = self.project()
        project.push({"VERSION.txt": "2\n"})
        server = self.server(project)
        page = self.page(server)
        self.wait_text(page, "#version", f"Version {project.short()} · 2026-11-18")
        self.assertEqual(page.inner_text("#version"), f"Version {project.short()} · 2026-11-18")
        self.wait_text(page, "#update-text", "1 change available")
        self.assertTrue(page.is_visible("#update"))
        self.assertTrue(page.is_enabled("#update"))
        self.assertEqual(page.inner_text("#update"), "Update")

    def test_e1_up_to_date_hides_update(self):
        project = self.project()
        server = self.server(project)
        page = self.page(server)
        wait_until(lambda: page.inner_text("#update-text").startswith("Up to date"), 8, "Up to date")
        self.assertTrue(page.is_hidden("#update"))
        self.assertTrue(page.is_visible("#update-check"))

    def test_e2_update_reloads_the_page_with_a_notice(self):
        project = self.project()
        project.push({"VERSION.txt": "2\n"})
        server = self.server(project)
        page = self.page(server)
        # Records every alert text across the reload, in the tab's window.name
        page.context.add_init_script("""
          window.addEventListener('DOMContentLoaded', () => {
            const alertNode = document.getElementById('alert');
            const note = () => {
              const text = alertNode.hidden ? '' : alertNode.textContent;
              if (text) window.name += '|' + text;
            };
            new MutationObserver(note).observe(alertNode, {
              childList: true, characterData: true, subtree: true, attributes: true });
            note();
          });
        """)
        page.reload()
        self.wait_text(page, "#update-text", "1 change available")
        wait_until(lambda: page.is_enabled("#update"), 5, "an enabled Update")
        page.evaluate("window.name = ''")
        old_text = page.inner_text("#version")
        page.click("#update")
        new_short = project.origin_short()
        wait_until(lambda: new_short in page.inner_text("#version"), 20, "the new version after the reload")
        self.assertNotEqual(page.inner_text("#version"), old_text)
        self.wait_text(page, "#alert", "The dashboard was updated.")
        seen = page.evaluate("window.name")
        self.assertIn(UPDATING_TEXT, seen)
        self.assertNotIn("not responding", seen)
        with page.expect_response(lambda r: r.url.endswith("/api/update-check")) as response:
            wait_until(lambda: page.is_enabled("#update-check"), 5, "Check for updates")
            page.click("#update-check")
        self.assertEqual(response.value.status, 200)

    def test_e3_live_run_disables_update(self):
        project = self.project()
        project.push({"VERSION.txt": "2\n"})
        server = self.server(project)
        run = "20261101T000000Z-1"
        lock = events.lock_path(project.events_file, run)
        os.makedirs(os.path.dirname(lock), mode=0o700, exist_ok=True)
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        write_events(project.events_file, [run_start(run, time.time(), total=2)])
        page = self.page(server)
        self.wait_text(page, "#status", "Running")
        self.wait_text(page, "#update", "Update after this run")
        self.assertTrue(page.is_visible("#update"))
        self.assertTrue(page.is_disabled("#update"))

    def test_e4_server_never_comes_back(self):
        project = self.project()
        project.push({"VERSION.txt": "2\n"})
        server = self.server(project)
        original = page_tests.static("app.js")
        self.assertIn("const UPDATE_WAIT_MS = 90000;", original)

        def shorten(route):
            response = route.fetch()
            body = response.text().replace("const UPDATE_WAIT_MS = 90000;", "const UPDATE_WAIT_MS = 4000;")
            route.fulfill(response=response, body=body)

        def kill_after_the_answer(route):
            response = route.fetch()
            os.kill(server.process.pid, signal.SIGKILL)
            route.fulfill(response=response)

        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.route("**/app.js", shorten)
        context.route("**/api/update", kill_after_the_answer)
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{server.port}/")
        self.wait_text(page, "#update-text", "1 change available")
        wait_until(lambda: page.is_enabled("#update"), 5, "an enabled Update")
        page.click("#update")
        self.wait_text(page, "#alert", UPDATING_TEXT, 3)
        self.assertNotIn("not responding", page.inner_text("#alert"))
        self.wait_text(page, "#alert", LOST_TEXT, 15)
        self.assertEqual(page.inner_text("#alert"), LOST_TEXT)

    def test_e5_checks_off(self):
        project = self.project()
        server = self.server(project, checks="off")
        page = self.page(server)
        self.wait_text(page, "#update-text", "Update checks are off.")
        self.assertTrue(page.is_hidden("#update-check"))
        self.assertTrue(page.is_hidden("#update"))


# ---------------------------------------------------------------- G: controls (the tests can fail)


class Controls(UpdateCase):
    def test_g1_without_the_guard_c3_fails(self):
        with self.assertRaises(AssertionError) as caught:
            Refusals.scenario_c3(self, harness={"skip_overwrite_guard": True})
        print(f"\nG1 (guard removed, git's flag kept): {str(caught.exception)[:300]}")
        self.assertIn("500", str(caught.exception))
        self.assertEqual(read(os.path.join(self.last_project.dir, "users.txt")), "LOCAL")

    def test_g1_without_the_guard_or_gits_flag_the_file_is_overwritten(self):
        with self.assertRaises(AssertionError) as caught:
            Refusals.scenario_c3(self, harness={"skip_overwrite_guard": True, "drop_no_overwrite_ignore": True})
        print(f"\nG1 (guard and flag removed): {str(caught.exception)[:300]}")
        self.assertEqual(read(os.path.join(self.last_project.dir, "users.txt")), "FROM ORIGIN")

    def test_g2_without_the_startup_check_c6_fails(self):
        with self.assertRaises(AssertionError) as caught:
            Refusals.scenario_c6(self, harness={"skip_startup_check": True})
        print(f"\nG2 (startup check skipped): {str(caught.exception)[:300]}")
        project, server = self.last_project, self.last_server
        self.assertEqual(project.head(), project.origin_head(), "HEAD stays at the bad commit")
        self.assertEqual(server.process.wait(15), 1, "the restart into the broken code fails")
        self.assertIn("ModuleNotFoundError", server.output())

    def test_g3_with_checks_off_a2_fails(self):
        began = time.monotonic()
        with self.assertRaises(AssertionError) as caught:
            StateAndVersion.scenario_a2(self, checks=None)
        print(f"\nG3 (checks off): {str(caught.exception)[:300]} after {time.monotonic() - began:.0f}s")
        self.assertEqual(self.last_server.state()["update"]["problem"], "off")


if __name__ == "__main__":
    unittest.main()
