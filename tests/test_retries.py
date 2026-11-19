"""Plan L3 and L4: one retry for an aborted vault navigation and for a login 5xx, no more."""

import unittest

from gow_support import PASSWORD, SUCCESS_MESSAGE, Account, GowTestCase, range_args, users_text

EMAIL = "retry@x.test"


class Retry(GowTestCase):
    def run_account(self, account, **kwargs):
        def build(script):
            mock = self.make_mock({EMAIL: account}, hydrate_ms=100)
            return mock, users_text([EMAIL]), [*range_args(script, 1), "--stagger", "0"], kwargs
        return self.for_each_script(build)

    def assert_claimed(self, script, mock, result):
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")], result.output[-1500:])
        self.assertIn(SUCCESS_MESSAGE[script], result.stdout)
        self.assertEqual(result.returncode, 0)


class VaultRetry(Retry):
    MESSAGE = f"[{EMAIL}] Vault navigation aborted, retrying in 2s"

    def test_one_abort_is_retried_and_the_claim_lands(self):
        for script, mock, result in self.run_account(Account(vault_abort_times=1)):
            with self.subTest(script):
                self.assert_claimed(script, mock, result)
                self.assertEqual(result.stdout.count(self.MESSAGE), 1)
                self.assertRegex(result.stdout, rf"\[WARNING\] \[{EMAIL}\] Vault navigation aborted")
                self.assertEqual(len(mock.vault_hits), 2)

    def test_the_abort_is_real_and_its_text_stays_out_of_the_retry_line(self):
        for script, mock, result in self.run_account(Account(vault_abort_times=1), harness={}):
            with self.subTest(script):
                self.assertEqual(result.spy["vault_gotos"], 2)
                self.assertNotIn("ERR_ABORTED", result.stdout)

    def test_two_aborts_fail_with_no_third_attempt(self):
        for script, mock, result in self.run_account(Account(vault_abort_times=2), harness={}):
            with self.subTest(script):
                self.assertEqual(len(mock.vault_hits), 2)
                self.assertEqual(result.spy["vault_gotos"], 2)
                self.assertEqual(result.summary_rows(), [(False, EMAIL)], result.output[-1500:])
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout.count(self.MESSAGE), 1)
                self.assert_tagged(result, [EMAIL])

    def test_abort_on_a_later_character(self):
        mock = self.make_mock({EMAIL: Account(characters=("c1", "c2", "c3"), vault_abort_at=(2,))})
        result = self.run_gow("claim_all_characters.py", mock, users_text([EMAIL]), "1", "1", "--stagger", "0")
        self.assertIn("Claimed 3/3 characters", result.stdout, result.output[-2500:])
        self.assertEqual(sorted(c for _, c in mock.ledger_pairs()), ["c1", "c2", "c3"])
        self.assertEqual(result.stdout.count(self.MESSAGE), 1)

    def test_a_goto_timeout_is_not_retried(self):
        for script, mock, result in self.run_account(Account(), harness={"goto_timeout_on_vault": True}):
            with self.subTest(script):
                self.assertEqual(result.spy["vault_gotos"], 1)
                self.assertNotIn("retrying", result.stdout)
                self.assertEqual(result.summary_rows(), [(False, EMAIL)], result.output[-1500:])
                self.assertEqual(result.returncode, 1)


class LoginRetry(Retry):
    def test_one_5xx_is_retried_from_the_login_page(self):
        for status in (500, 503):
            account = Account(login_failures=1, login_failure_status=status)
            for script, mock, result in self.run_account(account):
                with self.subTest(script, status=status):
                    self.assertEqual(result.stdout.count(f"Login failed: HTTP {status}, retrying in 5s"), 1,
                                     result.output[-1500:])
                    self.assertEqual(mock.logins, [(EMAIL, PASSWORD)] * 2)
                    self.assertEqual(len(mock.login_hits), 2)
                    self.assert_claimed(script, mock, result)

    def test_two_5xx_give_up(self):
        for script, mock, result in self.run_account(Account(login_failures=2)):
            with self.subTest(script):
                self.assertEqual(len(mock.logins), 2)
                self.assertRegex(result.stdout, r"\[ERROR\] .*Login rejected: HTTP 500")
                self.assertEqual(mock.vault_hits, [])
                self.assertEqual(result.summary_rows(), [(False, EMAIL)], result.output[-1500:])
                self.assertEqual(result.returncode, 1)

    def test_4xx_never_retries(self):
        for name, account in (("401", Account(password="other")),
                              ("403", Account(login_failures=9, login_failure_status=403)),
                              ("429", Account(login_failures=9, login_failure_status=429))):
            for script, mock, result in self.run_account(account):
                with self.subTest(script, status=name):
                    self.assertEqual(len(mock.logins), 1)
                    self.assertNotIn("retrying", result.stdout)
                    self.assertIn(f"Login rejected: HTTP {name}", result.stdout)

    def test_no_observed_response_never_retries(self):
        mock = self.make_mock({EMAIL: Account(login_hang=True)}, hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text([EMAIL]), "1", "1", "--stagger", "0")
        self.assertEqual(len(mock.logins), 1)
        self.assertNotIn("retrying", result.stdout)

    def test_login_page_error_is_not_retried(self):
        mock = self.make_mock({EMAIL: Account()}, hydrate_ms=100, login_page_status=503)
        result = self.run_gow("claim_batch.py", mock, users_text([EMAIL]), "1", "1", "--stagger", "0")
        self.assertEqual(len(mock.login_hits), 1)
        self.assertNotIn("retrying", result.stdout)

    def test_retry_keeps_the_global_login_spacing(self):
        addresses = ["space1@x.test", "space2@x.test", "space3@x.test"]
        accounts = {addresses[1]: Account(login_failures=1)}
        mock = self.make_mock(accounts, hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text(addresses), "1", "3", "--stagger", "1")
        gaps = [b - a for a, b in zip(mock.login_hits, mock.login_hits[1:])]
        self.assertEqual(len(mock.login_hits), 4, result.output[-1500:])
        self.assertGreaterEqual(min(gaps), 0.95, gaps)
        self.assertEqual(result.returncode, 0, result.output[-1500:])

    def test_retry_takes_a_paced_slot_when_starts_are_queued_up(self):
        """At --stagger 8 the third start is due at 16s, and the retry follows it at 24s:
        taking its slot, not going straight in after its 5s backoff"""
        addresses = ["queued1@x.test", "queued2@x.test", "queued3@x.test"]
        accounts = {addresses[0]: Account(login_failures=1, password=PASSWORD)}
        mock = self.make_mock(accounts, hydrate_ms=100)
        self.run_gow("claim_batch.py", mock, users_text(addresses), "1", "3", "--stagger", "8")
        gaps = [b - a for a, b in zip(mock.login_hits, mock.login_hits[1:])]
        self.assertEqual(len(mock.login_hits), 4)
        self.assertGreaterEqual(min(gaps), 7.9, gaps)

    def test_dialog_window_restarts_for_the_second_attempt(self):
        email = "retrydialog@x.test"
        account = Account(characters=("c1", "c2"), login_failures=1, dialog_ms=4000)
        mock = self.make_mock({email: account}, hydrate_ms=100)
        result = self.run_gow("claim_all_characters.py", mock, users_text([email]), "1", "1", "--stagger", "0")
        self.assertIn("Found 2 characters!", result.stdout, result.output[-1500:])
        self.assertEqual(sorted(c for _, c in mock.ledger_pairs()), ["c1", "c2"])


if __name__ == "__main__":
    unittest.main()
