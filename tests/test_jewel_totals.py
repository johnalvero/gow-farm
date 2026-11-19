"""Plan F5: Total Jewels Claimed counts new claims only, cooldown still counts as success."""

import unittest

from gow_support import ALL_SCRIPTS, Account, GowTestCase, range_args, users_text

MULTI = "claim_all_characters.py"
LEGACY = ("claim_batch.py", "claim_all.py")
EMAIL = "jewels@x.test"
TWO = ("c1", "c2")


class Totals(GowTestCase):
    def run_one(self, script, account, mock=None):
        mock = mock or self.make_mock({EMAIL: account}, hydrate_ms=100)
        result = self.run_gow(script, mock, users_text([EMAIL]), *range_args(script, 1), "--stagger", "0")
        return mock, result

    def assert_counts(self, result, jewels, already):
        self.assertEqual(result.summary_value("Total Jewels Claimed"), jewels, result.output[-1500:])
        self.assertEqual(result.summary_value("Characters Already Claimed"), already)


class MultiCharacterTotals(Totals):
    def test_rerun_after_both_characters_are_claimed(self):
        mock, _ = self.run_one(MULTI, Account(characters=TWO))
        self.assertEqual(len(mock.ledger_pairs()), 2)

        _, result = self.run_one(MULTI, None, mock)
        self.assert_counts(result, 0, 2)
        self.assertEqual(result.summary_value("Accounts Success"), 1)
        self.assertEqual(result.summary_value("Accounts Failed"), 0)
        self.assertIn("✅ 2/2 chars", result.stdout)
        self.assertIn("Claimed 2/2 characters (2 already claimed)", result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(mock.ledger_pairs()), 2)
        self.assertEqual(mock.duplicate_claims, [])

    def test_one_cooldown_and_one_claimable_in_either_order(self):
        runs = self.run_all([
            lambda c=character: self.run_one(MULTI, Account(characters=TWO, cooldown_characters=(c,)))
            for character in TWO])
        for character, (mock, result) in zip(TWO, runs):
            with self.subTest(cooldown=character):
                self.assert_counts(result, 75, 1)
                self.assertIn("✅ 2/2 chars", result.stdout)
                self.assertIn("Claimed 2/2 characters (1 already claimed)", result.stdout)
                self.assertEqual(len(mock.ledger_pairs()), 1)
                self.assertEqual(result.returncode, 0)

    def test_fresh_three_characters_have_no_already_claimed_suffix(self):
        _, result = self.run_one(MULTI, Account(characters=("c1", "c2", "c3")))
        complete = next(l for l in result.stdout.splitlines() if "ACCOUNT COMPLETE" in l)
        self.assertNotIn("already claimed", complete)
        self.assertIn("Claimed 3/3 characters", complete)
        self.assertRegex(result.stdout, r"Characters Already Claimed: 0\n")

    def test_partial_failure_counts_only_what_happened(self):
        mock, result = self.run_one(MULTI, Account(characters=TWO, unknown_characters=("c2",)))
        self.assert_counts(result, 75, 0)
        self.assertIn("✅ 1/2 chars", result.stdout)
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "c1")])
        self.assertEqual(result.returncode, 0)

    def test_order_of_the_new_summary_line(self):
        _, result = self.run_one(MULTI, Account())
        labels = [l.split("] ", 2)[-1].split(":")[0] for l in result.stdout.splitlines() if "[INFO]" in l]
        found = labels.index("Total Characters Found")
        self.assertEqual(labels[found + 1:found + 3], ["Characters Already Claimed", "Total Jewels Claimed"])


class LegacyScriptTotals(Totals):
    def test_cooldown_account(self):
        for script, _, result in self.for_each_script(lambda s: (
                self.make_mock({EMAIL: Account(vault="cooldown")}, hydrate_ms=100),
                users_text([EMAIL]), [*range_args(s, 1), "--stagger", "0"], {}), LEGACY):
            with self.subTest(script):
                self.assertIn("✅ SUCCESS", result.stdout)
                self.assert_counts(result, 0, 1)
                self.assertRegex(result.stdout, r"Total: 1 \| Success: 1 \| Failed: 0\n")
                self.assertEqual(result.returncode, 0)

    def test_fresh_account(self):
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock({EMAIL: Account()}, hydrate_ms=100),
                users_text([EMAIL]), [*range_args(s, 1), "--stagger", "0"], {}), LEGACY):
            with self.subTest(script):
                self.assert_counts(result, 75, 0)
                self.assertEqual(len(mock.ledger_pairs()), 1)
                self.assertRegex(result.stdout, r"Total: 1 \| Success: 1 \| Failed: 0\n")
                self.assertEqual(result.returncode, 0)

    def test_batch_keeps_its_range_line_above_the_total(self):
        _, result = self.run_one("claim_batch.py", Account())
        self.assertRegex(result.stdout, r"Range: Lines 1-1\n.*Total: 1 \| Success: 1 \| Failed: 0\n")

    def test_summary_order_is_total_then_already_then_jewels(self):
        for script, _, result in self.for_each_script(lambda s: (
                self.make_mock({EMAIL: Account()}, hydrate_ms=100),
                users_text([EMAIL]), [*range_args(s, 1), "--stagger", "0"], {}), LEGACY):
            with self.subTest(script):
                self.assertRegex(result.stdout, r"Total: .*\n.*Characters Already Claimed: 0\n.*Total Jewels Claimed: 75")

    def test_mixed_run_matches_the_ledger(self):
        accounts = {
            "fresh@x.test": Account(),
            "cool@x.test": Account(vault="cooldown"),
            "denied@x.test": Account(password="other"),
            "fresh2@x.test": Account(),
        }
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock(accounts, hydrate_ms=100), users_text(accounts),
                [*range_args(s, 4), "--concurrency", "3", "--stagger", "0.1"], {}), LEGACY):
            with self.subTest(script):
                self.assert_counts(result, 75 * len(mock.ledger_pairs()), 1)
                self.assertEqual(len(mock.ledger_pairs()), 2)
                self.assertRegex(result.stdout, r"Total: 4 \| Success: 3 \| Failed: 1\n")
                self.assertEqual(result.returncode, 1)


class FailuresAddNothing(Totals):
    """FAILED is a truthy string, and 'already' is a new key on every return path"""

    def assert_failed(self, result):
        self.assertIn("❌ FAILED", result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assert_counts(result, 0, 0)
        self.assertNotIn("KeyError", result.output)

    def run_case(self, accounts, **kwargs):
        return self.for_each_script(lambda s: (
            self.make_mock(accounts, hydrate_ms=100), users_text([EMAIL]),
            [*range_args(s, 1), "--stagger", "0"], kwargs))

    def test_unknown_vault(self):
        for script, _, result in self.run_case({EMAIL: Account(vault="nothing")}):
            with self.subTest(script):
                self.assert_failed(result)

    def test_rejected_login(self):
        for script, _, result in self.run_case({EMAIL: Account(password="other")}):
            with self.subTest(script):
                self.assert_failed(result)

    def test_worker_exception(self):
        for script, _, result in self.run_case({EMAIL: Account()}, harness={"raise_worker_for": [EMAIL]}):
            with self.subTest(script):
                self.assert_failed(result)
                self.assertNotEqual(result.spy, {})


if __name__ == "__main__":
    unittest.main()
