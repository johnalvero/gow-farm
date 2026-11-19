"""Plan F4: a failed or hung teardown keeps finished results, and Ctrl+C still wins."""

import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime

from gow_support import (
    ALL_SCRIPTS,
    PASSWORD,
    Account,
    GowTestCase,
    descendants,
    finish,
    kill_all,
    range_args,
    start_script,
    still_running,
    users_text,
    wait_until,
)

EMAIL = "teardown@x.test"
DENIED = "denied@x.test"
CLOSE_WARNING = "Error closing browser:"
STOP_WARNING = "Error stopping Playwright:"


def stamp_of(line):
    return datetime.strptime(re.match(r"\[(.*?)\]", line).group(1), "%Y-%m-%d %H:%M:%S")


def seconds_from_last_account_to_summary(result, email=EMAIL):
    lines = result.stdout.splitlines()
    summary = next(i for i, line in enumerate(lines) if "SUMMARY" in line)
    last_account = [line for line in lines[:summary] if email in line][-1]
    return (stamp_of(lines[summary]) - stamp_of(last_account)).total_seconds()


class Teardown(GowTestCase):
    def build(self, harness, accounts=None, addresses=(EMAIL,)):
        def build(script):
            mock = self.make_mock(accounts or {EMAIL: Account()}, hydrate_ms=100)
            return mock, users_text(addresses), [*range_args(script, len(addresses)), "--stagger", "0"], {"harness": harness}
        return build


class BrowserCloseRaising(Teardown):
    def test_all_accounts_succeeding_print_the_summary_and_exit_0(self):
        for script, mock, result in self.for_each_script(self.build({"browser_close_raises": True})):
            with self.subTest(script):
                self.assertEqual(result.summary_rows(), [(True, EMAIL)], result.output[-1500:])
                self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])
                self.assertEqual(result.returncode, 0)
                self.assertRegex(result.stdout, rf"\[WARNING\] {CLOSE_WARNING} forced browser close failure\n")
                self.assertNotIn("second line of browser close failure", result.output)
                self.assert_tagged(result, [EMAIL], exempt=[CLOSE_WARNING])

    def test_one_failed_account_prints_the_summary_and_exits_1(self):
        accounts = {EMAIL: Account(), DENIED: Account(password="other")}
        build = self.build({"browser_close_raises": True}, accounts, (EMAIL, DENIED))
        for script, _, result in self.for_each_script(build):
            with self.subTest(script):
                self.assertEqual(result.summary_rows(), [(True, EMAIL), (False, DENIED)], result.output[-1500:])
                self.assertEqual(result.returncode, 1)
                self.assertIn(CLOSE_WARNING, result.stdout)


class BrowserCloseHanging(Teardown):
    def test_summary_prints_about_35s_after_the_last_account(self):
        for script, mock, result in self.for_each_script(self.build({"browser_close_hangs": True})):
            with self.subTest(script):
                self.assertEqual(result.summary_rows(), [(True, EMAIL)], result.output[-1500:])
                self.assertEqual(result.returncode, 0)
                self.assertIn(f"{CLOSE_WARNING} TimeoutError", result.stdout)
                waited = seconds_from_last_account_to_summary(result)
                self.assertGreaterEqual(waited, 28)
                self.assertLessEqual(waited, 37)
                self.assert_tagged(result, [EMAIL], exempt=[CLOSE_WARNING])

    def run_wedged(self, harness, limit):
        """Freeze the real driver process during teardown. Returns (result, its child pids)."""
        temp_dir = tempfile.mkdtemp(prefix="gow-tests-")
        self.addCleanup(shutil.rmtree, temp_dir, True)
        mock = self.make_mock({EMAIL: Account()}, hydrate_ms=100)
        process = start_script("claim_batch.py", mock, users_text([EMAIL]), ("1", "1", "--stagger", "0"),
                               harness, temp_dir, new_session=True)
        started = time.monotonic()
        wait_until(lambda: mock.login_hits, 30, "the login page")
        children = descendants(process.pid)
        self.addCleanup(kill_all, children)
        try:
            stdout, stderr = process.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            process.kill()
            kill_all(children)
            stdout, stderr = process.communicate()
        result = finish(process, stdout, stderr, time.monotonic() - started, temp_dir, harness)
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")], result.output[-1000:])
        return result, children

    def test_frozen_driver_at_browser_close(self):
        """The likeliest real cause of a hung close: Browser.close hangs because the
        driver process stopped responding, not because of a patched coroutine"""
        result, children = self.run_wedged({"wedge_driver": True}, limit=60)
        self.assertEqual(result.summary_rows(), [(True, EMAIL)], result.output[-1000:])
        self.assertEqual(result.returncode, 0)
        self.assertIn(f"{CLOSE_WARNING} TimeoutError", result.stdout)
        self.assertIn(f"{STOP_WARNING} TimeoutError", result.stdout)
        self.assertLessEqual(seconds_from_last_account_to_summary(result), 37)
        self.assert_tagged(result, [EMAIL], exempt=[CLOSE_WARNING, STOP_WARNING, "Background error"])
        wait_until(lambda: not still_running(children), 15, "the frozen driver and its browser to exit")

    def test_frozen_driver_at_context_close(self):
        """All three bounds in a row: context 30s, browser 30s, Playwright stop 5s"""
        result, children = self.run_wedged({"wedge_driver_at_context_close": True}, limit=95)
        self.assertEqual(result.summary_rows(), [(True, EMAIL)], result.output[-1000:])
        self.assertEqual(result.returncode, 0)
        self.assertIn(f"[{EMAIL}] Error closing browser context: TimeoutError", result.stdout)
        self.assertLessEqual(seconds_from_last_account_to_summary(result), 37)
        self.assert_tagged(result, [EMAIL], exempt=[CLOSE_WARNING, STOP_WARNING, "Background error"])
        wait_until(lambda: not still_running(children), 15, "the frozen driver and its browser to exit")


class ContextCloseHanging(Teardown):
    def test_hung_context_close_is_bounded_and_keeps_the_result(self):
        for script, mock, result in self.for_each_script(self.build({"context_close_hangs": True})):
            with self.subTest(script):
                self.assertEqual(result.summary_rows(), [(True, EMAIL)], result.output[-1500:])
                self.assertEqual(result.returncode, 0)
                self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])
                self.assertRegex(result.stdout, rf"\[WARNING\] \[{EMAIL}\] Error closing browser context: TimeoutError\n")
                claimed = stamp_of(next(l for l in result.stdout.splitlines() if "75 jewels" in l))
                warned = stamp_of(next(l for l in result.stdout.splitlines() if "Error closing browser context" in l))
                self.assertGreaterEqual((warned - claimed).total_seconds(), 28)
                self.assertLessEqual((warned - claimed).total_seconds(), 37)
                self.assert_tagged(result, [EMAIL])


class PlaywrightStartAndStop(Teardown):
    def test_exit_raising_keeps_the_summary(self):
        for script, mock, result in self.for_each_script(self.build({"playwright_exit_raises": True})):
            with self.subTest(script):
                self.assertEqual(result.summary_rows(), [(True, EMAIL)], result.output[-1500:])
                self.assertEqual(result.returncode, 0)
                self.assertRegex(result.stdout, rf"\[WARNING\] {STOP_WARNING} forced playwright stop failure\n")
                self.assertNotIn("second line of playwright stop failure", result.output)
                self.assert_tagged(result, [EMAIL], exempt=[STOP_WARNING])

    def test_the_stop_warning_is_the_only_line_exempt(self):
        """Run-level under the amended note, and it needs the exemption: a sign the check
        would catch any other unnamed line on the same run"""
        for script, mock, result in self.for_each_script(self.build({"playwright_exit_raises": True})):
            with self.subTest(script):
                untagged = result.untagged_lines([EMAIL])
                self.assertEqual(len(untagged), 1, untagged)
                self.assertIn(STOP_WARNING, untagged[0])

    def test_exit_hanging_prints_the_summary_about_5s_later(self):
        for script, mock, result in self.for_each_script(self.build({"playwright_exit_hangs": True})):
            with self.subTest(script):
                self.assertEqual(result.summary_rows(), [(True, EMAIL)], result.output[-1500:])
                self.assertEqual(result.returncode, 0)
                self.assertIn(f"{STOP_WARNING} TimeoutError", result.stdout)
                waited = seconds_from_last_account_to_summary(result)
                self.assertGreaterEqual(waited, 3)
                self.assertLessEqual(waited, 8)
                self.assert_tagged(result, [EMAIL], exempt=[STOP_WARNING])

    def test_failing_to_start_is_fatal_as_today(self):
        for script, mock, result in self.for_each_script(self.build({"playwright_start_raises": True})):
            with self.subTest(script):
                self.assertEqual(result.returncode, 1)
                self.assertRegex(result.stdout, r"\[ERROR\] Fatal error: forced playwright start failure\n")
                self.assertNotIn("second line of playwright start failure", result.output)
                self.assertNotIn("SUMMARY", result.stdout)
                self.assertEqual(mock.total_requests, 0)


class CtrlC(Teardown):
    def assert_interrupted(self, result):
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("SUMMARY", result.stdout)
        for text in (PASSWORD, "Call log", "never retrieved"):
            self.assertNotIn(text, result.output)

    def test_during_a_hung_teardown(self):
        for script in ALL_SCRIPTS:
            for group in (False, True):
                with self.subTest(script, group=group):
                    temp_dir = tempfile.mkdtemp(prefix="gow-tests-")
                    self.addCleanup(shutil.rmtree, temp_dir, True)
                    marker = os.path.join(temp_dir, "teardown-started")
                    mock = self.make_mock({EMAIL: Account()}, hydrate_ms=100)
                    result, _ = self.interrupted_run(
                        script, mock, users_text([EMAIL]), (*range_args(script, 1), "--stagger", "0"),
                        marker=marker, group=group, limit=15,
                        harness={"browser_close_hangs": True, "teardown_marker": marker})
                    self.assert_interrupted(result)
                    self.assertIn("Interrupted by user", result.stdout)
                    self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])

    def run_pending_fill(self, script, harness, **kwargs):
        mock = self.make_mock({EMAIL: Account()}, hydrate_ms=100, pw_attr="disabled")
        return self.interrupted_run(script, mock, users_text([EMAIL]),
                                    (*range_args(script, 1), "--stagger", "0"), harness=harness, **kwargs)

    def test_browser_close_raising_after_ctrl_c(self):
        for script in ALL_SCRIPTS:
            with self.subTest(script):
                result, _ = self.run_pending_fill(script, {"browser_close_raises": True})
                self.assert_interrupted(result)
                self.assertIn("Interrupted by user", result.stdout)
                self.assertGreaterEqual(result.spy["browser_closes"], 1)
                self.assertIn(f"{CLOSE_WARNING} forced browser close failure", result.stdout)

    def test_browser_close_raising_after_ctrl_c_to_the_group(self):
        for script in ALL_SCRIPTS:
            with self.subTest(script):
                result, _ = self.run_pending_fill(script, {"browser_close_raises": True}, group=True)
                self.assert_interrupted(result)
                self.assertIn("Interrupted by user", result.stdout)

    def test_playwright_exit_raising_after_ctrl_c(self):
        for script in ALL_SCRIPTS:
            with self.subTest(script):
                result, _ = self.run_pending_fill(script, {"playwright_exit_raises": True})
                self.assert_interrupted(result)
                self.assertIn("Interrupted by user", result.stdout, result.output[-800:])
                self.assertNotIn("Fatal error", result.stdout)

    def test_ctrl_c_during_a_hung_context_close(self):
        for script in ALL_SCRIPTS:
            with self.subTest(script):
                mock = self.make_mock({EMAIL: Account()}, hydrate_ms=100)
                result, _ = self.interrupted_run(
                    script, mock, users_text([EMAIL]), (*range_args(script, 1), "--stagger", "0"),
                    trigger="jewels!", harness={"context_close_hangs": True}, limit=45)
                self.assert_interrupted(result)
                self.assertIn("Interrupted by user", result.stdout)


if __name__ == "__main__":
    unittest.main()
