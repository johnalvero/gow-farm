"""Plan L1: a reported 75 jewels must mean the server accepted the claim."""

import json
import re
import unittest
from datetime import datetime

from gow_support import (
    ALREADY_CLAIMED_MESSAGE,
    PASSWORD,
    SUCCESS_MESSAGE,
    Account,
    GowTestCase,
    range_args,
    users_text,
)

EMAIL = "verify@x.test"


def stamp(result, text):
    line = next(l for l in result.stdout.splitlines() if text in l)
    return datetime.strptime(re.match(r"\[(.*?)\]", line).group(1), "%Y-%m-%d %H:%M:%S")


def seconds_between(result, first, second):
    return (stamp(result, second) - stamp(result, first)).total_seconds()


class Verified(GowTestCase):
    def run_account(self, account, **kwargs):
        def build(script):
            mock = self.make_mock({EMAIL: account}, hydrate_ms=100)
            return mock, users_text([EMAIL]), [*range_args(script, 1), "--stagger", "0"], kwargs
        return self.for_each_script(build)

    def assert_claimed(self, script, mock, result):
        self.assertIn(SUCCESS_MESSAGE[script], result.stdout, result.output[-1500:])
        self.assertEqual(mock.ledger_pairs(), [(EMAIL, "only")])
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 75)
        self.assertEqual(result.summary_rows(), [(True, EMAIL)])
        self.assertNotIn("Claim failed", result.stdout)
        self.assertEqual(result.returncode, 0)

    def assert_refused(self, mock, result, *expected):
        self.assertEqual(result.summary_rows(), [(False, EMAIL)], result.output[-1500:])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 0)
        self.assertEqual(mock.ledger_pairs(), [])
        self.assertNotIn("75 jewels!", result.stdout)
        self.assertRegex(result.stdout, rf"\[ERROR\] \[{EMAIL}\] Claim failed: ")
        for text in expected:
            self.assertIn(text, result.stdout)


class AcceptedResponses(Verified):
    def test_ok_empty_object(self):
        for script, mock, result in self.run_account(Account(collect_status=200, collect_body="{}")):
            with self.subTest(script):
                self.assert_claimed(script, mock, result)

    def test_no_cooldown_text_is_not_waited_for(self):
        for script, mock, result in self.run_account(Account(confirm_ms=-1)):
            with self.subTest(script):
                self.assert_claimed(script, mock, result)
                self.assertLessEqual(seconds_between(result, "Found Claim button", "75 jewels!"), 4)

    def test_falsy_error_fields_count_as_success(self):
        for body in ('{"error":null,"errorCode":0}', '{"error":"","errorCode":false}', '{"errorCode":""}',
                     '{"error":false}', "null", "[]", '"ok"'):
            for script, mock, result in self.run_account(Account(collect_body=body)):
                with self.subTest(script, body=body):
                    self.assert_claimed(script, mock, result)

    def test_slow_response_past_the_request_timeout(self):
        for script, mock, result in self.run_account(Account(collect_ms=12000)):
            with self.subTest(script):
                self.assert_claimed(script, mock, result)


class RefusedResponses(Verified):
    def test_error_value_in_a_200(self):
        for script, mock, result in self.run_account(Account(collect_body='{"error":"offer unavailable"}')):
            with self.subTest(script):
                self.assert_refused(mock, result, "Claim failed: HTTP 200", "offer unavailable")

    def test_error_code_in_a_200(self):
        body = '{"errorCode":"E1","errorMessage":"nope"}'
        for script, mock, result in self.run_account(Account(collect_body=body)):
            with self.subTest(script):
                self.assert_refused(mock, result, "Claim failed: HTTP 200", "E1", "nope")

    def test_truthy_like_javascript(self):
        """{} and [] are errors, as in JS, though Python would call them falsy"""
        for body in ('{"error":{}}', '{"error":[]}', '{"errorCode":{}}', '{"errorCode":[]}',
                     '{"error":"0"}', '{"errorCode":1}', '{"error":true}'):
            for script, mock, result in self.run_account(Account(collect_body=body)):
                with self.subTest(script, body=body):
                    self.assert_refused(mock, result, "Claim failed: HTTP 200")

    def test_500_with_json_and_with_html(self):
        for body in ('{"error":"server"}', "<html>oops</html>"):
            for script, mock, result in self.run_account(Account(collect_status=500, collect_body=body)):
                with self.subTest(script, body=body):
                    self.assert_refused(mock, result, "HTTP 500")

    def test_ok_with_a_body_that_is_not_json(self):
        for body in ("<html>ok</html>", ""):
            for script, mock, result in self.run_account(Account(collect_body=body)):
                with self.subTest(script, body=body):
                    self.assert_refused(mock, result, "HTTP 200, unreadable response")

    def test_body_that_stalls_past_its_limit_is_unreadable(self):
        """Headers arrive, so the response is seen, but the body never does in time"""
        for script, mock, result in self.run_account(Account(collect_stall_ms=8000)):
            with self.subTest(script):
                self.assertEqual(result.summary_rows(), [(False, EMAIL)], result.output[-1500:])
                self.assertIn("unreadable response", result.stdout)
                self.assertEqual(result.summary_value("Total Jewels Claimed"), 0)
                self.assertLessEqual(seconds_between(result, "Found Claim button", "unreadable response"), 7)

    def test_logged_out_click_is_refused(self):
        """Claim Now! is visible logged out, and the click logs out rather than claiming"""
        for script, mock, result in self.run_account(Account(login_hang=True), timeout=120,
                                                     harness={"login_outcome_timeout_seconds": 3}):
            with self.subTest(script):
                self.assertEqual(result.summary_rows(), [(False, EMAIL)], result.output[-1500:])
                self.assertEqual(mock.logouts, 1)
                self.assertEqual(mock.claim_posts, 0)
                self.assertIn("Claim failed: click sent no claim request", result.stdout)


class NoResponse(Verified):
    def test_click_that_sends_nothing_gives_up_within_about_12s(self):
        for script, mock, result in self.run_account(Account(claim_no_request=True)):
            with self.subTest(script):
                self.assert_refused(mock, result, "click sent no claim request")
                self.assertEqual(mock.claim_posts, 0)
                waited = seconds_between(result, "Found Claim button", "✗ Claim failed")
                self.assertGreaterEqual(waited, 9)
                self.assertLessEqual(waited, 13)

    def test_request_with_no_response_forced_short(self):
        for script, mock, result in self.run_account(Account(collect_ms=-1),
                                                     harness={"claim_response_timeout_ms": 2000}):
            with self.subTest(script):
                self.assert_refused(mock, result, "claim request sent, no response")
                self.assertEqual(mock.claim_posts, 1)

    def test_request_with_no_response_at_the_real_30s(self):
        mock = self.make_mock({EMAIL: Account(collect_ms=-1)}, hydrate_ms=100)
        result = self.run_gow("claim_batch.py", mock, users_text([EMAIL]), "1", "1", "--stagger", "0")
        self.assert_refused(mock, result, "claim request sent, no response in 30s")
        waited = seconds_between(result, "Found Claim button", "no response in 30s")
        self.assertGreaterEqual(waited, 29)
        self.assertLessEqual(waited, 32)

    def test_cooldown_text_alone_is_not_a_claim(self):
        for script, mock, result in self.run_account(Account(cooldown_without_claim=True)):
            with self.subTest(script):
                self.assert_refused(mock, result, "click sent no claim request (cooldown text is visible)")

    def test_no_marker_when_no_cooldown_text_shows(self):
        for script, mock, result in self.run_account(Account(claim_no_request=True)):
            with self.subTest(script):
                self.assertNotIn("cooldown text is visible", result.stdout)


class FailureLine(Verified):
    def test_long_multi_line_message_logs_on_one_line_within_the_limit(self):
        message = ("word " * 60)[:250] + "\nSECONDLINE" + "z" * 40
        body = json.dumps({"errorCode": "E1", "errorMessage": message})
        self.assertGreater(len(message), 299)
        for script, mock, result in self.run_account(Account(collect_body=body)):
            with self.subTest(script):
                self.assert_refused(mock, result)
                line = next(l for l in result.stdout.splitlines() if "errorMessage=" in l)
                self.assertLessEqual(len(line.split("errorMessage=", 1)[1]), 200)
                self.assertNotIn("SECONDLINE", result.output)

    def test_only_the_three_named_fields_are_logged(self):
        body = json.dumps({"error": "refused", "debug": "OTHERFIELD", "request": {"body": "OTHERFIELD"}})
        for script, mock, result in self.run_account(Account(collect_body=body)):
            with self.subTest(script):
                self.assert_refused(mock, result, "error=refused")
                self.assertNotIn("OTHERFIELD", result.stdout.split("Console:")[0])

    def test_mock_password_in_an_error_body_is_redacted(self):
        body = json.dumps({"error": f"bad {PASSWORD} for leak@x.test token=abc123"})
        for script, mock, result in self.run_account(Account(collect_body=body), check_password=False):
            with self.subTest(script):
                self.assert_refused(mock, result, "error=bad <redacted> for <email> token=<redacted>")
                self.assertNotIn(PASSWORD, result.output)
                self.assertNotIn("abc123", result.output)


class MultiCharacter(GowTestCase):
    def test_second_claim_refused_and_the_third_still_attempted(self):
        email = "partial@x.test"
        mock = self.make_mock({email: Account(characters=("c1", "c2", "c3"), collect_fail_characters=("c2",))})
        result = self.run_gow("claim_all_characters.py", mock, users_text([email]), "1", "1", "--stagger", "0")
        self.assertEqual(sorted(mock.ledger_pairs()), [(email, "c1"), (email, "c3")], result.output[-2500:])
        self.assertEqual(mock.claim_posts, 3)
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 150)
        self.assertIn("✅ 2/3 chars", result.stdout)
        self.assertIn("Claimed 2/3 characters", result.stdout)
        self.assertIn("Character 2/3: ✗ Claim failed", result.stdout)
        self.assertEqual(result.returncode, 0)

    def test_every_character_refused_is_a_failed_account(self):
        email = "allfail@x.test"
        mock = self.make_mock({email: Account(characters=("c1", "c2"), collect_status=500)})
        result = self.run_gow("claim_all_characters.py", mock, users_text([email]), "1", "1", "--stagger", "0")
        self.assertEqual(result.summary_rows(), [(False, email)], result.output[-2500:])
        self.assertEqual(result.summary_value("Total Jewels Claimed"), 0)
        self.assertEqual(result.returncode, 1)


class MixedRun(GowTestCase):
    def test_total_equals_the_ledger_at_concurrency_3(self):
        accounts = {
            "fine@x.test": Account(),
            "bodyerror@x.test": Account(collect_body='{"error":"offer unavailable"}'),
            "norequest@x.test": Account(claim_no_request=True),
            "cool@x.test": Account(vault="cooldown"),
        }
        for script, mock, result in self.for_each_script(lambda s: (
                self.make_mock(accounts, hydrate_ms=100), users_text(accounts),
                [*range_args(s, 4), "--concurrency", "3", "--stagger", "0.1"], {})):
            with self.subTest(script):
                self.assertEqual(mock.ledger_pairs(), [("fine@x.test", "only")], result.output[-2000:])
                self.assertEqual(result.summary_value("Total Jewels Claimed"), 75 * len(mock.ledger_pairs()))
                self.assertEqual(result.summary_rows(), [
                    (True, "fine@x.test"), (False, "bodyerror@x.test"),
                    (False, "norequest@x.test"), (True, "cool@x.test")])
                self.assertIn(ALREADY_CLAIMED_MESSAGE[script], result.stdout)
                if script == "claim_all_characters.py":
                    self.assertEqual(result.summary_value("Accounts Success"), 2)
                    self.assertEqual(result.summary_value("Accounts Failed"), 2)
                else:
                    self.assertRegex(result.stdout, r"Total: 4 \| Success: 2 \| Failed: 2\n")
                self.assertEqual(result.returncode, 1)
                self.assert_tagged(result, list(accounts))


if __name__ == "__main__":
    unittest.main()
