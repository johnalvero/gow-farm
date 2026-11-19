"""T round, end to end. T1.7, T2.11 and T2.13 are controls: each shows the fix is what
changed the result, so the criteria they guard don't count unless they behave."""

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

SCRIPT = "claim_all_characters.py"
EMAIL = "tround@x.test"
NORMAL = "normal@x.test"
FROZEN = "frozen@x.test"
TWO = ("c1", "c2")
THREE = ("c1", "c2", "c3")
DEADLINE_LINE = "Account deadline of {}s passed, stopping this account"


def stamp(result, text):
    line = next(l for l in result.stdout.splitlines() if text in l)
    return datetime.strptime(re.match(r"\[(.*?)\]", line).group(1), "%Y-%m-%d %H:%M:%S")


class TRound(GowTestCase):
    def run_one(self, account, script=SCRIPT, harness=None, **kwargs):
        mock = self.make_mock({EMAIL: account}, hydrate_ms=100)
        result = self.run_gow(script, mock, users_text([EMAIL]), *range_args(script, 1), "--stagger", "0",
                              harness=harness, **kwargs)
        return mock, result

    def assert_t1_1(self, mock, result):
        self.assertIn("Character 2/2: ✗ Character switch failed, skipping claim", result.stdout, result.output[-2000:])
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "c1")])
        self.assertEqual(mock.claim_posts, 1)
        self.assertIn("✅ 1/2 chars", result.stdout)
        self.assertIn("Claimed 1/2 characters", result.stdout)
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 75)
        self.assertEqual(result.summary_value("Characters Already Claimed"), 0)
        self.assertEqual(result.summary_value("Accounts Success"), 1)
        self.assertEqual(result.summary_value("Accounts Failed"), 0)
        self.assertEqual(result.returncode, 0)


class FailedSwitch(TRound):
    def test_t1_1_switch_refused(self):
        mock, result = self.run_one(Account(characters=TWO, select_failures=("c2",)))
        self.assert_t1_1(mock, result)
        self.assertIn("select-profile failed: HTTP 500", result.stdout)

    def test_t1_2_switch_never_answers(self):
        account = Account(characters=TWO, select_failures=("c2",), select_failure_status=0)
        mock, result = self.run_one(account, harness={"select_profile_timeout_ms": 1500})
        self.assert_t1_1(mock, result)
        self.assertIn("select-profile not observed", result.stdout)

    def test_t1_3_third_character_still_runs(self):
        for keeps_dialog in (False, True):
            with self.subTest(keeps_dialog=keeps_dialog):
                account = Account(characters=THREE, select_failures=("c2",), select_failure_keeps_dialog=keeps_dialog)
                mock, result = self.run_one(account)
                self.assertEqual(sorted(mock.ledger_pairs()), [(EMAIL, "c1"), (EMAIL, "c3")], result.output[-2500:])
                self.assertIn("✅ 2/3 chars", result.stdout)
                self.assertEqual(result.summary_value("Total Jewels Claimed"), 150)
                self.assertEqual(result.returncode, 0)
                if keeps_dialog:
                    waited = (stamp(result, "Processing Character 3/3")
                              - stamp(result, "Character switch failed")).total_seconds()
                    self.assertLessEqual(waited, 2)
                    self.assertNotIn("Could not find avatar button", result.stdout)

    def test_switch_that_never_answers_with_a_character_after_it(self):
        """The pending POST keeps the dialog up, so the next card has to work from there"""
        account = Account(characters=THREE, select_failures=("c2",), select_failure_status=0)
        mock, result = self.run_one(account, harness={"select_profile_timeout_ms": 1500})
        self.assertEqual(sorted(mock.ledger_pairs()), [(EMAIL, "c1"), (EMAIL, "c3")], result.output[-2500:])

    def test_t1_4_every_character_fails(self):
        account = Account(characters=TWO, collect_fail_characters=("c1",), select_failures=("c2",))
        mock, result = self.run_one(account)
        self.assertEqual(result.summary_rows(), [(False, EMAIL)], result.output[-2000:])
        self.assertEqual(result.summary_value("Accounts Failed"), 1)
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 0)
        self.assertEqual(mock.ledger_pairs(), [])
        self.assertEqual(result.returncode, 1)

    def test_t1_5_selected_card_with_no_post(self):
        mock, result = self.run_one(Account(characters=TWO, selected_no_post=True))
        self.assertEqual(sorted(mock.ledger_pairs()), [(EMAIL, "c1"), (EMAIL, "c2")], result.output[-2000:])
        self.assertNotIn("Character switch failed", result.stdout)

    def test_t1_5_selected_card_with_a_500(self):
        """A failed POST means no sign-in on the mock, so the session is set directly"""
        account = Account(characters=TWO, select_failures=("c1",), session_body='{"user":{}}')
        mock, result = self.run_one(account)
        self.assertIn("select-profile failed: HTTP 500", result.stdout)
        self.assertNotIn("Character 1/2: ✗ Character switch failed", result.stdout)
        self.assertIn((EMAIL, "c1"), mock.ledger_pairs(), result.output[-2000:])

    def test_failed_switch_on_the_login_page_is_honest(self):
        """No header to reopen from, so the rest can't run: it must fail, and must end"""
        account = Account(characters=TWO, selected_character="c2", select_failures=("c1",))
        mock, result = self.run_one(account)
        self.assertIn("Character 1/2: ✗ Character switch failed", result.stdout, result.output[-2000:])
        self.assertEqual(mock.ledger_pairs(), [])
        self.assertEqual(result.summary_rows(), [(False, EMAIL)])
        self.assertEqual(result.returncode, 1)

    def test_t1_7_control_with_select_character_forced_true(self):
        mock, result = self.run_one(Account(characters=TWO, select_failures=("c2",)),
                                    harness={"select_character_true": True})
        self.assertIn("✅ 2/2 chars", result.stdout, result.output[-2000:])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "c1")])
        with self.assertRaises(AssertionError):
            self.assert_t1_1(mock, result)


class Frozen(GowTestCase):
    def beside_normal(self, script, frozen_account, deadline=20, harness=None, **options):
        accounts = {FROZEN: frozen_account, NORMAL: Account()}
        mock = self.make_mock(accounts, hydrate_ms=100)
        config = {"account_deadline_seconds": deadline, **(harness or {})}
        result = self.run_gow(script, mock, users_text([FROZEN, NORMAL]), *range_args(script, 2),
                              "--concurrency", "2", "--stagger", "0", harness=config,
                              timeout=deadline + 15 + 8, **options)
        return mock, result

    def assert_frozen_outcome(self, script, mock, result, deadline=20):
        self.assertEqual(result.summary_rows(), [(False, FROZEN), (True, NORMAL)], result.output[-2000:])
        self.assertEqual(mock.ledger_pairs(), [(NORMAL, "only")])
        self.assertRegex(result.stdout, rf"\[ERROR\] \[{FROZEN}\] " + DEADLINE_LINE.format(deadline))
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 75)
        if script == SCRIPT:
            self.assertEqual(result.summary_value("Accounts Failed"), 1)
        else:
            self.assertRegex(result.stdout, r"Total: 2 \| Success: 1 \| Failed: 1\n")
        self.assertEqual(result.returncode, 1)
        self.assertLessEqual(result.seconds, deadline + 15)

    def test_t2_2_dialog_freeze(self):
        mock, result = self.beside_normal(SCRIPT, Account(characters=TWO, freeze_on="dialog"))
        self.assert_frozen_outcome(SCRIPT, mock, result)

    def test_t2_12_shape_freeze_inside_the_second_dialog_lookup(self):
        """Lands exactly in the guarded inner_html, where a bare except would swallow the cancel"""
        mock, result = self.beside_normal(SCRIPT, Account(characters=TWO),
                                          harness={"freeze_at_inner_html": {FROZEN: 2}})
        self.assert_frozen_outcome(SCRIPT, mock, result)

    def test_t2_3_vault_freeze_claimable(self):
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock({FROZEN: Account(freeze_on="vault"), NORMAL: Account()}, hydrate_ms=100),
                users_text([FROZEN, NORMAL]), [*range_args(s, 2), "--concurrency", "2", "--stagger", "0"],
                {"harness": {"account_deadline_seconds": 20}, "timeout": 43})):
            with self.subTest(script):
                self.assert_frozen_outcome(script, mock, result)

    def test_t2_3_vault_freeze_on_cooldown(self):
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock({FROZEN: Account(vault="cooldown", freeze_on="vault", freeze_delay_ms=0),
                                NORMAL: Account()}, hydrate_ms=100),
                users_text([FROZEN, NORMAL]), [*range_args(s, 2), "--concurrency", "2", "--stagger", "0"],
                {"harness": {"account_deadline_seconds": 20}, "timeout": 43})):
            with self.subTest(script):
                self.assert_frozen_outcome(script, mock, result)

    def test_t2_4_twelve_accounts_freezing_at_staggered_delays(self):
        delays = list(range(10, 130, 10))
        names = [f"fz{d}@x.test" for d in delays]
        accounts = {n: Account(vault="cooldown", button_ms=0, freeze_on="vault", freeze_delay_ms=d)
                    for n, d in zip(names, delays)}
        mock = self.make_mock(accounts, hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text(names), "1", "12", "--concurrency", "4",
                              "--stagger", "0", harness={"account_deadline_seconds": 15}, timeout=150)
        rows = result.summary_rows()
        self.assertEqual([email for _, email in rows], names, result.output[-2000:])
        timed_out = {l.split("[")[3].split("]")[0] for l in result.stdout.splitlines() if "Account deadline of" in l}
        self.assertEqual({e for ok, e in rows if not ok}, timed_out)
        self.assertRegex(result.stdout, rf"Total: 12 \| Success: {12 - len(timed_out)} \| Failed: {len(timed_out)}\n")
        self.assertEqual(result.summary_value("Characters Already Claimed"), 12 - len(timed_out))
        self.assertEqual(result.returncode, 1 if timed_out else 0)

    def test_t2_5_dialog_freeze_in_the_legacy_scripts(self):
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock({FROZEN: Account(characters=TWO, freeze_on="dialog", freeze_delay_ms=200)}, hydrate_ms=100),
                users_text([FROZEN]), [*range_args(s, 1), "--stagger", "0"],
                {"harness": {"account_deadline_seconds": 20}, "timeout": 100}),
                ("claim_all.py", "claim_batch.py")):
            with self.subTest(script):
                self.assertIn("SUMMARY", result.stdout, result.output[-1500:])
                self.assertEqual(result.summary_rows(), [(False, FROZEN)])
                self.assertEqual(result.returncode, 1)

    def test_t2_9_a_frozen_account_releases_its_slot(self):
        accounts = {FROZEN: Account(freeze_on="vault"), NORMAL: Account()}
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock(accounts, hydrate_ms=100), users_text([FROZEN, NORMAL]),
                [*range_args(s, 2), "--concurrency", "1", "--stagger", "0"],
                {"harness": {"account_deadline_seconds": 15}, "timeout": 60})):
            with self.subTest(script):
                self.assertEqual(result.summary_rows(), [(False, FROZEN), (True, NORMAL)], result.output[-1500:])
                self.assertEqual(mock.ledger_pairs(), [(NORMAL, "only")])

    def test_t2_16_every_line_is_tagged(self):
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock({FROZEN: Account(freeze_on="vault"), NORMAL: Account()}, hydrate_ms=100),
                users_text([FROZEN, NORMAL]), [*range_args(s, 2), "--concurrency", "2", "--stagger", "0"],
                {"harness": {"account_deadline_seconds": 20}, "timeout": 60})):
            with self.subTest(script):
                self.assert_tagged(result, [FROZEN, NORMAL], exempt=["Background error"])
                for text in (PASSWORD, "Call log", "never retrieved"):
                    self.assertNotIn(text, result.output)

    def test_t2_16_deadline_during_a_pending_fill_keeps_the_password_out(self):
        """The internal future Ctrl+C left behind (plan finding 10), reached by the deadline"""
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock({EMAIL: Account()}, hydrate_ms=100, pw_attr="disabled"), users_text([EMAIL]),
                [*range_args(s, 1), "--stagger", "0"],
                {"harness": {"account_deadline_seconds": 3}, "timeout": 40, "check_password": False})):
            with self.subTest(script):
                for text in (PASSWORD, "Call log", "never retrieved"):
                    self.assertNotIn(text, result.output)
                self.assertEqual(result.summary_rows(), [(False, EMAIL)], result.output[-1500:])
                self.assert_tagged(result, [EMAIL], exempt=["Background error"])

    def test_t2_7_legacy_worker_that_claims_then_hangs(self):
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock(), users_text([EMAIL]), [*range_args(s, 1), "--stagger", "0"],
                {"harness": {"account_deadline_seconds": 2, "hang_worker_after_claim": [EMAIL]}, "timeout": 30}),
                ("claim_all.py", "claim_batch.py")):
            with self.subTest(script):
                self.assertEqual(result.summary_rows(), [(False, EMAIL)], result.output[-1500:])
                self.assertEqual(result.summary_value("Total Jewels Claimed"), 75)
                self.assertRegex(result.stdout, r"Total: 1 \| Success: 0 \| Failed: 1\n")
                self.assertEqual(result.returncode, 1)

    def test_a_worker_that_swallows_one_cancel_lets_the_run_exit(self):
        """T2.8 end to end. The shutdown's own cancel is the one that ends it"""
        mock = self.make_mock(hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text([EMAIL, NORMAL]), "1", "2", "--concurrency", "2",
                              "--stagger", "0", timeout=40,
                              harness={"account_deadline_seconds": 8, "account_cancel_grace_seconds": 2,
                                       "swallow_cancel": {EMAIL: 1}})
        self.assertIn(f"[{EMAIL}] Account did not stop, abandoning it", result.stdout)
        self.assertEqual(result.summary_rows(), [(False, EMAIL), (True, NORMAL)], result.output[-1500:])
        self.assertEqual(result.returncode, 1)

    def test_a_claim_then_a_freeze_in_the_after_claim_screenshot_stays_counted(self):
        """account['outcome'] on the real path: claim_all.py is past the claim when it stops"""
        mock = self.make_mock({EMAIL: Account()}, hydrate_ms=100)
        result = self.run_gow("claim_all.py", mock, users_text([EMAIL]), "--stagger", "0", timeout=40,
                              harness={"account_deadline_seconds": 10, "cdp_hang": "send",
                                       "cdp_hang_path": "_after_claim"})
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")], result.output[-1500:])
        self.assertEqual(result.summary_rows(), [(False, EMAIL)])
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 75)
        self.assertEqual(result.returncode, 1)

    def test_a_worker_that_never_stops_still_lets_the_run_exit(self):
        """T2.8 end to end: the abandoned task ignores every cancel, including the shutdown's"""
        mock = self.make_mock(hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text([EMAIL, NORMAL]), "1", "2", "--concurrency", "2",
                              "--stagger", "0", timeout=40,
                              harness={"account_deadline_seconds": 8, "account_cancel_grace_seconds": 2,
                                       "swallow_cancel": {EMAIL: -1}})
        self.assertIn(f"[{EMAIL}] Account did not stop, abandoning it", result.stdout)
        self.assertEqual(result.summary_rows(), [(False, EMAIL), (True, NORMAL)], result.output[-1500:])
        self.assertEqual(result.returncode, 1)

    def test_t2_10_healthy_multi_character_run(self):
        email = "healthy@x.test"
        mock = self.make_mock({email: Account(characters=TWO)}, hydrate_ms=100)
        result = self.run_gow(SCRIPT, mock, users_text([email]), "1", "1", "--stagger", "0")
        self.assertIn("✅ 2/2 chars", result.stdout, result.output[-1500:])
        self.assertNotIn("Account deadline", result.stdout)
        self.assertNotIn("abandoning", result.stdout)
        self.assertEqual(result.returncode, 0)


class FinishedCharacters(TRound):
    def assert_t2_6(self, mock, result, ledger, jewels, already):
        self.assertEqual(mock.ledger_pairs(), ledger, result.output[-2500:])
        self.assertEqual(result.summary_rows(), [(False, EMAIL)])
        self.assertEqual(result.summary_value("Total Jewels Claimed"), jewels)
        self.assertEqual(result.summary_value("Characters Already Claimed"), already)
        self.assertEqual(result.summary_value("Total Characters Found"), 3)
        self.assertEqual(result.summary_value("Accounts Failed"), 1)
        self.assertEqual(result.returncode, 1)

    def variant(self, freeze_on, *cooldown):
        return Account(characters=THREE, freeze_on=freeze_on, freeze_on_character="c2", cooldown_characters=cooldown)

    def test_t2_6_claim_then_freeze(self):
        results = self.run_all([
            lambda f=freeze_on: self.run_one(self.variant(f), harness={"account_deadline_seconds": 25}, timeout=75)
            for freeze_on in ("vault", "reopen")])
        for freeze_on, (mock, result) in zip(("vault", "reopen"), results):
            with self.subTest(freeze_on):
                self.assert_t2_6(mock, result, [(EMAIL, "c1")], 75, 0)

    def test_t2_6_cooldown_then_freeze(self):
        results = self.run_all([
            lambda f=freeze_on: self.run_one(self.variant(f, "c1"), harness={"account_deadline_seconds": 25}, timeout=75)
            for freeze_on in ("vault", "reopen")])
        for freeze_on, (mock, result) in zip(("vault", "reopen"), results):
            with self.subTest(freeze_on):
                self.assert_t2_6(mock, result, [], 0, 1)

    def test_a_timed_out_row_is_failed_even_with_finished_characters(self):
        mock, result = self.run_one(self.variant("vault"), harness={"account_deadline_seconds": 25}, timeout=75)
        self.assertNotIn("1/3 chars", result.stdout)
        self.assertIn(f"[{EMAIL}] " + DEADLINE_LINE.format(25), result.stdout)

    def test_t2_13_control_with_zeros_from_failed_account(self):
        mock, result = self.run_one(self.variant("vault"), timeout=75,
                                    harness={"account_deadline_seconds": 25, "failed_account_zeros": True})
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 0, result.output[-2000:])
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "c1")])
        with self.assertRaises(AssertionError):
            self.assert_t2_6(mock, result, [(EMAIL, "c1")], 75, 0)


class Control(GowTestCase):
    def test_t2_11_with_the_deadline_disabled_the_run_never_ends(self):
        temp_dir = tempfile.mkdtemp(prefix="gow-tests-")
        self.addCleanup(shutil.rmtree, temp_dir, True)
        mock = self.make_mock({FROZEN: Account(characters=TWO, freeze_on="dialog"), NORMAL: Account()}, hydrate_ms=100)
        harness = {"account_deadline_seconds": None}
        process = start_script(SCRIPT, mock, users_text([FROZEN, NORMAL]),
                               ("1", "2", "--concurrency", "2", "--stagger", "0"), harness, temp_dir, new_session=True)
        wait_until(lambda: mock.login_hits, 30, "the login page")
        time.sleep(1)
        children = descendants(process.pid)
        self.addCleanup(kill_all, children)
        started = time.monotonic()
        alive = False
        try:
            stdout, stderr = process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            alive = True
            process.kill()
            kill_all(children)
            stdout, stderr = process.communicate()
        result = finish(process, stdout, stderr, time.monotonic() - started, temp_dir, harness)
        self.assertTrue(alive, result.output[-1500:])
        self.assertNotIn("SUMMARY", result.stdout)
        self.assertEqual(mock.ledger_pairs(), [(NORMAL, "only")])


class Interrupted(TRound):
    def assert_interrupted(self, result, limit=40):
        self.assertEqual(result.returncode, 1)
        self.assertIn("Interrupted by user", result.stdout, result.output[-1500:])
        self.assertNotIn("SUMMARY", result.stdout)
        for text in (PASSWORD, "Call log", "never retrieved"):
            self.assertNotIn(text, result.output)
        self.assertLessEqual(result.seconds, limit)

    def run_frozen(self, script, **kwargs):
        mock = self.make_mock({EMAIL: Account(freeze_on="vault")}, hydrate_ms=100)
        return self.interrupted_run(script, mock, users_text([EMAIL]), (*range_args(script, 1), "--stagger", "0"),
                                    trigger="Navigating to vault", limit=40, **kwargs)

    def test_t2_15_ctrl_c_to_the_process_group(self):
        for script in ALL_SCRIPTS:
            with self.subTest(script):
                result, children = self.run_frozen(script, group=True)
                self.assert_interrupted(result)
                wait_until(lambda: not still_running(children), 10, "the driver and browser to exit")

    def test_t2_15_ctrl_c_to_python_only(self):
        for script in ALL_SCRIPTS:
            with self.subTest(script):
                result, children = self.run_frozen(script)
                self.assert_interrupted(result)
                wait_until(lambda: not still_running(children), 10, "the driver and browser to exit")

    def test_ctrl_c_with_a_worker_that_will_not_stop_waits_only_the_grace(self):
        temp_dir = tempfile.mkdtemp(prefix="gow-tests-")
        self.addCleanup(shutil.rmtree, temp_dir, True)
        marker = os.path.join(temp_dir, "worker-started")
        mock = self.make_mock(hydrate_ms=100)
        result, _ = self.interrupted_run(
            "claim_batch.py", mock, users_text([EMAIL]), ("1", "1", "--stagger", "0"), marker=marker,
            harness={"account_cancel_grace_seconds": 2, "swallow_cancel": {EMAIL: 1}, "worker_marker": marker},
            limit=30)
        self.assert_interrupted(result, limit=30)


if __name__ == "__main__":
    unittest.main()
