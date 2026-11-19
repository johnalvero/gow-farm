"""Reviewer fixes: stdout failures, damaged event files, the monitor, run_end and probe()."""

import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock as unittest_mock

from dash_support import DashTestCase, account_end, event, read_events, run_start, write_events
from gow_support import (
    ALL_SCRIPTS,
    PASSWORD,
    Account,
    descendants,
    finish,
    kill_all,
    range_args,
    start_script,
    users_text,
    wait_until,
)

from dashboard import events, network, server, store

EMAIL = "fixes@x.test"
SECOND = "second@x.test"
ACCOUNTS = {EMAIL: Account(), SECOND: Account(vault="cooldown")}
SLOW = Account(vault_delay_ms=25000)
RUN = "20261101T000000Z-4242"


def arguments(script):
    return (*range_args(script, 2), "--concurrency", "1", "--stagger", "0")


class ClosedStdout(DashTestCase):
    def run_closed(self, script, mock, args, harness=None, close_at=None, timeout=90):
        """Run with stdout a pipe nobody reads: closed from the start, or once close_at is printed"""
        temp = tempfile.mkdtemp(prefix="gow-tests-")
        self.addCleanup(shutil.rmtree, temp, True)
        read_end, write_end = os.pipe()
        if close_at is None:
            os.close(read_end)
        # Buffered, as a real run is: what a failed print leaves behind is the whole problem
        process = start_script(script, mock, users_text(ACCOUNTS), args, harness, temp, stdout=write_end,
                               env_extra={"PYTHONUNBUFFERED": None})
        os.close(write_end)
        self.addCleanup(lambda: process.poll() is None and (kill_all(descendants(process.pid)), process.kill()))
        self.addCleanup(process.stderr.close)

        if close_at is not None:
            seen = b""
            while close_at.encode() not in seen:
                chunk = os.read(read_end, 4096)
                self.assertTrue(chunk, seen[-500:])
                seen += chunk
            os.close(read_end)

        started = time.monotonic()
        stderr = process.stderr.read()
        process.wait(timeout)
        return finish(process, "", stderr, time.monotonic() - started, temp, harness)

    def assert_claimed(self, mock, result):
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])
        self.assertEqual(result.stderr, "")

    def each_script(self, run):
        def call(script):
            mock = self.make_mock(ACCOUNTS, hydrate_ms=100)
            return mock, run(script, mock)

        return zip(ALL_SCRIPTS, self.run_all([lambda s=s: call(s) for s in ALL_SCRIPTS]))

    def test_a_pipe_closed_before_the_run(self):
        for script, (mock, result) in self.each_script(lambda s, m: self.run_closed(s, m, arguments(s))):
            with self.subTest(script):
                self.assert_claimed(mock, result)

    def test_a_pipe_closed_mid_run(self):
        run = lambda s, m: self.run_closed(s, m, arguments(s), close_at="Filling credentials")
        for script, (mock, result) in self.each_script(run):
            with self.subTest(script):
                self.assert_claimed(mock, result)

    def test_a_failed_account_still_exits_1_through_a_closed_pipe(self):
        accounts = {EMAIL: Account(password="other")}
        mock = self.make_mock(accounts, hydrate_ms=100)
        result = self.run_closed("claim_batch.py", mock, ("1", "1", "--stagger", "0"))
        self.assertEqual(result.returncode, 1, result.stderr[-500:])
        self.assertEqual(result.stderr, "")

    def test_no_arguments_on_claim_all_characters(self):
        mock = self.make_mock(ACCOUNTS, hydrate_ms=100)
        result = self.run_closed("claim_all_characters.py", mock, ("--concurrency", "1", "--stagger", "0"))
        self.assert_claimed(mock, result)

    def test_enospc_after_n_writes(self):
        for allowed in (0, 3, 8):
            run = lambda s, m, n=allowed: self.run_gow(s, m, users_text(ACCOUNTS), *arguments(s),
                                                       harness={"stdout_enospc_after": n})
            for script, (mock, result) in self.each_script(run):
                with self.subTest(script, allowed=allowed):
                    self.assert_claimed(mock, result)
                    self.assertLessEqual(len(result.stdout.splitlines()), allowed)

    def test_control_without_release_stdout_exit_is_120(self):
        run = lambda s, m: self.run_closed(s, m, arguments(s), harness={"no_release_stdout": True})
        for script, (mock, result) in self.each_script(run):
            with self.subTest(script):
                self.assertEqual(result.returncode, 120, result.stderr[-500:])
                self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])

    def test_control_full_disk_without_release_stdout_exit_is_120(self):
        mock = self.make_mock(ACCOUNTS, hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text(ACCOUNTS), *arguments("claim_batch.py"),
                              harness={"stdout_enospc_after": 3, "no_release_stdout": True})
        self.assertEqual(result.returncode, 120, result.stderr[-500:])

    def test_a_stuck_worker_exits_by_its_result_through_a_closed_pipe(self):
        mock = self.make_mock(hydrate_ms=100)
        result = self.run_closed("claim_batch.py", mock, ("1", "2", "--concurrency", "2", "--stagger", "0"),
                                 harness={"account_deadline_seconds": 8, "account_cancel_grace_seconds": 2,
                                          "swallow_cancel": {EMAIL: -1}}, timeout=40)
        self.assertEqual(result.returncode, 1, result.stderr[-500:])
        self.assertEqual(mock.ledger_pairs(), [(SECOND, "only")])


class PartialLine(DashTestCase):
    def test_a_run_after_a_partial_line_is_read(self):
        dash = self.dash({EMAIL: SLOW})
        write_events(dash.events_file, [
            run_start(RUN, 1000.0, total=1), event("run_end", RUN, 1001.0, status="finished", dropped=0),
            b'{"v":1,"run":"20261101T000001Z-1","ts":10'])
        self.terminal_run(dash)
        state = dash.wait_state(lambda s: s["live"], timeout=15, message="the run after the partial line")
        self.assertTrue(state["live"])
        self.assertEqual(len(state["recent_runs"]), 2)
        self.assertEqual(state["run"]["source"], "terminal")
        self.assertEqual(dash.run(), 409)
        with open(dash.events_file, "rb") as f:
            lines = f.read().split(b"\n")
        self.assertEqual(lines[2], b'{"v":1,"run":"20261101T000001Z-1","ts":10')
        self.assertTrue(lines[3].startswith(b'{"v":1,"run":"'), lines[3][:40])

    def test_a_fresh_file_has_no_leading_newline(self):
        mock = self.make_mock(ACCOUNTS, hydrate_ms=100)
        result, path = self.recording("claim_batch.py", mock, users_text(ACCOUNTS), "1", "1", "--stagger", "0")
        with open(path, "rb") as f:
            self.assertEqual(f.read(1), b"{")

    def test_a_file_ending_in_a_newline_gets_no_blank_line(self):
        mock = self.make_mock(ACCOUNTS, hydrate_ms=100)
        temp = tempfile.mkdtemp(prefix="gow-dash-")
        self.addCleanup(shutil.rmtree, temp, True)
        path = os.path.join(temp, "data", "events.jsonl")
        write_events(path, [run_start(RUN, 1000.0, total=1), event("run_end", RUN, 1001.0, status="finished", dropped=0)])
        self.recording("claim_batch.py", mock, users_text(ACCOUNTS), "1", "1", "--stagger", "0", events_file=path)
        with open(path, "rb") as f:
            self.assertNotIn(b"\n\n", f.read())


class HugeNumbers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="gow-dash-")
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.path = os.path.join(self.temp, "data", "events.jsonl")

    def test_is_seconds(self):
        for value in (10 ** 401, -(10 ** 401), math.nan, math.inf, -math.inf, True, False, "1", None):
            with self.subTest(repr(value)[:20]):
                self.assertFalse(events.is_seconds(value, 1e9))
        for value in (0, 1, 0.5, 1e9):
            with self.subTest(value):
                self.assertTrue(events.is_seconds(value, 1e9))

    def huge(self, name="run_end", extra='"status":"finished","dropped":0', run=RUN):
        return ('{"v":1,"run":"%s","ts":%s,"event":"%s",%s}' % (run, "9" * 401, name, extra))

    def good(self):
        run = "20261101T000002Z-777"
        return run, [run_start(run, 2000.0, total=1), account_end(run, 2001.0, 1),
                     event("run_end", run, 2002.0, status="finished", dropped=0)]

    def test_a_401_digit_ts_before_a_valid_run(self):
        run, valid = self.good()
        write_events(self.path, [self.huge(name="run_start", run=RUN, extra=(
            '"source":"terminal","script":"claim_batch.py","concurrency":3,"stagger":10,"total":1,"lines":[1]')), *valid])
        reader = store.EventStore(self.path)
        state = reader.snapshot()
        self.assertEqual([r["id"] for r in state["recent_runs"]], [run])
        self.assertEqual(state["totals"]["jewels"], 75)
        self.assertTrue(reader.has_run_for_pid(777))
        self.assertFalse(reader.has_run_for_pid(4242))

    def test_has_run_for_pid_on_a_fresh_reader_comes_first(self):
        run, valid = self.good()
        write_events(self.path, [self.huge(), *valid])
        self.assertTrue(store.EventStore(self.path).has_run_for_pid(777))

    def test_every_number_field_refuses_it(self):
        digits = "9" * 401
        run, valid = self.good()
        write_events(self.path, [
            *valid,
            '{"v":1,"run":"%s","ts":1,"event":"account_end","line":%s}' % (run, digits),
            '{"v":%s,"run":"%s","ts":1,"event":"run_end"}' % (digits, run),
            '{"v":1,"run":"%s","ts":1e999,"event":"run_end","status":"interrupted","dropped":0}' % run,
            '{"v":1,"run":"%s","ts":%s}' % (run, "9" * 5000),
        ])
        state = store.EventStore(self.path).snapshot()
        self.assertEqual(state["status"], "finished")


class RaisingStore:
    def __init__(self):
        self.raising = True

    def snapshot(self):
        return {"live": False, "retry_lines": []}

    def has_run_for_pid(self, pid):
        if self.raising:
            raise OSError("the store failed")
        return True


class FlakyChild:
    """A real child whose first polls raise, for the monitor to live through"""

    def __init__(self, process):
        self.process = process
        self.pid = process.pid
        self.left = 3

    def poll(self):
        if self.left:
            self.left -= 1
            raise OSError("poll failed")
        return self.process.poll()


class Monitor(unittest.TestCase):
    def control(self, wrap=lambda process: process):
        control = server.RunControl(RaisingStore())
        self.spawned = []

        def spawn(arguments):
            process = subprocess.Popen(["/bin/sleep", "0.4"])
            self.addCleanup(process.wait)
            self.spawned.append(process)
            return wrap(process)

        control.spawn = spawn
        return control

    def test_a_store_that_raises_leaves_the_thread_alive_and_clears_the_child(self):
        with unittest_mock.patch.object(server, "MONITOR_SECONDS", 0.02):
            control = self.control()
            control.start(1)
            self.assertTrue(control.state()["owned"])
            watcher = [t for t in threading.enumerate() if t.name == "gow-monitor"][-1]
            time.sleep(0.15)
            self.assertTrue(watcher.is_alive(), "the store raised and the monitor died")
            wait_until(lambda: not control.state()["owned"], 3, "the child to clear")
            self.assertIsNone(control.state()["phase"])

            control.store.raising = False
            control.start(1)
            self.assertEqual(len(self.spawned), 2)
            wait_until(lambda: not control.state()["owned"], 3, "the second child to clear")

    def test_a_poll_that_raises_is_lived_through(self):
        with unittest_mock.patch.object(server, "MONITOR_SECONDS", 0.02):
            control = self.control(FlakyChild)
            control.start(1)
            wait_until(lambda: not control.state()["owned"], 3, "the child to clear")


class RunEnd(DashTestCase):
    def recorded_events(self, script, **options):
        mock = self.make_mock(ACCOUNTS, hydrate_ms=100)
        return mock, self.recording(script, mock, users_text(ACCOUNTS), *arguments(script), **options)

    def test_a_non_cancel_exception_leaves_no_run_end_and_reads_as_crashed(self):
        for script in ALL_SCRIPTS:
            with self.subTest(script):
                _, (result, path) = self.recorded_events(script, harness={"playwright_start_raises": True})
                self.assertEqual(result.returncode, 1)
                self.assertIn("Fatal error", result.stdout)
                recorded = read_events(path)
                self.assertEqual([e["event"] for e in recorded], ["run_start"])
                self.assertEqual(store.EventStore(path).snapshot()["status"], "crashed")

    def interrupted(self, script, **options):
        temp = tempfile.mkdtemp(prefix="gow-dash-")
        self.addCleanup(shutil.rmtree, temp, True)
        path = os.path.join(temp, "data", "events.jsonl")
        result, _ = self.interrupted_run(script, options.pop("mock"), users_text(ACCOUNTS),
                                         (*range_args(script, 2), "--concurrency", "1", "--stagger", "0"),
                                         env_extra={"GOW_EVENTS_FILE": path}, **options)
        return result, path

    def test_cancellation_gives_interrupted(self):
        for script in ALL_SCRIPTS:
            with self.subTest(script):
                mock = self.make_mock(ACCOUNTS, hydrate_ms=100, pw_attr="disabled")
                result, path = self.interrupted(script, mock=mock)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn(PASSWORD, result.output)
                self.assertEqual([e["status"] for e in read_events(path) if e["event"] == "run_end"], ["interrupted"])
                self.assertEqual(store.EventStore(path).snapshot()["status"], "stopped")

    def test_cancellation_after_gather_returned_stays_finished(self):
        for script in ALL_SCRIPTS:
            with self.subTest(script):
                temp = tempfile.mkdtemp(prefix="gow-tests-")
                self.addCleanup(shutil.rmtree, temp, True)
                marker = os.path.join(temp, "teardown-started")
                mock = self.make_mock(ACCOUNTS, hydrate_ms=100)
                result, path = self.interrupted(script, mock=mock, marker=marker, limit=15,
                                                harness={"browser_close_hangs": True, "teardown_marker": marker})
                self.assertEqual(result.returncode, 1, result.output[-800:])
                self.assertIn("Interrupted by user", result.stdout)
                self.assertEqual([e["status"] for e in read_events(path) if e["event"] == "run_end"], ["finished"])
                self.assertEqual(store.EventStore(path).snapshot()["status"], "finished")

    def test_ctrl_c_with_a_worker_that_never_stops_exits(self):
        """The Ctrl+C side of exit_if_stuck: this hung past 30s before the fix"""
        temp = tempfile.mkdtemp(prefix="gow-tests-")
        self.addCleanup(shutil.rmtree, temp, True)
        marker = os.path.join(temp, "worker-started")
        mock = self.make_mock(hydrate_ms=100)
        result, _ = self.interrupted_run(
            "claim_batch.py", mock, users_text([EMAIL]), ("1", "1", "--stagger", "0"), marker=marker, limit=30,
            harness={"account_cancel_grace_seconds": 2, "swallow_cancel": {EMAIL: -1}, "worker_marker": marker})
        self.assertEqual(result.returncode, 1)
        self.assertIn("Interrupted by user", result.stdout)
        self.assertNotIn("SUMMARY", result.stdout)


class Probe(unittest.TestCase):
    def monitor(self):
        env = {"GOW_BASE_URL": "http://127.0.0.1:9/en-us", "GOW_INTERNET_CHECK_URL": "http://127.0.0.1:9/check"}
        with unittest_mock.patch.dict(os.environ, env):
            return network.NetworkMonitor(lambda: False)

    def test_build_opener_raising_is_a_failed_check(self):
        with unittest_mock.patch.object(network.urllib.request, "build_opener", side_effect=TypeError("SECRET url")):
            self.assertEqual(network.probe("http://127.0.0.1:9/", "GET", 1), (None, "error"))

    def test_building_the_request_raising_is_a_failed_check(self):
        with unittest_mock.patch.object(network.urllib.request, "Request", side_effect=ValueError("SECRET url")):
            self.assertEqual(network.probe("http://127.0.0.1:9/", "GET", 1), (None, "error"))

    def test_nothing_is_counted_as_abandoned(self):
        monitor = self.monitor()
        with unittest_mock.patch.object(network.urllib.request, "build_opener", side_effect=TypeError("SECRET url")):
            status, failure, ms = monitor.bounded_probe("http://127.0.0.1:9/", "GET", 1)
        self.assertEqual((status, failure), (None, "error"))
        self.assertIsInstance(ms, int)
        self.assertEqual(monitor.abandoned, 0)


if __name__ == "__main__":
    unittest.main()
