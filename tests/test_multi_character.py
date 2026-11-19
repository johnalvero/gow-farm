"""Multi-character acceptance criteria. Only claim_all_characters.py has this flow."""

import re
import unittest
from datetime import datetime

from gow_support import Account, GowTestCase, users_text

SCRIPT = "claim_all_characters.py"
EMAIL = "multi@x.test"
THREE = ("c1", "c2", "c3")


def stamp(result, text):
    """Timestamp of the first log line containing text, to the second"""
    line = next(l for l in result.stdout.splitlines() if text in l)
    return datetime.strptime(re.match(r"\[(.*?)\]", line).group(1), "%Y-%m-%d %H:%M:%S")


class MultiCharacter(GowTestCase):
    def build(self, account):
        def build(_script=None):
            mock = self.make_mock({EMAIL: account})
            return mock, users_text([EMAIL]), ["1", "1", "--stagger", "0"], {}
        return build

    def run_variants(self, variants):
        """Run {name: Account} side by side, returning {name: (mock, result)}"""
        prepared = {name: self.build(account)() for name, account in variants.items()}

        def call(name):
            mock, users, args, _ = prepared[name]
            return self.run_gow(SCRIPT, mock, users, *args)

        results = self.run_all([lambda n=name: call(n) for name in variants])
        return {name: (prepared[name][0], result) for name, result in zip(variants, results)}

    def assert_all_claimed(self, mock, result, characters):
        count = len(characters)
        self.assertEqual(sorted(mock.ledger_pairs()), [(EMAIL, c) for c in sorted(characters)], result.output[-2500:])
        self.assertEqual(mock.duplicate_claims, [])
        self.assertIn(f"Claimed {count}/{count} characters", result.stdout)
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 75 * count)
        self.assertEqual(result.returncode, 0)

    def test_three_characters(self):
        (mock, result), = self.run_variants({"plain": Account(characters=THREE)}).values()
        self.assert_all_claimed(mock, result, THREE)
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 225)
        self.assertEqual(mock.image_hits + mock.analytics_hits + mock.font_hits, 0)
        self.assertEqual(result.stderr, "")

    def test_slow_select_profile_and_late_avatar(self):
        runs = self.run_variants({
            "select 2s": Account(characters=THREE, select_ms=2000),
            "avatar 3s": Account(characters=THREE, avatar_ms=3000),
            "both": Account(characters=THREE, select_ms=2000, avatar_ms=3000),
        })
        for name, (mock, result) in runs.items():
            with self.subTest(name):
                self.assert_all_claimed(mock, result, THREE)
                self.assertEqual(mock.cancelled_selects, 0)

    def test_character_count_with_cookie_dialog_in_any_position(self):
        runs = self.run_variants({
            "alone": Account(characters=("c1", "c2"), cookie="none"),
            "cookie first": Account(characters=("c1", "c2"), cookie="before"),
            "cookie last": Account(characters=("c1", "c2"), cookie="after"),
            "cookie first, slow shell": Account(characters=("c1", "c2"), cookie="before", shell_ms=1000),
        })
        for name, (mock, result) in runs.items():
            with self.subTest(name):
                self.assertIn("Found 2 characters!", result.stdout)
                self.assertEqual(result.stdout.count("Found 2 character buttons in dialog"), 2)
                self.assert_all_claimed(mock, result, ("c1", "c2"))

    def test_dialog_shell_rendering_before_its_buttons(self):
        (mock, result), = self.run_variants({"shell": Account(characters=THREE, shell_ms=1000)}).values()
        self.assertIn("Found 3 characters!", result.stdout)
        self.assert_all_claimed(mock, result, THREE)

    def test_reopened_dialog_shell_rendering_before_its_buttons(self):
        """The criterion's shell, on the dialog the script reopens for each later character"""
        (mock, result), = self.run_variants({"reopen": Account(characters=THREE, reopen_shell_ms=1000)}).values()
        self.assert_all_claimed(mock, result, THREE)

    def test_reopened_dialog_rendering_cards_one_at_a_time(self):
        (mock, result), = self.run_variants({"gap": Account(characters=THREE, reopen_card_gap_ms=1000)}).values()
        self.assert_all_claimed(mock, result, THREE)

    def test_reopened_dialog_whose_buttons_never_render(self):
        (mock, result), = self.run_variants({"never": Account(characters=THREE, reopen_shell_ms=-1)}).values()
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "c1")], result.output[-2500:])
        for number in (2, 3):
            with self.subTest(character=number):
                self.assertIn(f"Could not find button for character {number}", result.stdout)
                waited = (stamp(result, f"Could not find button for character {number}")
                          - stamp(result, f"Processing Character {number}/3")).total_seconds()
                self.assertLessEqual(waited, 6.5)
        self.assertIn("Claimed 1/3 characters", result.stdout)
        self.assertIn("MULTI-CHARACTER BATCH SUMMARY", result.stdout)
        self.assertIn("✅ 1/3 chars", result.stdout)

    def spied(self, account, **harness):
        mock = self.make_mock({EMAIL: account})
        result = self.run_gow(SCRIPT, mock, users_text([EMAIL]), "1", "1", "--stagger", "0", harness=harness)
        return mock, result

    def test_login_dialog_cards_one_second_apart(self):
        mock, result = self.spied(Account(characters=THREE, card_gap_ms=1000))
        self.assertIn("Found 3 character(s) in dialog", result.stdout)
        self.assert_all_claimed(mock, result, THREE)
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 225)
        self.assertEqual(len(result.spy["stable_calls"]), 1)
        self.assertTrue(result.spy["stable_calls"][0][1])

    def test_control_with_no_stability_window_undercounts(self):
        mock, result = self.spied(Account(characters=THREE, card_gap_ms=1000), card_count_stable_ms=0)
        self.assertIn("Claimed 1/1 characters", result.stdout, result.output[-1500:])
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "c1")])

    def test_twelve_slow_cards_warn_and_carry_on(self):
        names = tuple(f"c{i}" for i in range(1, 13))
        mock, result = self.spied(Account(characters=names, card_gap_ms=1000))
        self.assertRegex(result.stdout, rf"\[WARNING\] \[{EMAIL}\] Character count still changing, using the latest")
        (seconds, settled), = result.spy["stable_calls"]
        self.assertFalse(settled)
        self.assertLessEqual(seconds, 9)
        self.assertGreaterEqual(seconds, 7.9)
        self.assertIn("MULTI-CHARACTER BATCH SUMMARY", result.stdout)
        found = int(re.search(r"Found (\d+) character\(s\) in dialog", result.stdout).group(1))
        self.assertEqual(len(mock.ledger_pairs()), found)
        self.assertEqual(result.returncode, 0)

    def test_no_dialog_never_waits_for_a_stable_count(self):
        mock, result = self.spied(Account())
        self.assertEqual(result.spy["stable_calls"], [])
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])

    def test_one_card_dialog_waits_only_the_window(self):
        mock, result = self.spied(Account(characters=("c1",)))
        (seconds, settled), = result.spy["stable_calls"]
        self.assertTrue(settled)
        self.assertLessEqual(seconds, 2.0)
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "c1")])
        self.assertIn("✅ 1/1 chars", result.stdout)

    def test_dialog_whose_buttons_never_render_skips_the_wait(self):
        # Nothing to choose means no sign-in, so the session is set directly: this
        # test is about the path through the dialog, not about the claim
        mock, result = self.spied(Account(characters=THREE, shell_ms=-1, session_body='{"user":{}}'))
        self.assertEqual(result.spy["stable_calls"], [])
        self.assertIn("No character selection found - single character account", result.stdout)
        self.assertNotIn("Character count still changing", result.stdout)
        self.assertIn("✅ 1/1 chars", result.stdout, result.output[-1500:])
        self.assertEqual(result.returncode, 0)

    def test_late_dialogs_are_still_multi_character(self):
        runs = self.run_variants({
            "dialog 5s after login": Account(characters=("c1", "c2"), dialog_ms=5000),
            "login 8s then dialog 1s": Account(characters=("c1", "c2"), login_delay_ms=8000, dialog_ms=1000),
        })
        for name, (mock, result) in runs.items():
            with self.subTest(name):
                self.assertIn("Found 2 characters!", result.stdout)
                self.assert_all_claimed(mock, result, ("c1", "c2"))

    def test_single_character_account_claims_once(self):
        (mock, result), = self.run_variants({"single": Account()}).values()
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])
        self.assertIn("✅ 1/1 chars", result.stdout)
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 75)

    def test_selected_card_with_no_post_proceeds_after_a_warning(self):
        (mock, result), = self.run_variants({
            "no post": Account(characters=("c1", "c2"), selected_no_post=True),
        }).values()
        self.assertIn(f"[{EMAIL}] select-profile not observed, continuing", result.stdout)
        self.assert_all_claimed(mock, result, ("c1", "c2"))

        waited = (stamp(result, "select-profile not observed")
                  - stamp(result, "Processing Character 1/2")).total_seconds()
        self.assertGreaterEqual(waited, 2)
        self.assertLessEqual(waited, 5)


SINGLE_LINE = "No character selection found - single character account"


class LoginRace(GowTestCase):
    """LR1-LR3, LR5, C2, C3: login() waits for the dialog or the sign-in, not a fixed floor"""

    # Borrowed, not inherited, so MultiCharacter's tests don't run twice
    build = MultiCharacter.build
    run_variants = MultiCharacter.run_variants
    spied = MultiCharacter.spied

    def test_lr1_slow_dialogs_claim_every_character(self):
        runs = self.run_variants({
            "dialog 10s": Account(characters=("c1", "c2"), dialog_ms=10000),
            "dialog 20s": Account(characters=("c1", "c2"), dialog_ms=20000),
        })
        for name, (mock, result) in runs.items():
            with self.subTest(name):
                self.assertIn("Found 2 characters!", result.stdout, result.output[-2500:])
                self.assertIn("Character dialog appeared", result.stdout)
                self.assertNotIn(SINGLE_LINE, result.stdout)
                self.assertEqual(sorted(mock.ledger_pairs()), [(EMAIL, "c1"), (EMAIL, "c2")])
                self.assertIn("Claimed 2/2 characters", result.stdout)
                self.assertEqual(mock.logouts, 0)
                self.assertEqual(result.returncode, 0)

    def test_lr1_the_logged_wait_is_the_dialog_delay(self):
        """The new INFO line carries the real wait, which the user's run reads"""
        (mock, result), = self.run_variants({"dialog 10s": Account(characters=("c1", "c2"), dialog_ms=10000)}).values()
        seconds = float(re.search(r"Character dialog appeared (\d+\.\d)s after the Login click", result.stdout).group(1))
        self.assertGreaterEqual(seconds, 10)
        self.assertLess(seconds, 14)
        gap = (stamp(result, "Character dialog appeared") - stamp(result, "Logging in...")).total_seconds()
        self.assertGreaterEqual(gap, 10)
        self.assertLessEqual(gap, 14)

    def test_lr2_the_real_runs_shape(self):
        multi = [f"multi{n}@x.test" for n in range(1, 4)]
        single = [f"single{n}@x.test" for n in range(1, 4)]
        accounts = {email: Account(characters=("c1", "c2"), dialog_ms=10000) for email in multi}
        accounts.update({email: Account() for email in single})
        emails = [multi[0], single[0], multi[1], single[1], multi[2], single[2]]
        mock = self.make_mock(accounts)
        result = self.run_gow(SCRIPT, mock, users_text(emails), "1", "6", "--concurrency", "3", "--stagger", "0")
        self.assertEqual(sorted(result.summary_rows()), sorted((True, email) for email in emails), result.output[-3000:])
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 675)
        self.assertNotIn("❌ FAILED", result.stdout)
        self.assertEqual(result.returncode, 0)
        expected = sorted([(e, c) for e in multi for c in ("c1", "c2")] + [(e, "only") for e in single])
        self.assertEqual(sorted(mock.ledger_pairs()), expected)
        self.assertEqual(mock.duplicate_claims, [])

    def test_lr3_single_character_is_no_slower(self):
        (mock, result), = self.run_variants({"single": Account()}).values()
        gap = (stamp(result, SINGLE_LINE) - stamp(result, "Logging in...")).total_seconds()
        self.assertLessEqual(gap, 5, result.output[-2500:])
        self.assertIn("Signed in with no character dialog", result.stdout)
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])
        self.assertNotRegex(result.stdout, rf"\[WARNING\] \[{EMAIL}\]")

    def test_lr5_a_dialog_after_sign_in_is_caught_by_the_safety_net(self):
        account = Account(characters=("c1", "c2"), sign_in_before_dialog=True, dialog_ms=1500)
        (mock, result), = self.run_variants({"sign-in first": account}).values()
        self.assertIn("Character dialog appeared after sign-in, treating as multi-character", result.stdout,
                      result.output[-2500:])
        self.assertIn("Found 2 characters!", result.stdout)
        self.assertEqual(sorted(mock.ledger_pairs()), [(EMAIL, "c1"), (EMAIL, "c2")])
        self.assertIn("Claimed 2/2 characters", result.stdout)
        self.assertIn("✅ 2/2 chars", result.stdout)
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 150)
        self.assertEqual(result.returncode, 0)

    def test_c2_control_a_race_shorter_than_the_dialog_is_misjudged(self):
        """C2: the LR1 account fails as the real run did once the race gives up first"""
        mock, result = self.spied(Account(characters=("c1", "c2"), dialog_ms=10000), login_outcome_timeout_seconds=6)
        self.assertIn(SINGLE_LINE, result.stdout, result.output[-2500:])
        self.assertGreaterEqual(mock.logouts, 1)
        self.assertEqual(mock.claim_posts, 0)
        self.assertIn("❌ FAILED", result.stdout)
        self.assertEqual(result.returncode, 1)

    def test_c3_control_no_grace_misses_a_dialog_after_sign_in(self):
        """C3: without the safety net the LR5 account is taken for a single character.

        The early sign-in gives a real session whose selected character is c1, so the
        single character path claims only c1 and reports success: 1/1, never 2/2.
        That silent undercount is the opposite of LR5 on every count.
        """
        mock, result = self.spied(Account(characters=("c1", "c2"), sign_in_before_dialog=True, dialog_ms=1500),
                                  login_dialog_grace_ms=0)
        self.assertIn(SINGLE_LINE, result.stdout, result.output[-2500:])
        self.assertNotIn("treating as multi-character", result.stdout)
        self.assertNotIn("Found 2 characters!", result.stdout)
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "c1")])
        self.assertIn("Character 1/1: ✅ Claimed 75 jewels!", result.stdout)
        self.assertIn("Total Characters Found: 1", result.stdout)
        self.assertIn("✅ 1/1 chars", result.stdout)
        self.assertNotIn("2/2", result.stdout)
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 75)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
