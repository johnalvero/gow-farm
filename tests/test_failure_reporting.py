"""Failure isolation, summaries, the ledger, log tagging and browser recovery."""

import os
import shutil
import signal
import tempfile
import time
import unittest

from gow_support import (
    PASSWORD,
    Account,
    GowTestCase,
    command_of,
    descendants,
    finish,
    range_args,
    start_script,
    users_text,
    wait_until,
)


class MixedFailures(GowTestCase):
    FIVE = ["good1@x.test", "badpw@x.test", "hang@x.test", "boom@x.test", "good2@x.test"]

    def build(self, script):
        mock = self.make_mock({
            "badpw@x.test": Account(password="other"),
            "hang@x.test": Account(vault="hang"),
        }, hydrate_ms=100)
        args = [*range_args(script, 5), "--concurrency", "3", "--stagger", "0.1"]
        return mock, users_text(self.FIVE), args, {"harness": {"raise_worker_for": ["boom@x.test"]}}

    def test_one_bad_login_one_vault_timeout_one_raise(self):
        for script, mock, result in self.for_each_script(self.build):
            with self.subTest(script):
                self.assertEqual(result.returncode, 1)
                self.assertEqual(sorted(e for e, _ in mock.ledger_pairs()), ["good1@x.test", "good2@x.test"])
                self.assertEqual(result.summary_rows(), [
                    (True, "good1@x.test"),
                    (False, "badpw@x.test"),
                    (False, "hang@x.test"),
                    (False, "boom@x.test"),
                    (True, "good2@x.test"),
                ])
                self.assertIn("Timeout 60000ms exceeded", result.stdout)
                self.assertIn("Error processing boom@x.test: forced worker failure", result.stdout)
                self.assertNotIn("second line of worker failure", result.output)
                self.assert_tagged(result, self.FIVE)


class RejectedLogin(GowTestCase):
    def test_status_is_logged_with_no_vault_load(self):
        cases = {"denied@x.test": (Account(password="other"), 401),
                 "throttled@x.test": (Account(login_status=429), 429),
                 "broken@x.test": (Account(login_status=500), 500)}

        for email, (account, status) in cases.items():
            for script, mock, result in self.for_each_script(lambda s, e=email, a=account: (
                    self.make_mock({e: a}, hydrate_ms=100), users_text([e]),
                    [*range_args(s, 1), "--stagger", "0"], {})):
                with self.subTest(script, status=status):
                    self.assertRegex(result.stdout, rf"\[ERROR\] \[{email}\] Login rejected: HTTP {status}")
                    self.assertEqual(mock.vault_hits, [])
                    self.assertEqual(mock.ledger_pairs(), [])
                    self.assertIn("❌ FAILED", result.stdout)
                    self.assertNotIn("75 jewels", result.stdout)
                    self.assertEqual(result.returncode, 1)


class ExitCodes(GowTestCase):
    def test_all_accounts_succeeding_exits_0(self):
        addresses = ["ok1@x.test", "ok2@x.test"]
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock(hydrate_ms=100), users_text(addresses),
                [*range_args(s, 2), "--stagger", "0"], {})):
            with self.subTest(script):
                self.assertEqual(result.returncode, 0, result.output[-1500:])
                self.assertEqual(result.summary_rows(), [(True, a) for a in addresses])

    def test_zero_valid_accounts_exits_1_with_no_browser(self):
        for name, text in (("empty", ""), ("comments", "# one\n\n# two\n"), ("no password", "a@x.test:\n:\n")):
            for script, mock, result in self.for_each_script(lambda s, t=text: (
                    self.make_mock(), t, range_args(s, 3), {"harness": {}})):
                with self.subTest(script, users=name):
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.spy["launches"], 0)
                    self.assertEqual(mock.total_requests, 0)


class BrowserCrash(GowTestCase):
    def test_only_in_flight_accounts_fail_and_later_ones_relaunch(self):
        addresses = [f"crash{i}@x.test" for i in range(1, 5)]
        mock = self.make_mock(hydrate_ms=4000)
        temp_dir = tempfile.mkdtemp(prefix="gow-tests-")
        self.addCleanup(shutil.rmtree, temp_dir, True)

        process = start_script("claim_batch.py", mock, users_text(addresses),
                               ("1", "4", "--concurrency", "2", "--stagger", "0"), {}, temp_dir)
        started = time.monotonic()
        wait_until(lambda: len(mock.login_hits) >= 2, 30, "both accounts to reach the login page")

        browsers = [pid for pid in descendants(process.pid)
                    if "--type=" not in command_of(pid) and "headless" in command_of(pid).lower()
                    and "node" not in command_of(pid).split()[0]]
        self.assertEqual(len(browsers), 1, "expected exactly this run's browser")
        os.kill(int(browsers[0]), signal.SIGKILL)

        stdout, stderr = process.communicate(timeout=120)
        result = finish(process, stdout, stderr, time.monotonic() - started, temp_dir, {})

        self.assertNotIn(PASSWORD, result.output)
        self.assertEqual(result.summary_rows(), [
            (False, addresses[0]), (False, addresses[1]), (True, addresses[2]), (True, addresses[3])],
            result.output[-2000:])
        self.assertEqual(sorted(e for e, _ in mock.ledger_pairs()), addresses[2:])
        self.assertEqual(result.spy["launches"], 2)
        self.assertEqual(result.returncode, 1)
        self.assert_tagged(result, addresses)


class BrowserLaunchFailure(GowTestCase):
    def test_failed_launch_fails_that_account_only(self):
        addresses = ["launch1@x.test", "launch2@x.test"]
        mock = self.make_mock(hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text(addresses), "1", "2",
                              "--concurrency", "1", "--stagger", "0", harness={"launch_failures": 1})
        self.assertEqual(result.summary_rows(), [(False, addresses[0]), (True, addresses[1])], result.output[-1500:])
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"Error processing {addresses[0]}: forced launch failure", result.stdout)
        self.assertNotIn("second line of launch failure", result.output)

    def test_every_launch_failing_still_prints_a_full_summary(self):
        addresses = ["launch1@x.test", "launch2@x.test", "launch3@x.test"]
        mock = self.make_mock()
        result = self.run_gow("claim_all_characters.py", mock, users_text(addresses), "1", "3",
                              "--stagger", "0", harness={"launch_failures": 99})
        self.assertEqual(result.summary_rows(), [(False, a) for a in addresses], result.output[-1500:])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(mock.total_requests, 0)


class CloseFailures(GowTestCase):
    def test_context_close_raising_keeps_a_success(self):
        email = "closer@x.test"
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock({email: Account()}, hydrate_ms=100), users_text([email]),
                [*range_args(s, 1), "--stagger", "0"], {"harness": {"context_close_raises": True}})):
            with self.subTest(script):
                self.assertEqual(result.returncode, 0, result.output[-1500:])
                self.assertEqual(result.summary_rows(), [(True, email)])
                self.assertEqual(mock.ledger_pairs(), [(email, "only")])
                self.assertIn(f"[{email}] Error closing browser context: forced context close failure", result.stdout)
                self.assertNotIn("second line of close failure", result.output)
                self.assert_tagged(result, [email])

    def test_context_close_raising_keeps_a_failure(self):
        email = "closer@x.test"
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock({email: Account(password="other")}, hydrate_ms=100), users_text([email]),
                [*range_args(s, 1), "--stagger", "0"], {"harness": {"context_close_raises": True}})):
            with self.subTest(script):
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.summary_rows(), [(False, email)])

    def test_browser_close_raising_keeps_the_summary(self):
        """Edge the criteria don't name: the accounts are done, only teardown failed"""
        email = "closer@x.test"
        mock = self.make_mock({email: Account()}, hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text([email]), "1", "1", "--stagger", "0",
                              harness={"browser_close_raises": True})
        self.assertEqual(mock.ledger_pairs(), [(email, "only")])
        self.assertEqual(result.summary_rows(), [(True, email)], result.output[-1500:])
        self.assertEqual(result.returncode, 0)


class SummaryMatchesLedger(GowTestCase):
    """Acceptance criterion, verbatim: Summary counts and Total Jewels Claimed
    equal the mock ledger for a mixed run at concurrency 3."""

    def run_mixed(self, accounts):
        addresses = list(accounts)
        mock = self.make_mock(accounts, hydrate_ms=100)
        result = self.run_gow("claim_all_characters.py", mock, users_text(addresses), "1", str(len(addresses)),
                              "--concurrency", "3", "--stagger", "0.1")
        return mock, result

    def assert_matches(self, mock, result, accounts_success):
        pairs = mock.ledger_pairs()
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 75 * len(pairs),
                         f"ledger holds {len(pairs)} claims: {pairs}")
        # Plan F5: a cooldown account succeeds with no ledger entry, so this is
        # the plan's expected value rather than the ledger's account count
        self.assertEqual(result.summary_value("Accounts Success"), accounts_success)

    def test_mixed_run_including_an_already_claimed_account(self):
        mock, result = self.run_mixed({
            "single@x.test": Account(),
            "multi@x.test": Account(characters=("c1", "c2", "c3")),
            "cooldown@x.test": Account(vault="cooldown"),
            "denied@x.test": Account(password="other"),
        })
        self.assert_matches(mock, result, accounts_success=3)
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 300)
        self.assertEqual(result.summary_value("Characters Already Claimed"), 1)
        self.assertEqual(result.summary_value("Accounts Failed"), 1)
        self.assertEqual(result.returncode, 1)

    def test_mixed_run_without_already_claimed_accounts(self):
        """The same run minus cooldown, which isolates cooldown as the cause"""
        mock, result = self.run_mixed({
            "single@x.test": Account(),
            "multi@x.test": Account(characters=("c1", "c2", "c3")),
            "denied@x.test": Account(password="other"),
        })
        self.assert_matches(mock, result, accounts_success=2)


class Tagging(GowTestCase):
    def test_every_line_names_its_account_under_concurrency_3(self):
        accounts = {
            "tag1@x.test": Account(),
            "tag2@x.test": Account(characters=("c1", "c2")),
            "tag3@x.test": Account(vault="cooldown"),
            "tag4@x.test": Account(password="other"),
            "tag5@x.test": Account(collect_status=500),
        }
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock(accounts, hydrate_ms=100), users_text(accounts),
                [*range_args(s, 5), "--concurrency", "3", "--stagger", "0.1"], {})):
            with self.subTest(script):
                self.assertIn("SUMMARY", result.stdout)
                self.assert_tagged(result, list(accounts))
                self.assertIn("Claim failed: HTTP 500", result.stdout)
                self.assertIn((False, "tag5@x.test"), result.summary_rows())


if __name__ == "__main__":
    unittest.main()
