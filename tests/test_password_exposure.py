"""Criterion: no mock password appears anywhere in stdout or stderr, in any test.

GowTestCase.run_gow enforces that on every run in the suite. This module goes
after the paths most likely to break it. check_password=False just stops the
helper from failing first, so that each test can make its own assertion.
"""

import os
import shutil
import signal
import tempfile
import time
import unittest

from gow_support import (
    ALL_SCRIPTS,
    PASSWORD,
    Account,
    GowTestCase,
    command_of,
    descendants,
    finish,
    range_args,
    start_script,
    still_running,
    users_text,
    wait_until,
)

EMAIL = "secret@x.test"


class ForcedFillFailures(GowTestCase):
    def run_forced(self, **mock_options):
        return self.for_each_script(lambda s: (
            self.make_mock({EMAIL: Account()}, hydrate_ms=100, **mock_options),
            users_text([EMAIL]), [*range_args(s, 1), "--stagger", "0"], {"check_password": False}))

    def test_fill_timeout_on_the_password_input(self):
        for script, mock, result in self.run_forced(pw_attr="disabled"):
            with self.subTest(script):
                self.assertIn("Fatal error", result.stdout)
                self.assertIn("Timeout 30000ms exceeded", result.stdout)
                self.assertNotIn(PASSWORD, result.output)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.summary_rows(), [(False, EMAIL)])
                self.assertEqual(result.summary_value("Total Jewels Claimed"), 0)
                self.assertEqual(result.summary_value("Characters Already Claimed"), 0)
                self.assert_tagged(result, [EMAIL])

    def test_fatal_tracebacks_are_tagged_line_by_line(self):
        for script, mock, result in self.run_forced(pw_attr="disabled", email_attr=""):
            if script == "claim_batch.py":
                continue
            with self.subTest(script):
                self.assertRegex(result.stdout, rf"\[ERROR\] \[{EMAIL}\]   File ")
                self.assertRegex(result.stdout, rf"\[ERROR\] \[{EMAIL}\] TimeoutError: ")
                self.assertNotIn(PASSWORD, result.output)
                self.assert_tagged(result, [EMAIL])

    def test_fill_errors_that_fail_immediately(self):
        for attr in ('type="number"', 'type="checkbox"', 'type="date"', 'readonly'):
            for script, mock, result in self.run_forced(pw_attr=attr):
                with self.subTest(script, attr=attr):
                    self.assertNotIn(PASSWORD, result.output)
                    self.assert_tagged(result, [EMAIL])

    def test_login_rejected_after_a_real_submit(self):
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock({EMAIL: Account(password="other")}, hydrate_ms=100),
                users_text([EMAIL]), [*range_args(s, 1), "--stagger", "0"], {"check_password": False})):
            with self.subTest(script):
                self.assertEqual(mock.logins, [(EMAIL, PASSWORD)])
                self.assertNotIn(PASSWORD, result.output)


class Interrupted(GowTestCase):
    """Plan F2. A pending fill is held open by a disabled password input."""

    def run_interrupted(self, script, **kwargs):
        mock = self.make_mock({EMAIL: Account()}, hydrate_ms=100, pw_attr="disabled")
        return self.interrupted_run(script, mock, users_text([EMAIL]),
                                    (*range_args(script, 1), "--stagger", "0"), **kwargs)

    def assert_no_secret_text(self, result):
        for text in (PASSWORD, "Call log", "never retrieved"):
            self.assertNotIn(text, result.output)

    def assert_clean_exit(self, result):
        self.assertEqual(result.returncode, 1)
        self.assertIn("Interrupted by user", result.stdout)
        self.assertNotIn("SUMMARY", result.stdout)

    def test_sigint_to_python_only(self):
        for script in ALL_SCRIPTS:
            with self.subTest(script):
                result, _ = self.run_interrupted(script)
                self.assert_no_secret_text(result)
                self.assert_clean_exit(result)

    def test_sigint_to_the_process_group(self):
        for script in ALL_SCRIPTS:
            with self.subTest(script):
                result, _ = self.run_interrupted(script, group=True)
                self.assert_no_secret_text(result)
                self.assert_clean_exit(result)

    def test_two_sigints_to_python_only(self):
        for script in ALL_SCRIPTS:
            with self.subTest(script):
                result, _ = self.run_interrupted(script, signals=2, limit=30)
                self.assert_no_secret_text(result)
                self.assertNotEqual(result.returncode, 0)

    def test_two_sigints_to_the_process_group(self):
        for script in ALL_SCRIPTS:
            with self.subTest(script):
                result, _ = self.run_interrupted(script, signals=2, group=True, limit=30)
                self.assert_no_secret_text(result)
                self.assertNotEqual(result.returncode, 0)

    def test_several_accounts_with_pending_fills(self):
        addresses = [f"secret{i}@x.test" for i in range(1, 4)]
        mock = self.make_mock(hydrate_ms=100, pw_attr="disabled")
        result, _ = self.interrupted_run("claim_batch.py", mock, users_text(addresses),
                                         ("1", "3", "--stagger", "0"))
        self.assert_no_secret_text(result)
        self.assert_clean_exit(result)

    def test_sigint_leaves_no_browser_behind(self):
        result, children = self.run_interrupted("claim_batch.py")
        self.assert_clean_exit(result)
        self.assertTrue(children)
        wait_until(lambda: not still_running(children), 10, "the driver and browser to exit")

    def test_two_sigints_leave_no_browser_behind(self):
        result, children = self.run_interrupted("claim_batch.py", signals=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(children)
        wait_until(lambda: not still_running(children), 10, "the driver and browser to exit")


class BrowserKilledMidFill(GowTestCase):
    """Playwright's full message quotes the fill value, so a failed fill nobody
    awaits would reach stderr through asyncio's unretrieved-exception logging"""

    def test_kill_during_a_pending_fill(self):
        for script in ALL_SCRIPTS:
            with self.subTest(script):
                temp_dir = tempfile.mkdtemp(prefix="gow-tests-")
                self.addCleanup(shutil.rmtree, temp_dir, True)
                mock = self.make_mock({EMAIL: Account()}, hydrate_ms=100, pw_attr="disabled")
                process = start_script(script, mock, users_text([EMAIL]),
                                       (*range_args(script, 1), "--stagger", "0"), {}, temp_dir)
                started = time.monotonic()

                seen = []
                while "Filling credentials" not in "".join(seen):
                    line = process.stdout.readline()
                    self.assertTrue(line, "script ended before filling")
                    seen.append(line)
                time.sleep(0.5)

                browsers = [pid for pid in descendants(process.pid)
                            if "--type=" not in command_of(pid) and "headless" in command_of(pid).lower()
                            and "node" not in command_of(pid).split()[0]]
                self.assertEqual(len(browsers), 1, "expected exactly this run's browser")
                os.kill(int(browsers[0]), signal.SIGKILL)

                stdout, stderr = process.communicate(timeout=60)
                result = finish(process, "".join(seen) + stdout, stderr, time.monotonic() - started, temp_dir, {})

                self.assertNotIn(PASSWORD, result.output)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.summary_rows(), [(False, EMAIL)], result.output[-1500:])
                self.assert_tagged(result, [EMAIL])


class MalformedUsersFile(GowTestCase):
    """read_accounts on a line with no colon: the likeliest typo in a hand-kept file"""

    def run_malformed(self, text):
        return self.for_each_script(lambda s: (
            self.make_mock(), text, range_args(s, text.count("\n")), {"check_password": False}))

    def assert_line_withheld(self, line):
        for script, mock, result in self.run_malformed(line + "\n"):
            with self.subTest(script):
                self.assertIn("Invalid format on line 1", result.stdout)
                self.assertNotIn(PASSWORD, result.output)
                self.assertNotIn(EMAIL, result.output)
                self.assertEqual(result.returncode, 1)

    def test_space_separated_line(self):
        self.assert_line_withheld(f"{EMAIL} {PASSWORD}")

    def test_tab_separated_line(self):
        self.assert_line_withheld(f"{EMAIL}\t{PASSWORD}")

    def test_comma_separated_line(self):
        self.assert_line_withheld(f"{EMAIL},{PASSWORD}")

    def test_line_number_counts_real_file_lines(self):
        for script, mock, result in self.run_malformed(f"# comment\n\n{EMAIL} {PASSWORD}\n"):
            with self.subTest(script):
                self.assertIn("Invalid format on line 3", result.stdout)
                self.assertNotIn(PASSWORD, result.output)

    def test_valid_lines_around_a_malformed_one_still_run(self):
        good = "good@x.test"
        text = f"{EMAIL} {PASSWORD}\n{good}:{PASSWORD}\n"
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock(hydrate_ms=100), text, [*range_args(s, 2), "--stagger", "0"],
                {"check_password": False})):
            with self.subTest(script):
                self.assertEqual(mock.ledger_pairs(), [(good, "only")], result.output[-1500:])
                self.assertNotIn(PASSWORD, result.output)
                self.assertNotIn(EMAIL, result.output)

    def test_lines_with_a_colon_but_no_email_or_password(self):
        for script, mock, result in self.run_malformed(f":{PASSWORD}\n"):
            with self.subTest(script):
                self.assertNotIn(PASSWORD, result.output)

    def test_password_tail_in_the_email_field_with_a_colon_in_the_password(self):
        for separator in (" ", "\t", ",", ";"):
            for script, mock, result in self.run_malformed(f"user@x.test{separator}hunter:2\n"):
                with self.subTest(script, separator=separator):
                    self.assertIn("Invalid format on line 1", result.stdout)
                    self.assertNotIn("hunter", result.output)
                    self.assertNotIn("user@x.test", result.output)
                    self.assertEqual(result.returncode, 1)
                    self.assertNotIn("account(s) to process", result.stdout)
                    self.assertEqual(mock.total_requests, 0)

    def test_rejected_email_field_on_line_2_leaves_lines_1_and_3(self):
        first, third = "first@x.test", "third@x.test"
        text = f"{first}:{PASSWORD}\nuser@x.test hunter:2\n{third}:{PASSWORD}\n"
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock(hydrate_ms=100), text, [*range_args(s, 3), "--stagger", "0"], {})):
            with self.subTest(script):
                self.assertIn("Invalid format on line 2", result.stdout)
                self.assertNotIn("hunter", result.output)
                self.assertEqual(sorted(e for e, _ in mock.ledger_pairs()), [first, third], result.output[-1500:])
                self.assertEqual(result.returncode, 0)

    def test_undecodable_users_file(self):
        text = f"{EMAIL}:{PASSWORD}\n".encode() + b"\xff\xfe\n"
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock(), text, range_args(s, 2), {"check_password": False})):
            with self.subTest(script):
                self.assertNotIn(PASSWORD, result.output)
                self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
