"""Shared test plumbing. Every claim script in this suite is spawned from here,
so the checks that keep it off the real site and away from real credentials
live in one place: see assert_safe_env."""

import atexit
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

sys.dont_write_bytecode = True

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(TESTS_DIR)
PYTHON = os.path.join(PROJECT, ".venv", "bin", "python")
HARNESS = os.path.join(TESTS_DIR, "script_harness.py")
SCREENSHOT_DIR = os.path.join(PROJECT, "screenshots")
FAKE_DOMAIN = "x.test"

# In-process imports of claim_common read this at import time. No test here
# reaches that address: it exists so the real default can never apply.
os.environ["GOW_BASE_URL"] = "http://127.0.0.1:9/en-us"
os.environ["GOW_USERS_FILE"] = os.path.join(tempfile.gettempdir(), "gow-tests-no-such-users-file")
# Off: an in-process run_accounts must never start or open the real dashboard.
# Tests of the launcher set their own port, never the real one
os.environ["GOW_DASHBOARD_PORT"] = ""
sys.path.insert(0, PROJECT)

from mock_site import PASSWORD, Account, Mock  # noqa: E402

ALL_SCRIPTS = ("claim_all_characters.py", "claim_batch.py", "claim_all.py")

SUCCESS_MESSAGE = {
    "claim_all_characters.py": "✅ Claimed 75 jewels!",
    "claim_batch.py": "✅ Successfully claimed 75 jewels!",
    "claim_all.py": "✅ Successfully claimed 75 jewels!",
}
ALREADY_CLAIMED_MESSAGE = {
    "claim_all_characters.py": "✓ Already claimed",
    "claim_batch.py": "✓ Already claimed (on cooldown)",
    "claim_all.py": "✓ Jewels already claimed today (on cooldown)",
}


class UnsafeRun(Exception):
    """A script run would not be confined to the mock and fake credentials"""


def assert_safe_env(env):
    """Refuse to spawn unless both overrides are set and point at safe places"""
    base_url = env.get("GOW_BASE_URL")
    users_file = env.get("GOW_USERS_FILE")

    if not base_url:
        raise UnsafeRun("GOW_BASE_URL is not set")
    if not users_file:
        raise UnsafeRun("GOW_USERS_FILE is not set")
    if urlsplit(base_url).hostname != "127.0.0.1":
        raise UnsafeRun(f"GOW_BASE_URL is not local: {base_url}")

    real_users = os.path.realpath(users_file)
    if real_users.startswith(os.path.realpath(PROJECT) + os.sep):
        raise UnsafeRun(f"GOW_USERS_FILE is inside the project: {users_file}")
    if os.path.basename(real_users) in ("users.txt", "users-backup.txt"):
        raise UnsafeRun(f"GOW_USERS_FILE looks like a real users file: {users_file}")

    events_file = env.get("GOW_EVENTS_FILE")
    if events_file and os.path.realpath(events_file).startswith(os.path.realpath(PROJECT) + os.sep):
        raise UnsafeRun(f"GOW_EVENTS_FILE is inside the project: {events_file}")

    # A run that opens the dashboard must use a test's own port: absent would mean
    # the real default port, where the user's own dashboard runs
    if "GOW_DASHBOARD_PORT" not in env:
        raise UnsafeRun("GOW_DASHBOARD_PORT is not set")
    if is_real_dashboard_port(env["GOW_DASHBOARD_PORT"]):
        raise UnsafeRun("GOW_DASHBOARD_PORT is the real dashboard's port")


def is_real_dashboard_port(value):
    """The real dashboard's port in any all-digit spelling, leading zeros included"""
    return value is not None and re.fullmatch(r"[0-9]+", value) is not None and int(value) == 8765


def script_env(mock, users_file):
    env = dict(os.environ)
    env.pop("GOW_TEST_HARNESS", None)
    env.pop("GOW_EVENTS_FILE", None)
    env.pop("GOW_RUN_SOURCE", None)
    env.update(
        GOW_BASE_URL=mock.base_url,
        GOW_USERS_FILE=users_file,
        GOW_DASHBOARD_PORT="",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONIOENCODING="utf-8",
        PYTHONUNBUFFERED="1",
    )
    return env


def script_command(script, args, harness):
    if harness is not None:
        return [PYTHON, "-B", HARNESS, script, *args]
    return [PYTHON, "-B", os.path.join(PROJECT, script), *args]


def users_text(emails):
    return "".join(f"{email}:{PASSWORD}\n" for email in emails)


def range_args(script, count):
    """Positional args selecting the first count lines, where the script takes any"""
    return [] if script == "claim_all.py" else ["1", str(count)]


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    seconds: float
    spy: dict

    @property
    def output(self):
        return self.stdout + self.stderr

    def summary_rows(self):
        """(succeeded, email) per summary line, in the order printed"""
        rows = []
        for line in self.stdout.splitlines():
            found = re.search(r"\] (✅|❌) .*: (\S+@\S+)$", line)
            if found:
                rows.append((found.group(1) == "✅", found.group(2)))
        return rows

    def summary_value(self, label):
        found = re.search(rf"{re.escape(label)}: (\d+)", self.stdout)
        return int(found.group(1)) if found else None

    def untagged_lines(self, emails, exempt=()):
        """Lines of account work that name no account.

        The window opens after the separator following "Found N account(s) to
        process" and closes at the banner above the summary. Everything on
        stderr is in the window, as nothing there is ordered against stdout.
        exempt lists the run-level texts the plan allows on runs where that
        failure was forced.
        """
        lines = self.stdout.splitlines()
        opening = next((i for i, l in enumerate(lines) if "account(s) to process" in l), -1)
        closing = next((i for i, l in enumerate(lines) if "SUMMARY" in l), len(lines) + 1) - 1
        window = lines[opening + 2:closing] + self.stderr.splitlines()
        return [line for line in window if not any(text in line for text in (*emails, *exempt))]


def run_script(script, mock, users, *args, harness=None, timeout=240, env_extra=None):
    """Run a claim script against the mock with a temp file of fake credentials"""
    temp_dir = tempfile.mkdtemp(prefix="gow-tests-")
    try:
        process = start_script(script, mock, users, args, harness, temp_dir, env_extra=env_extra)
        started = time.monotonic()
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise AssertionError(f"{script} did not finish in {timeout}s:\n{stdout[-3000:]}\n{stderr[-1000:]}")
        return finish(process, stdout, stderr, time.monotonic() - started, temp_dir, harness)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def start_script(script, mock, users, args, harness, temp_dir, new_session=False, env_extra=None,
                 stdout=subprocess.PIPE):
    """Spawn without waiting. The only place this suite starts a script."""
    users_file = os.path.join(temp_dir, "fake-accounts")
    mode = "wb" if isinstance(users, bytes) else "w"
    encoding = None if isinstance(users, bytes) else "utf-8"
    with open(users_file, mode, encoding=encoding) as f:
        f.write(users)

    env = script_env(mock, users_file)
    env.update(env_extra or {})
    env = {name: value for name, value in env.items() if value is not None}
    if harness is not None:
        env["GOW_TEST_HARNESS"] = json.dumps({"spy_file": os.path.join(temp_dir, "spy.json"), **harness})

    assert_safe_env(env)
    # Never the runner's terminal: a script that sees a TTY would act as if started by hand
    return subprocess.Popen(
        script_command(script, args, harness),
        cwd=PROJECT, env=env, text=True, encoding="utf-8",
        stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.PIPE,
        start_new_session=new_session,
    )


def finish(process, stdout, stderr, seconds, temp_dir, harness):
    spy = {}
    spy_file = os.path.join(temp_dir, "spy.json")
    if harness is not None and os.path.exists(spy_file):
        with open(spy_file) as f:
            spy = json.load(f)
    return Result(process.returncode, stdout, stderr, seconds, spy)


def descendants(pid):
    """Process ids below pid, so a kill can only touch this run's own processes"""
    if int(pid) <= 1:
        raise ValueError(f"refusing to walk the process tree from pid {pid}")
    found = []
    pending = [str(pid)]
    while pending:
        out = subprocess.run(["pgrep", "-P", pending.pop()], capture_output=True, text=True).stdout
        children = out.split()
        found += children
        pending += children
    return found


def command_of(pid):
    return subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True).stdout


def still_running(pids):
    return [pid for pid in pids if command_of(pid).strip()]


def kill_all(pids):
    for pid in pids:
        if int(pid) <= 1:
            raise ValueError(f"refusing to signal pid {pid}")
        try:
            os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def wait_until(condition, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {message}")


def remove_test_screenshots():
    """Only the fake domain's files: real screenshots stay where they are"""
    for path in glob.glob(os.path.join(SCREENSHOT_DIR, "*_at_x_test_*.png")):
        os.remove(path)


def remove_bytecode():
    """The test modules' own .pyc files, which plain `python -m unittest` writes"""
    shutil.rmtree(os.path.join(TESTS_DIR, "__pycache__"), ignore_errors=True)


atexit.register(remove_bytecode)
atexit.register(remove_test_screenshots)


class GowTestCase(unittest.TestCase):
    maxDiff = None

    @classmethod
    def tearDownClass(cls):
        remove_test_screenshots()

    def make_mock(self, accounts=None, **kwargs):
        mock = Mock(accounts, **kwargs)
        self.addCleanup(mock.close)
        return mock

    def run_gow(self, script, mock, users, *args, harness=None, timeout=240, check_password=True, env_extra=None):
        """Run a script. No mock password may reach its output, in any test."""
        result = run_script(script, mock, users, *args, harness=harness, timeout=timeout, env_extra=env_extra)
        if check_password:
            self.assertNotIn(PASSWORD, result.output, "password reached stdout or stderr")
        return result

    def run_all(self, calls):
        """Run independent scripts side by side, returning results in call order"""
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            return [future.result() for future in [pool.submit(call) for call in calls]]

    def for_each_script(self, build, scripts=ALL_SCRIPTS):
        """Run build(script) -> (mock, users, args, kwargs) for each script, side by side.
        Yields (script, mock, result) for the caller's assertions."""
        prepared = {script: build(script) for script in scripts}

        def call(script):
            mock, users, args, kwargs = prepared[script]
            return self.run_gow(script, mock, users, *args, **kwargs)

        results = self.run_all([lambda s=script: call(s) for script in scripts])
        for script, result in zip(scripts, results):
            yield script, prepared[script][0], result

    def assert_tagged(self, result, emails, exempt=()):
        self.assertEqual(result.untagged_lines(emails, exempt), [], "lines with no account email")

    def interrupted_run(self, script, mock, users, args, *, trigger="Filling credentials",
                        marker=None, harness=None, signals=1, group=False, limit=30, env_extra=None):
        """Run a script and send it SIGINT once it reaches trigger, or marker if given.

        group=True signals the whole process group, as a terminal's Ctrl+C does.
        The script gets its own session, so that can never reach the test runner.
        Returns (result, pids of its child processes).
        """
        temp_dir = tempfile.mkdtemp(prefix="gow-tests-")
        self.addCleanup(shutil.rmtree, temp_dir, True)
        process = start_script(script, mock, users, args, harness, temp_dir, new_session=True, env_extra=env_extra)
        started = time.monotonic()

        seen = []
        if marker:
            wait_until(lambda: os.path.exists(marker), 60, "the teardown marker")
        else:
            while trigger not in "".join(seen):
                line = process.stdout.readline()
                self.assertTrue(line, f"script ended before {trigger!r}:\n{''.join(seen)}")
                seen.append(line)
        time.sleep(0.5)

        children = descendants(process.pid)
        self.addCleanup(kill_all, children)
        for number in range(signals):
            if number:
                time.sleep(0.2)
            try:
                if group:
                    os.killpg(process.pid, signal.SIGINT)
                else:
                    process.send_signal(signal.SIGINT)
            except (ProcessLookupError, PermissionError):
                # Already exited. macOS reports a group of zombies as EPERM.
                break

        try:
            stdout, stderr = process.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise AssertionError(f"{script} still running {limit}s after SIGINT:\n{stdout[-2000:]}\n{stderr[-800:]}")
        result = finish(process, "".join(seen) + stdout, stderr, time.monotonic() - started, temp_dir, harness)
        return result, children
