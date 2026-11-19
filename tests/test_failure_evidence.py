"""Plan L2: a failed claim leaves console evidence and one screenshot, safely."""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from gow_support import (
    PASSWORD,
    SCREENSHOT_DIR,
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

EMAIL = "evidence@x.test"


def script_email(tag, script):
    return f"{tag}-{script[6:-3].replace('_', '')}@x.test"


def failure_shot(email):
    safe = email.replace("@", "_at_").replace(".", "_")
    return os.path.join(SCREENSHOT_DIR, f"{safe}_claim_failed.png")


def console_lines(result):
    return [l for l in result.stdout.splitlines() if " Console: " in l]


class Evidence(GowTestCase):
    def run_account(self, account, **kwargs):
        def build(script):
            mock = self.make_mock({EMAIL: account}, hydrate_ms=100)
            return mock, users_text([EMAIL]), [*range_args(script, 1), "--stagger", "0"], kwargs
        return self.for_each_script(build)


class ConsoleCapture(Evidence):
    def test_app_failure_appears_as_one_console_warning(self):
        for script, _, result in self.run_account(Account(collect_body='{"error":"offer unavailable"}')):
            with self.subTest(script):
                lines = console_lines(result)
                self.assertEqual(len(lines), 1, result.output[-1500:])
                self.assertRegex(lines[0], rf"\[WARNING\] \[{EMAIL}\] Console: Free purchase handling failed: offer unavailable")

    def test_click_that_sends_nothing_shows_the_reason(self):
        for script, _, result in self.run_account(Account(claim_no_request=True)):
            with self.subTest(script):
                self.assertRegex(result.stdout, rf"\[WARNING\] \[{EMAIL}\] Console: .*Missing ENCRYPTION_PUBLIC_KEY")

    def test_thrown_page_error_is_logged_the_same_way(self):
        account = Account(collect_status=500, click_throw="thrown during the click")
        for script, _, result in self.run_account(account):
            with self.subTest(script):
                self.assertRegex(result.stdout, rf"\[WARNING\] \[{EMAIL}\] Console: .*thrown during the click")

    def test_success_logs_no_console_line(self):
        account = Account(click_console=(("error", "noise that is not a failure"),), click_throw="unrelated")
        for script, mock, result in self.run_account(account):
            with self.subTest(script):
                self.assertEqual(result.returncode, 0, result.output[-1500:])
                self.assertEqual(console_lines(result), [])

    def test_seven_errors_give_at_most_five_lines(self):
        errors = tuple(("error", f"problem number {i}") for i in range(7))
        for script, _, result in self.run_account(Account(collect_status=500, click_console=errors)):
            with self.subTest(script):
                lines = console_lines(result)
                self.assertEqual(len(lines), 5, result.output[-1500:])
                self.assertIn("problem number 0", lines[0])

    def test_warn_and_log_messages_are_ignored(self):
        noise = (("warn", "just a warning"), ("log", "just a log"), ("info", "just info"))
        for script, _, result in self.run_account(Account(collect_status=500, click_console=noise)):
            with self.subTest(script):
                self.assertNotIn("just a", result.stdout)
                self.assertNotIn("just info", result.stdout)

    def test_error_logged_shortly_after_the_response_is_still_caught(self):
        """The app logs only after parsing the response, so capture has to outlast it"""
        for script, _, result in self.run_account(Account(collect_status=500, fail_delay_ms=300)):
            with self.subTest(script):
                self.assertRegex(result.stdout, r"Console: Free purchase handling failed")

    def test_redaction_on_the_real_path(self):
        token = "Ab3" * 22
        text = f"boom {PASSWORD} from mailbox@x.test token=abc123 blob {token}"
        account = Account(collect_status=500, click_console=(("error", text),))
        for script, _, result in self.run_account(account, check_password=False):
            with self.subTest(script):
                for leaked in (PASSWORD, "mailbox@x.test", "abc123", token):
                    self.assertNotIn(leaked, result.output)
                line = next(l for l in console_lines(result) if "boom" in l)
                self.assertIn("boom <redacted> from <email> token=<redacted> blob <redacted>", line)

    def test_multi_line_message_keeps_its_first_line_within_the_limit(self):
        first = ("word " * 80)[:300]
        account = Account(collect_status=500, click_console=(("error", f"{first}\nSECONDLINE"),))
        for script, _, result in self.run_account(account):
            with self.subTest(script):
                line = next(l for l in console_lines(result) if "word" in l)
                self.assertLessEqual(len(line.split("Console: ", 1)[1]), 200)
                self.assertNotIn("SECONDLINE", result.output)

    def test_html_body_failure_stack_stays_on_one_line(self):
        """A failed r.json() reaches the console with a stack trace, as finding 26 saw"""
        account = Account(collect_status=500, collect_body="<html>oops</html>")
        for script, _, result in self.run_account(account):
            with self.subTest(script):
                failures = [l for l in console_lines(result) if "Free purchase handling failed" in l]
                self.assertEqual(len(failures), 1, result.output[-1500:])
                self.assertIn("SyntaxError", failures[0])
                self.assertNotRegex(result.stdout, r"(?m)^\s+at ")
                self.assert_tagged(result, [EMAIL])


class FailureScreenshot(GowTestCase):
    def build(self, tag, account, **kwargs):
        def build(script):
            email = script_email(tag, script)
            mock = self.make_mock({email: account}, hydrate_ms=100)
            return mock, users_text([email]), [*range_args(script, 1), "--stagger", "0"], kwargs
        return build

    def test_failed_claim_saves_one_screenshot_though_load_never_fires(self):
        for script, _, result in self.for_each_script(self.build("oneshot", Account(collect_status=500))):
            with self.subTest(script):
                path = failure_shot(script_email("oneshot", script))
                self.assertGreater(os.path.getsize(path), 0, result.output[-1500:])
                self.assertNotIn("Screenshot not saved", result.stdout)

    def test_successful_claim_saves_none(self):
        for script, _, result in self.for_each_script(self.build("clean", Account())):
            with self.subTest(script):
                self.assertEqual(result.returncode, 0, result.output[-1500:])
                self.assertFalse(os.path.exists(failure_shot(script_email("clean", script))))

    def test_claim_all_writes_after_claim_only_on_a_claim(self):
        email = "afterclaim@x.test"
        mock = self.make_mock({email: Account(collect_status=500)}, hydrate_ms=100)
        self.run_gow("claim_all.py", mock, users_text([email]), "--stagger", "0")
        base = os.path.join(SCREENSHOT_DIR, "afterclaim_at_x_test")
        self.assertTrue(os.path.exists(f"{base}_catalog.png"))
        self.assertTrue(os.path.exists(f"{base}_claim_failed.png"))
        self.assertFalse(os.path.exists(f"{base}_after_claim.png"))

    def test_second_failed_character_overwrites_the_first(self):
        email = "overwrite@x.test"
        mock = self.make_mock({email: Account(characters=("c1", "c2"), collect_status=500)})
        result = self.run_gow("claim_all_characters.py", mock, users_text([email]), "1", "1", "--stagger", "0",
                              harness={})
        shots = result.spy["screenshots"]
        self.assertEqual(len(shots), 2, result.output[-2000:])
        self.assertEqual({path for path, _, _ in shots}, {failure_shot(email)})
        self.assertGreater(shots[1][1], shots[0][1])
        names = [n for n in os.listdir(SCREENSHOT_DIR) if n.startswith("overwrite_at_x_test")]
        self.assertEqual(names, ["overwrite_at_x_test_claim_failed.png"])


class HungScreenshot(GowTestCase):
    def assert_unaffected(self, email, mock, result):
        (path, written, seconds), *_ = result.spy["screenshots"][-1:]
        self.assertIn("_claim_failed", path)
        self.assertGreaterEqual(seconds, 9.5)
        self.assertLessEqual(seconds, 11.5)
        self.assertRegex(result.stdout, rf"\[WARNING\] \[{email}\] Screenshot not saved")
        self.assertEqual(result.summary_rows(), [(False, email)], result.output[-1500:])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(mock.ledger_pairs(), [])
        self.assertNotIn("Error closing browser", result.stdout)
        self.assertNotIn("Error stopping Playwright", result.stdout)

    def test_each_cdp_step_hanging_is_bounded(self):
        for step in ("new_cdp_session", "send", "detach"):
            def build(script, step=step):
                email = script_email(f"hung-{step.replace('_', '')}", script)
                mock = self.make_mock({email: Account(collect_status=500)}, hydrate_ms=100)
                harness = {"cdp_hang": step, "cdp_hang_path": "_claim_failed"}
                return mock, users_text([email]), [*range_args(script, 1), "--stagger", "0"], {"harness": harness}

            for script, mock, result in self.for_each_script(build):
                with self.subTest(script, step=step):
                    self.assert_unaffected(script_email(f"hung-{step.replace('_', '')}", script), mock, result)

    def test_real_hung_send_on_a_frozen_renderer(self):
        """Nothing patched: the page spins forever, so Chromium never answers the capture"""
        def build(script):
            email = script_email("frozen", script)
            mock = self.make_mock({email: Account(collect_status=500, freeze_after_click=True)}, hydrate_ms=100)
            return mock, users_text([email]), [*range_args(script, 1), "--stagger", "0"], {"harness": {}, "timeout": 120}

        for script, mock, result in self.for_each_script(build):
            with self.subTest(script):
                self.assert_unaffected(script_email("frozen", script), mock, result)

    def test_real_hung_detach_on_a_frozen_driver(self):
        """Nothing patched past the freeze: the driver stops just before detach, so the
        call truly never returns, and only teardown's own bounds get the run out"""
        email = "wedgedetach@x.test"
        temp_dir = tempfile.mkdtemp(prefix="gow-tests-")
        self.addCleanup(shutil.rmtree, temp_dir, True)
        mock = self.make_mock({email: Account(collect_status=500)}, hydrate_ms=100)
        harness = {"wedge_driver_at_detach": True}
        process = start_script("claim_batch.py", mock, users_text([email]), ("1", "1", "--stagger", "0"),
                               harness, temp_dir, new_session=True)
        started = time.monotonic()
        wait_until(lambda: mock.login_hits, 30, "the login page")
        children = descendants(process.pid)
        self.addCleanup(kill_all, children)
        try:
            stdout, stderr = process.communicate(timeout=100)
        except subprocess.TimeoutExpired:
            process.kill()
            kill_all(children)
            stdout, stderr = process.communicate()
        result = finish(process, stdout, stderr, time.monotonic() - started, temp_dir, harness)

        self.assertRegex(result.stdout, rf"\[WARNING\] \[{email}\] Screenshot not saved", result.output[-1500:])
        self.assertEqual(result.summary_rows(), [(False, email)], result.output[-1500:])
        self.assertEqual(result.returncode, 1)
        self.assertLessEqual(result.spy["screenshots"][-1][2], 11.5)
        wait_until(lambda: not still_running(children), 15, "the frozen driver and its browser to exit")

    def test_hung_after_claim_screenshot_does_not_cost_the_claim(self):
        email = "afterhang@x.test"
        mock = self.make_mock({email: Account()}, hydrate_ms=100)
        result = self.run_gow("claim_all.py", mock, users_text([email]), "--stagger", "0",
                              harness={"cdp_hang": "send", "cdp_hang_path": "_after_claim"})
        self.assertEqual(result.summary_rows(), [(True, email)], result.output[-1500:])
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 75)
        self.assertEqual(mock.ledger_pairs(), [(email, "only")])
        self.assertIn("Screenshot not saved", result.stdout)
        self.assertEqual(result.returncode, 0)

    def test_frozen_page_with_no_request_still_finishes(self):
        """The no-request path also asks whether cooldown text shows, on the same frozen page"""
        email = "frozenquiet@x.test"
        mock = self.make_mock({email: Account(claim_no_request=True, freeze_after_click=True)}, hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text([email]), "1", "1", "--stagger", "0",
                              harness={}, timeout=90)
        self.assertEqual(result.summary_rows(), [(False, email)], result.output[-1500:])
        self.assertIn("click sent no claim request", result.stdout)
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
