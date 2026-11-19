"""S round, end to end. S1.6 and S3.9 are controls: with a wait turned into a no-op
the run must show a logout and no ledger entry, or the mock isn't detecting the wait."""

import os
import re
import unittest
from datetime import datetime

from gow_support import (
    ALL_SCRIPTS,
    PASSWORD,
    SCREENSHOT_DIR,
    SUCCESS_MESSAGE,
    Account,
    GowTestCase,
    range_args,
    users_text,
)
from playwright.async_api import async_playwright

import claim_common

EMAIL = "sround@x.test"
TWO = ("c1", "c2")
TOKEN = "Zx9Qw7Er5Ty3Ui1Op0As2Df4Gh6Jk8Lm0Nb3Vc5X"
NO_SIGNAL_WARNING = ("No character dialog and no sign-in 3s after the login response, "
                     "continuing as a single character account")
# The first line each script logs once login() has returned
NEXT_STAGE = {
    "claim_all_characters.py": "No character selection found - single character account",
    "claim_all.py": "After login URL",
    "claim_batch.py": "Navigating to vault...",
}


def stamp(result, text):
    """Timestamp of the first log line containing text, to the second"""
    line = next(l for l in result.stdout.splitlines() if text in l)
    return datetime.strptime(re.match(r"\[(.*?)\]", line).group(1), "%Y-%m-%d %H:%M:%S")


def network_lines(result):
    return [l.split(" Network: ", 1)[1] for l in result.stdout.splitlines() if " Network: " in l]


def console_lines(result):
    return [l.split(" Console: ", 1)[1] for l in result.stdout.splitlines() if " Console: " in l]


def screenshot_of(email):
    safe = email.replace("@", "_at_").replace(".", "_")
    return os.path.join(SCREENSHOT_DIR, f"{safe}_claim_failed.png")


class SRound(GowTestCase):
    def run_account(self, account, harness=None, scripts=ALL_SCRIPTS, extra=(), mock_options=None, **kwargs):
        def build(script):
            mock = self.make_mock({EMAIL: account}, hydrate_ms=100, **(mock_options or {}))
            options = dict(kwargs)
            if harness is not None:
                options["harness"] = harness
            return mock, users_text([EMAIL]), [*range_args(script, 1), "--stagger", "0", *extra], options
        return self.for_each_script(build, scripts)

    def assert_logged_out(self, mock, result):
        """The real site's failure: a logout, no claim, and an honest result"""
        self.assertGreaterEqual(mock.logouts, 1, result.output[-2000:])
        self.assertEqual(mock.ledger_pairs(), [])
        self.assertEqual(mock.claim_posts, 0)
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 0)
        self.assertEqual(result.returncode, 1)
        self.assertIn("❌ FAILED", result.stdout)


class SignInWait(SRound):
    def test_s1_1_login_path_callback_8s(self):
        """No dialog and an 8s callback: only the login race and the sign-in wait can hold the script here"""
        for script, mock, result in self.run_account(Account(callback_ms=8000)):
            with self.subTest(script):
                self.assertLess(mock.callback_responses[0][0], mock.vault_documents[0][0])
                self.assertEqual(mock.cancelled_callbacks, 0)
                self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")], result.output[-1500:])
                self.assertEqual(result.returncode, 0)

    def test_s1_2_two_characters_callback_3s(self):
        mock = self.make_mock({EMAIL: Account(characters=TWO, callback_ms=3000)}, hydrate_ms=100)
        result = self.run_gow("claim_all_characters.py", mock, users_text([EMAIL]), "1", "1", "--stagger", "0")
        self.assertEqual(sorted(mock.ledger_pairs()), [(EMAIL, "c1"), (EMAIL, "c2")], result.output[-2500:])
        self.assertEqual(len(mock.callback_responses), 2)
        self.assertEqual(len(mock.vault_documents), 2)
        for callback, document in zip(mock.callback_responses, mock.vault_documents):
            self.assertLess(callback[0], document[0])
        self.assertEqual([c for _, _, c in mock.callback_responses], list(TWO))
        self.assertEqual(mock.cancelled_callbacks, 0)

    def assert_sign_in_timeout(self, mock, result):
        self.assertRegex(result.stdout, rf"\[WARNING\] \[{EMAIL}\] Sign-in did not finish in 2s, continuing")
        (seconds, *_) = result.spy["sign_in_waits"]
        self.assertGreaterEqual(seconds, 2)
        self.assertLessEqual(seconds, 3)
        self.assertEqual(len(mock.vault_documents), 1, result.output[-1500:])

    def test_s1_4_successful_selection_with_no_sign_in_pays_the_start_bound(self):
        """A real page: no sign-in ever starts, and the 3s start wait passes in silence"""
        account = Account(characters=("c1",), no_sign_in=True, session_body='{"user":{}}')
        for script, mock, result in self.run_account(account, {}, scripts=("claim_all_characters.py",)):
            with self.subTest(script):
                self.assertEqual(mock.token_requests + mock.callback_requests, 0)
                self.assertNotIn("Sign-in", result.stdout)
                seconds = result.spy["sign_in_waits"][-1]
                self.assertGreaterEqual(seconds, claim_common.SIGN_IN_START_TIMEOUT_SECONDS - 0.2)
                self.assertLessEqual(seconds, claim_common.SIGN_IN_START_TIMEOUT_SECONDS + 0.5)
                self.assertEqual(mock.ledger_pairs(), [(EMAIL, "c1")], result.output[-1500:])

    def test_s1_3_dialog_timeout_path(self):
        for script, mock, result in self.run_account(Account(callback_ms=-1), {"sign_in_timeout_seconds": 2,
                                                                              "login_outcome_timeout_seconds": 3}):
            with self.subTest(script):
                self.assert_sign_in_timeout(mock, result)

    def test_s1_3_character_selection_path(self):
        for script, mock, result in self.run_account(Account(characters=("c1",), callback_ms=-1),
                                                     {"sign_in_timeout_seconds": 2}, scripts=("claim_all_characters.py",)):
            with self.subTest(script):
                self.assert_sign_in_timeout(mock, result)

    def test_s1_4_no_sign_in_on_a_real_page_warns_of_the_login_timeout(self):
        """Spec change (login race plan, step 8): no dialog and no sign-in now earns the timeout WARNING"""
        account = Account(no_sign_in=True, session_body='{"user":{}}')
        for script, mock, result in self.run_account(account, {"login_outcome_timeout_seconds": 3}):
            with self.subTest(script):
                self.assertEqual(mock.token_requests + mock.callback_requests, 0)
                self.assertRegex(result.stdout, rf"\[WARNING\] \[{EMAIL}\] {NO_SIGNAL_WARNING}")
                self.assertNotIn("Sign-in", result.stdout)
                self.assertEqual(result.returncode, 0, result.output[-1500:])
                self.assertLess(result.spy["sign_in_waits"][0], 0.1)

    def test_a_sign_in_that_starts_after_the_settle_is_still_waited_for(self):
        """S1.7"""
        mock = self.make_mock({EMAIL: Account(characters=("c1",), sign_in_start_ms=1000)}, hydrate_ms=100)
        result = self.run_gow("claim_all_characters.py", mock, users_text([EMAIL]), "1", "1", "--stagger", "0")
        self.assertEqual(mock.cancelled_callbacks, 0, result.output[-2000:])
        self.assertEqual(mock.logouts, 0)
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "c1")])


class Controls(SRound):
    """If these pass only because of the mock's timing, the S1 and S3 results mean nothing"""

    def test_s1_6_login_path_callback_8s(self):
        for script, mock, result in self.run_account(Account(callback_ms=8000), {"no_wait_for_sign_in": True,
                                                                                 "login_outcome_timeout_seconds": 2}):
            with self.subTest(script):
                self.assertGreaterEqual(mock.cancelled_callbacks, 1, result.output[-1500:])
                self.assert_logged_out(mock, result)
                self.assertEqual(result.summary_rows(), [(False, EMAIL)])

    def test_s1_6_dialog_account_callback_3s(self):
        mock = self.make_mock({EMAIL: Account(characters=("c1",), callback_ms=3000)}, hydrate_ms=100)
        result = self.run_gow("claim_all_characters.py", mock, users_text([EMAIL]), "1", "1", "--stagger", "0",
                              harness={"no_wait_for_sign_in": True})
        self.assertGreaterEqual(mock.cancelled_callbacks, 1, result.output[-1500:])
        self.assert_logged_out(mock, result)

    def test_s3_10_control_keeping_stale_requests(self):
        """Only the navigation listener is removed, so the stale request ends the wait"""
        account = Account(vault_abort_times=1, session_start_ms=2000, poll_session_on_login=True)
        for script, mock, result in self.run_account(account, {"keep_stale_session_requests": True}):
            with self.subTest(script):
                self.assertEqual(mock.logouts, 1, result.output[-1500:])
                self.assertEqual(mock.ledger_pairs(), [])
                self.assertEqual(result.returncode, 1)

    def test_s3_9_verbatim(self):
        for script, mock, result in self.run_account(Account(session_ms=3000), {"no_wait_for_session": True}):
            with self.subTest(script):
                self.assert_logged_out(mock, result)
                self.assertGreaterEqual(mock.signouts, 1)
                self.assertIn("click sent no claim request", result.stdout)
                self.assertEqual(result.summary_rows(), [(False, EMAIL)])
                paths = " ".join(network_lines(result))
                self.assertIn("/api/account/logout", paths)
                self.assertIn("/api/auth/signout", paths)


class SessionWait(SRound):
    def test_s3_1_session_3s(self):
        for script, mock, result in self.run_account(Account(session_ms=3000)):
            with self.subTest(script):
                self.assertLess(mock.session_responses[-1][0], mock.collect_requests[0][0])
                self.assertEqual(mock.logouts, 0)
                self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")], result.output[-1500:])
                self.assertIn(SUCCESS_MESSAGE[script], result.stdout)

    def test_s3_2_empty_session_bodies(self):
        for body in ("", "{}", "null", " {} "):
            for script, mock, result in self.run_account(Account(session_body=body)):
                with self.subTest(script, body=body):
                    self.assertRegex(result.stdout, rf"\[WARNING\] \[{EMAIL}\] App session is empty, so the claim will not work")
                    self.assertIn("Found Claim button", result.stdout)
                    self.assertEqual(result.summary_rows(), [(False, EMAIL)], result.output[-1500:])
                    self.assertEqual(result.summary_value("Total Jewels Claimed"), 0)
                    self.assertEqual(mock.ledger_pairs(), [])

    def test_s3_3_session_never_answers(self):
        for script, mock, result in self.run_account(Account(session_ms=-1), {"session_timeout_seconds": 2}):
            with self.subTest(script):
                self.assertRegex(result.stdout, rf"\[WARNING\] \[{EMAIL}\] Session load not confirmed: TimeoutError")
                self.assertIn("Found Claim button", result.stdout)
                self.assertLessEqual(result.spy["session_waits"][0], 3.5)

    def test_s3_6_cooldown_never_waits_for_the_session(self):
        for script, mock, result in self.run_account(Account(vault="cooldown", session_ms=-1), {}):
            with self.subTest(script):
                self.assertEqual(result.spy["session_waits"], [])
                self.assertNotIn("Session load", result.stdout)
                self.assertNotIn("No session request", result.stdout)
                self.assertEqual(result.returncode, 0, result.output[-1500:])

    def test_s3_7_fetch_starting_after_the_button_hydrates(self):
        for script, mock, result in self.run_account(Account(session_start_ms=2000)):
            with self.subTest(script):
                self.assertLess(mock.session_responses[-1][0], mock.collect_requests[0][0], result.output[-1500:])
                self.assertEqual(mock.logouts, 0)
                self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])
                self.assertEqual(result.returncode, 0)

    def test_s3_8_no_session_fetch_at_all(self):
        for script, mock, result in self.run_account(Account(session_start_ms=-1), {}):
            with self.subTest(script):
                self.assertRegex(result.stdout, rf"\[WARNING\] \[{EMAIL}\] No session request seen, continuing")
                self.assertLessEqual(result.spy["session_waits"][0],
                                     claim_common.SESSION_REQUEST_TIMEOUT_SECONDS + 1.5)
                self.assertIn("Found Claim button", result.stdout)

    def test_s3_8_session_request_with_no_response(self):
        for script, mock, result in self.run_account(Account(session_drop=True), {}):
            with self.subTest(script):
                self.assertRegex(result.stdout, rf"\[WARNING\] \[{EMAIL}\] Session load not confirmed")
                self.assertIn("Found Claim button", result.stdout)

    def test_a_stale_request_does_not_hide_a_late_one(self):
        """S3.10"""
        account = Account(vault_abort_times=1, session_start_ms=2000, poll_session_on_login=True)
        for script, mock, result in self.run_account(account):
            with self.subTest(script):
                self.assertEqual(mock.logouts, 0, result.output[-1500:])
                self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])
                own = [t for t, _ in mock.session_responses if t > mock.vault_documents[0][0]]
                self.assertTrue(own)
                self.assertLess(own[0], mock.collect_requests[0][0])

    def test_a_same_path_vault_reload_clears_requests_from_the_page_before(self):
        """The normal multi-character flow. The first vault page keeps fetching through a
        2s abort, then the reload of the same URL fetches late: only a clear can wait for it"""
        account = Account(characters=TWO, session_start_ms=2000, poll_session_on_vault_load=1, vault_abort_at=(2,))
        mock = self.make_mock({EMAIL: account}, hydrate_ms=100)
        result = self.run_gow("claim_all_characters.py", mock, users_text([EMAIL]), "1", "1", "--stagger", "0")
        self.assertEqual(mock.logouts, 0, result.output[-2500:])
        self.assertEqual(sorted(mock.ledger_pairs()), [(EMAIL, "c1"), (EMAIL, "c2")])
        self.assertIn("Vault navigation aborted", result.stdout)

    def test_same_document_replace_state_on_the_same_path_keeps_the_request(self):
        account = Account(replace_state_url="/en-us/catalog/main?tab=1", session_ms=1000)
        for script, mock, result in self.run_account(account, {}):
            with self.subTest(script):
                self.assertNotIn("No session request seen", result.stdout)
                self.assertLess(result.spy["session_waits"][0], 3)
                self.assertLess(mock.session_responses[-1][0], mock.collect_requests[0][0])
                self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")], result.output[-1500:])

    def test_replace_state_to_another_path_is_the_accepted_cost(self):
        """Pins the plan's risk: the request is lost, the 5s and the WARNING are the price"""
        account = Account(replace_state_url="/en-us/catalog/other", session_ms=1000)
        mock = self.make_mock({EMAIL: account}, hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text([EMAIL]), "1", "1", "--stagger", "0", harness={})
        self.assertIn("No session request seen, continuing", result.stdout)
        self.assertGreaterEqual(result.spy["session_waits"][0], 5)
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")], result.output[-1500:])


class AbortedNavigation(SRound):
    def test_s4_1_session_from_the_second_attempt(self):
        account = Account(vault_abort_times=1, session_ms=1000, poll_session_on_login=True)
        for script, mock, result in self.run_account(account):
            with self.subTest(script):
                self.assertEqual(result.stdout.count("Vault navigation aborted, retrying in 2s"), 1)
                document = mock.vault_documents[0][0]
                after_document = [t for t, _ in mock.session_responses if t > document]
                self.assertTrue(after_document, result.output[-1500:])
                self.assertLess(after_document[0], mock.collect_requests[0][0])
                self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])

    def test_s4_2_second_abort_reaches_the_handler(self):
        for script, mock, result in self.run_account(Account(vault_abort_times=2), {}):
            with self.subTest(script):
                self.assertEqual(result.spy["vault_gotos"], 2)
                self.assertEqual(result.summary_rows(), [(False, EMAIL)], result.output[-1500:])
                self.assertEqual(result.returncode, 1)


class NetworkEvidence(SRound):
    def test_s5_1_failed_load_lines_do_not_crowd_out_the_app_error(self):
        noise = tuple(("error", f"Failed to load resource: number {i}") for i in range(6))
        account = Account(collect_status=500, click_console=noise + (("error", "the useful app error"),))
        for script, _, result in self.run_account(account):
            with self.subTest(script):
                self.assertNotIn("Failed to load resource", result.stdout)
                self.assertTrue(any("the useful app error" in l for l in console_lines(result)), result.output[-1500:])

    def test_s5_2_api_responses_are_listed_and_assets_are_not(self):
        account = Account(collect_status=500, click_urls=("/poll", "/f/fake.woff2", "/a/late.png", "/en-us/gated"))
        for script, _, result in self.run_account(account):
            with self.subTest(script):
                lines = network_lines(result)
                self.assertIn("POST /api/appcharge/collect-offer -> HTTP 500", lines, result.output[-1500:])
                self.assertEqual([l for l in lines if "/api/" not in l and "net::" not in l], [])

    def test_s5_3_network_lines_stop_at_the_limit(self):
        for script, _, result in self.run_account(Account(collect_status=500, click_api_calls=20)):
            with self.subTest(script):
                self.assertEqual(len(network_lines(result)), claim_common.CLAIM_NETWORK_LIMIT, result.output[-1500:])
                self.assertEqual(claim_common.CLAIM_NETWORK_LIMIT, 12)

    def test_s5_4_query_strings_and_secret_path_segments_stay_out(self):
        urls = (f"/api/probe?token=SECRETTOKEN&password={PASSWORD}&userEmail=mailbox@x.test",
                f"/api/probe/{PASSWORD}/data")
        for script, _, result in self.run_account(Account(collect_status=500, click_urls=urls), check_password=False):
            with self.subTest(script):
                lines = network_lines(result)
                self.assertTrue(any("/api/probe" in l for l in lines), result.output[-1500:])
                for leaked in ("SECRETTOKEN", PASSWORD, "mailbox@x.test"):
                    self.assertNotIn(leaked, result.output)
                self.assertEqual([l for l in lines if "?" in l], [])

    def test_s5_5_quiet_click_says_so_once(self):
        for script, _, result in self.run_account(Account(claim_no_request=True)):
            with self.subTest(script):
                self.assertEqual(network_lines(result), ["no API calls or failed requests during the click"],
                                 result.output[-1500:])

    def test_s5_6_every_failure_mode_saves_a_screenshot(self):
        modes = {
            "errorbody": Account(collect_body='{"error":"no"}'),
            "status500": Account(collect_status=500),
            "notjson": Account(collect_body="<html></html>"),
            "norequest": Account(claim_no_request=True),
            "noresponse": Account(collect_ms=-1),
            "cooldownonly": Account(cooldown_without_claim=True),
        }
        calls = {}
        for name, account in modes.items():
            email = f"{name}@x.test"
            calls[email] = (self.make_mock({email: account}, hydrate_ms=100), email)
        results = self.run_all([
            lambda m=mock, e=email: self.run_gow("claim_batch.py", m, users_text([e]), "1", "1", "--stagger", "0",
                                                 harness={"claim_response_timeout_ms": 2000})
            for mock, email in calls.values()])
        for email, result in zip(calls, results):
            with self.subTest(email):
                self.assertEqual(result.returncode, 1, result.output[-1000:])
                self.assertGreater(os.path.getsize(screenshot_of(email)), 0)

    def test_s5_6_success_shows_neither_line_and_no_screenshot(self):
        email = "cleanrun@x.test"
        mock = self.make_mock({email: Account(click_console=(("error", "noise"),), click_api_calls=2)}, hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text([email]), "1", "1", "--stagger", "0")
        self.assertEqual(result.returncode, 0, result.output[-1000:])
        self.assertEqual(network_lines(result), [])
        self.assertEqual(console_lines(result), [])
        self.assertFalse(os.path.exists(screenshot_of(email)))

    def test_s5_8_failed_requests_keep_readable_paths(self):
        sku = "/api/appcharge/get-item-by-sku/webportalfreedaily"
        drops = ("/api/appcharge/collect-offer", sku, f"/api/x/{TOKEN}/data")
        account = Account(click_urls=(sku, f"/api/x/{TOKEN}/data"))
        for script, _, result in self.run_account(account, {"claim_response_timeout_ms": 2000},
                                                  mock_options={"drop_paths": drops}):
            with self.subTest(script):
                lines = network_lines(result)
                self.assertTrue(any(l.startswith("POST 127.0.0.1/api/appcharge/collect-offer -> net::") for l in lines), lines)
                self.assertTrue(any(l.startswith(f"GET 127.0.0.1{sku} -> net::") for l in lines), lines)
                self.assertTrue(any(l.startswith("GET 127.0.0.1/api/x/<redacted>/data -> net::") for l in lines), lines)
                self.assertNotIn(TOKEN, result.output)

    ANALYTICS = "/api/tunnel/analytics/post-event"

    def test_s5_9_analytics_take_no_slot(self):
        """Fired first. 11 calls plus collect-offer make exactly the 12 others that fit"""
        account = Account(collect_status=500, click_analytics_calls=8, click_api_calls=11)
        for extra in ((), ("--no-block",)):
            for script, _, result in self.run_account(account, extra=extra):
                with self.subTest(script, extra=extra):
                    lines = network_lines(result)
                    self.assertEqual(len(lines), 12, result.output[-1500:])
                    self.assertEqual([l for l in lines if "analytics" in l], [])
                    self.assertIn("POST /api/appcharge/collect-offer -> HTTP 500", lines)
                    self.assertEqual(len([l for l in lines if l.startswith("GET /api/extra/")]), 11)

    def test_s5_9_more_others_than_slots(self):
        account = Account(collect_status=500, click_analytics_calls=8, click_api_calls=12)
        for script, _, result in self.run_account(account):
            with self.subTest(script):
                lines = network_lines(result)
                self.assertEqual(len(lines), 12, result.output[-1500:])
                self.assertEqual([l for l in lines if "analytics" in l], [])

    def test_s5_9_logout_scenario_with_analytics(self):
        account = Account(session_ms=3000, click_analytics_calls=8)
        for extra in ((), ("--no-block",)):
            for script, mock, result in self.run_account(account, {"no_wait_for_session": True}, extra=extra):
                with self.subTest(script, extra=extra):
                    self.assert_logged_out(mock, result)
                    paths = " ".join(network_lines(result))
                    self.assertIn("POST /api/account/logout -> HTTP 200", paths)
                    self.assertIn("POST /api/auth/signout -> HTTP 200", paths)
                    self.assertNotIn("analytics", paths)

    def test_s5_9_only_analytics_gives_the_quiet_line(self):
        account = Account(claim_no_request=True, click_analytics_calls=8)
        for extra in ((), ("--no-block",)):
            for script, _, result in self.run_account(account, extra=extra):
                with self.subTest(script, extra=extra):
                    self.assertEqual(network_lines(result), ["no API calls or failed requests during the click"],
                                     result.output[-1500:])

    def test_s5_9_failed_analytics_take_no_slot(self):
        account = Account(claim_no_request=True, click_analytics_calls=8)
        for script, _, result in self.run_account(account, extra=("--no-block",),
                                                  mock_options={"drop_paths": (self.ANALYTICS,)}):
            with self.subTest(script):
                self.assertEqual(network_lines(result), ["no API calls or failed requests during the click"],
                                 result.output[-1500:])


class FrozenRenderer(GowTestCase):
    def test_s6_2_frozen_account_beside_a_normal_one(self):
        frozen, normal = "frozen62@x.test", "normal62@x.test"
        accounts = {frozen: Account(claim_no_request=True, freeze_after_click=True), normal: Account()}
        mock = self.make_mock(accounts, hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text([frozen, normal]), "1", "2",
                              "--concurrency", "2", "--stagger", "0", timeout=120)
        self.assertEqual(mock.ledger_pairs(), [(normal, "only")], result.output[-2000:])
        self.assertEqual(result.summary_rows(), [(False, frozen), (True, normal)])
        self.assertLess(result.seconds, 80)


class EmailField(GowTestCase):
    def test_s8_2_hash_in_the_domain(self):
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock(), "user@x.test#hunter:2\n", range_args(s, 1), {})):
            with self.subTest(script):
                self.assertIn("Invalid format on line 1", result.stdout)
                self.assertNotIn("hunter", result.output)
                self.assertNotIn("user@x.test", result.output)
                self.assertEqual(mock.total_requests, 0)


class MockFlow(SRound):
    def test_s9_1_default_flow_runs_on_every_claim(self):
        for script, mock, result in self.run_account(Account()):
            with self.subTest(script):
                self.assertEqual(result.returncode, 0, result.output[-1500:])
                self.assertEqual(mock.token_requests, 1)
                self.assertEqual(mock.callback_requests, 1)
                self.assertGreaterEqual(mock.session_fetches, 1)
                self.assertEqual(mock.logouts, 0)

    def assert_redirected(self, mock, result):
        self.assert_logged_out(mock, result)
        self.assertGreaterEqual(mock.signouts, 1)
        self.assertEqual(len(mock.login_hits), 2, result.output[-1500:])

    def test_s9_2_click_while_the_session_is_loading(self):
        for script, mock, result in self.run_account(Account(session_ms=-1), {"session_timeout_seconds": 1}):
            with self.subTest(script):
                self.assert_redirected(mock, result)

    def test_s9_2_click_with_an_empty_session(self):
        for script, mock, result in self.run_account(Account(no_sign_in=True), {"login_outcome_timeout_seconds": 3}):
            with self.subTest(script):
                self.assert_redirected(mock, result)


class LegacyMultiCharacter(GowTestCase):
    def test_s10_1_dialog_on_login_fails_honestly(self):
        email = "legacymulti@x.test"
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock({email: Account(characters=TWO)}, hydrate_ms=100), users_text([email]),
                [*range_args(s, 1), "--stagger", "0"], {}), ("claim_all.py", "claim_batch.py")):
            with self.subTest(script):
                self.assertEqual(mock.ledger_pairs(), [], result.output[-1500:])
                self.assertEqual(result.summary_value("Total Jewels Claimed"), 0)
                self.assertEqual(result.summary_rows(), [(False, email)])
                self.assertEqual(result.returncode, 1)
                self.assertIn("App session is empty", result.stdout)


class LoginRaceOutcomes(SRound):
    """LR4, LR6, LR7: what the login race says when nothing, or a refusal, follows the login"""

    def test_lr4_no_dialog_and_no_sign_in_warns_and_continues(self):
        account = Account(no_sign_in=True, session_body='{"user":{}}')
        for script, mock, result in self.run_account(account, {"login_outcome_timeout_seconds": 3}):
            with self.subTest(script):
                self.assertIn(NO_SIGNAL_WARNING, result.stdout, result.output[-2000:])
                gap = (stamp(result, NEXT_STAGE[script]) - stamp(result, "Logging in...")).total_seconds()
                self.assertGreaterEqual(gap, 3)
                self.assertLessEqual(gap, 5)
                self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])
                self.assertEqual(result.summary_rows(), [(True, EMAIL)])
                self.assertEqual(result.returncode, 0)
                self.assertNotIn("Sign-in", result.stdout)

    def test_lr6_a_refused_sign_in_is_reported(self):
        for script, mock, result in self.run_account(Account(callback_status=401)):
            with self.subTest(script):
                self.assertRegex(result.stdout, rf"\[WARNING\] \[{EMAIL}\] Sign-in callback returned HTTP 401",
                                 result.output[-2000:])
                self.assertEqual(result.summary_rows(), [(False, EMAIL)])
                if script == "claim_all_characters.py":
                    self.assertIn("❌ FAILED", result.stdout)
                self.assertEqual(mock.ledger_pairs(), [])
                self.assertEqual(result.returncode, 1)

    def test_lr7_an_account_with_no_signal_and_no_session_fails_with_the_reason(self):
        for script, mock, result in self.run_account(Account(no_sign_in=True), {"login_outcome_timeout_seconds": 3},
                                                     scripts=("claim_all_characters.py",)):
            with self.subTest(script):
                empty = "App session is empty, so the claim will not work"
                self.assertIn(NO_SIGNAL_WARNING, result.stdout, result.output[-2000:])
                self.assertIn(empty, result.stdout)
                self.assertLess(result.stdout.index(NO_SIGNAL_WARNING), result.stdout.index(empty))
                self.assertEqual(result.summary_rows(), [(False, EMAIL)])
                self.assertIn("❌ FAILED", result.stdout)
                self.assertEqual(mock.claim_posts, 0)


class RealPageListeners(unittest.IsolatedAsyncioTestCase):
    async def test_s2_4_stop_twice_on_a_real_page(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()

            def handler(request):
                pass

            page.on("request", handler)
            page.remove_listener("request", handler)
            with self.assertRaises(KeyError):       # finding 31, so the next lines mean something
                page.remove_listener("request", handler)

            state = claim_common.track_sign_in(page)
            state["stop"]()
            state["stop"]()
            await browser.close()


if __name__ == "__main__":
    unittest.main()
