"""Dashboard D8: a recorder failure, or no dashboard at all, can't change a claim."""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

from dash_support import DashTestCase, run_copy
from gow_support import ALL_SCRIPTS, Account, descendants, range_args, run_script, still_running, users_text, wait_until
from mock_site import Mock

FAST = {"default_stagger": 0.1}
ADDRESSES = ["alpha@x.test", "beta@x.test"]
ACCOUNTS = {ADDRESSES[0]: Account(), ADDRESSES[1]: Account(vault="cooldown")}


def plain(text):
    """Stdout without timestamps, and without the port each mock happens to get"""
    text = re.sub(r"(?m)^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] ", "", text)
    return re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:PORT", text)


def arguments(script):
    return [*range_args(script, 2), "--concurrency", "1", "--stagger", "0"]


class Isolation(DashTestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = {}
        temp = tempfile.mkdtemp(prefix="gow-dash-")
        try:
            for script in ALL_SCRIPTS:
                mock = Mock(ACCOUNTS, hydrate_ms=100)
                recording = os.path.join(temp, script, "events.jsonl")
                off = run_script(script, mock, users_text(ADDRESSES), *arguments(script))
                on_mock = Mock(ACCOUNTS, hydrate_ms=100)
                on = run_script(script, on_mock, users_text(ADDRESSES), *arguments(script),
                                env_extra={"GOW_EVENTS_FILE": recording})
                cls.baseline[script] = (off, on, mock.ledger_pairs(), on_mock.ledger_pairs())
                mock.close()
                on_mock.close()
        finally:
            shutil.rmtree(temp, True)

    def assert_unchanged(self, script, mock, result):
        off, _, ledger, _ = self.baseline[script]
        self.assertEqual(result.returncode, off.returncode, result.output[-1500:])
        self.assertEqual(mock.ledger_pairs(), ledger)
        self.assertEqual(plain(result.stdout), plain(off.stdout))
        self.assertEqual(result.stderr, "")

    def each_script(self, build):
        def run(script):
            return build(script, self.make_mock(ACCOUNTS, hydrate_ms=100))

        scripts = ALL_SCRIPTS
        return zip(scripts, self.run_all([lambda s=s: run(s) for s in scripts]))

    def temp_path(self, *parts):
        temp = tempfile.mkdtemp(prefix="gow-dash-")
        self.addCleanup(shutil.rmtree, temp, True)
        return os.path.join(temp, *parts)

    def test_iso1_events_import_fails(self):
        def build(script, mock):
            result, _ = self.recording(script, mock, users_text(ADDRESSES), *arguments(script),
                                       harness={"events_import_fails": True})
            return mock, result

        for script, (mock, result) in self.each_script(build):
            with self.subTest(script):
                self.assert_unchanged(script, mock, result)

    def test_iso1_dashboard_physically_missing(self):
        def build(script, mock):
            return mock, run_copy(self, script, mock, users_text(ADDRESSES), arguments(script), drop_dashboard=True)

        for script, (mock, result) in self.each_script(build):
            with self.subTest(script):
                self.assert_unchanged(script, mock, result)

    def test_iso1_events_py_broken_on_disk(self):
        def build(script, mock):
            return mock, run_copy(self, script, mock, users_text(ADDRESSES), arguments(script),
                                  replace=[("dashboard/events.py", "import fcntl\n", "raise RuntimeError('broken')\n")])

        for script, (mock, result) in self.each_script(build):
            with self.subTest(script):
                self.assert_unchanged(script, mock, result)

    def test_iso2_unwritable_path(self):
        def build(script, mock):
            folder = self.temp_path("locked")
            os.makedirs(folder, mode=0o500)
            self.addCleanup(os.chmod, folder, 0o700)
            result, _ = self.recording(script, mock, users_text(ADDRESSES), *arguments(script),
                                       events_file=os.path.join(folder, "sub", "events.jsonl"))
            return mock, result

        for script, (mock, result) in self.each_script(build):
            with self.subTest(script):
                self.assert_unchanged(script, mock, result)

    def test_iso2_path_is_a_directory(self):
        def build(script, mock):
            folder = self.temp_path("events.jsonl")
            os.makedirs(folder)
            result, _ = self.recording(script, mock, users_text(ADDRESSES), *arguments(script), events_file=folder)
            return mock, result, folder

        for script, (mock, result, folder) in self.each_script(build):
            with self.subTest(script):
                self.assert_unchanged(script, mock, result)
                self.assertEqual(os.listdir(os.path.dirname(folder)), ["events.jsonl"])

    def test_iso2_every_write_raises_enospc(self):
        def build(script, mock):
            result, _ = self.recording(script, mock, users_text(ADDRESSES), *arguments(script),
                                       harness={"events_write_raises": True})
            return mock, result

        for script, (mock, result) in self.each_script(build):
            with self.subTest(script):
                self.assert_unchanged(script, mock, result)

    def wall_time_ok(self, script, result):
        self.assertLessEqual(abs(result.seconds - self.baseline[script][1].seconds), 2)

    def test_iso3_writes_hanging_forever(self):
        def build(script, mock):
            result, _ = self.recording(script, mock, users_text(ADDRESSES), *arguments(script),
                                       harness={"events_write_hangs": True}, timeout=60)
            return mock, result

        for script, (mock, result) in self.each_script(build):
            with self.subTest(script):
                self.assert_unchanged(script, mock, result)
                self.wall_time_ok(script, result)

    def test_iso3_path_is_a_fifo_with_no_reader(self):
        def build(script, mock):
            path = self.temp_path("events.jsonl")
            os.mkfifo(path, 0o600)
            result, _ = self.recording(script, mock, users_text(ADDRESSES), *arguments(script), events_file=path,
                                       timeout=60)
            return mock, result, path

        for script, (mock, result, path) in self.each_script(build):
            with self.subTest(script):
                self.assert_unchanged(script, mock, result)
                self.wall_time_ok(script, result)
                self.assertEqual(os.listdir(os.path.dirname(path)), ["events.jsonl"])

    def test_iso4_stdout_is_the_same_with_recording_on(self):
        for script in ALL_SCRIPTS:
            with self.subTest(script):
                off, on, off_ledger, on_ledger = self.baseline[script]
                self.assertEqual(plain(on.stdout), plain(off.stdout))
                self.assertEqual(on.stderr, off.stderr)
                self.assertEqual(on.returncode, off.returncode)
                self.assertEqual(on_ledger, off_ledger)

    def test_iso7_control_writes_on_the_event_loop_hang_the_run(self):
        mock = self.make_mock(ACCOUNTS, hydrate_ms=100)
        path = self.temp_path("data", "events.jsonl")
        with self.assertRaisesRegex(AssertionError, "did not finish"):
            run_script("claim_batch.py", mock, users_text(ADDRESSES), *arguments("claim_batch.py"),
                       harness={"events_sync_writes": True}, env_extra={"GOW_EVENTS_FILE": path}, timeout=25)
        self.assertEqual(mock.ledger_pairs(), [])


class ServerDeath(DashTestCase):
    ACCOUNTS = {"first@x.test": Account(), "second@x.test": Account(vault_delay_ms=3000)}

    BOTH = ["first@x.test", "second@x.test"]

    def mid_run(self, child_harness=FAST, **kwargs):
        dash = self.dash(self.ACCOUNTS, child_harness=child_harness, **kwargs)
        self.assertEqual(dash.run(1), 200)
        dash.wait_state(lambda s: s["slots"] and s["slots"][0]["line"] == 1, message="the first account")
        return dash, str(dash.child_pid())

    def killed_mid_run(self, child_harness=FAST, **kwargs):
        dash, pid = self.mid_run(child_harness, **kwargs)
        family = [pid] + descendants(pid)
        dash.kill_server()
        return dash, family

    def stdout_kind(self, pid):
        """What fd 1 of a running child really is, seen from outside it"""
        found = subprocess.run(["lsof", "-a", "-p", pid, "-d", "1", "-Ft"], capture_output=True, text=True).stdout
        return next(line[1:] for line in found.splitlines() if line.startswith("t"))

    def claimed(self, dash):
        return sorted(e for e, _ in dash.mock.ledger_pairs())

    def test_iso6_killing_the_server_leaves_the_run_going(self):
        dash, family = self.killed_mid_run()
        wait_until(lambda: not still_running(family[:1]), 60, "the run to end on its own")
        self.assertEqual(dash.child_exit(), "0")
        self.assertEqual(sorted(e for e, _ in dash.mock.ledger_pairs()), ["first@x.test", "second@x.test"])
        self.assertIn("MULTI-CHARACTER BATCH SUMMARY", dash.log())

    def test_iso7_a_piped_stdout_with_say_unguarded_costs_the_claims(self):
        """Required control: both protections off and the server gone, ISO6 fails.
        The three tests below it each take one condition away, and keep the claims."""
        dash, pid = self.mid_run({**FAST, "no_say_guard": True}, harness={"stdout_pipe": True})
        self.assertEqual(self.stdout_kind(pid), "PIPE")
        dash.kill_server()
        wait_until(lambda: not still_running([pid]), 60, "the run to end")
        self.assertNotEqual(self.claimed(dash), self.BOTH)
        self.assertNotEqual(dash.child_exit() if os.path.exists(dash.exit_file) else "none", "0")

    def test_iso7_a_piped_stdout_alone_keeps_the_claims(self):
        """say() alone is enough now, which is why the old single-switch control went"""
        dash, pid = self.mid_run(harness={"stdout_pipe": True})
        self.assertEqual(self.stdout_kind(pid), "PIPE")
        dash.kill_server()
        wait_until(lambda: not still_running([pid]), 60, "the run to end")
        self.assertEqual(self.claimed(dash), self.BOTH)
        self.assertEqual(dash.child_exit(), "0")

    def test_iso7_say_unguarded_alone_keeps_the_claims(self):
        dash, pid = self.mid_run({**FAST, "no_say_guard": True})
        self.assertEqual(self.stdout_kind(pid), "REG")
        dash.kill_server()
        wait_until(lambda: not still_running([pid]), 60, "the run to end")
        self.assertEqual(self.claimed(dash), self.BOTH)
        self.assertEqual(dash.child_exit(), "0")

    def test_iso7_both_off_with_the_server_alive_keeps_the_claims(self):
        """So the harness switches break nothing by themselves: it takes the dead reader"""
        dash, pid = self.mid_run({**FAST, "no_say_guard": True}, harness={"stdout_pipe": True})
        self.assertEqual(self.stdout_kind(pid), "PIPE")
        dash.wait_state(lambda s: not s["control"]["owned"], timeout=60, message="the run to end")
        self.assertEqual(self.claimed(dash), self.BOTH)
        self.assertEqual(dash.child_exit(), "0")


if __name__ == "__main__":
    unittest.main()
