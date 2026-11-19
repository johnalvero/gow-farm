"""Login and vault acceptance criteria, end to end, on all three scripts."""

import os
import unittest

from gow_support import (
    ALREADY_CLAIMED_MESSAGE,
    PASSWORD,
    SCREENSHOT_DIR,
    SUCCESS_MESSAGE,
    Account,
    GowTestCase,
    range_args,
    users_text,
)

EMAIL = "flow@x.test"


class Waits(GowTestCase):
    def one_account(self, account=None, **mock_options):
        """Build a single-account run for each script"""
        def build(script):
            mock = self.make_mock({EMAIL: account or Account()}, **mock_options)
            return mock, users_text([EMAIL]), [*range_args(script, 1), "--stagger", "0"], {}
        return build

    def test_claim_completes_with_no_load_event_and_no_network_idle(self):
        for script, mock, result in self.for_each_script(self.one_account()):
            with self.subTest(script):
                self.assertEqual(result.returncode, 0, result.output[-1500:])
                self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])
                self.assertIn(SUCCESS_MESSAGE[script], result.stdout)
                self.assertEqual(result.stderr, "")

    def test_credentials_survive_hydration_two_seconds_after_inputs_show(self):
        for script, mock, result in self.for_each_script(self.one_account(hydrate_ms=2000)):
            with self.subTest(script):
                self.assertEqual(mock.logins, [(EMAIL, PASSWORD)])
                self.assertEqual(result.returncode, 0, result.output[-1500:])

    def test_badge_two_seconds_ahead_of_the_button_is_not_cooldown(self):
        for script, mock, result in self.for_each_script(self.one_account(Account(button_ms=2000))):
            with self.subTest(script):
                self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])
                self.assertIn(SUCCESS_MESSAGE[script], result.stdout)
                self.assertNotIn(ALREADY_CLAIMED_MESSAGE[script], result.stdout)

    def test_cooldown_states_send_no_claim_and_report_already_claimed(self):
        for vault in ("cooldown", "claimedmsg"):
            for script, mock, result in self.for_each_script(self.one_account(Account(vault=vault))):
                with self.subTest(script, vault=vault):
                    self.assertEqual(mock.ledger_pairs(), [])
                    self.assertEqual(mock.anonymous_claims, 0)
                    self.assertIn(ALREADY_CLAIMED_MESSAGE[script], result.stdout)
                    self.assertEqual(result.returncode, 0, result.output[-1500:])

    def test_vault_with_only_i18n_literals_is_a_failure(self):
        for script, mock, result in self.for_each_script(self.one_account(Account(vault="nothing"))):
            with self.subTest(script):
                self.assertEqual(result.returncode, 1)
                self.assertIn("❌ FAILED", result.stdout)
                self.assertEqual(mock.ledger_pairs(), [])
                self.assertNotIn("75 jewels", result.stdout)

    def test_claim_lands_when_the_click_is_ignored_before_hydration(self):
        accounts = (Account(claim_hydrate_ms=2000),
                    Account(claim_hydrate_ms=2000, replace_on_hydrate=True))
        for account in accounts:
            for script, mock, result in self.for_each_script(self.one_account(account)):
                with self.subTest(script, replaced=account.replace_on_hydrate):
                    self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])
                    self.assertIn(SUCCESS_MESSAGE[script], result.stdout)

    def test_cooldown_text_five_seconds_after_click_is_success(self):
        for script, mock, result in self.for_each_script(self.one_account(Account(confirm_ms=5000))):
            with self.subTest(script):
                self.assertIn(SUCCESS_MESSAGE[script], result.stdout)

    def test_ok_response_with_no_cooldown_text_is_success(self):
        for script, mock, result in self.for_each_script(self.one_account(Account(confirm_ms=-1))):
            with self.subTest(script):
                self.assertIn(SUCCESS_MESSAGE[script], result.stdout)
                self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])
                self.assertEqual(result.returncode, 0, result.output[-1500:])
                self.assertEqual(result.summary_rows(), [(True, EMAIL)])
                self.assertEqual(result.summary_value("Total Jewels Claimed"), 75)

    def test_claim_all_writes_both_screenshots_though_load_never_fires(self):
        mock = self.make_mock({EMAIL: Account()})
        result = self.run_gow("claim_all.py", mock, users_text([EMAIL]), "--stagger", "0")
        self.assertEqual(result.returncode, 0, result.output[-1500:])
        self.assertNotIn("Screenshot not saved", result.stdout)
        for suffix in ("catalog", "after_claim"):
            with self.subTest(suffix):
                path = os.path.join(SCREENSHOT_DIR, f"flow_at_x_test_{suffix}.png")
                self.assertGreater(os.path.getsize(path), 0)


class LoginEdges(GowTestCase):
    def test_cookie_overlay_covering_login_is_dismissed(self):
        for banner in ("onetrust", "accept"):
            mock = self.make_mock({EMAIL: Account()}, banner=banner)
            with self.subTest(banner):
                result = self.run_gow("claim_batch.py", mock, users_text([EMAIL]), "1", "1", "--stagger", "0")
                self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")], result.output[-1500:])

    def test_unicode_and_colon_passwords_reach_the_server_intact(self):
        for password in ("pässwörd-✓-密码", "with:colons:inside", 'quo"te\\slash', "  padded  "):
            with self.subTest(password):
                stored = password.strip()
                mock = self.make_mock({EMAIL: Account(password=stored)})
                result = self.run_gow("claim_batch.py", mock, f"{EMAIL}:{password}\n", "1", "1", "--stagger", "0")
                self.assertEqual(mock.logins, [(EMAIL, stored)])
                self.assertNotIn(stored, result.output)
                self.assertEqual(result.returncode, 0, result.output[-1500:])

    def test_colons_and_spaces_in_a_password_survive_in_all_three_scripts(self):
        email = "a@x.test"
        for password in ("pass:with:colons", "my pass"):
            for script, mock, result in self.for_each_script(lambda s, p=password: (
                    self.make_mock({email: Account(password=p)}, hydrate_ms=100),
                    f"{email}:{p}\n", [*range_args(s, 1), "--stagger", "0"], {})):
                with self.subTest(script, password=password):
                    self.assertEqual(mock.logins, [(email, password)])
                    self.assertEqual(result.returncode, 0, result.output[-1500:])
                    self.assertNotIn(password, result.output)

    def test_login_page_error_fails_without_typing_anything(self):
        for script, mock, result in self.for_each_script(lambda script: (
                self.make_mock({EMAIL: Account()}, login_page_status=503),
                users_text([EMAIL]), [*range_args(script, 1), "--stagger", "0"], {})):
            with self.subTest(script):
                self.assertEqual(result.returncode, 1)
                self.assertIn("Failed to load page: 503", result.stdout)
                self.assertEqual(mock.logins, [])
                self.assertEqual(mock.vault_hits, [])
                self.assertIn("❌ FAILED", result.stdout)

    def test_unobserved_login_response_warns_and_continues(self):
        mock = self.make_mock({EMAIL: Account(login_hang=True)})
        result = self.run_gow("claim_batch.py", mock, users_text([EMAIL]), "1", "1", "--stagger", "0")
        self.assertIn(f"[{EMAIL}] Login response not observed, continuing", result.stdout)
        self.assertEqual(len(mock.vault_hits), 1)
        self.assertEqual(mock.ledger_pairs(), [])
        self.assertEqual(len(mock.logins), 1)
        # The plan's residual false success is gone: a logged-out click is refused
        self.assertEqual(result.summary_rows(), [(False, EMAIL)], result.output[-1500:])
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 0)
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
